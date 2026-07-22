#!/usr/bin/env python3
"""
Local video asset pipeline.

This module turns a folder of product video material into clip_paths that can
reuse the existing post-production pipeline.
"""

from __future__ import annotations

import base64
import copy
import contextlib
import fcntl
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from frame_evidence import (
    FRAME_EVIDENCE_VERSION,
    build_contact_sheet_evidence,
    write_frame_evidence_artifacts,
)
from script_feedback import ScriptFeedbackStore, candidate_preference_score

from config import (
    LOCAL_ASSET_CONTACT_SHEET_FRAMES,
    LOCAL_ASSET_INDEX_PATH,
    LOCAL_ASSET_MAX_WINDOWS,
    LOCAL_ASSET_WINDOW_SECONDS,
    LOCAL_ASSET_WINDOW_STRIDE,
    OUTPUT_FPS,
    OUTPUT_RESOLUTION,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_ENABLED,
    VISION_MAX_RETRIES,
    VISION_MODEL,
    VISION_TIMEOUT,
)


INDEX_VERSION = 5
VISION_ANALYSIS_VERSION = 1
REFERENCE_PROFILE_VERSION = 5
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
MIN_MATCH_SCORE = 0.70
MIN_PLAN_SCORE = 0.72
MIN_KEY_SEGMENT_CONFIDENCE = 0.75
SCENE_CHANGE_THRESHOLD = 0.28
MIN_SCENE_SECONDS = 1.0
MOTION_SAMPLE_FRAMES = 12
MAX_INITIAL_SCRIPT_JSON_ATTEMPTS = 3
SCRIPT_CONTRACT_VERSION = 50
MIN_VOICEOVER_UNITS_PER_SECOND = 4.2
MIN_OUTRO_VOICEOVER_UNITS_PER_SECOND = 4.4
MAX_OUTRO_VOICEOVER_UNITS_PER_SECOND = 5.4

# Only concrete effect, health and quality assertions remain hard factual claims.
# Ordinary food-and-drink sensory language is persuasive expression and may be
# learned from user feedback; it must not be a developer-authored blocking rule.
NONVISUAL_CLAIM_PATTERNS = (
    r"治愈|疗愈|放松|满足感|幸福感",
    r"提神|醒脑|解腻|解渴|消暑|降火|助眠|健康|养生|低卡|减脂",
    r"优质|高品质|天然|原生态|甄选|精选|上等|高端",
)
UNVERIFIED_COPY_TERM_EXAMPLES = (
    "茶香", "咖啡香", "香气", "清爽", "提神", "好喝", "口感",
    "解腻", "健康", "低卡", "天然", "优质", "甄选", "精选",
)
PRODUCT_STORY_ROLES = {
    "finished_product", "ingredient", "origin", "production",
    "usage", "result", "context", "unknown",
}
MARKETING_INTENTS = {"hook", "value", "proof", "cta"}
GENERIC_SALES_COPY = re.compile(r"^(?:这款|这杯|这瓶)?[^，。！？]{0,8}(?:值得细看|一起来看|来看看|看过来)[！!。]?$" )
CLAIM_FACT_PATHS = {
    "ingredient": ("ingredients", "raw_materials"),
    "origin": ("origin",),
    "production": ("production_process",),
    "specification": ("specifications",),
    "effect": ("verified_claims",),
    "price": ("price", "pricing"),
    "social_proof": ("social_proof", "sales_proof", "awards", "reviews", "testimonials"),
    "quality": ("quality_proof", "authenticity_proof", "certifications", "verified_claims"),
}
EXPLICIT_INGREDIENT_CLAIM = re.compile(
    r"(?:采用|选用|使用|用到|添加|加入|含有|源自).{0,18}(?:原料|配方|茶|咖啡|花|果|奶)|"
    r"(?:原料|配方).{0,8}(?:是|采用|选用|来自)"
)
EXPLICIT_ORIGIN_CLAIM = re.compile(r"来自.{0,18}(?:产区|产地)|源自.{0,18}(?:产区|产地)|原产(?:地|于)?")
EXPLICIT_PRODUCTION_CLAIM = re.compile(
    r"(?:采用|使用|经过|通过).{0,16}(?:工艺|萃取|调香|烘焙|发酵|窨制|制作)|"
    r"(?:不是|并非|没有).{0,8}(?:工业调香|人工调香)|(?:工艺|制作方式|生产方式).{0,12}(?:是|采用|使用)"
)
EXPLICIT_SPECIFICATION_CLAIM = re.compile(
    r"(?:大小|多种|不同).{0,6}(?:规格|容量|尺寸|包装)|(?:规格|容量|尺寸).{0,8}(?:有|可选|齐全)"
)
PRODUCT_RELATION_ASSERTION = re.compile(
    r"来自这里|源自这里|产自|原产|(?:用到|使用|采用|选用).{0,12}(?:原料|茶|咖啡|茉莉)|"
    r"(?:这里|这片|这些).{0,8}(?:是|就是).{0,8}(?:产地|原料)|"
    r"(?:看到|看着|看完).{0,30}(?:知道|看出|说明|证明).{0,24}(?:原料|产地|来源|品质|底子)"
)
PRICE_CLAIM = re.compile(
    r"(?:到手(?:价)?|售价|价格|优惠|立减|包邮|买一送一)|"
    r"(?:\d+(?:\.\d+)?\s*(?:元|折))|"
    r"(?:[一二三四五六七八九十百千万两]+\s*元)"
)
SOCIAL_PROOF_CLAIM = re.compile(
    r"最近(?:很)?火|火起来|爆火|爆款|都在(?:买|喝|用)|大家都|很多人|好多人|"
    r"抢着|卖爆|销量|回购|口碑|人气|热门|获奖|推荐榜|排名"
)
QUALITY_ENDORSEMENT_CLAIM = re.compile(
    r"用料(?:很)?实在|用料扎实|原料扎实|好原料|好基底|品质(?:更)?好|高品质|"
    r"不掺假|没有掺假|假货|正品|保真|货真价实|真材实料|靠谱|放心(?:买|选|喝|用)"
)
ACTION_CONCEPT_PATTERNS = {
    "pick": re.compile(r"拿起|取出|举起|抬起"),
    "place": re.compile(r"放回|放下|摆放|排列|放置|调整"),
    "pour": re.compile(r"倒入|倒进|倒出|倾倒|倒一杯|液面.*升高"),
    "point": re.compile(r"指向|手指.*标签"),
    "move": re.compile(r"移动|移出|移走|伸向"),
    "tilt": re.compile(r"倾斜|翻转|旋转|摇晃"),
    "open": re.compile(r"打开|关闭|撕开"),
    "mix": re.compile(r"搅拌|冲泡|按压|挤出|加入"),
}
AMBIGUOUS_STATE_ACTIONS = re.compile(r"摆放|排列|放置")
ACTION_CONTEXT = re.compile(r"手|双手|将|正在|开始|持续|逐步|依次|调整|向.+(?:移动|倒|放)")
CAPABILITY_ACTION_CONTEXT = re.compile(r"随时|可以|可直接|能(?:够)?|方便|无需|不用|想.+就")


class LocalAssetError(RuntimeError):
    """Raised when local asset mode cannot produce a publishable edit."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


@dataclass
class LocalAssetContext:
    folder: Path
    cache_dir: Path
    index_path: Path


def vision_backend_available() -> bool:
    return bool(VISION_ENABLED and VISION_BASE_URL and VISION_API_KEY and VISION_MODEL)


def ensure_vision_backend_available() -> None:
    if not vision_backend_available():
        raise LocalAssetError(
            "本地素材模式需要视觉分析能力。请配置 VISION_ENABLED=true、"
            "VISION_BASE_URL、VISION_API_KEY、VISION_MODEL 后重试。"
        )
    base_url = VISION_BASE_URL.rstrip("/")
    model = VISION_MODEL.lower()
    if "/images/generations" in base_url:
        raise LocalAssetError(
            "VISION_BASE_URL 当前是图片生成接口。素材分析需要视觉理解 Chat 接口，"
            "请改为类似 https://ark.cn-beijing.volces.com/api/v3，"
            "程序会自动请求 /chat/completions。"
        )
    if "seedream" in model:
        raise LocalAssetError(
            "VISION_MODEL 当前是 Seedream 图片生成模型。素材分析需要支持 image_url 输入的视觉理解模型，"
            "请换成火山方舟视觉理解/多模态 Chat 模型。"
        )


def _run(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip())
    return stem.strip("._") or "asset"


def _folder_cache_context(folder: Path) -> LocalAssetContext:
    resolved = folder.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise LocalAssetError(f"本地素材文件夹不存在：{resolved}")
    cache_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    cache_dir = LOCAL_ASSET_INDEX_PATH / cache_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return LocalAssetContext(
        folder=resolved,
        cache_dir=cache_dir,
        index_path=cache_dir / "index.json",
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_video_files(folder: Path) -> List[Path]:
    files = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return sorted(files, key=lambda p: str(p).lower())


def _ffprobe(path: Path) -> Dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json",
        str(path),
    ]
    try:
        result = _run(cmd, timeout=30)
        data = json.loads(result.stdout or "{}")
    except Exception as exc:
        raise LocalAssetError(f"无法读取视频信息：{path.name} ({exc})") from exc

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def _source_signature(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": _hash_file(path),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


def _signatures_match(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> bool:
    if len(old) != len(new):
        return False
    old_by_path = {item.get("path"): item for item in old}
    for item in new:
        prev = old_by_path.get(item.get("path"))
        if not prev:
            return False
        if prev.get("sha256") != item.get("sha256"):
            return False
        if prev.get("size") != item.get("size"):
            return False
    return True


def _load_reusable_window_analyses(
    index_path: Path,
    signatures: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Reuse semantic analysis when only downstream evidence/index formats changed."""
    if not index_path.exists():
        return {}
    try:
        cached = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if (
        cached.get("build_complete") is not True
        or int(cached.get("vision_analysis_version") or 1) != VISION_ANALYSIS_VERSION
        or not _signatures_match(cached.get("sources") or [], signatures)
    ):
        return {}

    required_fields = {
        "shot_type",
        "narrative_roles",
        "product_story_role",
        "usable_for_ad",
        "confidence",
    }
    reusable = {}
    for window in cached.get("windows") or []:
        analysis = window.get("analysis") or {}
        window_id = str(window.get("window_id") or "")
        if window_id and required_fields.issubset(analysis):
            reusable[window_id] = dict(analysis)
    return reusable


def _detect_scene_ranges(source: Path, duration: float) -> List[Dict[str, Any]]:
    """Use FFmpeg's content detector so windows never straddle a real edit."""
    if duration <= MIN_SCENE_SECONDS:
        return [{"scene_id": 0, "start": 0.0, "end": max(duration - 0.05, 0.5)}]
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(source),
        "-vf", f"select='gt(scene,{SCENE_CHANGE_THRESHOLD})',showinfo",
        "-an", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
        boundaries = [
            float(value)
            for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr or "")
        ]
    except Exception:
        boundaries = []

    usable_end = max(duration - 0.05, 0.5)
    clean = []
    for boundary in sorted(set(boundaries)):
        if boundary < MIN_SCENE_SECONDS or boundary > usable_end - MIN_SCENE_SECONDS:
            continue
        if clean and boundary - clean[-1] < MIN_SCENE_SECONDS:
            continue
        clean.append(boundary)
    points = [0.0, *clean, usable_end]
    return [
        {"scene_id": index, "start": round(start, 3), "end": round(end, 3)}
        for index, (start, end) in enumerate(zip(points, points[1:]))
        if end - start >= 0.8
    ]


def _candidate_windows(source: Dict[str, Any], window_seconds: float, stride: float) -> List[Dict[str, Any]]:
    duration = float(source.get("duration") or 0)
    if duration <= 0.8:
        return []
    windows = []
    scenes = _detect_scene_ranges(Path(source["path"]), duration)
    for scene in scenes:
        scene_start = float(scene["start"])
        scene_end = float(scene["end"])
        scene_duration = scene_end - scene_start
        if scene_duration <= window_seconds:
            windows.append(dict(scene))
            continue
        start = scene_start
        while start < scene_end - 0.8:
            end = min(start + window_seconds, scene_end)
            if end - start >= 1.5:
                windows.append({
                    "scene_id": scene["scene_id"],
                    "start": round(start, 3),
                    "end": round(end, 3),
                })
            if end >= scene_end:
                break
            start += stride
    return windows


def _sample_motion_frames(source: Path, start: float, end: float, count: int) -> List[Tuple[float, Any]]:
    """Decode evenly spaced frames for temporal analysis without writing intermediates."""
    try:
        import cv2
    except ImportError as exc:
        raise LocalAssetError("动态视频理解需要现有 OpenCV 运行时，当前环境不可用") from exc

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise LocalAssetError(f"无法为动态分析打开视频：{source.name}")
    frames: List[Tuple[float, Any]] = []
    try:
        duration = max(end - start, 0.2)
        for index in range(max(3, count)):
            timestamp = start + duration * index / max(count - 1, 1)
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            scale = min(1.0, 320.0 / max(width, 1))
            if scale < 1.0:
                frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
            frames.append((round(timestamp, 3), cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
    finally:
        capture.release()
    if len(frames) < 3:
        raise LocalAssetError(f"动态分析抽帧不足：{source.name} {start:.1f}-{end:.1f}s")
    return frames


def _analyze_window_motion(source: Path, start: float, end: float) -> Dict[str, Any]:
    """Separate global camera motion from residual foreground/subject motion."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise LocalAssetError("动态视频理解需要现有 OpenCV/NumPy 运行时，当前环境不可用") from exc

    frames = _sample_motion_frames(source, start, end, MOTION_SAMPLE_FRAMES)
    samples = []
    camera_vectors = []
    residual_ratios = []
    temporal_deltas = []
    reliable_pairs = 0
    for (time_a, previous), (time_b, current) in zip(frames, frames[1:]):
        points = cv2.goodFeaturesToTrack(
            previous, maxCorners=240, qualityLevel=0.01, minDistance=7, blockSize=7,
        )
        matrix = None
        tracked_count = 0
        inlier_ratio = 0.0
        if points is not None and len(points) >= 8:
            tracked, status, _ = cv2.calcOpticalFlowPyrLK(
                previous, current, points, None,
                winSize=(21, 21), maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
            )
            valid = status.reshape(-1) == 1 if status is not None else np.zeros(len(points), dtype=bool)
            src_points = points.reshape(-1, 2)[valid]
            dst_points = tracked.reshape(-1, 2)[valid] if tracked is not None else np.empty((0, 2))
            tracked_count = len(src_points)
            if tracked_count >= 8:
                matrix, inliers = cv2.estimateAffinePartial2D(
                    src_points, dst_points, method=cv2.RANSAC, ransacReprojThreshold=2.5,
                )
                if inliers is not None:
                    inlier_ratio = float(inliers.mean())

        height, width = previous.shape
        if matrix is None:
            matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        dx = float(matrix[0, 2]) / max(width, 1)
        dy = float(matrix[1, 2]) / max(height, 1)
        scale = math.hypot(float(matrix[0, 0]), float(matrix[0, 1]))
        rotation = math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))
        aligned = cv2.warpAffine(previous, matrix, (width, height), flags=cv2.INTER_LINEAR)
        difference = cv2.absdiff(aligned, current)
        difference = cv2.GaussianBlur(difference, (5, 5), 0)
        moving = cv2.threshold(difference, 24, 255, cv2.THRESH_BINARY)[1]
        moving = cv2.morphologyEx(moving, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        residual_ratio = float(np.count_nonzero(moving)) / float(moving.size)
        temporal_delta = float(np.mean(cv2.absdiff(previous, current))) / 255.0
        camera_speed = math.hypot(dx, dy) + abs(scale - 1.0) + abs(rotation) / 90.0
        if tracked_count >= 8 and inlier_ratio >= 0.35:
            reliable_pairs += 1
        camera_vectors.append((dx, dy, scale, rotation, camera_speed))
        residual_ratios.append(residual_ratio)
        temporal_deltas.append(temporal_delta)
        samples.append({
            "start": time_a,
            "end": time_b,
            "camera_dx": round(dx, 5),
            "camera_dy": round(dy, 5),
            "camera_scale": round(scale, 5),
            "camera_rotation": round(rotation, 3),
            "subject_motion_ratio": round(residual_ratio, 4),
            "temporal_delta": round(temporal_delta, 4),
            "tracked_points": tracked_count,
            "inlier_ratio": round(inlier_ratio, 3),
        })

    median_dx = float(np.median([value[0] for value in camera_vectors]))
    median_dy = float(np.median([value[1] for value in camera_vectors]))
    median_scale = float(np.median([value[2] for value in camera_vectors]))
    median_rotation = float(np.median([value[3] for value in camera_vectors]))
    camera_speed = float(np.median([value[4] for value in camera_vectors]))
    subject_motion_ratio = float(np.percentile(residual_ratios, 75))
    temporal_change = float(np.mean(temporal_deltas))
    camera_jitter = float(np.std([value[0] for value in camera_vectors]) + np.std([value[1] for value in camera_vectors]))

    if abs(median_scale - 1.0) >= 0.006:
        camera_motion = "zoom_in" if median_scale > 1.0 else "zoom_out"
    elif abs(median_dx) >= abs(median_dy) and abs(median_dx) >= 0.004:
        camera_motion = "pan_right" if median_dx > 0 else "pan_left"
    elif abs(median_dy) >= 0.004:
        camera_motion = "tilt_down" if median_dy > 0 else "tilt_up"
    elif abs(median_rotation) >= 0.5 or camera_jitter >= 0.008:
        camera_motion = "handheld"
    else:
        camera_motion = "static"

    subject_motion = "high" if subject_motion_ratio >= 0.12 else "medium" if subject_motion_ratio >= 0.045 else "low"
    motion_class = "dynamic" if camera_speed >= 0.025 or subject_motion == "high" else "semi_dynamic" if camera_speed >= 0.008 or subject_motion == "medium" else "static"
    active_ranges = [
        {"start": sample["start"], "end": sample["end"], "strength": sample["subject_motion_ratio"]}
        for sample in samples
        if sample["subject_motion_ratio"] >= 0.045
    ]
    reliability = reliable_pairs / max(len(samples), 1)
    return {
        "method": "sparse_optical_flow_global_affine_plus_residual_motion_v1",
        "sample_count": len(frames),
        "motion_class": motion_class,
        "camera_motion": camera_motion,
        "camera_speed": round(camera_speed, 4),
        "camera_dx": round(median_dx, 5),
        "camera_dy": round(median_dy, 5),
        "camera_scale": round(median_scale, 5),
        "camera_rotation": round(median_rotation, 3),
        "camera_jitter": round(camera_jitter, 4),
        "subject_motion": subject_motion,
        "subject_motion_ratio": round(subject_motion_ratio, 4),
        "temporal_change": round(temporal_change, 4),
        "stability": round(max(0.0, 1.0 - min(1.0, camera_jitter * 35.0)), 3),
        "confidence": round(min(1.0, 0.45 + reliability * 0.55), 3),
        "active_ranges": active_ranges,
        "samples": samples,
    }


def _analyze_window_frame_quality(source: Path, start: float, end: float) -> Dict[str, Any]:
    """Measure whether sampled frames are clear and exposed enough for factual VLM reading."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise LocalAssetError("静态视频理解质量分析需要现有 OpenCV/NumPy 运行时") from exc

    frames = _sample_motion_frames(source, start, end, MOTION_SAMPLE_FRAMES)
    samples = []
    for timestamp, gray in frames:
        brightness = float(np.mean(gray))
        contrast = float(np.std(gray))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        samples.append({
            "time": timestamp,
            "brightness": round(brightness, 2),
            "contrast": round(contrast, 2),
            "sharpness": round(sharpness, 2),
            "readable": bool(24.0 <= brightness <= 232.0 and contrast >= 18.0 and sharpness >= 28.0),
        })
    readable_ratio = sum(1 for sample in samples if sample["readable"]) / max(len(samples), 1)
    return {
        "method": "multi_frame_brightness_contrast_laplacian_v1",
        "sample_count": len(samples),
        "readable_ratio": round(readable_ratio, 3),
        "median_brightness": round(float(np.median([sample["brightness"] for sample in samples])), 2),
        "median_contrast": round(float(np.median([sample["contrast"] for sample in samples])), 2),
        "median_sharpness": round(float(np.median([sample["sharpness"] for sample in samples])), 2),
        "passed": readable_ratio >= 0.67,
        "samples": samples,
    }


def _make_contact_sheet(
    source: Path,
    start: float,
    end: float,
    output: Path,
    frame_count: int,
    preferred_times: Optional[List[float]] = None,
    tile_size: Tuple[int, int] = (360, 240),
    columns: int = 4,
    jpeg_quality: int = 88,
) -> Path:
    build_contact_sheet_evidence(
        source=source,
        start=start,
        end=end,
        output=output,
        frame_count=frame_count,
        required_times=preferred_times,
        tile_size=tile_size,
        columns=columns,
        jpeg_quality=jpeg_quality,
    )
    return output


def _reference_contact_sheet_times(
    scenes: List[Dict[str, Any]],
    duration: float,
    frame_count: int,
) -> List[float]:
    """Cover every scene and reserve three observations for the final CTA scene."""
    if frame_count <= 0 or duration <= 0:
        return []
    valid_scenes = [
        (max(0.0, float(scene["start"])), min(duration, float(scene["end"])))
        for scene in scenes
        if float(scene.get("end", 0.0)) > float(scene.get("start", 0.0))
    ]
    candidates = [(start + end) / 2 for start, end in valid_scenes]
    if valid_scenes:
        final_start, final_end = valid_scenes[-1]
        inset = min(0.08, max((final_end - final_start) * 0.03, 0.02))
        candidates.extend([final_start + inset, (final_start + final_end) / 2, final_end - inset])
    candidates.extend(duration * (index + 0.5) / frame_count for index in range(frame_count))

    minimum_gap = min(0.04, duration / max(frame_count * 20, 1))
    selected: List[float] = []
    for timestamp in candidates:
        timestamp = max(0.0, min(duration, timestamp))
        if all(abs(timestamp - existing) >= minimum_gap for existing in selected):
            selected.append(timestamp)
        if len(selected) >= frame_count:
            break
    return sorted(selected)


def _image_data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("utf-8")


def _json_from_text(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def _streamed_chat_json(response: requests.Response) -> Dict[str, Any]:
    """Collect one Chat Completions SSE response and decode its JSON content."""
    content_parts: List[str] = []
    decoder = json.JSONDecoder()
    event_buffer = ""

    def consume_events() -> bool:
        nonlocal event_buffer
        while event_buffer.strip():
            data = event_buffer.lstrip()
            if data == "[DONE]":
                event_buffer = ""
                return True
            try:
                event, consumed = decoder.raw_decode(data)
            except json.JSONDecodeError:
                return False
            event_buffer = data[consumed:]
            if event.get("error"):
                raise ValueError(f"视觉服务返回错误事件：{event['error']}")
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            message = choice.get("delta") or choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                content_parts.append(content)
            elif isinstance(content, list):
                content_parts.extend(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("text")
                )
        return False

    done = False
    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line or "")
        if not line.strip() or line.startswith(":"):
            continue
        payload = line[5:].lstrip() if line.startswith("data:") else line.strip()
        event_buffer += payload
        done = consume_events()
        if done:
            break
    if not done:
        consume_events()
    if event_buffer.strip() and not done:
        raise ValueError("视觉服务流式响应包含未完成 JSON 事件")
    if not content_parts:
        raise ValueError("视觉服务流式响应没有返回可解析内容")
    return _json_from_text("".join(content_parts))


def _chat_response_json(response: requests.Response) -> Dict[str, Any]:
    """Read a streamed response, while retaining compatibility with test doubles and legacy gateways."""
    if hasattr(response, "iter_lines"):
        return _streamed_chat_json(response)
    data = response.json()
    return _json_from_text(data["choices"][0]["message"]["content"])


def _is_retryable_vision_error(exc: Exception) -> bool:
    """Retry transient transport/server failures, but fail fast on bad requests or auth."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in {408, 409, 425, 429} or exc.response.status_code >= 500
    return isinstance(exc, (requests.RequestException, json.JSONDecodeError, ValueError, KeyError))


def _nonvisual_claims_by_field(segment: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return sensory, emotional and effect claims that pixels cannot prove."""
    violations: Dict[str, List[str]] = {}
    for field in ("subtitle", "voiceover"):
        text = str(segment.get(field) or "")
        matches = []
        for pattern in NONVISUAL_CLAIM_PATTERNS:
            matches.extend(match.group(0) for match in re.finditer(pattern, text))
        if matches:
            violations[field] = list(dict.fromkeys(matches))
    return violations


def _fact_values(product_info: Dict[str, Any], paths: Tuple[str, ...]) -> List[str]:
    values: List[str] = []
    for path in paths:
        value = product_info.get(path)
        if isinstance(value, dict):
            value = list(value.values())
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        for item in value:
            text = str(item or "").strip()
            if text:
                values.append(text)
    return list(dict.fromkeys(values))


def _text_matches_facts(text: str, facts: List[str]) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    return any(
        re.sub(r"\s+", "", fact).lower() in normalized
        or normalized in re.sub(r"\s+", "", fact).lower()
        for fact in facts
        if len(re.sub(r"\s+", "", fact)) >= 2
    )


def _infer_copy_claims(text: str) -> List[Dict[str, str]]:
    claims: List[Dict[str, str]] = []
    if EXPLICIT_INGREDIENT_CLAIM.search(text):
        claims.append({"text": text, "type": "ingredient"})
    if EXPLICIT_ORIGIN_CLAIM.search(text):
        claims.append({"text": text, "type": "origin"})
    if EXPLICIT_PRODUCTION_CLAIM.search(text):
        claims.append({"text": text, "type": "production"})
    if EXPLICIT_SPECIFICATION_CLAIM.search(text):
        claims.append({"text": text, "type": "specification"})
    if PRICE_CLAIM.search(text):
        claims.append({"text": text, "type": "price"})
    if SOCIAL_PROOF_CLAIM.search(text):
        claims.append({"text": text, "type": "social_proof"})
    if QUALITY_ENDORSEMENT_CLAIM.search(text):
        claims.append({"text": text, "type": "quality"})
    nonvisual = [
        match.group(0)
        for pattern in NONVISUAL_CLAIM_PATTERNS
        for match in re.finditer(pattern, text)
    ]
    if nonvisual:
        claims.extend({"text": claim, "type": "effect"} for claim in dict.fromkeys(nonvisual))
    return claims


def _claim_supported(
    claim: Dict[str, Any],
    product_info: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> bool:
    claim_type = str(claim.get("type") or "").strip().lower()
    claim_text = str(claim.get("text") or "").strip()
    if claim_type not in CLAIM_FACT_PATHS or not claim_text:
        return False
    evidence_source = str(claim.get("evidence_source") or "").strip()
    if evidence_source == "visual":
        return True
    if evidence_source == "verified_relationship":
        return bool(
            (analysis or {}).get("product_relationship_verified")
            and _text_matches_facts(claim_text, [
                *(str(value) for value in (analysis or {}).get("matched_product_facts") or []),
                str((analysis or {}).get("relation_evidence") or ""),
            ])
        )
    if evidence_source == "reference_video":
        return _text_matches_facts(claim_text, _fact_values(product_info, CLAIM_FACT_PATHS[claim_type]))
    allowed_paths = CLAIM_FACT_PATHS[claim_type]
    if evidence_source:
        prefix = "product_info."
        if not evidence_source.startswith(prefix) or evidence_source[len(prefix):] not in allowed_paths:
            return False
    facts = _fact_values(product_info, allowed_paths)
    if claim_type == "effect":
        terms = _infer_effect_terms(claim_text)
        return bool(terms) and all(_text_matches_facts(value, facts) for value in terms)
    return _text_matches_facts(claim_text, facts)


def _infer_effect_terms(text: str) -> List[str]:
    return list(dict.fromkeys(
        match.group(0)
        for pattern in NONVISUAL_CLAIM_PATTERNS
        for match in re.finditer(pattern, text)
    ))


def _marketing_claim_violations(
    segment: Dict[str, Any],
    product_info: Optional[Dict[str, Any]] = None,
    analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Allow persuasive copy while rejecting factual claims without trusted evidence."""
    product_info = product_info or {}
    analysis = analysis or {}
    violations: Dict[str, List[str]] = {}
    structured_claims = segment.get("claims") or []
    if not isinstance(structured_claims, list):
        structured_claims = []
    unsupported_structured = [
        str(claim.get("text") or claim)
        for claim in structured_claims
        if not isinstance(claim, dict) or not _claim_supported(claim, product_info, analysis)
    ]
    if unsupported_structured:
        violations["claims"] = unsupported_structured

    for field in ("subtitle", "voiceover"):
        text = str(segment.get(field) or "")
        claims = _infer_copy_claims(text)
        unsupported = [claim["text"] for claim in claims if not _claim_supported(claim, product_info, analysis)]
        for claim in structured_claims:
            if (
                isinstance(claim, dict)
                and str(claim.get("text") or "") in text
                and not _claim_supported(claim, product_info, analysis)
            ):
                unsupported.append(str(claim.get("text")))
        if unsupported:
            violations[field] = list(dict.fromkeys(unsupported))
    return violations


def _temporal_action_claims(segment: Dict[str, Any]) -> List[str]:
    claims = []
    for field in ("subtitle", "voiceover", "visual_requirement"):
        text = str(segment.get(field) or "")
        for concept, pattern in ACTION_CONCEPT_PATTERNS.items():
            for action_match in pattern.finditer(text):
                if field != "visual_requirement" and any(
                    capability.end() <= action_match.start()
                    and action_match.start() - capability.end() <= 4
                    for capability in CAPABILITY_ACTION_CONTEXT.finditer(text)
                ):
                    continue
                if (
                    concept == "place"
                    and AMBIGUOUS_STATE_ACTIONS.search(text)
                    and not ACTION_CONTEXT.search(text)
                ):
                    continue
                claims.append(concept)
                break
    return list(dict.fromkeys(claims))


def _unsupported_action_claims(segment: Dict[str, Any], analysis: Dict[str, Any]) -> List[str]:
    claims = _temporal_action_claims(segment)
    if not claims:
        return []
    event_text = " ".join([
        " ".join(str(value) for value in analysis.get("literal_actions") or []),
        " ".join(str(event.get("action") or "") for event in analysis.get("temporal_events") or []),
    ])
    return [
        concept
        for concept in claims
        if not ACTION_CONCEPT_PATTERNS[concept].search(event_text)
    ]


def _desired_product_story_role(segment: Dict[str, Any]) -> str:
    """Return the semantic role requested by copy, never a prior match result."""
    desired = str(segment.get("desired_product_story_role") or "").strip().lower()
    if desired:
        return desired
    visual = str(segment.get("visual_story_role") or "").strip().lower()
    if visual:
        return visual
    if "desired_product_story_role" in segment:
        return ""
    return str(segment.get("product_story_role") or "").strip().lower()


def _story_role_supported(segment: Dict[str, Any], analysis: Dict[str, Any]) -> bool:
    required = _desired_product_story_role(segment)
    if not required:
        return True
    actual = str(analysis.get("product_story_role") or "unknown").strip().lower()
    if required not in PRODUCT_STORY_ROLES or actual not in PRODUCT_STORY_ROLES:
        return False
    if required == actual:
        return True
    product_roles = {"finished_product", "usage", "result"}
    if required in product_roles and actual in product_roles:
        return str(analysis.get("product_relevance_prior") or "").lower() == "high"
    if required in {"ingredient", "origin", "production"}:
        return (
            actual == "finished_product"
            and str(analysis.get("product_relevance_prior") or "").lower() == "high"
            and int(analysis.get("product_visibility") or 0) >= 3
        )
    return False


def _apply_product_relationships_to_windows(
    asset_index: Dict[str, Any],
    product_info: Dict[str, Any],
) -> None:
    """Attach the current product's verified relationships to indexed window evidence."""
    catalog = _material_catalog(asset_index)
    _annotate_product_relationships(catalog, product_info)
    relationships = {str(item.get("window_id")): item for item in catalog}
    fields = (
        "product_relevance_prior",
        "product_relevance_source",
        "product_identity_supported",
        "product_identity_evidence",
        "product_relationship_verified",
        "product_relationship_source",
        "matched_product_facts",
    )
    for window in asset_index.get("windows") or []:
        relationship = relationships.get(str(window.get("window_id")))
        if relationship is None:
            continue
        analysis = window.setdefault("analysis", {})
        for field in fields:
            analysis[field] = relationship.get(field)


def _normalize_claim_objects(
    segment: Dict[str, Any],
    product_info: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Normalize LLM claim shape without inventing types or evidence."""
    normalized: List[Dict[str, Any]] = []
    role_to_type = {
        "ingredient": "ingredient",
        "origin": "origin",
        "production": "production",
    }
    story_role = str(segment.get("product_story_role") or "").strip().lower()
    for claim in segment.get("claims") or []:
        if isinstance(claim, dict):
            if (
                str(claim.get("text") or "").strip()
                and _claim_supported(claim, product_info, analysis)
            ):
                normalized.append(dict(claim))
            continue
        claim_text = str(claim or "").strip()
        claim_type = role_to_type.get(story_role)
        if not claim_text or not claim_type:
            continue
        for path in CLAIM_FACT_PATHS[claim_type]:
            if _text_matches_facts(claim_text, _fact_values(product_info, (path,))):
                normalized.append({
                    "text": claim_text,
                    "type": claim_type,
                    "evidence_source": f"product_info.{path}",
                })
                break
    return normalized


def _speech_units(text: str) -> int:
    """Estimate Mandarin TTS load without treating punctuation as spoken content."""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", str(text or "")))
    latin = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    return chinese + sum(max(1, math.ceil(len(token) / 4)) for token in latin)


def _synchronize_subtitle_to_voiceover(segment: Dict[str, Any]) -> None:
    """Keep authored subtitle semantics identical to the punctuated spoken cue."""
    voiceover = re.sub(r"\s+", " ", str(segment.get("voiceover") or "")).strip()
    segment["voiceover"] = voiceover
    segment["subtitle"] = voiceover


def materialize_continuous_voiceover_contract(script: Dict[str, Any]) -> str:
    """Derive all spoken and subtitle fields from the one authored cue sequence."""
    segments = script.get("segments") or []
    cues = script.get("voiceover_cues") or []
    if not isinstance(cues, list) or len(cues) != len(segments):
        raise LocalAssetError(
            f"连续口播 cue 数量必须与分镜一致：cue {len(cues) if isinstance(cues, list) else 0}，"
            f"分镜 {len(segments)}"
        )
    normalized_cues = [re.sub(r"\s+", " ", str(cue or "")).strip() for cue in cues]
    if any(not cue for cue in normalized_cues):
        raise LocalAssetError("连续口播存在空 cue")
    for segment, cue in zip(segments, normalized_cues):
        segment["voiceover"] = cue
        _synchronize_subtitle_to_voiceover(segment)
    outro_cue = re.sub(r"\s+", " ", str(script.get("voiceover_outro_cue") or "")).strip()
    full_text = "".join(normalized_cues) + outro_cue
    script["voiceover_cues"] = normalized_cues
    script["voiceover_outro_cue"] = outro_cue
    script["voiceover_full"] = full_text
    return full_text


def _refresh_continuous_voiceover_from_segments(
    script: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> str:
    script["segments"] = segments
    script["voiceover_cues"] = [str(segment.get("voiceover") or "") for segment in segments]
    return materialize_continuous_voiceover_contract(script)


def validate_continuous_voiceover_contract(
    script: Dict[str, Any],
    segments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return the authored one-take narration after verifying its ordered visual cues."""
    current_segments = segments if segments is not None else script.get("segments") or []
    full_text = re.sub(r"\s+", " ", str(script.get("voiceover_full") or "")).strip()
    if not full_text:
        raise LocalAssetError("本地素材脚本缺少 voiceover_full，已阻断分镜短句拼接口播")
    cues = [re.sub(r"\s+", " ", str(item.get("voiceover") or "")).strip() for item in current_segments]
    if not cues or any(not cue for cue in cues):
        raise LocalAssetError("本地素材连续口播存在空的分镜口播 cue")
    expected = "".join(cues) + str(script.get("voiceover_outro_cue") or "")
    if expected != full_text:
        raise LocalAssetError(
            "voiceover_full 必须由分镜 voiceover cue 无损、有序切分，字幕与口播才能共享同一文案源"
        )
    return full_text


def build_one_take_timeline(
    script: Dict[str, Any],
    main_duration: float,
    outro_duration: float = 0.0,
    transition_duration: float = 0.0,
    max_clip_durations: Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    """Map one punctuated narration onto a continuous edit timeline."""
    validate_continuous_voiceover_contract(script)
    segments = script.get("segments") or []
    cues = [str(segment.get("voiceover") or "").strip() for segment in segments]
    main_duration = max(float(main_duration), len(cues) * 0.5)
    weights = [
        max(1.0, float(_speech_units(cue)) + 0.8 * len(re.findall(r"[，。！？；]", cue)))
        for cue in cues
    ]
    weight_total = sum(weights) or 1.0
    cue_durations = [main_duration * weight / weight_total for weight in weights]
    capacities = [
        max(
            0.5,
            float((max_clip_durations or {}).get(int(segment.get("segment", index)), main_duration))
            - (transition_duration if index > 0 else 0.0),
        )
        for index, segment in enumerate(segments)
    ]
    total_capacity = sum(capacities)
    if total_capacity + 0.05 < main_duration:
        gap = {
            "reason": "semantic_segment_material_capacity_gap",
            "stage": "one_take_timeline",
            "required_duration": round(main_duration, 3),
            "covered_duration": round(total_capacity, 3),
            "missing_duration": round(main_duration - total_capacity, 3),
        }
        raise LocalAssetError(
            f"可用素材总时长 {total_capacity:.2f}s 无法覆盖单条口播主内容 {main_duration:.2f}s",
            details=gap,
        )
    active = set(range(len(cue_durations)))
    remaining = main_duration
    final_durations = [0.0] * len(cue_durations)
    while active:
        active_weight = sum(weights[index] for index in active) or float(len(active))
        capped = []
        for index in active:
            proposed = remaining * weights[index] / active_weight
            if proposed > capacities[index]:
                final_durations[index] = capacities[index]
                remaining -= capacities[index]
                capped.append(index)
        if not capped:
            for index in active:
                final_durations[index] = remaining * weights[index] / active_weight
            remaining = 0.0
            break
        active.difference_update(capped)
    cue_durations = final_durations
    cursor = 0.0
    voiceover_lines = []
    clip_durations: Dict[int, float] = {}
    for index, (segment, cue, cue_duration) in enumerate(zip(segments, cues, cue_durations)):
        end = main_duration if index == len(cues) - 1 else cursor + cue_duration
        voiceover_lines.append({
            "text": cue,
            "start": round(cursor, 3),
            "end": round(end, 3),
            "segment": int(segment.get("segment", index)),
        })
        clip_durations[int(segment.get("segment", index))] = round(
            max(0.5, end - cursor),
            3,
        )
        cursor = end
    outro_cue = str(script.get("voiceover_outro_cue") or "").strip()
    if outro_cue and outro_duration > 0:
        voiceover_lines.append({
            "text": outro_cue,
            "start": round(main_duration, 3),
            "end": round(main_duration + outro_duration, 3),
            "segment": len(segments),
            "is_outro": True,
        })
    return {
        "voiceover_lines": voiceover_lines,
        "clip_durations": clip_durations,
        "main_duration": round(main_duration, 3),
        "outro_duration": round(max(0.0, float(outro_duration)), 3),
        "total_duration": round(main_duration + max(0.0, float(outro_duration)), 3),
        "source": "single_take_master_duration_and_punctuated_cue_weights",
        "alignment_precision": "cue_weight_estimate",
    }


def _copy_preflight_check(
    segment: Dict[str, Any],
    duration: Optional[float],
    product_info: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Reject only copy claims that exceed trusted product or visual evidence."""
    violations = _marketing_claim_violations(segment, product_info, analysis)
    identity_violations = _product_identity_violations(
        segment,
        str(product_info.get("name") or ""),
        analysis,
    )
    for field, claims in identity_violations.items():
        violations.setdefault(field, []).extend(claims)
    if not violations:
        return None

    unsupported_fields = set()
    unsupported_claims = []
    for field, claims in violations.items():
        unsupported_fields.add(field)
        unsupported_claims.extend(f"{field}: {claim}" for claim in claims)
    unsupported_claims = list(dict.fromkeys(unsupported_claims))
    return {
        "supported": False,
        "subtitle_supported": "subtitle" not in unsupported_fields,
        "voiceover_supported": "voiceover" not in unsupported_fields,
        "visual_supported": True,
        "static_supported": True,
        "dynamic_required": False,
        "dynamic_supported": True,
        "confidence": 1.0,
        "unsupported_fields": sorted(unsupported_fields),
        "unsupported_claims": unsupported_claims,
        "reason": "字幕或口播包含缺少可信产品资料或画面证据的事实主张",
    }


def _validate_derived_material_segment(
    segment: Dict[str, Any],
    window: Dict[str, Any],
    product_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate copy against independent indexed evidence without re-querying that same evidence."""
    analysis = window.get("analysis") or {}
    motion = window.get("motion") or {}
    if not _story_role_supported(segment, analysis):
        return {
            "supported": False,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": False,
            "static_supported": True,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": ["product_story_role", "visual_requirement"],
            "unsupported_claims": [
                f"素材语义类别不匹配：需要 {segment.get('product_story_role')}，"
                f"实际 {analysis.get('product_story_role')}"
            ],
            "reason": "脚本要求的素材语义类别与视频内容识别结果不一致",
            "validation_source": "indexed_local_evidence",
        }
    deterministic = _copy_preflight_check(segment, None, product_info, analysis)
    if deterministic is not None:
        return {**deterministic, "validation_source": "indexed_local_evidence"}
    action_claims = _temporal_action_claims(segment)
    unsupported_actions = _unsupported_action_claims(segment, analysis)
    dynamic_supported = bool(
        not action_claims
        or motion.get("subject_motion") in {"medium", "high"}
        or float(motion.get("subject_motion_ratio") or 0.0) >= 0.045
    )
    if unsupported_actions or not dynamic_supported:
        claims = unsupported_actions or action_claims
        return {
            "supported": False,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": False,
            "static_supported": True,
            "dynamic_required": bool(action_claims),
            "dynamic_supported": False,
            "confidence": max(0.75, float(analysis.get("confidence") or 0.0)),
            "unsupported_fields": ["visual_requirement"],
            "unsupported_claims": [f"时序事件不支持动作类别：{value}" for value in claims],
            "reason": "索引中的连续帧与光流证据不支持脚本动作",
            "validation_source": "indexed_local_evidence",
        }
    return {
        "supported": True,
        "subtitle_supported": True,
        "voiceover_supported": True,
        "visual_supported": True,
        "static_supported": True,
        "dynamic_required": bool(action_claims),
        "dynamic_supported": True,
        "confidence": max(0.75, float(analysis.get("confidence") or 0.0)),
        "unsupported_fields": [],
        "unsupported_claims": [],
        "reason": "通过已索引的本地逐帧、动作与素材角色证据校验",
        "validation_source": "indexed_local_evidence",
    }


def _script_checkpoint_context(
    asset_index: Optional[Dict[str, Any]],
    product_info: Dict[str, Any],
    num_segments: int,
    script_style: str,
    segment_durations: Optional[Dict[int, float]] = None,
    user_policy_fingerprint: str = "",
    narration_contract: Optional[Dict[str, Any]] = None,
    narrative_plan: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Path], str]:
    asset_folder = str((asset_index or {}).get("asset_folder") or "")
    if not asset_folder:
        return None, ""
    signature_payload = {
        "script_contract_version": SCRIPT_CONTRACT_VERSION,
        "index_version": (asset_index or {}).get("index_version"),
        "frame_evidence_version": (asset_index or {}).get("frame_evidence_version"),
        "sources": [
            {key: source.get(key) for key in ("path", "sha256", "size", "mtime_ns")}
            for source in (asset_index or {}).get("sources") or []
        ],
        "product_info": {
            key: product_info.get(key)
            for key in (
                "name", "type", "selling_point", "ingredients", "raw_materials",
                "origin", "production_process", "specifications", "verified_claims",
                "price", "pricing", "usage", "usage_scenarios",
            )
        },
        "num_segments": num_segments,
        "script_style": script_style,
        "segment_durations": segment_durations or {},
        "narration_contract": narration_contract or {},
        "narrative_plan": narrative_plan or [],
        "user_policy_fingerprint": user_policy_fingerprint,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    cache_id = hashlib.sha256(str(Path(asset_folder).expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return LOCAL_ASSET_INDEX_PATH / cache_id / "script_checkpoint.json", signature


def _load_script_checkpoint(path: Optional[Path], signature: str) -> Optional[Dict[str, Any]]:
    if not path or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("signature") != signature:
        return None
    result = payload.get("result")
    status = payload.get("status")
    if status not in {"in_progress", "complete"}:
        return None
    return result if isinstance(result, dict) and isinstance(result.get("segments"), list) else None


def _write_script_checkpoint(
    path: Optional[Path],
    signature: str,
    result: Dict[str, Any],
    status: str = "in_progress",
) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signature": signature,
        "status": status,
        "updated_at": time.time(),
        "result": result,
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _product_identity_violations(
    segment: Dict[str, Any],
    product_name: str,
    analysis: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Require direct identity evidence whenever copy names the product."""
    normalized_name = str(product_name or "").strip()
    if len(normalized_name) < 2:
        return {}
    identity_evidence = " ".join([
        " ".join(str(value) for value in analysis.get("visible_text") or []),
        " ".join(str(value) for value in analysis.get("visible_objects") or []),
        str(analysis.get("evidence") or ""),
    ])
    verified_product_relation = bool(analysis.get("product_relationship_verified"))
    if normalized_name in identity_evidence or verified_product_relation:
        return {}
    high_relevance_broll = str(analysis.get("product_relevance_prior") or "").lower() == "high"
    if high_relevance_broll and not any(
        PRODUCT_RELATION_ASSERTION.search(str(segment.get(field) or ""))
        for field in ("subtitle", "voiceover")
    ):
        return {}
    return {
        field: [normalized_name]
        for field in ("subtitle", "voiceover")
        if normalized_name in str(segment.get(field) or "")
    }


def _motion_consistency(analysis: Dict[str, Any], motion: Dict[str, Any]) -> Dict[str, Any]:
    """Reject only high-confidence static/dynamic disagreements between independent channels."""
    visual_class = str(analysis.get("shot_type") or "static")
    computed_class = str(motion.get("motion_class") or "static")
    motion_confidence = float(motion.get("confidence") or 0.0)
    visual_confidence = float(analysis.get("confidence") or 0.0)
    reasons = []
    if motion_confidence >= 0.75 and visual_confidence >= 0.75:
        if visual_class == "static" and computed_class == "dynamic":
            reasons.append("VLM 判断静态，但光流检测到高强度动态")
        elif visual_class == "dynamic" and computed_class == "static":
            reasons.append("VLM 判断动态，但连续帧未检测到可靠运动")
    return {
        "passed": not reasons,
        "visual_class": visual_class,
        "computed_class": computed_class,
        "reasons": reasons,
    }


class VisionAnalyzer:
    def __init__(self):
        self.base_url = VISION_BASE_URL.rstrip("/")
        self.api_key = VISION_API_KEY
        self.model = VISION_MODEL

    def analyze_window(self, sheet_path: Path, metadata: Dict[str, Any]) -> Dict[str, Any]:
        system_prompt = (
            "你是严格的逐帧视频素材分析员。只描述画面直接可见事实，不推断产品配方、具体产地、"
            "感官、功效或人物身份。必须识别素材在商品叙事中可能承担的成品、原料、产地环境、"
            "生产过程、使用、结果或背景角色，但关系候选只能描述画面本身，不能断言其属于某产品。"
            "必须区分桌面/柜台/货架，倒入/摆放/指向等动作。只返回有效 JSON。"
        )
        user_text = f"""
Analyze this contact sheet for a local product ad edit.

Window metadata:
- source_video: {metadata.get("source_video")}
- start: {metadata.get("start")}
- end: {metadata.get("end")}
- duration: {metadata.get("duration")}
- frame_count: {metadata.get("frame_count")}
- computed_motion: {json.dumps(metadata.get("motion") or {}, ensure_ascii=False)}

Return this exact JSON shape:
{{
  "shot_type": "static|semi_dynamic|dynamic",
  "narrative_roles": ["hook|pain_point|product_showcase|usage_demo|result|cta|filler"],
  "action_phase": "setup|action|outcome|none",
  "motion_level": "low|medium|high",
  "visible_subjects": ["person|product|hands|environment|text|unknown"],
  "setting": "桌面|柜台|货架|室内房间|传统茶室|其他|unknown",
  "literal_actions": ["画面中直接可见的动作，使用中文"],
  "temporal_events": [{{"start": 0.0, "end": 0.0, "action": "按时间顺序描述直接可见动作"}}],
  "visible_objects": ["画面中直接可见的对象，使用中文"],
  "object_tracks": [{{"object": "对象", "visible_frame_count": 0, "first_seen": 0.0, "last_seen": 0.0}}],
  "visible_text": [{{"text": "可辨认的画面文字", "visible_frame_count": 0, "first_seen": 0.0, "last_seen": 0.0}}],
  "product_story_role": "finished_product|ingredient|origin|production|usage|result|context|unknown",
  "relation_candidates": ["画面直接可见、可供产品资料核验的原料名、地貌、工艺或用途候选"],
  "relation_confidence": 0.0,
  "relation_evidence": "只写支持角色判断的画面事实，不断言与具体产品有关",
  "product_visibility": 0,
  "camera_scale": "wide|medium|closeup|macro|unknown",
  "emotion": "pain|curiosity|relief|excitement|calm|unknown",
  "usable_for_ad": true,
  "confidence": 0.0,
  "evidence": "用中文逐字描述画面证据，不写推断"
}}

Rules:
- product_visibility is 0-5.
- confidence and relation_confidence are 0-1.
- product_story_role describes what the footage itself depicts, not an asserted relationship to the advertised product.
- A field, plantation or landscape may be origin footage, but never infer its geographic name.
- Loose leaves, flowers, fruit, beans or other materials may be ingredient footage, but never infer that the product uses them.
- A filename is metadata only and cannot prove an ingredient, origin or production relationship.
- Use unknown/none when not visually supported.
- Do not infer product benefits that are not visible.
- A home counter or tabletop is not a retail shelf.
- Smell, taste, refreshment, health effects and office-worker identity are never visually supported.
- Frame labels contain actual timestamps. List actions in temporal order with timestamps.
- Track important objects across frames and report how many timestamped frames support each object.
- visible_frame_count must be counted from the contact sheet and cannot exceed frame_count.
- Report visible text only when the same text is legible in at least two timestamped frames. Never guess a blurred label.
- computed_motion is independent optical-flow evidence. Do not claim an action when both the frames and motion evidence are static.
"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": _image_data_url(sheet_path)}},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 1600,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload["stream"] = True
        last_error: Optional[Exception] = None
        for attempt in range(1, VISION_MAX_RETRIES + 2):
            print(
                f"      视觉理解请求 {attempt}/{VISION_MAX_RETRIES + 1} "
                f"（连续无响应数据上限 {VISION_TIMEOUT}s）",
                flush=True,
            )
            response: Optional[requests.Response] = None
            started_at = time.monotonic()
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=(10, VISION_TIMEOUT),
                    stream=True,
                )
                response.raise_for_status()
                parsed = _streamed_chat_json(response)
                return _normalize_analysis(parsed)
            except Exception as exc:
                last_error = exc
                elapsed = time.monotonic() - started_at
                request_id = ""
                if response is not None:
                    request_id = response.headers.get("x-request-id") or response.headers.get("x-tt-logid") or ""
                retryable = _is_retryable_vision_error(exc)
                if retryable and attempt <= VISION_MAX_RETRIES:
                    wait_seconds = min(2 ** attempt, 8)
                    request_suffix = f"，request_id={request_id}" if request_id else ""
                    print(
                        f"      视觉理解失败（{type(exc).__name__}，耗时 {elapsed:.1f}s{request_suffix}），"
                        f"{wait_seconds}s 后重试：{exc}",
                        flush=True,
                    )
                    time.sleep(wait_seconds)
                    continue
                break
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        response.close()
        raise LocalAssetError(f"视觉分析失败：{sheet_path.name} ({last_error})")

    def analyze_reference_ad(self, sheet_path: Path, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Extract a reusable ad contract from a user-provided reference video."""
        prompt = f"""
你正在分析用户明确提供的同产品参考广告。只提取联系表中可直接看见的字幕、标签、镜头结构和尾卡，
不得从音频、文件名或常识补全事实。参考视频仅为当前产品提供证据和创作结构。

metadata: {json.dumps(metadata, ensure_ascii=False)}

返回严格 JSON：
{{
  "sales_structure": ["question_hook|ingredient_proof|origin_proof|process_proof|benefit_translation|product_showcase|usage_demo|cta_outro"],
  "visible_sales_copy": [{{"text": "逐条抄录画面中可辨认的销售字幕", "evidence_times": [1.23, 2.34]}}],
  "factual_claims": [{{"text": "画面明确写出的事实", "type": "ingredient|origin|production|specification|effect", "evidence_times": [1.23, 2.34]}}],
  "copy_tone": ["question|proof|benefit|cta"],
  "creative_mechanics": {{
    "hook_mechanism": "question|contrast_question|curiosity_gap|demonstration",
    "proof_pattern": "source_to_reason|ingredient_to_reason|process_to_reason|demonstration_to_convenience|none",
    "progression_pattern": "hook_to_proof_to_value_to_action",
    "spoken_style": "conversational|expert|testimonial|energetic",
    "sentence_rhythm": "short_punchy|mixed|measured",
    "cta_pressure": "soft|medium|strong"
  }},
  "cta_text": "尾卡中直接可见的行动号召",
  "cta_evidence_times": [17.5, 19.2],
  "outro_duration": 0.0,
  "recommended_material_segments": 5,
  "recommended_main_duration": 0.0,
  "evidence": "说明每个结论来自哪些可见文字或帧"
}}

规则：
- factual_claims 只收录画面文字明确陈述的事实，不把氛围、画面对象或推测写成事实。
- 每条 visible_sales_copy、factual_claims 和 cta_text 必须由至少两个不同时间戳的清晰帧一致支持；
  不足两帧、字形模糊或多帧读法冲突时必须省略，禁止猜字。
- sales_structure 按实际出现顺序输出，可重复但不要虚构缺失环节。
- creative_mechanics 只概括可迁移的创作方法，不复制具体地名、原料、功效或原句。
- outro_duration 和 recommended_main_duration 参考 metadata 中场景边界及尾卡画面估算。
- 不评价好坏，不输出 JSON 之外的文字。
"""
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _image_data_url(sheet_path)}},
                ],
            }],
            "temperature": 0.0,
            "max_tokens": 1400,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        headers["Accept"] = "text/event-stream"
        payload["stream"] = True
        last_error: Optional[Exception] = None
        for attempt in range(1, VISION_MAX_RETRIES + 2):
            response: Optional[requests.Response] = None
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=VISION_TIMEOUT,
                    stream=True,
                )
                response.raise_for_status()
                raw = _chat_response_json(response)
                return _normalize_reference_profile(raw, metadata)
            except Exception as exc:
                last_error = exc
                if _is_retryable_vision_error(exc) and attempt <= VISION_MAX_RETRIES:
                    time.sleep(min(2 * attempt, 6))
                else:
                    break
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        response.close()
        raise LocalAssetError(f"参考广告分析失败：{sheet_path.name} ({last_error})")

    def validate_segment(
        self,
        sheet_path: Path,
        segment: Dict[str, Any],
        motion: Optional[Dict[str, Any]] = None,
        analysis: Optional[Dict[str, Any]] = None,
        product_name: str = "",
        product_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate visuals strictly and marketing facts against trusted evidence."""
        motion = motion or {}
        analysis = analysis or {}
        product_info = {**(product_info or {}), "name": (product_info or {}).get("name") or product_name}
        action_claims = _temporal_action_claims(segment)
        dynamic_required = bool(action_claims)
        computed_dynamic_supported = bool(
            not dynamic_required
            or motion.get("subject_motion") in {"medium", "high"}
            or float(motion.get("subject_motion_ratio") or 0.0) >= 0.045
        )
        unsupported_actions = _unsupported_action_claims(segment, analysis)
        deterministic_violations = _marketing_claim_violations(segment, product_info, analysis)
        if not _story_role_supported(segment, analysis):
            expected = str(segment.get("product_story_role") or "unknown")
            actual = str(analysis.get("product_story_role") or "unknown")
            return {
                "supported": False,
                "subtitle_supported": True,
                "voiceover_supported": True,
                "visual_supported": False,
                "static_supported": True,
                "dynamic_required": dynamic_required,
                "dynamic_supported": computed_dynamic_supported,
                "confidence": 1.0,
                "unsupported_fields": ["product_story_role", "visual_requirement"],
                "unsupported_claims": [f"素材语义类别不匹配：需要 {expected}，实际 {actual}"],
                "reason": "脚本要求的素材语义类别与视频内容识别结果不一致",
            }
        if deterministic_violations:
            unsupported_fields = list(deterministic_violations)
            unsupported_claims = [
                f"{field}: {claim}"
                for field, claims in deterministic_violations.items()
                for claim in claims
            ]
            return {
                "supported": False,
                "subtitle_supported": "subtitle" not in deterministic_violations,
                "voiceover_supported": "voiceover" not in deterministic_violations,
                "visual_supported": True,
                "static_supported": True,
                "dynamic_required": dynamic_required,
                "dynamic_supported": computed_dynamic_supported,
                "confidence": 1.0,
                "unsupported_fields": unsupported_fields,
                "unsupported_claims": unsupported_claims,
                "reason": (
                    "字幕或口播包含缺少可信产品资料或画面证据的事实主张"
                ),
            }
        if dynamic_required and motion and not computed_dynamic_supported:
            return {
                "supported": False,
                "subtitle_supported": True,
                "voiceover_supported": True,
                "visual_supported": False,
                "static_supported": True,
                "dynamic_required": True,
                "dynamic_supported": False,
                "confidence": max(0.75, float(motion.get("confidence") or 0.0)),
                "unsupported_fields": ["visual_requirement"],
                "unsupported_claims": [f"缺少动作的连续帧证据：{claim}" for claim in action_claims],
                "reason": "脚本描述明确动作，但连续帧光流与相机运动分离后未检测到足够主体运动",
            }
        if unsupported_actions:
            return {
                "supported": False,
                "subtitle_supported": True,
                "voiceover_supported": True,
                "visual_supported": False,
                "static_supported": True,
                "dynamic_required": True,
                "dynamic_supported": False,
                "confidence": max(0.75, float(analysis.get("confidence") or 0.0)),
                "unsupported_fields": ["visual_requirement"],
                "unsupported_claims": [f"时序事件不支持动作类别：{concept}" for concept in unsupported_actions],
                "reason": "连续帧存在运动，但索引时序事件与脚本动作类别不一致",
            }

        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Validate visual_requirement strictly against visible pixels and temporal evidence. "
                            "Subtitle and voiceover are sales copy, not literal image captions. Allow hooks, "
                            "curiosity, value framing, persuasion and CTA without requiring their wording to "
                            "appear in pixels. For subtitle and voiceover, judge only concrete factual claims. "
                            "A factual claim may be supported by visible pixels, trusted_product_info, or a "
                            "verified asset-product relationship. Never accept invented taste, effect, origin, "
                            "ingredient, price, identity or action. voiceover is supplied as text: never reject "
                            "it merely because spoken audio itself is not visible. Overall supported may be true "
                            "only when the visual requirement and all factual claims are supported. Return only JSON: "
                            '{"supported": true, "subtitle_supported": true, '
                            '"voiceover_supported": true, "visual_supported": true, '
                            '"static_supported": true, "dynamic_supported": true, '
                            '"confidence": 0.0, "unsupported_fields": [], '
                            '"unsupported_claims": [], "reason": ""}.\n'
                            f"dynamic_required: {dynamic_required}\n"
                            f"action_claims: {json.dumps(action_claims, ensure_ascii=False)}\n"
                            f"computed_motion: {json.dumps(motion, ensure_ascii=False)}\n"
                            f"indexed_visual_evidence: {json.dumps(analysis, ensure_ascii=False)}\n"
                            f"trusted_product_info: {json.dumps(product_info, ensure_ascii=False)}\n"
                            f"marketing_intent: {segment.get('marketing_intent', '')}\n"
                            f"required_product_story_role: {segment.get('product_story_role', '')}\n"
                            f"structured_claims: {json.dumps(segment.get('claims') or [], ensure_ascii=False)}\n"
                            f"subtitle: {segment.get('subtitle', '')}\n"
                            f"voiceover_text_claims: {segment.get('voiceover', '')}\n"
                            f"visual_requirement: {segment.get('visual_requirement', '')}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _image_data_url(sheet_path)}},
                ],
            }],
            "temperature": 0.0,
            "max_tokens": 1000,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        headers["Accept"] = "text/event-stream"
        payload["stream"] = True
        last_error: Optional[Exception] = None
        for attempt in range(1, VISION_MAX_RETRIES + 2):
            response: Optional[requests.Response] = None
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=VISION_TIMEOUT,
                    stream=True,
                )
                response.raise_for_status()
                raw = _chat_response_json(response)
                required_boolean_fields = {
                    "supported",
                    "subtitle_supported",
                    "voiceover_supported",
                    "visual_supported",
                    "static_supported",
                    "dynamic_supported",
                }
                missing_fields = sorted(required_boolean_fields - raw.keys())
                if missing_fields:
                    raise ValueError(f"视觉判定缺少字段：{', '.join(missing_fields)}")
                if any(not isinstance(raw[field], bool) for field in required_boolean_fields):
                    raise ValueError("视觉判定布尔字段类型无效")
                subtitle_supported = bool(raw.get("subtitle_supported", False))
                voiceover_supported = bool(raw.get("voiceover_supported", False))
                visual_supported = bool(raw.get("visual_supported", False))
                static_supported = bool(raw.get("static_supported", True))
                vlm_dynamic_supported = bool(raw.get("dynamic_supported", not dynamic_required))
                dynamic_supported = bool(
                    not dynamic_required
                    or (computed_dynamic_supported and vlm_dynamic_supported)
                )
                unsupported_fields = [
                    field
                    for field, supported in (
                        ("subtitle", subtitle_supported),
                        ("voiceover", voiceover_supported),
                        ("visual_requirement", visual_supported),
                    )
                    if not supported
                ]
                return {
                    "supported": bool(
                        raw.get("supported", False)
                        and subtitle_supported
                        and voiceover_supported
                        and visual_supported
                        and static_supported
                        and dynamic_supported
                    ),
                    "subtitle_supported": subtitle_supported,
                    "voiceover_supported": voiceover_supported,
                    "visual_supported": visual_supported,
                    "static_supported": static_supported,
                    "dynamic_required": dynamic_required,
                    "dynamic_supported": dynamic_supported,
                    "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.0)))),
                    "unsupported_fields": unsupported_fields,
                    "unsupported_claims": [str(value) for value in raw.get("unsupported_claims") or []],
                    "reason": str(raw.get("reason") or ""),
                }
            except Exception as exc:
                last_error = exc
                if _is_retryable_vision_error(exc) and attempt <= VISION_MAX_RETRIES:
                    time.sleep(min(2 * attempt, 6))
                else:
                    break
            finally:
                if response is not None:
                    with contextlib.suppress(Exception):
                        response.close()
        raise LocalAssetError(f"脚本与素材视觉语义校验失败：{sheet_path.name} ({last_error})")


def _normalize_analysis(raw: Dict[str, Any]) -> Dict[str, Any]:
    roles = raw.get("narrative_roles") or []
    if isinstance(roles, str):
        roles = [roles]
    subjects = raw.get("visible_subjects") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    literal_actions = raw.get("literal_actions") or []
    if isinstance(literal_actions, str):
        literal_actions = [literal_actions]
    visible_objects = raw.get("visible_objects") or []
    if isinstance(visible_objects, str):
        visible_objects = [visible_objects]
    visible_text = raw.get("visible_text") or []
    if isinstance(visible_text, (str, dict)):
        visible_text = [visible_text]
    normalized_text = []
    for item in visible_text if isinstance(visible_text, list) else []:
        if isinstance(item, str):
            normalized_text.append(item)
            continue
        if not isinstance(item, dict) or not item.get("text"):
            continue
        try:
            visible_count = int(item.get("visible_frame_count", 0))
        except (TypeError, ValueError):
            visible_count = 0
        if visible_count >= 2:
            normalized_text.append(str(item["text"]))
    object_tracks = []
    for track in raw.get("object_tracks") or []:
        if not isinstance(track, dict) or not track.get("object"):
            continue
        try:
            visible_count = int(track.get("visible_frame_count", 0))
            first_seen = float(track.get("first_seen", 0.0))
            last_seen = float(track.get("last_seen", first_seen))
        except (TypeError, ValueError):
            continue
        object_tracks.append({
            "object": str(track["object"]),
            "visible_frame_count": max(0, visible_count),
            "first_seen": round(first_seen, 3),
            "last_seen": round(max(first_seen, last_seen), 3),
        })
    temporal_events = raw.get("temporal_events") or []
    normalized_events = []
    for event in temporal_events if isinstance(temporal_events, list) else []:
        if not isinstance(event, dict) or not event.get("action"):
            continue
        try:
            start = float(event.get("start", 0.0))
            end = max(start, float(event.get("end", start)))
        except (TypeError, ValueError):
            continue
        normalized_events.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "action": str(event["action"]),
        })
    try:
        product_visibility = max(0, min(5, int(float(raw.get("product_visibility", 0)))))
    except (TypeError, ValueError):
        product_visibility = 0
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    story_role = str(raw.get("product_story_role") or "unknown").strip().lower()
    if story_role not in PRODUCT_STORY_ROLES:
        story_role = "unknown"
    relation_candidates = raw.get("relation_candidates") or []
    if isinstance(relation_candidates, str):
        relation_candidates = [relation_candidates]
    try:
        relation_confidence = max(0.0, min(1.0, float(raw.get("relation_confidence", 0))))
    except (TypeError, ValueError):
        relation_confidence = 0.0
    return {
        "shot_type": str(raw.get("shot_type") or "static"),
        "narrative_roles": [str(r) for r in roles],
        "action_phase": str(raw.get("action_phase") or "none"),
        "motion_level": str(raw.get("motion_level") or "low"),
        "visible_subjects": [str(s) for s in subjects],
        "setting": str(raw.get("setting") or "unknown"),
        "literal_actions": [str(value) for value in literal_actions],
        "temporal_events": normalized_events,
        "visible_objects": [str(value) for value in visible_objects],
        "object_tracks": object_tracks,
        "visible_text": normalized_text,
        "product_story_role": story_role,
        "relation_candidates": [str(value) for value in relation_candidates if str(value).strip()],
        "relation_confidence": relation_confidence,
        "relation_evidence": str(raw.get("relation_evidence") or ""),
        "product_visibility": product_visibility,
        "camera_scale": str(raw.get("camera_scale") or "unknown"),
        "emotion": str(raw.get("emotion") or "unknown"),
        "usable_for_ad": bool(raw.get("usable_for_ad", False)),
        "confidence": confidence,
        "evidence": str(raw.get("evidence") or ""),
    }


def _normalize_reference_profile(raw: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    allowed_structure = {
        "question_hook", "ingredient_proof", "origin_proof", "process_proof",
        "benefit_translation", "product_showcase", "usage_demo", "cta_outro",
    }
    structure = raw.get("sales_structure") or []
    if isinstance(structure, str):
        structure = [structure]
    def _supported_text_items(values: Any) -> List[Dict[str, Any]]:
        supported = []
        for value in values or []:
            if not isinstance(value, dict):
                continue
            text = str(value.get("text") or "").strip()
            evidence_times = []
            for timestamp in value.get("evidence_times") or []:
                try:
                    evidence_times.append(round(float(timestamp), 3))
                except (TypeError, ValueError):
                    continue
            evidence_times = sorted(set(evidence_times))
            if text and len(evidence_times) >= 2:
                supported.append({"text": text, "evidence_times": evidence_times})
        return supported

    supported_copy = _supported_text_items(raw.get("visible_sales_copy"))
    factual_claims = []
    for claim in raw.get("factual_claims") or []:
        if not isinstance(claim, dict) or not str(claim.get("text") or "").strip():
            continue
        supported = _supported_text_items([claim])
        if not supported:
            continue
        factual_claims.append({
            "text": str(claim["text"]).strip(),
            "type": str(claim.get("type") or "specification").strip(),
            "evidence_source": "reference_video",
            "evidence_times": supported[0]["evidence_times"],
        })
    duration = max(0.0, float(metadata.get("duration") or 0.0))
    try:
        outro_duration = max(0.0, min(duration, float(raw.get("outro_duration") or 0.0)))
    except (TypeError, ValueError):
        outro_duration = 0.0
    try:
        main_duration = max(0.0, min(duration, float(raw.get("recommended_main_duration") or 0.0)))
    except (TypeError, ValueError):
        main_duration = 0.0
    if main_duration <= 0 and duration > 0:
        main_duration = max(0.0, duration - outro_duration)
    try:
        material_segments = max(3, min(7, int(raw.get("recommended_material_segments") or 5)))
    except (TypeError, ValueError):
        material_segments = 5
    cta_text = str(raw.get("cta_text") or "").strip()
    cta_times = []
    for timestamp in raw.get("cta_evidence_times") or []:
        try:
            cta_times.append(round(float(timestamp), 3))
        except (TypeError, ValueError):
            continue
    cta_times = sorted(set(cta_times))
    if len(cta_times) < 2:
        cta_text = ""
        outro_duration = 0.0
        main_duration = duration

    return {
        "reference_profile_version": REFERENCE_PROFILE_VERSION,
        "source_path": str(metadata.get("source_path") or ""),
        "source_sha256": str(metadata.get("source_sha256") or ""),
        "duration": round(duration, 3),
        "scene_boundaries": metadata.get("scene_boundaries") or [],
        "sales_structure": [str(value) for value in structure if str(value) in allowed_structure],
        "visible_sales_copy": [item["text"] for item in supported_copy],
        "factual_claims": factual_claims,
        "copy_tone": [str(value) for value in raw.get("copy_tone") or [] if str(value).strip()],
        "creative_mechanics": _normalize_reference_creative_mechanics(raw.get("creative_mechanics")),
        "cta_text": cta_text,
        "cta_evidence_times": cta_times if cta_text else [],
        "outro_duration": round(outro_duration, 3),
        "recommended_material_segments": material_segments,
        "recommended_main_duration": round(main_duration, 3),
        "continuous_voiceover": True,
        "evidence": str(raw.get("evidence") or ""),
    }


def _normalize_reference_creative_mechanics(value: Any) -> Dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    allowed = {
        "hook_mechanism": {"question", "contrast_question", "curiosity_gap", "demonstration"},
        "proof_pattern": {
            "source_to_reason", "ingredient_to_reason", "process_to_reason",
            "demonstration_to_convenience", "none",
        },
        "progression_pattern": {"hook_to_proof_to_value_to_action"},
        "spoken_style": {"conversational", "expert", "testimonial", "energetic"},
        "sentence_rhythm": {"short_punchy", "mixed", "measured"},
        "cta_pressure": {"soft", "medium", "strong"},
    }
    defaults = {
        "hook_mechanism": "curiosity_gap",
        "proof_pattern": "source_to_reason",
        "progression_pattern": "hook_to_proof_to_value_to_action",
        "spoken_style": "conversational",
        "sentence_rhythm": "mixed",
        "cta_pressure": "soft",
    }
    return {
        key: str(raw.get(key)) if str(raw.get(key)) in choices else defaults[key]
        for key, choices in allowed.items()
    }


def _build_viral_creative_blueprint(profile: Dict[str, Any], script_style: str) -> Dict[str, Any]:
    """Expose observed reference mechanics as generation context, never as review rules."""
    structure_map = {
        "question_hook": "hook",
        "ingredient_proof": "source_proof",
        "origin_proof": "source_proof",
        "process_proof": "source_proof",
        "benefit_translation": "buyer_value",
        "product_showcase": "product_confirmation",
        "usage_demo": "usage_value",
        "cta_outro": "action_close",
    }
    structure = list(dict.fromkeys(
        structure_map[str(value)]
        for value in profile.get("sales_structure") or []
        if str(value) in structure_map
    ))
    observed_copy = [
        str(value).strip()
        for value in (
            profile.get("copywriting_style_examples")
            or profile.get("visible_sales_copy")
            or []
        )
        if str(value).strip()
    ]
    mechanics = _normalize_reference_creative_mechanics(profile.get("creative_mechanics"))
    pattern_map = {
        "hook": "用目标人群或使用场景提出一个具体问题",
        "source_proof": "用一个已核验的原料产地或工艺事实回答开头悬念",
        "buyer_value": "把事实翻译成消费者能理解的选择理由",
        "product_confirmation": "用产品形态确认前文对象，不逐项描述包装",
        "usage_value": "用一个真实动作说明使用有多直接",
        "action_close": "用一句短促自然的行动邀请收尾",
    }
    patterns = [pattern_map[item] for item in structure if item in pattern_map]
    average_units = round(
        sum(_speech_units(text) for text in observed_copy) / max(len(observed_copy), 1),
        1,
    )
    return {
        "source": "reference_ad_observation" if structure else "none",
        "script_style": script_style,
        "creative_mechanics": mechanics,
        "reference_sales_arc": structure,
        "reference_copy_tone": [str(value) for value in profile.get("copy_tone") or []],
        "reference_copy_patterns": patterns,
        "reference_rhythm": {
            "average_units": average_units,
            "sentence_rhythm": mechanics["sentence_rhythm"],
            "spoken_style": mechanics["spoken_style"],
        },
        "reference_example_scope": "style_and_rhythm_only",
        "candidate_strategy": {
            "count": 3,
            "routes": "由模型依据素材、目标风格和使用者规则提出三个不同方向",
            "selection": "只按执行可行性和使用者真实反馈偏好选择",
        },
    }


def bind_reference_profile_to_product(
    profile: Dict[str, Any],
    product_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep reference-video facts only when an independent product source agrees."""
    bound = dict(profile)
    bound["copywriting_style_examples"] = [
        str(text).strip()
        for text in profile.get("visible_sales_copy") or []
        if str(text).strip()
    ]
    supported_claims = []
    rejected_texts = set()
    for claim in profile.get("factual_claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_type = str(claim.get("type") or "").strip().lower()
        claim_text = str(claim.get("text") or "").strip()
        if claim_type in CLAIM_FACT_PATHS and _text_matches_facts(
            claim_text,
            _fact_values(product_info, CLAIM_FACT_PATHS[claim_type]),
        ):
            supported_claims.append(dict(claim))
        elif claim_text:
            rejected_texts.add(claim_text)
    bound["factual_claims"] = supported_claims
    supported_fact_texts = [str(claim.get("text") or "") for claim in supported_claims]
    cta_text = str(profile.get("cta_text") or "").strip()
    bound["visible_sales_copy"] = [
        str(text)
        for text in profile.get("visible_sales_copy") or []
        if str(text).strip()
        and (
            str(text).strip() == cta_text
            or _text_matches_facts(str(text), supported_fact_texts)
        )
    ]
    bound["rejected_reference_claims"] = sorted(rejected_texts)
    return bound


def _reference_profile_for_main_script(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Remove CTA copy from main-script inspiration when a separate outro owns it."""
    main = dict(profile or {})
    cta_text = str(main.get("cta_text") or "").strip()
    external_cta = bool(cta_text and float(main.get("outro_duration") or 0.0) > 0)
    if not external_cta:
        main["external_cta_outro"] = False
        return main
    main["sales_structure"] = [
        value for value in main.get("sales_structure") or []
        if str(value) != "cta_outro"
    ]
    main["visible_sales_copy"] = [
        value for value in main.get("visible_sales_copy") or []
        if str(value).strip() != cta_text
    ]
    main["cta_text"] = ""
    main["external_cta_outro"] = True
    return main


def analyze_reference_ad(reference_video: Path) -> Dict[str, Any]:
    """Analyze and cache a user-provided reference ad by file hash."""
    ensure_vision_backend_available()
    path = Path(reference_video).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise LocalAssetError(f"参考视频不存在：{path}")
    probe = _ffprobe(path)
    duration = float(probe.get("duration") or 0.0)
    source_sha256 = _hash_file(path)
    cache_dir = LOCAL_ASSET_INDEX_PATH / "reference_ads" / source_sha256[:16]
    profile_path = cache_dir / "profile.json"
    if profile_path.exists():
        try:
            cached = json.loads(profile_path.read_text(encoding="utf-8"))
            if (
                cached.get("source_sha256") == source_sha256
                and cached.get("reference_profile_version") == REFERENCE_PROFILE_VERSION
            ):
                return cached
            migratable_v4 = (
                cached.get("source_sha256") == source_sha256
                and cached.get("reference_profile_version") == 4
                and isinstance(cached.get("sales_structure"), list)
                and isinstance(cached.get("copy_tone"), list)
                and isinstance(cached.get("visible_sales_copy"), list)
                and float(cached.get("duration") or 0.0) > 0.0
            )
            if migratable_v4:
                migrated = dict(cached)
                migrated["reference_profile_version"] = REFERENCE_PROFILE_VERSION
                migrated["creative_mechanics"] = _normalize_reference_creative_mechanics(None)
                tmp_path = profile_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp_path.replace(profile_path)
                return migrated
        except Exception:
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)
    scenes = _detect_scene_ranges(path, duration)
    frame_count = max(16, len(scenes) + 3)
    sample_times = _reference_contact_sheet_times(scenes, duration, frame_count)
    sheet_path = cache_dir / "contact_sheet.jpg"
    _make_contact_sheet(
        path,
        0.0,
        duration,
        sheet_path,
        frame_count=frame_count,
        preferred_times=sample_times,
        tile_size=(216, 384),
        columns=4,
        jpeg_quality=82,
    )
    metadata = {
        "source_path": str(path),
        "source_sha256": source_sha256,
        "duration": duration,
        "scene_boundaries": [scene["start"] for scene in scenes] + ([scenes[-1]["end"]] if scenes else []),
        "sample_times": sample_times,
    }
    profile = VisionAnalyzer().analyze_reference_ad(sheet_path, metadata)
    profile["reference_profile_version"] = REFERENCE_PROFILE_VERSION
    tmp_path = profile_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(profile_path)
    return profile


def _coverage_from_windows(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    role_scores: Dict[str, float] = {}
    role_counts: Dict[str, int] = {}
    story_role_scores: Dict[str, float] = {}
    story_role_counts: Dict[str, int] = {}
    for window in windows:
        analysis = window.get("analysis") or {}
        if not analysis.get("usable_for_ad"):
            continue
        confidence = float(analysis.get("confidence") or 0)
        product_bonus = min(float(analysis.get("product_visibility") or 0) / 5.0, 1.0) * 0.15
        story_role = str(analysis.get("product_story_role") or "unknown").lower()
        story_role_scores[story_role] = max(
            story_role_scores.get(story_role, 0.0),
            min(confidence + product_bonus, 1.0),
        )
        story_role_counts[story_role] = story_role_counts.get(story_role, 0) + 1
        for role in analysis.get("narrative_roles") or []:
            score = min(confidence + product_bonus, 1.0)
            role_scores[role] = max(role_scores.get(role, 0.0), score)
            role_counts[role] = role_counts.get(role, 0) + 1

    core_roles = ["pain_point", "product_showcase", "usage_demo", "result", "cta"]
    return {
        "role_scores": {role: round(role_scores.get(role, 0.0), 3) for role in core_roles},
        "role_counts": {role: role_counts.get(role, 0) for role in core_roles},
        "story_role_scores": {
            role: round(story_role_scores.get(role, 0.0), 3)
            for role in sorted(PRODUCT_STORY_ROLES - {"unknown"})
        },
        "story_role_counts": {
            role: story_role_counts.get(role, 0)
            for role in sorted(PRODUCT_STORY_ROLES - {"unknown"})
        },
        "usable_windows": sum(1 for w in windows if (w.get("analysis") or {}).get("usable_for_ad")),
        "total_windows": len(windows),
    }


def build_local_asset_creative_profile(asset_index: Dict[str, Any]) -> Dict[str, Any]:
    """Turn measured local-video motion and exposure into an AV creative contract."""
    usable = [
        window
        for window in asset_index.get("windows") or []
        if (window.get("analysis") or {}).get("usable_for_ad")
    ]
    if not usable:
        raise LocalAssetError("本地素材没有可用于生成声音与特效合同的有效视频窗口")

    motion_class_score = {"static": 0.12, "semi_dynamic": 0.55, "dynamic": 1.0}
    weighted_scores: List[Tuple[float, float]] = []
    brightness_values: List[float] = []
    contrast_values: List[float] = []
    story_role_counts: Dict[str, int] = {}
    for window in usable:
        story_role = str((window.get("analysis") or {}).get("product_story_role") or "unknown")
        story_role_counts[story_role] = story_role_counts.get(story_role, 0) + 1
        motion = window.get("motion") or {}
        confidence = max(0.2, float(motion.get("confidence") or 0.0))
        score = (
            0.55 * motion_class_score.get(str(motion.get("motion_class") or "static"), 0.12)
            + 0.20 * min(1.0, float(motion.get("camera_speed") or 0.0) / 0.04)
            + 0.15 * min(1.0, float(motion.get("subject_motion_ratio") or 0.0) / 0.15)
            + 0.10 * min(1.0, float(motion.get("temporal_change") or 0.0) / 0.12)
        )
        weighted_scores.append((score, confidence))
        frame_quality = window.get("frame_quality") or {}
        if frame_quality.get("median_brightness") is not None:
            brightness_values.append(float(frame_quality["median_brightness"]))
        if frame_quality.get("median_contrast") is not None:
            contrast_values.append(float(frame_quality["median_contrast"]))

    total_weight = sum(weight for _, weight in weighted_scores)
    motion_score = sum(score * weight for score, weight in weighted_scores) / max(total_weight, 0.001)
    energy = "high" if motion_score >= 0.68 else "medium" if motion_score >= 0.38 else "low"
    source_story_count = sum(
        story_role_counts.get(role, 0)
        for role in ("ingredient", "origin", "production")
    )
    semantic_tone = "natural_origin" if source_story_count else "product_demo"
    if source_story_count and energy == "high":
        energy = "medium"
    energy_settings = {
        "high": {
            "recommended_pace": "fast", "bpm_min": 116, "bpm_max": 132,
            "intro_type": "immediate", "transition_base_duration": 0.25,
            "sfx_intensity": "moderate",
        },
        "medium": {
            "recommended_pace": "moderate", "bpm_min": 96, "bpm_max": 116,
            "intro_type": "immediate", "transition_base_duration": 0.4,
            "sfx_intensity": "subtle",
        },
        "low": {
            "recommended_pace": "cinematic", "bpm_min": 76, "bpm_max": 96,
            "intro_type": "fade_in", "transition_base_duration": 0.55,
            "sfx_intensity": "minimal",
        },
    }

    def median(values: List[float], default: float) -> float:
        ordered = sorted(values)
        if not ordered:
            return default
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2

    brightness_value = median(brightness_values, 128.0)
    contrast_value = median(contrast_values, 42.0)
    brightness = "dark" if brightness_value < 78 else "bright" if brightness_value > 168 else "balanced"
    contrast = "soft" if contrast_value < 32 else "high" if contrast_value > 62 else "balanced"
    return {
        "source": "local_asset_analysis",
        "usable_window_count": len(usable),
        "motion_score": round(motion_score, 3),
        "measurement_confidence": round(total_weight / len(weighted_scores), 3),
        "energy": energy,
        "brightness": brightness,
        "median_brightness": round(brightness_value, 2),
        "contrast": contrast,
        "median_contrast": round(contrast_value, 2),
        "story_role_counts": story_role_counts,
        "semantic_tone": semantic_tone,
        **energy_settings[energy],
    }


@contextlib.contextmanager
def _index_build_lock(lock_path: Path):
    """Serialize costly index rebuilds across repeated CLI invocations."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_local_asset_index(asset_folder: Path) -> Dict[str, Any]:
    ensure_vision_backend_available()
    ctx = _folder_cache_context(asset_folder)
    with _index_build_lock(ctx.cache_dir / ".index.lock"):
        return _build_local_asset_index_locked(ctx)


def _build_local_asset_index_locked(ctx: LocalAssetContext) -> Dict[str, Any]:
    ensure_vision_backend_available()
    files = _scan_video_files(ctx.folder)
    if not files:
        raise LocalAssetError(f"素材文件夹中没有可用视频文件：{ctx.folder}")

    signatures = [_source_signature(path) for path in files]
    evidence_manifest_path = ctx.cache_dir / "frame_evidence.json"
    evidence_report_path = ctx.cache_dir / "frame_evidence_report.html"
    if ctx.index_path.exists():
        try:
            cached = json.loads(ctx.index_path.read_text(encoding="utf-8"))
            if (
                cached.get("index_version") == INDEX_VERSION
                and cached.get("build_complete") is True
                and cached.get("frame_evidence_version") == FRAME_EVIDENCE_VERSION
                and _signatures_match(cached.get("sources", []), signatures)
                and evidence_manifest_path.exists()
                and evidence_report_path.exists()
                and all(
                    (window.get("frame_evidence") or {}).get("schema_version")
                    == FRAME_EVIDENCE_VERSION
                    for window in cached.get("windows") or []
                )
            ):
                current_coverage = _coverage_from_windows(cached.get("windows") or [])
                if cached.get("coverage") != current_coverage:
                    cached["coverage"] = current_coverage
                    tmp_index = ctx.index_path.with_suffix(".json.tmp")
                    tmp_index.write_text(
                        json.dumps(cached, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    tmp_index.replace(ctx.index_path)
                return cached
        except Exception:
            pass

    analyzer = VisionAnalyzer()
    reusable_analyses = _load_reusable_window_analyses(ctx.index_path, signatures)
    sheets_dir = ctx.cache_dir / "contact_sheets"
    sources = []
    for source_idx, path in enumerate(files):
        probe = _ffprobe(path)
        source_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        source = {
            **signatures[source_idx],
            "source_id": source_id,
            "name": path.name,
            **probe,
        }
        sources.append(source)

    draft_path = ctx.cache_dir / "index.building.json"
    windows: List[Dict[str, Any]] = []
    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            if (
                draft.get("index_version") == INDEX_VERSION
                and draft.get("frame_evidence_version") == FRAME_EVIDENCE_VERSION
                and _signatures_match(draft.get("sources", []), signatures)
            ):
                windows = list(draft.get("windows") or [])
        except Exception:
            windows = []
    completed_window_ids = {str(window.get("window_id")) for window in windows}

    def _write_draft() -> None:
        draft = {
            "index_version": INDEX_VERSION,
            "frame_evidence_version": FRAME_EVIDENCE_VERSION,
            "vision_analysis_version": VISION_ANALYSIS_VERSION,
            "build_complete": False,
            "asset_folder": str(ctx.folder),
            "created_at": time.time(),
            "sources": sources,
            "windows": windows,
            "coverage": _coverage_from_windows(windows),
        }
        tmp_draft = draft_path.with_suffix(".json.tmp")
        tmp_draft.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_draft.replace(draft_path)

    window_plan = []
    for source in sources:
        for win_idx, win in enumerate(
            _candidate_windows(source, LOCAL_ASSET_WINDOW_SECONDS, LOCAL_ASSET_WINDOW_STRIDE)
        ):
            if len(window_plan) >= LOCAL_ASSET_MAX_WINDOWS:
                break
            window_plan.append((source, win_idx, win))
        if len(window_plan) >= LOCAL_ASSET_MAX_WINDOWS:
            break

    total_windows = len(window_plan)
    resumed_windows = sum(
        1
        for source, win_idx, _ in window_plan
        if f"{source['source_id']}_{win_idx:04d}" in completed_window_ids
    )
    if resumed_windows:
        print(f"   索引断点续建：已完成 {resumed_windows}/{total_windows} 个窗口", flush=True)
    if reusable_analyses:
        print(
            f"   语义缓存可复用：{len(reusable_analyses)} 个窗口；只重建新增帧证据",
            flush=True,
        )

    for plan_position, (source, win_idx, win) in enumerate(window_plan, start=1):
            path = Path(source["path"])
            source_id = str(source["source_id"])
            window_id = f"{source_id}_{win_idx:04d}"
            if window_id in completed_window_ids:
                print(
                    f"   [{plan_position}/{total_windows}] {path.name} "
                    f"{win['start']:.1f}-{win['end']:.1f}s：断点缓存命中",
                    flush=True,
                )
                continue
            print(
                f"   [{plan_position}/{total_windows}] {path.name} "
                f"{win['start']:.1f}-{win['end']:.1f}s：动态/静态分析与帧证据",
                flush=True,
            )
            sheet_path = sheets_dir / f"{window_id}.jpg"
            metadata = {
                "source_video": path.name,
                "start": win["start"],
                "end": win["end"],
                "duration": round(win["end"] - win["start"], 3),
                "frame_count": max(4, LOCAL_ASSET_CONTACT_SHEET_FRAMES),
            }
            motion = _analyze_window_motion(path, win["start"], win["end"])
            frame_quality = _analyze_window_frame_quality(path, win["start"], win["end"])
            motion_peaks = sorted(
                motion.get("active_ranges") or [],
                key=lambda item: float(item.get("strength") or 0.0),
                reverse=True,
            )[:4]
            preferred_times = []
            for active_range in motion_peaks:
                range_start = float(active_range["start"])
                range_end = float(active_range["end"])
                preferred_times.extend([range_start, (range_start + range_end) / 2.0, range_end])
            frame_evidence = build_contact_sheet_evidence(
                source=path,
                start=win["start"],
                end=win["end"],
                output=sheet_path,
                frame_count=max(4, LOCAL_ASSET_CONTACT_SHEET_FRAMES),
                preferred_times=preferred_times,
            )
            selected_frame_records = [
                candidate
                for candidate in frame_evidence["candidates"]
                if candidate["kept"]
            ]
            reason_counts: Dict[str, int] = {}
            for candidate in selected_frame_records:
                reason = str(candidate["selection_reason"])
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            metadata["frame_count"] = int(frame_evidence["kept_count"])
            metadata["frame_evidence"] = {
                "schema_version": FRAME_EVIDENCE_VERSION,
                "candidate_count": frame_evidence["candidate_count"],
                "kept_count": frame_evidence["kept_count"],
                "selected_timestamps": [
                    candidate["timestamp"] for candidate in selected_frame_records
                ],
                "selection_reasons": reason_counts,
            }
            metadata["motion"] = {
                key: value for key, value in motion.items() if key != "samples"
            }
            metadata["frame_quality"] = {
                key: value for key, value in frame_quality.items() if key != "samples"
            }
            analysis = reusable_analyses.get(window_id)
            if analysis is None:
                print("      素材语义：请求视觉模型", flush=True)
                analysis = analyzer.analyze_window(sheet_path, metadata)
            else:
                print("      素材语义：复用同源窗口的已验证理解", flush=True)
            consistency = _motion_consistency(analysis, motion)
            if not consistency["passed"] or not frame_quality["passed"]:
                analysis["usable_for_ad"] = False
                analysis["confidence"] = min(float(analysis.get("confidence") or 0.0), 0.5)
            windows.append({
                "window_id": window_id,
                "source_id": source_id,
                "scene_id": win.get("scene_id", 0),
                "source_path": str(path),
                "source_video": path.name,
                "start": win["start"],
                "end": win["end"],
                "duration": round(win["end"] - win["start"], 3),
                "contact_sheet": str(sheet_path),
                "analysis": analysis,
                "motion": motion,
                "frame_quality": frame_quality,
                "frame_evidence": frame_evidence,
                "motion_consistency": consistency,
            })
            completed_window_ids.add(window_id)
            _write_draft()

    coverage = _coverage_from_windows(windows)
    write_frame_evidence_artifacts(
        windows,
        evidence_manifest_path,
        evidence_report_path,
    )
    for window in windows:
        for candidate in (window.get("frame_evidence") or {}).get("candidates") or []:
            candidate.pop("audit_thumbnail", None)
    index = {
        "index_version": INDEX_VERSION,
        "frame_evidence_version": FRAME_EVIDENCE_VERSION,
        "vision_analysis_version": VISION_ANALYSIS_VERSION,
        "build_complete": True,
        "asset_folder": str(ctx.folder),
        "created_at": time.time(),
        "sources": sources,
        "windows": windows,
        "coverage": coverage,
        "frame_evidence_manifest": str(evidence_manifest_path),
        "frame_evidence_report": str(evidence_report_path),
    }
    tmp_index = ctx.index_path.with_suffix(".json.tmp")
    tmp_index.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_index.replace(ctx.index_path)
    draft_path.unlink(missing_ok=True)
    return index


def _material_catalog(asset_index: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    catalog = []
    for window in (asset_index or {}).get("windows") or []:
        analysis = window.get("analysis") or {}
        if not analysis.get("usable_for_ad"):
            continue
        catalog.append({
            "window_id": window.get("window_id"),
            "product_relevance_prior": "high",
            "product_relevance_source": "curated_local_asset_folder",
            "source_video": window.get("source_video"),
            "source_path": window.get("source_path"),
            "start": window.get("start"),
            "end": window.get("end"),
            "confidence": analysis.get("confidence") or 0.0,
            "narrative_roles": analysis.get("narrative_roles") or [],
            "action_phase": analysis.get("action_phase"),
            "visible_subjects": analysis.get("visible_subjects") or [],
            "setting": analysis.get("setting"),
            "literal_actions": analysis.get("literal_actions") or [],
            "temporal_events": analysis.get("temporal_events") or [],
            "visible_objects": analysis.get("visible_objects") or [],
            "object_tracks": analysis.get("object_tracks") or [],
            "visible_text": analysis.get("visible_text") or [],
            "product_story_role": analysis.get("product_story_role") or "unknown",
            "relation_candidates": analysis.get("relation_candidates") or [],
            "relation_confidence": analysis.get("relation_confidence") or 0.0,
            "relation_evidence": analysis.get("relation_evidence") or "",
            "product_visibility": analysis.get("product_visibility"),
            "evidence": analysis.get("evidence"),
            "motion": {
                key: value
                for key, value in (window.get("motion") or {}).items()
                if key != "samples"
            },
            "motion_consistency": window.get("motion_consistency") or {},
            "frame_quality": {
                key: value
                for key, value in (window.get("frame_quality") or {}).items()
                if key != "samples"
            },
            "frame_evidence": {
                "schema_version": (window.get("frame_evidence") or {}).get("schema_version"),
                "candidate_count": (window.get("frame_evidence") or {}).get("candidate_count", 0),
                "kept_count": (window.get("frame_evidence") or {}).get("kept_count", 0),
                "selected_timestamps": [
                    candidate.get("timestamp")
                    for candidate in (window.get("frame_evidence") or {}).get("candidates") or []
                    if candidate.get("kept")
                ],
                "selection_reasons": [
                    candidate.get("selection_reason")
                    for candidate in (window.get("frame_evidence") or {}).get("candidates") or []
                    if candidate.get("kept")
                ],
            },
        })
    return catalog


def _annotate_product_relationships(catalog: List[Dict[str, Any]], product_info: Dict[str, Any]) -> None:
    """Join visual role candidates to trusted product facts without inventing relationships."""
    normalized_name = str(product_info.get("name") or "").strip()
    role_fact_paths = {
        "ingredient": CLAIM_FACT_PATHS["ingredient"],
        "origin": CLAIM_FACT_PATHS["origin"],
        "production": CLAIM_FACT_PATHS["production"],
        "usage": ("usage", "usage_scenarios"),
        "result": ("verified_claims",),
    }
    for item in catalog:
        evidence = " ".join([
            " ".join(str(value) for value in item.get("visible_text") or []),
            " ".join(str(value) for value in item.get("visible_objects") or []),
            str(item.get("evidence") or ""),
        ])
        item["product_identity_supported"] = bool(
            len(normalized_name) >= 2 and normalized_name in evidence
        )
        item["product_identity_evidence"] = (
            normalized_name if item["product_identity_supported"] else ""
        )
        role = str(item.get("product_story_role") or "unknown")
        candidates = [str(value) for value in item.get("relation_candidates") or []]
        facts = _fact_values(product_info, role_fact_paths.get(role, ()))
        matched_facts = [fact for fact in facts if any(_text_matches_facts(candidate, [fact]) for candidate in candidates)]
        direct_product = item["product_identity_supported"] and role == "finished_product"
        item["product_relationship_verified"] = bool(direct_product or matched_facts)
        item["product_relationship_source"] = (
            "visual.product_identity" if direct_product
            else f"product_info.{role_fact_paths[role][0]}" if matched_facts and role in role_fact_paths
            else ""
        )
        item["matched_product_facts"] = matched_facts


def _material_attention_score(item: Dict[str, Any]) -> float:
    motion = item.get("motion") or {}
    motion_class = {"static": 0.08, "semi_dynamic": 0.55, "dynamic": 1.0}.get(
        str(motion.get("motion_class") or "static"), 0.08,
    )
    return min(1.0, (
        0.52 * motion_class
        + 0.18 * min(1.0, float(motion.get("camera_speed") or 0.0) / 0.04)
        + 0.20 * min(1.0, float(motion.get("subject_motion_ratio") or 0.0) / 0.16)
        + 0.10 * min(1.0, float(motion.get("temporal_change") or 0.0) / 0.12)
    ))


def _story_transition_score(previous_role: str, current_role: str) -> float:
    if previous_role == current_role:
        return -0.20
    forward_pairs = {
        ("origin", "ingredient"), ("ingredient", "production"),
        ("production", "finished_product"), ("finished_product", "usage"),
        ("usage", "result"), ("result", "finished_product"),
    }
    reveal_pairs = {
        ("finished_product", "ingredient"), ("finished_product", "origin"),
        ("usage", "ingredient"), ("context", "finished_product"),
    }
    if (previous_role, current_role) in forward_pairs:
        return 0.16
    if (previous_role, current_role) in reveal_pairs:
        return 0.08
    return 0.02


def _optimize_material_story_sequence(
    material_catalog: List[Dict[str, Any]],
    num_segments: int,
    external_cta: bool,
) -> List[Dict[str, Any]]:
    """Beam-search a footage sequence from measured attention, evidence and narrative flow."""
    candidates = [item for item in material_catalog if item.get("window_id")]
    if len(candidates) < num_segments:
        raise LocalAssetError(f"可用本地素材不足以组成 {num_segments} 段互不冲突的证据化叙事")

    if len(candidates) > 24:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in candidates:
            grouped.setdefault(str(item.get("product_story_role") or "unknown"), []).append(item)
        candidates = []
        for role_items in grouped.values():
            role_items.sort(
                key=lambda item: (
                    _material_attention_score(item),
                    float(item.get("confidence") or 0.0),
                    float(item.get("product_visibility") or 0.0),
                ),
                reverse=True,
            )
            candidates.extend(role_items[:3])

    identity_candidates = [item for item in candidates if item.get("product_identity_supported")]
    beam: List[Tuple[float, List[Dict[str, Any]]]] = [(0.0, [])]
    for position in range(num_segments):
        next_beam: List[Tuple[float, List[Dict[str, Any]]]] = []
        for total_score, selected in beam:
            selected_roles = {
                str(item.get("product_story_role") or "unknown")
                for item in selected
            }
            for item in candidates:
                if any(
                    str(item.get("window_id")) == str(chosen.get("window_id"))
                    or _overlap_ratio(item, chosen) > 0.30
                    for chosen in selected
                ):
                    continue
                is_last = position == num_segments - 1
                if is_last and not external_cta and identity_candidates and not item.get("product_identity_supported"):
                    continue

                role = str(item.get("product_story_role") or "unknown")
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.75)))
                frame_quality = item.get("frame_quality") or {}
                readability = max(0.0, min(1.0, float(frame_quality.get("readable_ratio") or 0.75)))
                motion = item.get("motion") or {}
                stability = max(0.0, min(1.0, float(motion.get("stability") or 0.75)))
                visibility = max(0.0, min(1.0, float(item.get("product_visibility") or 0.0) / 5.0))
                attention = _material_attention_score(item)
                identity = float(bool(item.get("product_identity_supported")))
                verified = float(bool(
                    item.get("product_relationship_verified") and item.get("matched_product_facts")
                ))
                score = 0.18 * confidence + 0.12 * readability + 0.08 * stability
                if position == 0:
                    score += 0.62 * attention + 0.12 * visibility
                elif is_last:
                    score += (
                        0.42 * identity + 0.18 * visibility + 0.14 * stability
                        + 0.10 * attention + (0.12 if role not in selected_roles else 0.0)
                    )
                else:
                    score += (
                        0.18 * attention + 0.22 * verified + 0.10 * visibility
                        + (0.28 if role not in selected_roles else -0.18)
                        + (0.12 if role in {"ingredient", "origin", "production"} else 0.0)
                    )
                if selected:
                    score += _story_transition_score(
                        str(selected[-1].get("product_story_role") or "unknown"),
                        role,
                    )
                next_beam.append((total_score + score, [*selected, item]))

        if not next_beam:
            raise LocalAssetError(f"可用本地素材不足以组成 {num_segments} 段互不冲突的证据化叙事")
        next_beam.sort(
            key=lambda state: (
                state[0],
                tuple(str(item.get("window_id")) for item in state[1]),
            ),
            reverse=True,
        )
        beam = next_beam[:48]

    valid = [
        state for state in beam
        if not identity_candidates or any(item.get("product_identity_supported") for item in state[1])
    ]
    return (valid or beam)[0][1]


def _build_coverage_driven_narrative_plan(
    material_catalog: List[Dict[str, Any]],
    num_segments: int,
    external_cta: bool,
) -> List[Dict[str, Any]]:
    """Dynamically derive a sales arc from the measured local-footage sequence."""
    if num_segments <= 0:
        return []
    sequence = _optimize_material_story_sequence(material_catalog, num_segments, external_cta)
    plan = []
    for index, material in enumerate(sequence):
        role = str(material.get("product_story_role") or "unknown")
        verified = bool(
            material.get("product_relationship_verified")
            and material.get("matched_product_facts")
        )
        if index == 0:
            narrative = "hook" if num_segments > 1 or external_cta else "hook_cta"
            intent = "hook" if num_segments > 1 or external_cta else "cta"
            copy_goal = "根据该镜头真实动作或视觉反差建立好奇心，不预设成品、原料或产地开场"
        elif index == num_segments - 1:
            narrative = "value_closer" if external_cta else "cta"
            intent = "value" if external_cta else "cta"
            copy_goal = (
                "根据最终镜头补充最后一个购买理由，CTA 留给独立尾卡"
                if external_cta else
                "结合最终镜头自然邀请了解或购买，不虚构优惠和入口"
            )
        elif role in {"ingredient", "origin", "production"} and verified:
            narrative = f"{role}_proof"
            intent = "proof"
            copy_goal = "只表达该镜头与可信资料共同支持的事实证据"
        elif role in {"ingredient", "origin", "production", "context"}:
            narrative = "source_context"
            intent = "value"
            copy_goal = (
                "将当前产品相关的原料、产地或生产情境转译成消费者能理解的购买理由；"
                "不得虚构具体地名、配方比例、品质结论或功效"
            )
        elif role == "usage":
            narrative = "usage_demo"
            intent = "value"
            copy_goal = "结合真实使用动作说明产品如何进入日常场景"
        elif role == "result":
            narrative = "result"
            intent = "value"
            copy_goal = "只表达画面可见结果，不扩展成功效承诺"
        else:
            narrative = "product_showcase"
            intent = "value"
            copy_goal = "把清晰可见的产品信息翻译成新的购买理由"
        plan.append({
            "segment": index,
            "narrative": narrative,
            "marketing_intent": intent,
            "copy_goal": copy_goal,
            "product_story_role": role,
            "asset_window_ids": [str(material["window_id"])],
            "product_relevance_prior": str(material.get("product_relevance_prior") or "high"),
            "product_relationship_verified": bool(material.get("product_relationship_verified")),
            "matched_product_facts": material.get("matched_product_facts") or [],
            "planning_basis": {
                "attention_score": round(_material_attention_score(material), 3),
                "confidence": float(material.get("confidence") or 0.0),
                "motion_class": str((material.get("motion") or {}).get("motion_class") or "static"),
                "product_visibility": float(material.get("product_visibility") or 0.0),
            },
        })
    return plan


def build_local_asset_story_contract(
    asset_index: Dict[str, Any],
    product_info: Dict[str, Any],
    requested_duration: Optional[float] = None,
    preview: bool = False,
) -> Dict[str, Any]:
    """Derive segment count, bindings and duration exclusively from usable local footage."""
    material_catalog = _material_catalog(asset_index)
    _annotate_product_relationships(material_catalog, product_info)
    if not material_catalog:
        raise LocalAssetError("本地素材没有可用于构建自然时长故事的窗口")

    roles = {
        str(item.get("product_story_role") or "unknown")
        for item in material_catalog
        if str(item.get("product_story_role") or "unknown") not in {"unknown", "context"}
    }
    identity_count = sum(1 for item in material_catalog if item.get("product_identity_supported"))
    desired_segments = 1 if preview else max(3, len(roles) + int(identity_count >= 2 and len(roles) > 1))
    desired_segments = min(7, desired_segments, len(material_catalog))

    narrative_plan = None
    for segment_count in range(desired_segments, 0, -1):
        try:
            narrative_plan = _build_coverage_driven_narrative_plan(
                material_catalog,
                segment_count,
                bool(
                    (product_info.get("reference_ad_profile") or {}).get("cta_text")
                    and (product_info.get("reference_ad_profile") or {}).get("outro_duration")
                ),
            )
            break
        except LocalAssetError:
            continue
    if not narrative_plan:
        raise LocalAssetError("本地素材无法形成互不冲突的自然时长叙事")

    catalog_by_id = {str(item["window_id"]): item for item in material_catalog}
    segment_durations = {}
    selected_windows = []
    for plan_item in narrative_plan:
        window_id = str(plan_item["asset_window_ids"][0])
        material = catalog_by_id[window_id]
        duration = max(0.1, float(material.get("end") or 0.0) - float(material.get("start") or 0.0))
        segment_durations[int(plan_item["segment"])] = round(duration, 3)
        selected_windows.append(window_id)

    natural_main_duration = round(sum(segment_durations.values()), 3)
    requested = float(requested_duration) if requested_duration is not None else None
    return {
        "source": "local_video_understanding",
        "duration_source": "selected_local_asset_windows",
        "recommended_segments": len(narrative_plan),
        "narrative_plan": narrative_plan,
        "selected_window_ids": selected_windows,
        "segment_durations": segment_durations,
        "natural_main_duration": natural_main_duration,
        "requested_duration": requested,
        "requested_duration_applied": bool(
            requested is not None and abs(requested - natural_main_duration) <= 0.25
        ),
    }


def _deduplicated_material_duration(materials: List[Dict[str, Any]]) -> float:
    intervals_by_source: Dict[str, List[Tuple[float, float]]] = {}
    for material in materials:
        source = str(material.get("source_path") or material.get("source_video") or "")
        start = float(material.get("start") or 0.0)
        end = float(material.get("end") or 0.0)
        if source and end > start:
            intervals_by_source.setdefault(source, []).append((start, end))
    duration = 0.0
    for intervals in intervals_by_source.values():
        merged: List[List[float]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        duration += sum(end - start for start, end in merged)
    return duration


def _global_material_capability_pool(
    material_catalog: List[Dict[str, Any]],
    product_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Aggregate what the whole footage set can support without exposing shot bindings."""
    role_groups: Dict[str, Dict[str, Any]] = {}
    aggregate_analysis: Dict[str, Any] = {
        "usable_for_ad": bool(material_catalog),
        "product_relevance_prior": "high",
        "product_relationship_verified": False,
        "product_identity_supported": False,
        "matched_product_facts": [],
        "literal_actions": [],
        "temporal_events": [],
        "visible_objects": [],
        "visible_text": [],
        "relation_candidates": [],
    }

    def append_unique(target: List[Any], values: Any, limit: int = 20) -> None:
        for value in values or []:
            normalized = value if isinstance(value, dict) else str(value).strip()
            if normalized and normalized not in target:
                target.append(normalized)
            if len(target) >= limit:
                break

    role_materials: Dict[str, List[Dict[str, Any]]] = {}
    for material in material_catalog:
        role = str(material.get("product_story_role") or "unknown").lower()
        role_materials.setdefault(role, []).append(material)
        group = role_groups.setdefault(role, {
            "role": role,
            "window_count": 0,
            "capacity_seconds": 0.0,
            "visual_concepts": [],
            "literal_actions": [],
            "visible_text": [],
            "verified_facts": [],
        })
        group["window_count"] += 1
        append_unique(group["visual_concepts"], material.get("visible_objects"), 8)
        append_unique(group["literal_actions"], material.get("literal_actions"), 6)
        append_unique(group["visible_text"], material.get("visible_text"), 6)
        append_unique(group["verified_facts"], material.get("matched_product_facts"), 6)

        aggregate_analysis["product_relationship_verified"] = bool(
            aggregate_analysis["product_relationship_verified"]
            or material.get("product_relationship_verified")
        )
        aggregate_analysis["product_identity_supported"] = bool(
            aggregate_analysis["product_identity_supported"]
            or material.get("product_identity_supported")
        )
        for field, limit in (
            ("matched_product_facts", 20),
            ("literal_actions", 20),
            ("temporal_events", 20),
            ("visible_objects", 20),
            ("visible_text", 20),
            ("relation_candidates", 20),
        ):
            append_unique(aggregate_analysis[field], material.get(field), limit)

    role_capabilities = []
    for role, group in sorted(
        role_groups.items(),
        key=lambda item: (-float(item[1]["capacity_seconds"]), item[0]),
    ):
        role_capabilities.append({
            **group,
            "capacity_seconds": round(
                _deduplicated_material_duration(role_materials.get(role) or []),
                3,
            ),
        })

    evidence_anchors: List[Dict[str, Any]] = []
    product_name = str(product_info.get("name") or "").strip()
    if product_name:
        evidence_anchors.append({
            "id": "product:name",
            "text": product_name,
            "kind": "product_info",
            "usage_scope": "product_identity",
        })
    fact_fields = (
        "ingredients", "raw_materials", "origin", "production_process",
        "specifications", "verified_claims", "price", "pricing", "usage", "usage_scenarios",
    )
    for field in fact_fields:
        for fact_index, fact in enumerate(_fact_values(product_info, (field,))):
            evidence_anchors.append({
                "id": f"product:{field}:{fact_index}",
                "text": fact,
                "kind": "verified_fact",
                "usage_scope": "product_fact",
            })
    return {
        "source": "global_local_video_understanding",
        "total_usable_duration": round(_deduplicated_material_duration(material_catalog), 3),
        "available_story_roles": [role["role"] for role in role_capabilities],
        "role_capabilities": role_capabilities,
        "evidence_anchors": evidence_anchors,
        "aggregate_analysis": aggregate_analysis,
    }


def _semantic_copy_constraints(
    capability_pool: Dict[str, Any],
    num_segments: int,
    segment_durations: Dict[int, float],
    external_cta: bool,
    narrative_plan: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Create sales beats from the material story plan without exposing window IDs."""
    plan_by_index = {
        int(item.get("segment")): item
        for item in narrative_plan or []
        if isinstance(item, dict) and str(item.get("segment", "")).isdigit()
    }
    capabilities_by_role = {
        str(item.get("role") or "").strip().lower(): item
        for item in capability_pool.get("role_capabilities") or []
        if str(item.get("role") or "").strip()
    }
    constraints: Dict[str, Dict[str, Any]] = {}
    for index in range(num_segments):
        plan_item = plan_by_index.get(index) or {}
        is_first = index == 0
        is_last = index == num_segments - 1
        default_intent = "hook" if is_first else "value" if is_last and external_cta else "cta" if is_last else "value"
        intent = str(plan_item.get("marketing_intent") or default_intent).strip().lower()
        if intent not in MARKETING_INTENTS:
            intent = default_intent
        default_goal = (
            "建立具体的消费者悬念或选择冲突，让人愿意继续听"
            if is_first else
            "完成最后一个购买理由并自然承接独立 CTA 尾声"
            if is_last and external_cta else
            "用已经建立的购买理由自然推动一次了解或尝试"
            if is_last else
            "从可信产品事实中选择新信息，把它转成购买理由并推进整篇口播"
        )
        goal = str(plan_item.get("copy_goal") or default_goal).strip()
        role = str(plan_item.get("product_story_role") or "").strip().lower()
        capability = capabilities_by_role.get(role) or {}
        visual_capability = {
            "role": role,
            "visual_concepts": list(capability.get("visual_concepts") or [])[:8],
            "literal_actions": list(capability.get("literal_actions") or [])[:6],
            "visible_text": list(capability.get("visible_text") or [])[:6],
        } if role else {}
        constraints[str(index)] = {
            "segment": index,
            "narrative": str(plan_item.get("narrative") or "").strip(),
            "segment_duration": round(float(segment_durations.get(index) or 0.0), 3),
            "marketing_intent": intent,
            "copy_goal": goal,
            "product_story_role": role,
            "visual_capability": visual_capability,
            "evidence_anchors": [],
            "evidence_scope": "trusted_product_facts_only",
            "relation_boundary": "具体产地原料工艺功效必须来自可信产品资料",
            "claims_rule": "事实主张必须引用可信产品事实",
            "forbidden_without_verified_facts": [],
        }
    return constraints


def _build_compact_script_prompt(
    product_info: Dict[str, Any],
    script_reference_profile: Dict[str, Any],
    external_cta: bool,
    script_style: str,
    copy_constraints: Dict[str, Dict[str, Any]],
    material_capability_pool: Optional[Dict[str, Any]] = None,
    required_cta_text: str = "",
    outro_duration: float = 0.0,
    user_script_policy: Optional[Dict[str, Any]] = None,
    narration_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask the LLM only for creative copy; all visual state already belongs to the asset index."""
    trusted_product_info = {
        key: product_info.get(key)
        for key in (
            "name", "type", "ingredients", "raw_materials", "origin",
            "production_process", "specifications", "verified_claims", "price", "pricing",
            "usage", "usage_scenarios",
        )
        if product_info.get(key)
    }
    segment_contracts = []
    forbidden_terms: List[str] = []
    for key in sorted(copy_constraints, key=int):
        constraint = copy_constraints[key]
        forbidden_terms.extend(constraint.get("forbidden_without_verified_facts") or [])
        anchors = list(constraint.get("evidence_anchors") or [])
        ranked_anchors = sorted(
            anchors,
            key=lambda item: (
                {"product_info": 0, "verified_fact": 1, "material_capability": 2, "visual": 3}.get(
                    str(item.get("kind")), 4,
                ),
                int(str(item.get("id", "0")).rsplit(":", 1)[-1])
                if str(item.get("id", "")).rsplit(":", 1)[-1].isdigit() else 0,
            ),
        )[:6]
        segment_contract = {
            field: constraint.get(field)
            for field in (
                "segment", "marketing_intent", "copy_goal", "evidence_anchors",
                "claims_rule", "evidence_scope", "relation_boundary",
                "narrative", "product_story_role", "visual_capability",
            )
            if constraint.get(field) not in (None, "", [], {})
        }
        segment_contract["evidence_anchors"] = ranked_anchors
        segment_contracts.append(segment_contract)
    outro_contract = None
    if external_cta:
        outro_contract = {
            "required_text": str(required_cta_text or "").strip(),
            "duration": round(float(outro_duration), 3),
            "enforced_length": False,
            "suggested_min_voiceover_units": max(
                4,
                math.ceil(float(outro_duration) * MIN_OUTRO_VOICEOVER_UNITS_PER_SECOND),
            ),
            "suggested_max_voiceover_units": max(
                4,
                math.floor(float(outro_duration) * MAX_OUTRO_VOICEOVER_UNITS_PER_SECOND),
            ),
            "rule": (
                "outro_cue 必须自然承接主口播并逐字包含 required_text；"
                "长度字段只作非阻断性精炼建议"
            ),
        }
    main_duration = sum(
        float(constraint.get("segment_duration") or 0.0)
        for constraint in copy_constraints.values()
    )
    total_duration = main_duration + max(0.0, float(outro_duration))
    narration_contract = narration_contract or {}
    physical_duration = float(narration_contract.get("physical_duration") or total_duration)
    maximum_units = int(narration_contract.get("maximum_voiceover_units") or 0)
    narration_guidance = {
        "enforced": maximum_units > 0,
        "duration_source": (
            "deduplicated_local_asset_capacity"
            if maximum_units > 0 else
            "estimated_text_load_only"
        ),
        "duration": round(physical_duration, 3),
        "voice": str(narration_contract.get("voice") or ""),
        "maximum_voiceover_units": maximum_units,
        "suggested_min_voiceover_units": math.ceil(
            physical_duration * MIN_VOICEOVER_UNITS_PER_SECOND
        ) if maximum_units <= 0 else 0,
        "suggested_max_voiceover_units": maximum_units or math.floor(
            physical_duration * MAX_OUTRO_VOICEOVER_UNITS_PER_SECOND
        ),
        "rule": (
            "每个候选的全部 cue 与 outro_cue 合计不得超过 maximum_voiceover_units；"
            "该上限来自去重后的可剪辑素材净时长和当前 TTS 音色自然语速"
            if maximum_units > 0 else
            "仅用于控制初稿冗余，不参与候选淘汰或生成失败；"
            "真实单条 TTS 时长与可用素材容量决定最终时间轴"
        ),
    }
    narration_guidance["suggested_target_voiceover_units"] = round(
        (
            narration_guidance["suggested_min_voiceover_units"]
            + narration_guidance["suggested_max_voiceover_units"]
        ) / 2
    )
    creative_blueprint = _build_viral_creative_blueprint(script_reference_profile, script_style)
    user_script_policy = user_script_policy or {
        "source": "explicit_user_feedback_only",
        "rules": [],
        "positive_examples": [],
        "negative_examples": [],
    }
    active_rules = [
        rule for rule in user_script_policy.get("rules") or []
        if rule.get("status") == "active"
    ]

    def compact_feedback_example(example: Dict[str, Any]) -> Dict[str, Any]:
        script = example.get("script") or {}
        voiceover = str(
            script.get("voiceover_full")
            or "".join(script.get("voiceover_cues") or [])
        ).strip()
        return {
            "rule_id": str(example.get("rule_id") or ""),
            "user_comment": str(example.get("user_comment") or "").strip(),
            "voiceover_full": voiceover,
        }

    return {
        "task": "基于素材角色、可信事实、参考片机制和使用者反馈创作连续带货口播",
        "copywriting_brief": {
            "material_role": "素材决定时间轴和每段视觉角色；可见情境提供销售语境，具体事实只取可信产品资料",
            "transformation": "在既定素材角色下写购买理由，不解说镜头",
            "reference_usage": "只迁移句式节奏和销售推进，不复制实体或事实",
            "information_density": "每个 cue 只推进一个新信息，不复述镜头对象，不用过场填充句",
            "narrative_voice": "像真人直接向消费者种草，不使用导演解说、看图说话或参观导览口吻",
            "fact_usage": "只把 trusted_product_info 和对应 evidence_anchors 当作事实来源",
        },
        "rules": [
            "每段返回一个语义节拍和 cue，按 segment 顺序拼接为一条连续口播",
            "各 cue 只作语义锚点，不按镜头限字；narration_guidance 仅为非阻断性精炼建议",
            "只引用全局 evidence_anchors id，不得虚构事实或感官",
            "product_story_role 由素材理解确定，不得改写",
            "cue 只推进购买理由；visual_query 只能取本段 visual_capability，不得含素材 ID 或文件名",
            "cue 可跨段成句，标点供单次 TTS 断句",
            "返回三个不同候选；差异方向由素材、script_style、参考片观察和使用者规则决定",
            (
                "outro_cue 是同一篇完整口播的结尾，必须遵守 outro_contract；不能生成第二条口播"
                if external_cta else
                "最后一个 cue 仍属于同一条连续口播"
            ),
        ],
        "trusted_product_info": trusted_product_info,
        "copy_evidence_anchors": (material_capability_pool or {}).get("evidence_anchors") or [],
        "video_timeline_contract": {
            "source": (material_capability_pool or {}).get("source"),
            "total_usable_duration": (material_capability_pool or {}).get("total_usable_duration"),
            "semantic_beat_count": len(segment_contracts),
        },
        "visual_capabilities": (material_capability_pool or {}).get("role_capabilities") or [],
        "viral_creative_blueprint": creative_blueprint,
        "user_script_quality_policy": {
            "source": "explicit_user_feedback_only",
            "active_rules": active_rules,
            "positive_examples": [
                compact_feedback_example(example)
                for example in user_script_policy.get("positive_examples") or []
                if example.get("rule_status") == "active"
            ],
            "negative_examples": [
                compact_feedback_example(example)
                for example in user_script_policy.get("negative_examples") or []
                if example.get("rule_status") == "active"
            ],
            "cold_start": not bool(user_script_policy.get("rules")),
            "rule": (
                "只有 active_rules 是已建立的使用者规则；positive/negative_examples 保留原始反馈语境。"
                "不得自行增加审核原则。cold_start=true 时没有主观质量规则。"
            ),
        },
        "script_style": script_style,
        "forbidden_without_verified_facts": list(dict.fromkeys(forbidden_terms)),
        "narration_guidance": narration_guidance,
        "segment_contracts": segment_contracts,
        "outro_contract": outro_contract,
        "json_shape": {"creative_candidates": [{
            "route": "candidate direction name",
            "segments": [{
                    "segment": 0,
                "marketing_intent": "hook or proof or value or cta",
                "cue": "connected spoken copy grounded in referenced evidence",
                "evidence_refs": ["allowed evidence id"],
                "claims": [],
                "visual_query": ["items from this segment visual_capability"],
            }],
            **({"outro_cue": "connected CTA ending containing required_text"} if external_cta else {}),
        }]},
    }


def _punctuate_voiceover_cue(cue: Any) -> str:
    text = re.sub(r"\s+", " ", str(cue or "")).strip()
    if not text or re.search(r"[，。！？；,.!?;~～]$", text):
        return text
    return text + ("？" if re.search(r"(?:吗|么|为什么|怎么|什么|哪|谁)$", text) else "。")


def _normalize_compact_script_response(
    result: Any,
    num_segments: int,
    required_cta_text: str = "",
    creative_blueprint: Optional[Dict[str, Any]] = None,
    segment_contracts: Optional[List[Dict[str, Any]]] = None,
    require_creative_candidates: bool = False,
    candidate_validator: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    require_valid_candidate: bool = False,
    maximum_voiceover_units: int = 0,
    user_script_policy: Optional[Dict[str, Any]] = None,
) -> bool:
    if isinstance(result, dict) and isinstance(result.get("creative_candidates"), list):
        evaluated = []
        for candidate in result["creative_candidates"]:
            if not isinstance(candidate, dict):
                continue
            normalized = dict(candidate)
            if not _normalize_compact_script_response(
                normalized,
                num_segments,
                required_cta_text=required_cta_text,
            ):
                continue
            contract_violations = candidate_validator(normalized) if candidate_validator else []
            narration_units = _speech_units(
                "".join(normalized.get("voiceover_cues") or [])
                + str(normalized.get("voiceover_outro_cue") or "")
            )
            if maximum_voiceover_units > 0 and narration_units > maximum_voiceover_units:
                contract_violations.append(
                    f"口播 {narration_units} 单位超过物理容量 {maximum_voiceover_units} 单位"
                )
            preference_score = candidate_preference_score(normalized, user_script_policy or {})
            evaluated.append((preference_score, normalized, contract_violations))
        if not evaluated:
            return False
        contract_valid = [
            item for item in evaluated
            if not item[2]
        ]
        if require_valid_candidate and not contract_valid:
            return False
        if contract_valid:
            _, selected, selected_contract_violations = max(
                contract_valid, key=lambda item: item[0],
            )
        else:
            _, selected, selected_contract_violations = min(
                evaluated,
                key=lambda item: (
                    len(item[2]),
                    -item[0],
                ),
            )
        summaries = [
            {
                "route": str(candidate.get("route") or ""),
                "user_preference_score": score,
                "passed": not candidate_contract_violations,
                "factual_violations": candidate_contract_violations,
            }
            for score, candidate, candidate_contract_violations in evaluated
        ]
        result.clear()
        result.update(selected)
        result["subjective_quality"] = {
            "enforced": False,
            "source": "explicit_user_feedback_only",
            "active_rules": [
                rule for rule in (user_script_policy or {}).get("rules") or []
                if rule.get("status") == "active"
            ],
            "user_preference_score": candidate_preference_score(selected, user_script_policy or {}),
        }
        result["candidate_factual_violations"] = selected_contract_violations
        result["creative_candidates_evaluated"] = summaries
        return True
    if require_creative_candidates:
        return False
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        return False
    by_index = {
        int(item.get("segment")): item
        for item in result["segments"]
        if isinstance(item, dict) and str(item.get("segment", "")).isdigit()
    }
    if set(by_index) != set(range(num_segments)):
        return False
    cues = [_punctuate_voiceover_cue(by_index[index].get("cue")) for index in range(num_segments)]
    if any(not cue for cue in cues):
        top_level_cues = result.get("voiceover_cues")
        if isinstance(top_level_cues, list) and len(top_level_cues) == num_segments:
            cues = [_punctuate_voiceover_cue(cue) for cue in top_level_cues]
    if any(not cue for cue in cues):
        return False
    result["segments"] = [by_index[index] for index in range(num_segments)]
    result["voiceover_cues"] = cues
    required_cta = re.sub(r"\s+", "", str(required_cta_text or ""))
    if required_cta:
        outro_cue = re.sub(r"\s+", " ", str(result.get("outro_cue") or "")).strip()
        if not outro_cue or required_cta not in re.sub(r"\s+", "", outro_cue):
            return False
        result["voiceover_outro_cue"] = outro_cue
    else:
        result["voiceover_outro_cue"] = ""
    return True


def _script_response_token_budget(num_segments: int) -> int:
    return min(3000, max(1000, 520 * max(1, num_segments) + 300))


def _visual_requirement_from_material(material: Dict[str, Any]) -> str:
    parts = [
        str(material.get("setting") or "").strip(),
        "、".join(str(value) for value in (material.get("visible_objects") or [])[:4]),
        "、".join(str(value) for value in (material.get("literal_actions") or [])[:2]),
    ]
    visible_text = [str(value) for value in material.get("visible_text") or [] if str(value).strip()]
    if visible_text:
        parts.append("可见文字" + "、".join(visible_text[:3]))
    return "；".join(part for part in parts if part)


def _materialize_semantic_script_segments(
    result: Dict[str, Any],
    copy_constraints: Dict[str, Dict[str, Any]],
    capability_pool: Dict[str, Any],
    product_info: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Materialize buyer-facing semantic beats without choosing any footage window."""
    copy_by_index = {
        int(item.get("segment")): item
        for item in result.get("segments") or []
        if isinstance(item, dict) and str(item.get("segment", "")).isdigit()
    }
    expected = {int(index) for index in copy_constraints}
    if set(copy_by_index) != expected:
        raise LocalAssetError("本地素材脚本文案没有完整覆盖语义节拍")
    global_analysis = capability_pool.get("aggregate_analysis") or {}
    anchor_by_id = {
        str(anchor.get("id")): anchor
        for anchor in capability_pool.get("evidence_anchors") or []
        if anchor.get("id")
    }
    field_to_role = {
        "ingredients": "ingredient",
        "raw_materials": "ingredient",
        "origin": "origin",
        "production_process": "production",
        "usage": "usage",
        "usage_scenarios": "usage",
    }
    segments: List[Dict[str, Any]] = []
    for index in sorted(expected):
        copy = copy_by_index[index]
        contract = copy_constraints[str(index)]
        referenced_anchors = [
            anchor_by_id[str(reference)]
            for reference in copy.get("evidence_refs") or []
            if str(reference) in anchor_by_id
        ]
        evidence_roles = list(dict.fromkeys(
            field_to_role[field]
            for anchor in referenced_anchors
            for field in [str(anchor.get("id") or "").split(":")[1] if ":" in str(anchor.get("id") or "") else ""]
            if field in field_to_role
        ))
        contract_role = str(contract.get("product_story_role") or "").strip().lower()
        requested_role = str(copy.get("desired_story_role") or "").strip().lower()
        available_roles = set(capability_pool.get("available_story_roles") or [])
        desired_role = contract_role or (evidence_roles[0] if len(evidence_roles) == 1 else "")
        visual_story_role = (
            contract_role
            if contract_role in available_roles
            else requested_role if requested_role in available_roles else ""
        )
        contract_intent = str(contract.get("marketing_intent") or "").strip().lower()
        marketing_intent = str(
            contract_intent or copy.get("marketing_intent") or "value"
        ).strip().lower()
        if marketing_intent not in MARKETING_INTENTS:
            marketing_intent = "value"
        derived_narrative = (
            "hook" if marketing_intent == "hook" else
            "cta" if marketing_intent == "cta" else
            "usage_demo" if desired_role == "usage" else
            "result" if desired_role == "result" else
            f"{desired_role}_proof" if marketing_intent == "proof" and desired_role in {"ingredient", "origin", "production"} else
            "product_showcase" if desired_role == "finished_product" else
            "value"
        )
        narrative = str(contract.get("narrative") or derived_narrative).strip() or derived_narrative
        evidence_query = list(dict.fromkeys([
            str(anchor.get("text") or "").strip()
            for anchor in referenced_anchors
            if str(anchor.get("text") or "").strip()
        ]))[:6]
        visual_query = copy.get("visual_query") or []
        if isinstance(visual_query, str):
            visual_query = [visual_query]
        visual_query = list(dict.fromkeys(
            str(value).strip()
            for value in visual_query
            if str(value).strip()
        ))[:6]
        visual_capability = contract.get("visual_capability") or {}
        allowed_visual_queries = list(dict.fromkeys(
            str(value).strip()
            for field in ("visual_concepts", "literal_actions", "visible_text")
            for value in visual_capability.get(field) or []
            if str(value).strip()
        ))[:6]
        if contract_role:
            visual_query = [value for value in visual_query if value in allowed_visual_queries]
            if not visual_query:
                visual_query = allowed_visual_queries
        segment = {
            "segment": index,
            "narrative": narrative,
            "product_story_role": desired_role,
            "desired_product_story_role": desired_role,
            "visual_story_role": visual_story_role,
            "marketing_intent": marketing_intent,
            "marketing_device": copy.get("marketing_device"),
            "buyer_value": copy.get("buyer_value"),
            "evidence_refs": copy.get("evidence_refs") or [],
            "continuity_from": index - 1 if index > 0 else None,
            "claims": copy.get("claims") or [],
            "visual_requirement": "",
            "scene_prompt": "",
            "visual_query": visual_query,
            "asset_query": evidence_query,
            "fallback_visual": "",
        }
        segment["claims"] = _normalize_claim_objects(segment, product_info, global_analysis)
        segments.append(segment)
    result["segments"] = segments
    return segments


def build_material_constrained_script(
    product_info: dict,
    coverage: Dict[str, Any],
    num_segments: int,
    script_style: str,
    asset_index: Optional[Dict[str, Any]] = None,
    segment_durations: Optional[Dict[int, float]] = None,
    narrative_plan_override: Optional[List[Dict[str, Any]]] = None,
    user_script_policy: Optional[Dict[str, Any]] = None,
    narration_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate sales semantics from the whole footage set before any shot is selected."""
    try:
        from config import LLM_ENABLED
        from llm_client import generate_json, LLMJSONError, LLMJSONTruncatedError
    except Exception as exc:
        raise LocalAssetError(f"本地素材脚本生成依赖不可用：{exc}") from exc
    if not LLM_ENABLED:
        raise LocalAssetError("本地素材模式的 LLM 文案生成未启用，无法生成可验证脚本")

    user_script_policy = user_script_policy or ScriptFeedbackStore().build_policy(
        product_category=str(product_info.get("type") or ""),
        script_style=script_style,
    )
    material_catalog = _material_catalog(asset_index)
    _annotate_product_relationships(material_catalog, product_info)
    if not material_catalog:
        raise LocalAssetError("本地素材没有可用于生成销售语义的有效视频窗口")
    for window in (asset_index or {}).get("windows") or []:
        material = next(
            (
                item for item in material_catalog
                if str(item.get("window_id")) == str(window.get("window_id"))
            ),
            None,
        )
        if material is None:
            continue
        analysis = window.setdefault("analysis", {})
        for key in (
            "product_relationship_verified", "product_relationship_source",
            "matched_product_facts", "product_relevance_prior",
        ):
            analysis[key] = material.get(key)

    reference_profile = product_info.get("reference_ad_profile") or {}
    external_cta = bool(reference_profile.get("cta_text") and reference_profile.get("outro_duration"))
    script_reference_profile = _reference_profile_for_main_script(reference_profile)
    capability_pool = _global_material_capability_pool(material_catalog, product_info)
    narration_contract = dict(narration_contract or {})
    transition_duration = max(0.0, float(narration_contract.get("transition_duration") or 0.0))
    physical_duration = max(
        0.5,
        float(capability_pool.get("total_usable_duration") or 0.0)
        - transition_duration * max(0, num_segments - 1),
    )
    max_units_per_second = max(
        0.0,
        float(narration_contract.get("max_units_per_second") or 0.0),
    )
    narration_contract.update({
        "physical_duration": round(physical_duration, 3),
        "maximum_voiceover_units": (
            math.floor(physical_duration * max_units_per_second)
            if max_units_per_second > 0 else 0
        ),
    })
    provided_durations = {
        int(index): max(0.0, float(value or 0.0))
        for index, value in (segment_durations or {}).items()
    }
    if set(provided_durations) != set(range(num_segments)) or any(
        duration <= 0 for duration in provided_durations.values()
    ):
        average_duration = max(
            0.5,
            float(capability_pool.get("total_usable_duration") or 0.0) / max(num_segments, 1),
        )
        provided_durations = {index: round(average_duration, 3) for index in range(num_segments)}
    copy_constraints = _semantic_copy_constraints(
        capability_pool,
        num_segments,
        provided_durations,
        external_cta,
        narrative_plan=narrative_plan_override,
    )
    semantic_narrative_plan = [
        {
            "segment": int(item.get("segment")),
            "narrative": str(item.get("narrative") or ""),
            "marketing_intent": str(item.get("marketing_intent") or ""),
            "copy_goal": str(item.get("copy_goal") or ""),
            "product_story_role": str(item.get("product_story_role") or ""),
        }
        for item in narrative_plan_override or []
        if isinstance(item, dict) and str(item.get("segment", "")).isdigit()
    ]
    checkpoint_path, checkpoint_signature = _script_checkpoint_context(
        asset_index,
        product_info,
        num_segments,
        script_style,
        provided_durations,
        user_policy_fingerprint=str(user_script_policy.get("fingerprint") or ""),
        narration_contract=narration_contract,
        narrative_plan=semantic_narrative_plan,
    )
    system_prompt = """你是短视频带货广告编导。写完整、精炼、有信息推进的销售口播。
视频素材理解已经决定总时长、语义节拍和每段视觉角色；严格遵守每段合同，不要重新规划或解说镜头。
只使用可信产品资料表达具体事实。输出严格 JSON，不要解释。"""
    public_capability_pool = {
        "source": capability_pool["source"],
        "total_usable_duration": capability_pool["total_usable_duration"],
        "role_capabilities": capability_pool.get("role_capabilities") or [],
        "evidence_anchors": capability_pool.get("evidence_anchors") or [],
    }
    prompt = _build_compact_script_prompt(
        product_info=product_info,
        script_reference_profile=script_reference_profile,
        external_cta=external_cta,
        script_style=script_style,
        copy_constraints=copy_constraints,
        material_capability_pool=public_capability_pool,
        required_cta_text=str(reference_profile.get("cta_text") or ""),
        outro_duration=float(reference_profile.get("outro_duration") or 0.0),
        user_script_policy=user_script_policy,
        narration_contract=narration_contract,
    )
    creative_blueprint = dict(prompt["viral_creative_blueprint"])
    prompt_segment_contracts = list(prompt["segment_contracts"])
    result = _load_script_checkpoint(checkpoint_path, checkpoint_signature)
    if result is None:
        attempt_failures: List[Dict[str, Any]] = []
        last_script_response: Any = None
        last_structure_reason = "尚未生成完整结构化脚本"
        for json_attempt in range(1, MAX_INITIAL_SCRIPT_JSON_ATTEMPTS + 1):
            attempt_failure_reason = "上一响应缺少完整的语义 segments JSON"
            attempt_prompt = dict(prompt)
            if json_attempt > 1:
                attempt_prompt["structured_output_retry"] = {
                    "attempt": json_attempt,
                    "reason": last_structure_reason,
                    "instruction": "保持同一创作任务，从头返回完整 json_shape，不续写残片",
                }
            request_bytes = len(json.dumps(attempt_prompt, ensure_ascii=False).encode("utf-8"))
            print(
                f"🗣️  生成全视频单条口播（结构尝试 {json_attempt}/{MAX_INITIAL_SCRIPT_JSON_ATTEMPTS}，"
                f"{request_bytes / 1024:.1f}KB，先文案后选片）...",
                flush=True,
            )
            try:
                result = generate_json(
                    json.dumps(attempt_prompt, ensure_ascii=False),
                    system_prompt=system_prompt,
                    temperature=0.3 if json_attempt == 1 else 0.0,
                    max_tokens=_script_response_token_budget(num_segments),
                    raise_on_parse_error=True,
                )
            except LLMJSONError as exc:
                last_structure_reason = (
                    "上一响应因输出 token 上限被截断"
                    if isinstance(exc, LLMJSONTruncatedError) else
                    "上一响应 JSON 结构损坏"
                )
                last_script_response = {
                    "parse_error": type(exc).__name__,
                    "finish_reason": exc.finish_reason,
                    "raw_output": exc.raw_text,
                }
                attempt_failures.append({
                    "attempt": json_attempt,
                    "structured_valid": False,
                    "parse_error": type(exc).__name__,
                    "finish_reason": exc.finish_reason,
                    "truncated": isinstance(exc, LLMJSONTruncatedError),
                })
                continue
            last_script_response = result
            if result is None:
                raise LocalAssetError(
                    "本地素材脚本生成服务不可用：请求超时或未返回结果，已停止重复调用"
                )
            structured_valid = _normalize_compact_script_response(
                result,
                num_segments,
                required_cta_text=(str(reference_profile.get("cta_text") or "") if external_cta else ""),
                creative_blueprint=creative_blueprint,
                segment_contracts=prompt_segment_contracts,
                require_creative_candidates=True,
                require_valid_candidate=(
                    int(narration_contract.get("maximum_voiceover_units") or 0) > 0
                ),
                maximum_voiceover_units=int(
                    narration_contract.get("maximum_voiceover_units") or 0
                ),
                user_script_policy=user_script_policy,
            )
            maximum_voiceover_units = int(
                narration_contract.get("maximum_voiceover_units") or 0
            )
            if structured_valid and maximum_voiceover_units > 0:
                narration_units = _speech_units(
                    "".join(result.get("voiceover_cues") or [])
                    + str(result.get("voiceover_outro_cue") or "")
                )
                if narration_units > maximum_voiceover_units:
                    structured_valid = False
                    attempt_failure_reason = (
                        f"上一响应口播 {narration_units} 单位超过当前音色与素材净时长容量 "
                        f"{maximum_voiceover_units} 单位"
                    )
            if not structured_valid:
                last_structure_reason = attempt_failure_reason
                attempt_failures.append({
                    "attempt": json_attempt,
                    "structured_valid": False,
                    "reason": attempt_failure_reason,
                })
                continue
            result["json_generation_attempt"] = json_attempt
            break
        else:
            failure_result = {
                "terminal_failure": {
                    "reason": "invalid_structured_script_output",
                    "json_attempts": MAX_INITIAL_SCRIPT_JSON_ATTEMPTS,
                    "details": "所有响应均未形成有效的完整语义 segments JSON",
                    "attempt_failures": attempt_failures,
                },
                "material_capability_pool": public_capability_pool,
                "last_script_response": last_script_response,
            }
            _write_script_checkpoint(
                checkpoint_path,
                checkpoint_signature,
                failure_result,
                status="failed",
            )
            raise LocalAssetError("所有响应均未形成有效的完整 segments JSON")

    segments = _materialize_semantic_script_segments(
        result,
        copy_constraints,
        capability_pool,
        product_info,
    )
    materialize_continuous_voiceover_contract(result)
    validate_continuous_voiceover_contract(result, segments)
    result["segments"] = segments
    result["narrative_plan"] = [
        {
            "segment": int(segment["segment"]),
            "narrative": segment.get("narrative"),
            "marketing_intent": segment.get("marketing_intent"),
            "desired_product_story_role": segment.get("desired_product_story_role"),
            "asset_query": segment.get("asset_query") or [],
        }
        for segment in segments
    ]
    result["material_capability_pool"] = public_capability_pool
    result["generation_order"] = "material_story_plan_then_sales_copy_then_same_role_shot_matching"
    result["material_story_plan_applied"] = bool(narrative_plan_override)
    result.setdefault("title", f"{product_info.get('name', '产品')}真实体验")
    result.setdefault("hashtags", ["#好物推荐", "#真实体验"])
    result["generated_by"] = "local_asset_global_semantic_llm"
    result["user_script_quality_policy"] = user_script_policy
    _write_script_checkpoint(checkpoint_path, checkpoint_signature, result, status="complete")
    return result


def _segment_queries(segment: Dict[str, Any]) -> List[str]:
    queries = segment.get("asset_query") or []
    if isinstance(queries, str):
        queries = [queries]
    visual_queries = segment.get("visual_query") or []
    if isinstance(visual_queries, str):
        visual_queries = [visual_queries]
    values = [
        segment.get("narrative", ""),
        *visual_queries,
        segment.get("visual_requirement", ""),
        segment.get("scene_prompt", ""),
        *queries,
    ]
    return [str(v).lower() for v in values if v]


def _role_score(segment: Dict[str, Any], analysis: Dict[str, Any]) -> float:
    narrative = str(segment.get("narrative", "")).lower()
    roles = {str(r).lower() for r in analysis.get("narrative_roles") or []}
    required_story_role = _desired_product_story_role(segment)
    actual_story_role = str(analysis.get("product_story_role") or "unknown").lower()
    phase = str(analysis.get("action_phase", "")).lower()
    subjects = {str(s).lower() for s in analysis.get("visible_subjects") or []}

    score = 0.0
    if required_story_role and required_story_role == actual_story_role:
        score += 0.35
    elif (
        required_story_role not in {"ingredient", "origin", "production"}
        and str(analysis.get("product_relevance_prior") or "").lower() == "high"
    ):
        score += 0.32
    if narrative in roles:
        score += 0.35
    if narrative == "hook" and roles & {"hook", "pain_point", "result", "product_showcase"}:
        score += 0.22
    if narrative == "pain_point" and (phase == "setup" or "pain_point" in roles):
        score += 0.24
    if narrative == "usage_demo" and (phase == "action" or "hands" in subjects):
        score += 0.28
    if narrative == "result" and (phase == "outcome" or "result" in roles):
        score += 0.26
    if narrative in {"product_showcase", "cta"} and "product" in subjects:
        score += 0.24
    return min(score, 0.45)


def _keyword_score(segment: Dict[str, Any], analysis: Dict[str, Any]) -> float:
    from material_copy_optimizer import semantic_overlap

    evidence = [
        str(analysis.get("shot_type", "")),
        str(analysis.get("action_phase", "")),
        str(analysis.get("motion_level", "")),
        str(analysis.get("camera_scale", "")),
        str(analysis.get("emotion", "")),
        *(str(x) for x in analysis.get("visible_subjects") or []),
        str(analysis.get("setting", "")),
        *(str(x) for x in analysis.get("literal_actions") or []),
        *(str(x) for x in analysis.get("visible_objects") or []),
        *(str(x) for x in analysis.get("visible_text") or []),
        *(str(x) for x in analysis.get("narrative_roles") or []),
        str(analysis.get("product_story_role", "")),
        *(str(x) for x in analysis.get("relation_candidates") or []),
        str(analysis.get("evidence", "")),
    ]
    queries = _segment_queries(segment)
    if not queries or not evidence:
        return 0.0
    return min(
        0.15,
        max(semantic_overlap(query, value) for query in queries for value in evidence) * 0.15,
    )


def _visual_intent_alignment(segment: Dict[str, Any], analysis: Dict[str, Any]) -> float:
    from material_copy_optimizer import semantic_overlap

    queries = segment.get("visual_query") or []
    if isinstance(queries, str):
        queries = [queries]
    evidence = [
        *(str(value) for value in analysis.get("literal_actions") or []),
        *(str(value) for value in analysis.get("visible_objects") or []),
        *(str(value) for value in analysis.get("visible_text") or []),
    ]
    if not queries or not evidence:
        return 0.0
    return max(
        semantic_overlap(query, value)
        for query in queries
        for value in evidence
    )


def _motion_score(segment: Dict[str, Any], window: Dict[str, Any]) -> Tuple[float, Optional[str]]:
    """Score dynamic evidence only when the script contains an actual temporal action."""
    action_claims = _temporal_action_claims(segment)
    if not action_claims:
        return 0.05, None
    unsupported_actions = _unsupported_action_claims(segment, window.get("analysis") or {})
    if unsupported_actions:
        return 0.0, f"时序事件不支持动作类别：{','.join(unsupported_actions)}"
    motion = window.get("motion") or {}
    if motion.get("subject_motion") not in {"medium", "high"}:
        return 0.0, f"动作 {','.join(action_claims)} 缺少连续帧主体运动证据"
    confidence = float(motion.get("confidence") or 0.0)
    active_ranges = motion.get("active_ranges") or []
    if confidence < 0.65 or not active_ranges:
        return 0.0, f"动作 {','.join(action_claims)} 的动态证据置信度不足"
    temporal_events = (window.get("analysis") or {}).get("temporal_events") or []
    event_bonus = 0.04 if temporal_events else 0.0
    return min(0.14, 0.08 + confidence * 0.04 + event_bonus), None


def _overlap_ratio(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    if a.get("source_path") != b.get("source_path"):
        return 0.0
    start = max(float(a.get("start") or 0.0), float(b.get("start") or 0.0))
    end = min(float(a.get("end") or 0.0), float(b.get("end") or 0.0))
    overlap = max(0.0, end - start)
    shorter = min(
        float(a.get("end") or 0.0) - float(a.get("start") or 0.0),
        float(b.get("end") or 0.0) - float(b.get("start") or 0.0),
    )
    return overlap / max(shorter, 0.001)


def _unselected_window_slices(
    window: Dict[str, Any],
    selected: List[Dict[str, Any]],
    min_duration: float = 0.5,
) -> List[Dict[str, Any]]:
    """Return source ranges that remain after subtracting already selected footage."""
    start = float(window.get("start") or 0.0)
    end = float(window.get("end") or 0.0)
    intervals = [(start, end)] if end > start else []
    for previous in selected:
        if previous.get("source_path") != window.get("source_path"):
            continue
        previous_start = float(previous.get("start") or 0.0)
        previous_end = float(previous.get("end") or 0.0)
        remaining = []
        for interval_start, interval_end in intervals:
            if previous_end <= interval_start or previous_start >= interval_end:
                remaining.append((interval_start, interval_end))
                continue
            if previous_start - interval_start >= min_duration:
                remaining.append((interval_start, previous_start))
            if interval_end - previous_end >= min_duration:
                remaining.append((previous_end, interval_end))
        intervals = remaining
    return [
        {
            **window,
            "start": interval_start,
            "end": interval_end,
            "duration": round(interval_end - interval_start, 3),
        }
        for interval_start, interval_end in intervals
        if interval_end - interval_start >= min_duration
    ]


def _score_window(
    segment: Dict[str, Any],
    window: Dict[str, Any],
    selected: List[Dict[str, Any]],
    allow_replan: bool = False,
) -> Tuple[float, Dict[str, float]]:
    analysis = window.get("analysis") or {}
    bound_ids = segment.get("asset_window_ids") or []
    if isinstance(bound_ids, str):
        bound_ids = [bound_ids]
    if not allow_replan and bound_ids and str(window.get("window_id")) not in {str(value) for value in bound_ids}:
        return 0.0, {"asset_binding_mismatch": 1.0}
    if not _story_role_supported(segment, analysis):
        return 0.0, {"product_story_role_mismatch": 1.0}
    if not analysis.get("usable_for_ad"):
        return 0.0, {"unusable": 1.0}
    if any(_overlap_ratio(window, prev) > 0.30 for prev in selected):
        return 0.0, {"overlap": 1.0}

    confidence = float(analysis.get("confidence") or 0)
    product_visibility = float(analysis.get("product_visibility") or 0) / 5.0
    role = _role_score(segment, analysis)
    keyword = _keyword_score(segment, analysis)
    visual_intent_alignment = _visual_intent_alignment(segment, analysis)
    product = 0.12 * product_visibility
    motion, motion_reject = _motion_score(segment, window)
    if motion_reject:
        return 0.0, {"motion_evidence_missing": 1.0}

    order_bonus = 0.0
    same_source_prev = [p for p in selected if p.get("source_path") == window.get("source_path")]
    if same_source_prev:
        last = same_source_prev[-1]
        if float(window["start"]) >= float(last["end"]) - 0.1:
            order_bonus = 0.08
        else:
            order_bonus = -0.10

    repetition_penalty = min(0.12, len(same_source_prev) * 0.03)
    score = (
        0.38 * confidence
        + role
        + keyword
        + product
        + motion
        + order_bonus
        - repetition_penalty
    )
    details = {
        "confidence": confidence,
        "role": role,
        "keyword": keyword,
        "visual_intent_alignment": visual_intent_alignment,
        "product": product,
        "motion": motion,
        "order_bonus": order_bonus,
        "repetition_penalty": repetition_penalty,
    }
    return round(max(0.0, min(score, 1.0)), 3), details


def _hard_segment_ok(segment: Dict[str, Any], window: Dict[str, Any], score: float) -> Tuple[bool, str]:
    narrative = str(segment.get("narrative", "")).lower()
    analysis = window.get("analysis") or {}
    confidence = float(analysis.get("confidence") or 0)
    product_visibility = int(analysis.get("product_visibility") or 0)
    story_role = str(analysis.get("product_story_role") or "unknown")
    if score < MIN_MATCH_SCORE:
        return False, f"匹配分 {score:.2f} 低于阈值 {MIN_MATCH_SCORE:.2f}"
    if narrative in {"usage_demo", "result"} and confidence < MIN_KEY_SEGMENT_CONFIDENCE:
        return False, f"{narrative} 置信度 {confidence:.2f} 低于阈值 {MIN_KEY_SEGMENT_CONFIDENCE:.2f}"
    if (
        narrative in {"product_showcase", "usage_demo", "result", "cta"}
        and story_role in {"finished_product", "usage", "result"}
        and product_visibility < 3
    ):
        return False, f"{narrative} 产品可见度 {product_visibility}/5 低于阈值 3/5"
    _, motion_reject = _motion_score(segment, window)
    if motion_reject:
        return False, motion_reject
    consistency = window.get("motion_consistency") or {}
    if consistency and not consistency.get("passed", False):
        return False, "静态视觉理解与动态计算证据冲突"
    return True, ""


def _narrative_role_affinity(segment: Dict[str, Any], analysis: Dict[str, Any]) -> float:
    """Measure how well a visual role serves the current narrative stage."""
    required_role = _desired_product_story_role(segment)
    actual_role = str(analysis.get("product_story_role") or "unknown").strip().lower()
    if required_role:
        return 1.0 if required_role == actual_role else 0.0

    narrative = str(segment.get("narrative") or "").strip().lower()
    intent = str(segment.get("marketing_intent") or "").strip().lower()
    if narrative == "cta" or intent == "cta":
        stage_affinity = {
            "finished_product": 1.0,
            "usage": 0.9,
            "result": 0.9,
            "context": 0.35,
            "unknown": 0.35,
            "ingredient": 0.15,
            "origin": 0.15,
            "production": 0.15,
        }.get(actual_role, 0.2)
    elif narrative == "hook" or intent == "hook":
        stage_affinity = {
            "finished_product": 1.0,
            "usage": 0.9,
            "result": 0.9,
            "ingredient": 0.65,
            "origin": 0.65,
            "production": 0.65,
            "context": 0.55,
            "unknown": 0.5,
        }.get(actual_role, 0.5)
    else:
        stage_affinity = 0.8 if actual_role in PRODUCT_STORY_ROLES - {"unknown"} else 0.55

    preferred_role = str(segment.get("visual_story_role") or "").strip().lower()
    if not preferred_role:
        return stage_affinity
    product_roles = {"finished_product", "usage", "result"}
    source_roles = {"ingredient", "origin", "production"}
    preferred_affinity = (
        1.0
        if preferred_role == actual_role else
        0.7
        if preferred_role in product_roles and actual_role in product_roles else
        0.55
        if preferred_role in source_roles and actual_role in source_roles else
        0.25
    )
    return 0.55 * stage_affinity + 0.45 * preferred_affinity


def _write_gap_report(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from quality_gate import record_failure_case
        failed_segment = payload.get("failed_segment") or {}
        record_failure_case(
            failure_type="local_asset_semantic_mismatch",
            failure_reason=str(payload.get("reason") or "local asset planning failed"),
            segment_index=failed_segment.get("segment"),
            narrative_type=str(failed_segment.get("narrative") or ""),
            quality_score=float(payload.get("plan_score") or 0.0),
            extra={
                "negative_sample": True,
                "gap_report": str(path),
                "unsupported_claims": payload.get("unsupported_claims") or [],
                "top_candidates": payload.get("top_candidates") or [],
            },
        )
    except Exception:
        pass


def _materialize_clip(source: Path, start: float, end: float, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(end - start, 0.5)
    width, height = OUTPUT_RESOLUTION.split("x")
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-r", str(OUTPUT_FPS),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an",
        "-movflags", "+faststart",
        str(output),
    ]
    _run(cmd, timeout=180)
    if not output.exists() or output.stat().st_size < 1000:
        raise LocalAssetError(f"本地素材裁剪失败：{output}")
    return output


def _planning_windows_with_source_capacity(asset_index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Restore the tiny source tail reserved by frame analysis for physical editing."""
    windows = copy.deepcopy(asset_index.get("windows") or [])
    source_durations = {
        str(source.get("path") or ""): float(source.get("duration") or 0.0)
        for source in asset_index.get("sources") or []
        if source.get("path") and float(source.get("duration") or 0.0) > 0.0
    }
    last_window_end: Dict[str, float] = {}
    for window in windows:
        source_path = str(window.get("source_path") or "")
        if source_path in source_durations:
            last_window_end[source_path] = max(
                last_window_end.get(source_path, 0.0),
                float(window.get("end") or 0.0),
            )

    for window in windows:
        source_path = str(window.get("source_path") or "")
        source_duration = source_durations.get(source_path)
        final_analysis_end = last_window_end.get(source_path)
        window_end = float(window.get("end") or 0.0)
        if source_duration is None or final_analysis_end is None:
            continue
        tail_inset = source_duration - final_analysis_end
        if abs(window_end - final_analysis_end) <= 0.001 and 0.0 < tail_inset <= 0.1:
            window["end"] = source_duration
            window["duration"] = round(
                source_duration - float(window.get("start") or 0.0),
                3,
            )
    return windows


def plan_and_materialize_local_clips(
    asset_index: Dict[str, Any],
    ad_script: Dict[str, Any],
    rhythm_template: Dict[str, Any],
    clips_dir: Path,
    final_dir: Path,
    output_name: str,
    product_info: Optional[Dict[str, Any]] = None,
    plan_only: bool = False,
    record_failure: bool = True,
) -> Dict[str, Any]:
    clips_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    segments = copy.deepcopy(ad_script.get("segments") or [])
    planning_asset_index = copy.deepcopy(asset_index)
    _apply_product_relationships_to_windows(planning_asset_index, product_info or {})
    windows = _planning_windows_with_source_capacity(planning_asset_index)
    selected: List[Dict[str, Any]] = []
    selected_segments: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    if not segments:
        raise LocalAssetError("素材约束脚本没有可用段落")

    def compatible_capacity(segment: Dict[str, Any]) -> float:
        capacity = 0.0
        for window in windows:
            analysis = window.get("analysis") or {}
            candidate_visual = _visual_requirement_from_material({
                "setting": analysis.get("setting"),
                "visible_objects": analysis.get("visible_objects") or [],
                "literal_actions": analysis.get("literal_actions") or [],
                "visible_text": analysis.get("visible_text") or [],
            })
            candidate_segment = {
                **segment,
                "subtitle": "",
                "voiceover": "",
                "claims": [],
            }
            score, _ = _score_window(
                candidate_segment,
                window,
                [],
                allow_replan=True,
            )
            semantic_check = _validate_derived_material_segment(
                candidate_segment,
                window,
                product_info or {},
            )
            hard_ok, _ = _hard_segment_ok(candidate_segment, window, score)
            if semantic_check.get("supported") and hard_ok:
                capacity += _narrative_role_affinity(segment, analysis) * max(
                    0.0,
                    float(window.get("end") or 0.0) - float(window.get("start") or 0.0),
                )
        return capacity

    planning_segments = sorted(
        enumerate(segments),
        key=lambda item: (
            compatible_capacity(item[1]),
            int(item[1].get("segment", item[0])),
        ),
    )

    planned_by_semantic: Dict[int, List[Dict[str, Any]]] = {}
    for semantic_position, segment in planning_segments:
        seg_idx = int(segment.get("segment", semantic_position))
        target_duration = float(
            next(
                (
                    item.get("duration")
                    for item in rhythm_template.get("segments") or []
                    if int(item.get("index", -1)) == seg_idx
                ),
                0.0,
            ) or 0.0
        )
        if target_duration <= 0:
            raise LocalAssetError(f"脚本段 {seg_idx} 缺少有效的素材目标时长")
        remaining_duration = target_duration
        segment_window_ids: List[str] = []
        first_visual = ""

        while remaining_duration > 0.08:
            ranked: List[
                Tuple[float, float, Dict[str, Any], Dict[str, float], str]
            ] = []
            for base_window in windows:
                for window in _unselected_window_slices(base_window, selected):
                    analysis = window.get("analysis") or {}
                    candidate_visual = _visual_requirement_from_material({
                        "setting": analysis.get("setting"),
                        "visible_objects": analysis.get("visible_objects") or [],
                        "literal_actions": analysis.get("literal_actions") or [],
                        "visible_text": analysis.get("visible_text") or [],
                    })
                    candidate_segment = {
                        **segment,
                        "subtitle": "",
                        "voiceover": "",
                        "claims": [],
                    }
                    score, details = _score_window(
                        candidate_segment,
                        window,
                        selected,
                        allow_replan=True,
                    )
                    semantic_check = _validate_derived_material_segment(
                        candidate_segment,
                        window,
                        product_info or {},
                    )
                    if not semantic_check.get("supported"):
                        rejected.append({
                            "semantic_segment": seg_idx,
                            "source_video": window.get("source_video"),
                            "source_start": window.get("start"),
                            "source_end": window.get("end"),
                            "reject_reason": str(
                                semantic_check.get("reason") or "素材证据不支持当前口播"
                            ),
                        })
                        continue
                    ok, reason = _hard_segment_ok(candidate_segment, window, score)
                    if ok:
                        affinity = _narrative_role_affinity(segment, analysis)
                        ranked.append((score, affinity, window, details, candidate_visual))
                    elif score > 0:
                        rejected.append({
                            "semantic_segment": seg_idx,
                            "narrative": segment.get("narrative"),
                            "source_video": window.get("source_video"),
                            "source_start": window.get("start"),
                            "source_end": window.get("end"),
                            "score": score,
                            "reject_reason": reason,
                        })
            ranked.sort(
                key=lambda item: (
                    item[0]
                    + 0.25 * item[1]
                    + 0.35 * item[3].get("visual_intent_alignment", 0.0),
                    min(
                        remaining_duration,
                        float(item[2]["end"]) - float(item[2]["start"]),
                    ),
                ),
                reverse=True,
            )
            if not ranked:
                covered_duration = target_duration - remaining_duration
                gap_path = final_dir / f"{output_name}_local_asset_gap_report.json"
                gap_payload = {
                    "reason": "semantic_segment_material_capacity_gap",
                    "failed_segment": segment,
                    "required_duration": round(target_duration, 3),
                    "covered_duration": round(covered_duration, 3),
                    "missing_duration": round(remaining_duration, 3),
                    "top_candidates": rejected[-10:],
                    "coverage": asset_index.get("coverage"),
                }
                if record_failure:
                    _write_gap_report(gap_path, gap_payload)
                raise LocalAssetError(
                    f"本地素材无法完整覆盖脚本段 {seg_idx}：需要 {target_duration:.2f}s，"
                    f"已匹配 {covered_duration:.2f}s"
                    + (f"，报告已保存：{gap_path}" if record_failure else ""),
                    details=gap_payload,
                )

            score, affinity, window, details, projected_visual = ranked[0]
            details = {**details, "narrative_role_affinity": round(affinity, 3)}
            available_duration = float(window["end"]) - float(window["start"])
            allocated_duration = min(remaining_duration, available_duration)
            selected_window = {
                **window,
                "end": float(window["start"]) + allocated_duration,
                "duration": round(allocated_duration, 3),
            }
            edit_index = len(selected)
            selected.append(selected_window)
            segment_window_ids.append(str(window.get("window_id")))
            first_visual = first_visual or projected_visual
            clip_output = clips_dir / f"_planned_{edit_index + 1:02d}_{output_name}_local.mp4"
            if not plan_only:
                _materialize_clip(
                    source=Path(selected_window["source_path"]),
                    start=float(selected_window["start"]),
                    end=float(selected_window["end"]),
                    output=clip_output,
                )
            selected_segments.append({
                "edit_index": edit_index,
                "semantic_segment": seg_idx,
                "script_segment": seg_idx,
                "narrative": segment.get("narrative"),
                "product_story_role": str(
                    (selected_window.get("analysis") or {}).get("product_story_role")
                    or "unknown"
                ),
                "subtitle": segment.get("subtitle"),
                "voiceover": segment.get("voiceover"),
                "source_video": selected_window.get("source_video"),
                "source_path": selected_window.get("source_path"),
                "source_start": selected_window.get("start"),
                "source_end": selected_window.get("end"),
                "target_duration": round(allocated_duration, 3),
                "clip_path": str(clip_output),
                "match_score": score,
                "score_details": details,
                "analysis": selected_window.get("analysis"),
                "motion": {
                    key: value
                    for key, value in (selected_window.get("motion") or {}).items()
                    if key != "samples"
                },
                "frame_quality": {
                    key: value
                    for key, value in (selected_window.get("frame_quality") or {}).items()
                    if key != "samples"
                },
                "selection_reason": (
                    selected_window.get("analysis") or {}
                ).get("evidence", ""),
            })
            planned_by_semantic.setdefault(seg_idx, []).append(selected_segments.pop())
            remaining_duration -= allocated_duration

        segment["visual_requirement"] = first_visual
        segment["scene_prompt"] = first_visual
        segment["asset_window_ids"] = list(dict.fromkeys(segment_window_ids))
        matched_roles = list(dict.fromkeys(
            str((item.get("analysis") or {}).get("product_story_role") or "unknown")
            for item in planned_by_semantic.get(seg_idx, [])
        ))
        segment["matched_product_story_roles"] = matched_roles
        segment["product_story_role"] = _desired_product_story_role(segment)

    selected_segments = []
    for semantic_position, segment in enumerate(segments):
        semantic_segment = int(segment.get("segment", semantic_position))
        for item in planned_by_semantic.get(semantic_segment, []):
            edit_index = len(selected_segments)
            old_path = Path(item["clip_path"])
            new_path = clips_dir / f"clip_{edit_index + 1:02d}_{output_name}_local.mp4"
            if not plan_only and old_path != new_path:
                old_path.replace(new_path)
            item["edit_index"] = edit_index
            item["clip_path"] = str(new_path)
            selected_segments.append(item)

    plan_score = sum(item["match_score"] for item in selected_segments) / max(len(selected_segments), 1)
    if plan_only:
        return {
            "selected_segments": selected_segments,
            "bound_segments": segments,
            "plan_score": round(plan_score, 4),
            "semantic_indices": [int(item["semantic_segment"]) for item in selected_segments],
        }
    creative_profile = build_local_asset_creative_profile({"windows": selected})
    creative_profile["source"] = "selected_local_assets"
    final_evidence_manifest = final_dir / f"{output_name}_frame_evidence.json"
    final_evidence_report = final_dir / f"{output_name}_frame_evidence_report.html"
    cached_manifest = Path(str(asset_index.get("frame_evidence_manifest") or ""))
    cached_report = Path(str(asset_index.get("frame_evidence_report") or ""))
    if cached_manifest.is_file() and cached_report.is_file():
        shutil.copy2(cached_manifest, final_evidence_manifest)
        shutil.copy2(cached_report, final_evidence_report)
    else:
        write_frame_evidence_artifacts(
            windows,
            final_evidence_manifest,
            final_evidence_report,
        )
    report_path = final_dir / f"{output_name}_edit_decision_report.json"
    report = {
        "mode": "local_assets",
        "asset_folder": asset_index.get("asset_folder"),
        "coverage": asset_index.get("coverage"),
        "plan_score": round(plan_score, 3),
        "script_strategy": ad_script.get("generated_by", "local_asset"),
        "creative_profile": creative_profile,
        "frame_evidence_manifest": str(final_evidence_manifest),
        "frame_evidence_report": str(final_evidence_report),
        "selected_segments": selected_segments,
        "bound_segments": segments,
        "rejected_candidates": rejected[:80],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if plan_score < MIN_PLAN_SCORE:
        gap_path = final_dir / f"{output_name}_local_asset_gap_report.json"
        _write_gap_report(gap_path, {
            "reason": "plan_low_confidence",
            "plan_score": plan_score,
            "threshold": MIN_PLAN_SCORE,
            "edit_decision_report": str(report_path),
        })
        raise LocalAssetError(f"本地素材整体规划分 {plan_score:.2f} 低于阈值，报告已保存：{gap_path}")

    return {
        "clip_paths": [Path(item["clip_path"]) for item in selected_segments],
        "selected_indices": [int(item["edit_index"]) for item in selected_segments],
        "edit_indices": [int(item["edit_index"]) for item in selected_segments],
        "semantic_indices": [int(item["semantic_segment"]) for item in selected_segments],
        "edit_decision_report": report_path,
        "plan_score": plan_score,
        "creative_profile": creative_profile,
        "frame_evidence_manifest": final_evidence_manifest,
        "frame_evidence_report": final_evidence_report,
        "selected_segments": selected_segments,
        "bound_segments": segments,
    }
