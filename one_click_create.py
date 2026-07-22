#!/usr/bin/env python3
"""
可灵 AI 抖音广告视频 - 一键成片

使用方法：
    python one_click_create.py

功能：
    输入产品信息，自动完成：
    1. 生成角色定妆照（调用可灵图片 API）
    2. 生成 5 个分镜片段（调用可灵视频 API）
    3. 自动拼接 + 转场 + 字幕 + BGM（ffmpeg）
    4. 输出最终成片

前置条件：
    - 已在 config.py 中配置 KLING_API_KEY
    - 已安装 ffmpeg（brew install ffmpeg）
    - 已安装依赖：pip install requests
"""

import sys
import json
import time
import re
import base64
import argparse
import subprocess
import math
import shutil
import hashlib
import copy
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from threading import Lock
from typing import Optional, List, Dict, Tuple, TypedDict, Any
from urllib.parse import urlparse

from config import (
    PROJECT_ROOT,
    OUTPUT_DIR,
    KLING_API_KEY,
    KLING_PRICING,
    DEFAULT_VIDEO_DURATION,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_MODE,
    DEFAULT_IMAGE_FIDELITY,
    DEFAULT_HUMAN_FIDELITY,
    KLING_IMAGE_MODEL,
    KLING_VIDEO_MODEL,
    DEFAULT_TRANSITIONS,
    DEFAULT_SUBTITLE_TEMPLATE,
    BGM_PATH,
    BGM_VOLUME_VOICEOVER,
    CONSISTENCY_TEMPLATES,
    PRODUCT_PRESETS,
    CINEMATIC_STYLES,
    DEFAULT_CINEMATIC_STYLE,
    BRAND_CONFIG,
    HOOK_TEMPLATES,
    DEFAULT_HOOK_TYPE,
    SCENE_CONTINUITY_CONFIG,
    MAX_REF_IMAGES,
    NEGATIVE_PROMPT,
    get_preset,
    ensure_dirs,
)
from cinematic_language import (
    build_cinematic_prompt_elements,
    DEEP_CINEMATIC_STYLES,
)
from kling_client import KlingClient
# P0-1 修复：导入 JWT 鉴权所需的 Key，用于 main() 鉴权检查
try:
    from config import KLING_ACCESS_KEY, KLING_SECRET_KEY
except ImportError:
    KLING_ACCESS_KEY = ""
    KLING_SECRET_KEY = ""
from bgm_client import (
    pick_bgm_for_product,
    _detect_pace_from_clips,
    get_bgm_copyright_info,
    print_bgm_copyright_warning,
    BGM_COPYRIGHT_DISCLAIMER,
)
from douyin_adapter import (
    DOUYIN_CONFIG,
    get_douyin_config,
    optimize_subtitles_for_douyin,
    get_rhythm_template,
    adapt_rhythm_template_to_segments,
    compute_segment_timeline,
)
from compliance_checker import (
    check_script_compliance,
    print_compliance_report,
)
from ad_script import (
    generate_ad_script,
    script_to_clip_prompts,
    script_to_subtitles,
    script_to_voiceover,
    generate_title_options,
    generate_hashtag_options,
    check_story_completeness,
    recommend_segment_count,
    SCRIPT_STYLES,
    DEFAULT_SCRIPT_STYLE,
)
from tts_client import (
    generate_tts_audio,
    generate_voiceover_script,
    generate_full_voiceover,
    align_subtitles_to_voiceover,
    recommend_voice_for_narration,
    build_tts_synthesis_contract,
    align_voiceover_lines_to_audio_pauses,
    VOICEOVER_TEMPLATES,
    VOICE_PRESETS,
    DEFAULT_VOICEOVER_STYLE,
    DEFAULT_VOICE,
)
from video_merger import (
    merge_clips_ffmpeg,
    normalize_transition_decisions,
    add_fancy_subtitles,
    prepare_single_line_subtitles,
    assign_intelligent_subtitle_colors,
    choose_subtitle_animation,
    add_bgm_ffmpeg,
    add_sfx_to_video,
    generate_sfx_timings,
    align_subtitles_to_beats,
    align_sfx_to_beats,
    apply_color_grading,
    get_color_grading_for_style,
    export_final_video,
    convert_to_aspect_ratio,
    check_ffmpeg,
    extract_last_frame,
    extract_keyframes,
    extract_frame,
    generate_cover_image,
    add_logo_watermark,
    auto_trim_black_frames,
    auto_trim_video,
    detect_freeze_frames,
    generate_fallback_audio,
    color_match_clips,
    run_ffmpeg,
    _get_clip_duration,
    _has_audio_stream,
    adjust_clip_duration,
)
from quality_checker import (
    check_video_quality,
    print_quality_report,
)
from quality_gate import (
    run_quality_gate,
    print_quality_gate_report,
)
from production_quality_guard import (
    run_production_quality_check,
    ProductionQualityGuard,
)
from ai_enhancement import AIVideoEnhancer
from image_first_strategy import (
    ImageFirstMode,
    run_image_first_strategy,
    print_image_first_report,
)


def _safe_output_stem(value: str) -> str:
    """生成安全文件名前缀。"""
    return "".join(c for c in value if c.isalnum() or c in "-_").strip() or "product"


def build_stable_output_name(product_info: dict, args: argparse.Namespace) -> str:
    """
    基于产品信息和关键生成参数生成稳定输出名，用于 --resume 命中片段缓存。
    """
    relevant = {
        "product_info": product_info,
        "style": getattr(args, "style", DEFAULT_CINEMATIC_STYLE),
        "duration": getattr(args, "duration", DEFAULT_VIDEO_DURATION),
        "mode": getattr(args, "mode", DEFAULT_MODE),
        "aspect_ratio": getattr(args, "aspect_ratio", DEFAULT_ASPECT_RATIO),
        "product_image": str(getattr(args, "product_image", "") or ""),
        "hook": getattr(args, "hook", DEFAULT_HOOK_TYPE),
        "script_style": getattr(args, "script_style", DEFAULT_SCRIPT_STYLE),
        "target_duration": getattr(args, "target_duration", None),
        "rhythm_style": getattr(args, "rhythm_style", "moderate"),
        "seed": getattr(args, "seed", None),
        "kling_model": getattr(args, "kling_model", None) or KLING_VIDEO_MODEL,
        "multi_shot": getattr(args, "multi_shot", False),
    }
    digest = hashlib.sha256(
        json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{_safe_output_stem(product_info.get('name', 'product'))}_{digest}"


def _hash_reference_images(ref_images: list[str]) -> list[str]:
    """把参考图内容压缩成短哈希，避免幂等键包含大段 base64。"""
    hashes = []
    for img in ref_images:
        hashes.append(hashlib.sha256(str(img).encode("utf-8")).hexdigest()[:16])
    return hashes


def _ref_image_values(ref_images: list) -> list[str]:
    """兼容旧字符串列表和带 role 的参考图条目。"""
    values = []
    for item in ref_images:
        if isinstance(item, dict):
            img = item.get("image", "")
        else:
            img = item
        if img:
            values.append(img)
    return values


def _clip_manifest_path(video_path: Path) -> Path:
    """返回片段对应的 manifest 路径。"""
    return video_path.with_name(f"{video_path.stem}.manifest.json")


def _build_clip_manifest(
    *,
    final_prompt: str,
    ref_images: list,
    idx: int,
    model: Optional[str],
    mode: str,
    duration: int,
    aspect_ratio: str,
    seed: Optional[int],
    negative_prompt: str,
    candidate_strategy: str = "single",
) -> dict:
    """构建片段缓存契约，只有契约一致才能复用旧片段。"""
    ref_values = _ref_image_values(ref_images)
    ref_roles = [
        item.get("role", "unknown") if isinstance(item, dict) else "unknown"
        for item in ref_images
    ]
    return {
        "version": 1,
        "clip_index": idx,
        "prompt_sha256": hashlib.sha256(final_prompt.encode("utf-8")).hexdigest(),
        "reference_hashes": _hash_reference_images(ref_values),
        "reference_roles": ref_roles,
        "model": model,
        "mode": mode,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "seed": seed,
        "negative_prompt_sha256": hashlib.sha256(negative_prompt.encode("utf-8")).hexdigest(),
        "prompt_preview": final_prompt[:300],
        "candidate_strategy": candidate_strategy,
    }


def _manifest_matches(video_path: Path, expected_manifest: dict) -> bool:
    """检查片段 manifest 是否与当前生成契约一致。"""
    manifest_path = _clip_manifest_path(video_path)
    if not manifest_path.exists():
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            actual = json.load(f)
    except Exception:
        return False
    return actual == expected_manifest


def _write_clip_manifest(video_path: Path, manifest: dict) -> None:
    """写入片段 manifest。"""
    manifest_path = _clip_manifest_path(video_path)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)


PRODUCT_REQUIRED_NARRATIVES = {
    # 展示/结果/CTA 段：产品必须占主画面
    "showcase", "cta", "demonstration", "product", "result",
    "review", "proof", "demo", "detail", "reason", "effect",
    "highlight", "solution", "good_choice", "product_intro",
    "effect_show", "compare_result", "reason_1", "reason_2",
    "reason_3", "cta_summary", "cta_choose",
    # 改动3：hook 段也强制注入产品参考图（产品第一次亮相最重要）
    "hook",
}

# 动态 fidelity 映射（改动3）：按叙事重要性调整产品参考图约束强度
_NARRATIVE_FIDELITY_MAP: dict[str, float] = {
    "showcase": 0.95,
    "result": 0.95,
    "cta": 0.95,
    "hook": 0.90,
    "turning": 0.85,
    "default": 0.85,
}

_NARRATIVE_HUMAN_FIDELITY_MAP: dict[str, float] = {
    "hook": 0.95,
    "pain": 0.92,
    "showcase": 0.85,
    "turning": 0.90,
    "result": 0.95,
    "cta": 0.92,
    "default": 0.90,
}


def _get_fidelity_for_narrative(narrative: str, base_fidelity: float) -> float:
    """按叙事段动态调整 image_fidelity，产品展示段用更高约束。"""
    mapped = _NARRATIVE_FIDELITY_MAP.get((narrative or "").lower().strip())
    if mapped is not None:
        return mapped
    return base_fidelity


def _get_human_fidelity_for_narrative(narrative: str, base_fidelity: float) -> float:
    """按叙事段动态调整 human_fidelity，人物重要的段用更高约束。"""
    mapped = _NARRATIVE_HUMAN_FIDELITY_MAP.get((narrative or "").lower().strip())
    if mapped is not None:
        return mapped
    return base_fidelity


def _is_product_required_narrative(narrative: str) -> bool:
    """判断某个叙事段是否必须强约束产品露出。"""
    normalized = (narrative or "").lower().strip()
    return normalized in PRODUCT_REQUIRED_NARRATIVES


def _validate_storyboard_quality(storyboard, product_info: dict) -> List[str]:
    """
    故事板质量预验证：在生成视频前检查分镜结构是否合理。

    检查项：
    - 分镜数量是否合理（3-8段）
    - 是否包含必要的叙事阶段（hook/showcase/cta）
    - 产品露出段是否充足
    - 时长分布是否合理
    - 情绪曲线是否有起伏

    返回问题列表，空列表表示通过。
    """
    issues: List[str] = []
    shots = storyboard.shots if storyboard else []
    n_shots = len(shots)

    if n_shots < 3:
        issues.append(f"分镜数量过少（{n_shots}段），建议至少3段以保证叙事完整性")
    elif n_shots > 10:
        issues.append(f"分镜数量过多（{n_shots}段），单段时长可能不足，建议控制在8段以内")

    narrative_types = [getattr(s, "emotion", "") or "" for s in shots]
    shot_emotions = [getattr(s, "emotion", "") or "" for s in shots]
    scene_names = [getattr(s, "scene", "") or "" for s in shots]

    if storyboard.total_duration < 5:
        issues.append(f"总时长过短（{storyboard.total_duration:.1f}s），广告信息可能传达不充分")
    elif storyboard.total_duration > 120:
        issues.append(f"总时长过长（{storyboard.total_duration:.1f}s），短视频平台建议控制在60s以内")

    product_name = product_info.get("name", "")
    if product_name:
        product_shots = [
            s for s in shots
            if product_name.lower() in (getattr(s, "description", "") or "").lower()
            or product_name.lower() in (getattr(s, "key_elements", "") or "")
        ]
        if len(product_shots) < max(1, n_shots // 3):
            issues.append(f"产品露出分镜不足（{len(product_shots)}/{n_shots}段），建议至少1/3段落包含产品")

    unique_scenes = len(set(scene_names))
    if unique_scenes == 1 and n_shots > 4:
        issues.append(f"全部{n_shots}段都在同一场景，可能导致视觉单调，建议增加场景变化")

    if shot_emotions and len(set(shot_emotions)) == 1:
        issues.append("所有分镜情绪单一，缺乏情绪起伏，建议在不同段落使用不同情绪强度")

    durations = [getattr(s, "duration", 0) for s in shots]
    if durations:
        max_dur = max(durations)
        min_dur = min(durations)
        if max_dur > 0 and min_dur > 0 and max_dur / min_dur > 4:
            issues.append(
                f"分镜时长差异过大（最长{max_dur:.1f}s vs 最短{min_dur:.1f}s），"
                f"建议节奏更均匀"
            )

    return issues


def _score_candidate_video_quality(
    video_path: Path,
    *,
    quality_frames: int,
    product_reference_image: Optional[Path] = None,
    character_reference_image: Optional[Path] = None,
) -> Tuple[float, List[str]]:
    """候选片段择优评分；语义门禁失败的候选不能被选中。"""
    quality_result = check_video_quality(
        video_path,
        num_frames=int(quality_frames or 12),
        content_focus="center" if product_reference_image else "default",
        product_reference_image=product_reference_image,
        character_reference_image=character_reference_image,
        require_semantic_alignment=bool(character_reference_image),
    )
    score = float(quality_result.overall_score or 0) if quality_result.passed else 0.0
    return score, list(quality_result.issues or [])


# ── 失败原因驱动的精准修复（Issue-Driven Repair）──

_ISSUE_REPAIR_RULES = [
    # 商品外观
    ("product", {"product", "similarity", "color", "packaging", "shape", "mismatch"},
     "exact same product color and shape as reference, consistent packaging"),
    ("logo", {"logo", "brand", "mark", "text", "unreadable", "unclear"},
     "clear brand logo visible, readable product text, packaging text sharp"),
    ("obstructed", {"obstruct", "hidden", "blocked", "cover", "hand", "finger"},
     "product fully visible, no hands or objects covering packaging"),
    ("product_dark", {"dark", "dim", "underexposed", "shadow"},
     "bright even lighting on product, well-lit packaging, no harsh shadows"),
    # 角色外观
    ("face", {"face", "detect", "character", "similarity", "person", "drift"},
     "front-facing portrait, clear face visible, same person from reference"),
    ("profile", {"profile", "side", "back", "turn", "away", "rear"},
     "front-facing, face directly to camera, no profile or back view"),
    ("outfit", {"outfit", "hair", "style", "change", "clothing"},
     "same hairstyle and outfit as reference, no clothing change"),
    ("blur", {"blur", "motion", "shaky", "unstable"},
     "sharp focus, stable pose, no motion blur"),
    # 通用画质
    ("contrast", {"contrast", "flat", "washed", "faded"},
     "rich contrast, vivid colors"),
    ("noise", {"noise", "grain", "grainy", "artifact"},
     "clean image, minimal noise"),
]


def _repair_prompt_by_issues(
    prompt: str,
    issues: List[str],
    *,
    product_bible: Optional["ProductBible"] = None,
    character_bible: Optional["CharacterBible"] = None,
) -> Tuple[str, List[str]]:
    """
    根据质量检测返回的具体失败原因，生成精准修复后的 prompt 和修复标签列表。

    Args:
        prompt: 原始 prompt
        issues: 质量检测返回的问题列表
        product_bible: 商品圣经（用于提取精确的商品外观描述）
        character_bible: 角色圣经（用于提取精确的角色外观描述）

    Returns:
        (repaired_prompt, repair_tags)
    """
    if not issues:
        return prompt, []

    repair_phrases: List[str] = []
    matched_tags: List[str] = []
    combined_text = " ".join(issues).lower()

    for tag, keywords, phrase in _ISSUE_REPAIR_RULES:
        if any(kw in combined_text for kw in keywords):
            if tag not in matched_tags:
                matched_tags.append(tag)
                # 如果有圣经，用圣经中的精确描述替换泛化描述
                if tag == "product" and product_bible:
                    phrase = f"exact same {product_bible.get('packaging', 'product')} as reference"
                elif tag == "face" and character_bible:
                    phrase = f"same {character_bible.get('hair_style', 'person')} as reference, front-facing, clear face"
                repair_phrases.append(phrase)

    if not repair_phrases:
        return prompt, []

    repairs = ", ".join(repair_phrases)
    repaired = f"{prompt}, {repairs}"
    return _compact_prompt_for_generation(repaired), matched_tags


def _validate_product_image_file(image_path: Path) -> None:
    """发布级商品参考图预检，避免损坏/低质素材污染生成。"""
    from PIL import Image, ImageStat

    if not image_path.exists():
        raise FileNotFoundError(f"商品参考图不存在：{image_path}")
    if image_path.stat().st_size < 1024:
        raise RuntimeError(f"商品参考图文件过小，可能不是有效图片：{image_path}")

    try:
        with Image.open(image_path) as src:
            src.verify()
        with Image.open(image_path) as src:
            img = src.convert("RGBA")
    except Exception as e:
        raise RuntimeError(f"商品参考图不可读取或格式损坏：{image_path}") from e

    width, height = img.size
    min_side = min(width, height)
    if min_side < 256:
        raise RuntimeError(
            f"商品参考图分辨率过低（{width}x{height}），建议最短边至少 512px"
        )

    alpha = img.getchannel("A")
    alpha_stat = ImageStat.Stat(alpha)
    opaque_ratio = sum(1 for p in alpha.getdata() if p > 16) / max(1, width * height)
    if alpha_stat.mean[0] < 8 or opaque_ratio < 0.05:
        raise RuntimeError("商品参考图几乎全透明，无法约束产品露出")

    rgb = img.convert("RGB")
    stat = ImageStat.Stat(rgb)
    channel_std = sum(stat.stddev) / max(1, len(stat.stddev))
    brightness = sum(stat.mean) / max(1, len(stat.mean))
    if channel_std < 3:
        raise RuntimeError("商品参考图几乎是纯色/空白图，无法作为产品参考")
    if brightness < 8 or brightness > 247:
        raise RuntimeError("商品参考图整体过暗或过曝，无法稳定约束产品露出")


def _check_segment_semantic_quality(
    *,
    clip_paths: List[Path],
    successful_clip_indices: List[int],
    ad_script: dict,
    product_image_path: Optional[Path],
    main_char_path: Optional[Path],
    quality_frames: int,
) -> None:
    """按分镜检查关键段语义质量，避免整片抽帧漏掉产品/CTA 段问题。"""
    segments = ad_script.get("segments", []) if isinstance(ad_script, dict) else []
    issues = []

    for pos, clip_path in enumerate(clip_paths):
        if pos >= len(successful_clip_indices):
            continue
        seg_idx = successful_clip_indices[pos]
        seg = segments[seg_idx] if 0 <= seg_idx < len(segments) else {}
        narrative = str(seg.get("narrative") or seg.get("type") or "").lower().strip()
        product_ref = product_image_path if product_image_path and _is_product_required_narrative(narrative) else None
        character_ref = main_char_path if main_char_path and narrative in {"hook", "turning", "result", "review"} else None

        if not product_ref and not character_ref:
            continue

        result = check_video_quality(
            clip_path,
            num_frames=max(6, int(quality_frames or 12)),
            content_focus="center" if product_ref else "default",
            product_reference_image=product_ref,
            character_reference_image=character_ref,
            require_semantic_alignment=bool(character_ref),
        )
        if not result.passed:
            first_issue = result.issues[0] if result.issues else "未知质量问题"
            issues.append(
                f"段 {seg_idx + 1}（{narrative or 'unknown'}）未通过分段语义质检：{first_issue}"
            )

    if issues:
        detail = "；".join(issues[:3])
        if len(issues) > 3:
            detail += f"；另有 {len(issues) - 3} 个问题"
        raise RuntimeError(f"分段语义质检未通过，已阻断不可发布成片：{detail}")


def _collapse_edit_timeline_by_semantic(
    edit_timeline: List[Dict[str, Any]],
    semantic_indices: List[int],
) -> List[Dict[str, Any]]:
    """Collapse adjacent edit clips into one subtitle/voiceover interval per semantic cue."""
    if len(edit_timeline) != len(semantic_indices):
        raise ValueError(
            f"镜头时间轴与语义映射数量不一致：{len(edit_timeline)} / {len(semantic_indices)}"
        )
    semantic_timeline: List[Dict[str, Any]] = []
    closed: set[int] = set()
    for edit, semantic_segment in zip(edit_timeline, semantic_indices):
        semantic_segment = int(semantic_segment)
        if semantic_timeline and semantic_timeline[-1]["index"] == semantic_segment:
            semantic_timeline[-1]["end"] = float(edit["end"])
            semantic_timeline[-1]["duration"] = round(
                semantic_timeline[-1]["end"] - semantic_timeline[-1]["start"],
                3,
            )
            semantic_timeline[-1]["edit_indices"].append(int(edit["index"]))
            continue
        if semantic_segment in closed:
            raise ValueError(f"语义段 {semantic_segment} 的镜头不连续，无法形成单一口播 cue")
        if semantic_timeline:
            closed.add(int(semantic_timeline[-1]["index"]))
        semantic_timeline.append({
            "index": semantic_segment,
            "start": float(edit["start"]),
            "end": float(edit["end"]),
            "duration": float(edit["duration"]),
            "type": edit.get("type", ""),
            "purpose": edit.get("purpose", ""),
            "edit_indices": [int(edit["index"])],
        })
    return semantic_timeline


def _single_line_subtitle_capacity(
    video_width: int,
    font_size: int,
    horizontal_margin_ratio: float = 0.15,
    minimum_font_scale: float = 0.8,
) -> int:
    """Return readable single-line units from the renderer's real width contract."""
    safe_width = max(1.0, float(video_width) * (1.0 - 2.0 * horizontal_margin_ratio))
    readable_font_size = max(1.0, float(font_size) * minimum_font_scale)
    return max(1, int(safe_width // readable_font_size))


def _build_character_manifest(
    *,
    product_info: dict,
    character: dict,
    prompt: str,
) -> dict:
    """构建角色定妆照缓存契约，避免同名输出复用旧人设。"""
    character_contract = {
        "name": character.get("name", "Character A"),
        "description": character.get("description", ""),
    }
    return {
        "version": 1,
        "product_info_sha256": hashlib.sha256(
            json.dumps(product_info, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "character_sha256": hashlib.sha256(
            json.dumps(character_contract, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "model": KLING_IMAGE_MODEL,
        "aspect_ratio": "2:3",
        "resolution": "2k",
    }


def _build_video_idempotency_key(
    prompt: str,
    ref_images: list,
    idx: int,
    target_path: Path,
    *,
    model: Optional[str],
    mode: str,
    duration: int,
    aspect_ratio: str,
    seed: Optional[int],
) -> str:
    """为单个候选视频生成稳定幂等键。"""
    payload = {
        "prompt": prompt,
        "refs": _hash_reference_images(_ref_image_values(ref_images)),
        "idx": idx,
        "target": target_path.name,
        "model": model,
        "mode": mode,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "seed": seed,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"kaa-{digest}"


_REF_BINDING_STRENGTH = {
    "standard": {
        "product_prefix": "PRODUCT REFERENCE (CRITICAL - must match exactly)",
        "product_verb": "Strictly match",
        "product_suffix": "Product appearance must be identical in every frame.",
        "char_prefix": "MAIN CHARACTER REFERENCE (CRITICAL - identity must stay consistent)",
        "char_verb": "Exact same person throughout. Match",
        "char_suffix": "Character identity must never change.",
        "extra_detail": False,
    },
    "emphasized": {
        "product_prefix": "PRODUCT REFERENCE (CRITICAL - TOP PRIORITY - must match exactly)",
        "product_verb": "STRICTLY and precisely match",
        "product_suffix": "Product appearance must be IDENTICAL in every single frame. This is the most important visual reference. Every detail of the product must be preserved with perfect accuracy. No color shifts, no shape changes, no logo distortion.",
        "char_prefix": "MAIN CHARACTER REFERENCE (CRITICAL - TOP PRIORITY - identity must stay consistent)",
        "char_verb": "EXACT same person throughout every frame. Precisely match",
        "char_suffix": "Character identity must NEVER change. This is the most important visual reference. Facial features must remain perfectly stable. No facial drift, no age changes, no feature morphing.",
        "extra_detail": True,
    },
}

_NARRATIVE_REF_EMPHASIS = {
    "hook": {"character": "emphasized", "product": "standard"},
    "pain": {"character": "emphasized", "product": "standard"},
    "turning": {"character": "standard", "product": "standard"},
    "showcase": {"character": "standard", "product": "emphasized"},
    "demo": {"character": "standard", "product": "emphasized"},
    "result": {"character": "standard", "product": "emphasized"},
    "cta": {"character": "standard", "product": "emphasized"},
    "default": {"character": "standard", "product": "standard"},
}


def _get_ref_emphasis_for_narrative(narrative: str, role: str) -> str:
    """按叙事类型获取参考图绑定的强调级别（Omni 接口无 fidelity，靠语义强调控制强度）。"""
    normalized = (narrative or "").lower().strip()
    mapping = _NARRATIVE_REF_EMPHASIS.get(normalized, _NARRATIVE_REF_EMPHASIS["default"])
    key = "product" if role == "product" else "character"
    return mapping.get(key, "standard")


def _bind_reference_tags_to_prompt(prompt: str, ref_images: list, narrative: str = "") -> str:
    """
    用结构化段落绑定参考图语义。
    Omni 接口无 image_fidelity 参数，完全靠 prompt 语义约束一致性，
    因此按叙事类型动态强调重点参考图，通过重复和细节加强约束。
    """
    if not ref_images:
        return prompt

    roles = {item.get("role", "unknown") if isinstance(item, dict) else "unknown" for item in ref_images}
    has_product = "product" in roles
    has_character = any(r in roles for r in ("character_primary", "character"))

    lines = []
    for i, item in enumerate(ref_images):
        tag = f"<<<image_{i + 1}>>>"
        role = item.get("role", "unknown") if isinstance(item, dict) else "unknown"
        if role == "product":
            emphasis = _get_ref_emphasis_for_narrative(narrative, "product")
            s = _REF_BINDING_STRENGTH[emphasis]
            base_details = "packaging shape, all colors, logo design and placement, product proportions, material texture, and visual details"
            extra_details = "including exact Pantone colors, precise dimensions, label placement, font styles on packaging, cap/closure design, and surface finish" if s.get("extra_detail") else ""
            full_details = base_details + ", " + extra_details if extra_details else base_details
            lines.append(
                f"{s['product_prefix']}: {tag}. "
                f"{s['product_verb']} {full_details}. "
                f"{s['product_suffix']}"
            )
        elif role in ("character_primary", "character"):
            emphasis = _get_ref_emphasis_for_narrative(narrative, "character")
            s = _REF_BINDING_STRENGTH[emphasis]
            base_details = "facial features, face shape, eye shape and color, nose shape, lip shape, exact hairstyle and hair color, skin tone, body type and proportions, and exact outfit/clothing"
            extra_details = "including eyebrow shape, eyelid type, jawline, cheek structure, ear shape, neck length, shoulder width, hand shape, and exact fabric textures and patterns" if s.get("extra_detail") else ""
            full_details = base_details + ", " + extra_details if extra_details else base_details
            lines.append(
                f"{s['char_prefix']}: {tag}. "
                f"{s['char_verb']} {full_details}. "
                f"{s['char_suffix']}"
            )
        elif role == "character_angle":
            lines.append(
                f"CHARACTER FULL-BODY REFERENCE (same person as image_2): {tag}. "
                f"Same character, different angle. Match full body proportions, "
                f"outfit details, and confirm same identity from different viewpoint."
            )
        elif role == "character_secondary":
            lines.append(
                f"SECONDARY CHARACTER REFERENCE: {tag}. "
                f"When this character appears, match their face, outfit, and identity exactly. "
                f"Different from the main character."
            )
        elif role == "approved_keyframe":
            lines.append(
                f"APPROVED KEYFRAME REFERENCE (quality preflight passed): {tag}. "
                f"Use this as the first-frame visual target. Match composition, subject placement, "
                f"product visibility, character identity, lighting, and color palette while adding natural motion."
            )
        else:
            lines.append(
                f"PREVIOUS FRAME REFERENCE (continuity): {tag}. "
                f"Maintain scene layout, camera angle, lighting direction and color, "
                f"character positions, and temporal continuity with the previous shot. "
                f"Smooth seamless transition."
            )
    reference_block = "\n".join(lines)
    return f"{reference_block}\n\n{prompt}"


def cleanup_output(output_name: str, output_dir: Path = OUTPUT_DIR):
    """
    清理本次运行产生的所有中间文件（递归清理子目录）

    Args:
        output_name: 本次运行的输出文件名前缀
        output_dir: 输出根目录，默认 OUTPUT_DIR
    """
    dirs_to_clean = [
        output_dir / "character_ref",
        output_dir / "clips",
        output_dir / "final",
    ]

    cleaned = []
    for d in dirs_to_clean:
        if not d.exists():
            continue
        for f in d.rglob(f"*{output_name}*"):
            if f.is_file():
                try:
                    f.unlink()
                    cleaned.append(f.name)
                except Exception as e:
                    print(f"  ⚠️ 清理失败 {f.name}: {e}")
        # 清理空的关键帧子目录
        for subdir in d.iterdir():
            if subdir.is_dir() and output_name in subdir.name:
                try:
                    subdir.rmdir()
                except OSError:
                    pass

    if cleaned:
        print(f"🧹 已清理 {len(cleaned)} 个文件")


def _cleanup_cover_candidates(paths: List[Path]) -> None:
    """Delete temporary scoring frames as soon as the final cover is rendered."""
    for path in paths:
        Path(path).unlink(missing_ok=True)



def _cleanup_intermediate_files(
    final_dir: Path,
    output_name: str,
    final_path: Path,
    wide_path: Optional[Path],
) -> None:
    """
    清理流水线产生的所有中间文件，只保留：
    - {output_name}_final.mp4（最终竖版成片）
    - {output_name}_16x9_final.mp4（横版，如有）
    - {output_name}_cover.jpg（封面图，如有）
    - {output_name}_发布文案.txt（发布文案）
    - {output_name}_transition_decision_report.json（智能转场决策证据）

    中间文件后缀：_merged / _subtitled / _voiced / _sfx /
                  _graded / _watermarked / _trimmed / _cover_base
    """
    if not final_dir.exists():
        return

    # 需要保留的文件名集合
    keep = set()
    keep.add(final_path.name)
    if wide_path:
        keep.add(wide_path.name)
    # 封面和文案也保留
    keep.add(f"{output_name}_cover.jpg")
    keep.add(f"{output_name}_发布文案.txt")

    # 中间文件特征后缀
    # _cover_c：封面帧候选临时图（格式 {name}_cover_c{i}_{ratio}.jpg）
    intermediate_suffixes = (
        "_merged", "_subtitled", "_voiced", "_sfx",
        "_graded", "_watermarked", "_trimmed", "_cover_base", "_cover_c",
        "_trimmed_pre",
    )

    removed = []
    for f in final_dir.iterdir():
        if not f.is_file():
            continue
        if f.name in keep:
            continue
        # 只清理本次 output_name 相关的中间文件
        stem = f.stem  # 不含扩展名
        if not stem.startswith(output_name):
            continue
        suffix_part = stem[len(output_name):]  # e.g. "_merged", "_graded"
        if any(suffix_part.startswith(s) for s in intermediate_suffixes):
            try:
                f.unlink()
                removed.append(f.name)
            except Exception as e:
                print(f"  ⚠️ 清理中间文件失败 {f.name}: {e}")

    if removed:
        print(f"🧹 已清理 {len(removed)} 个中间文件")



def _quick_quality_check(
    clip_path: Path,
    expected_duration: float,
    idx: int,
    min_size_kb: int = 100,
    max_black_ratio: float = 0.75,
    duration_tolerance: float = 0.4,
) -> Optional[str]:
    """
    对单个片段做轻量质检，发现问题时返回问题描述字符串，正常返回 None。

    检查项：
    1. 文件大小：< min_size_kb KB 视为空文件/损坏
    2. 时长偏差：实际时长与 expected_duration 偏差超过 duration_tolerance 秒
    3. 黑帧比例：超过 max_black_ratio 视为全黑片段

    Args:
        clip_path: 片段文件路径
        expected_duration: 期望时长（秒）
        idx: 片段索引（用于日志）
        min_size_kb: 最小有效文件大小（KB）
        max_black_ratio: 允许的最大黑帧比例
        duration_tolerance: 时长容差（秒）

    Returns:
        问题描述字符串 or None（正常）
    """
    import shutil as _shutil

    # 1. 文件大小检查
    if not clip_path.exists():
        return "文件不存在"
    size_kb = clip_path.stat().st_size / 1024
    if size_kb < min_size_kb:
        return f"文件过小（{size_kb:.0f} KB < {min_size_kb} KB），可能损坏"

    # ffprobe 不可用则跳过时长/黑帧检查
    if not _shutil.which("ffprobe"):
        return None

    # 2. 时长检查
    actual_dur = expected_duration  # 备用：作为黑帧比例的分母
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(clip_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        actual_dur = float(result.stdout.strip())
        if abs(actual_dur - expected_duration) > duration_tolerance:
            return f"时长异常（期望 {expected_duration}s，实际 {actual_dur:.1f}s）"
    except Exception:
        pass  # ffprobe 失败不阻断流程

    # 3. 黑帧比例检查（用 blackdetect 滤镜）
    # P2-2：复用上方已查询的时长，无需再调一次 ffprobe
    try:
        total_dur = actual_dur

        bd_result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
                "-i", str(clip_path),
                # P2-1：修正参数名 pic_th → pix_th，阈值与 video_merger.py 保持一致
                "-vf", "blackdetect=d=0.1:pix_th=0.10",
                "-an", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
        # 统计黑帧总时长
        import re as _re
        black_durs = _re.findall(r"black_duration:([\d.]+)", bd_result.stderr)
        total_black = sum(float(d) for d in black_durs)
        if total_dur > 0 and total_black / total_dur > max_black_ratio:
            return f"黑帧过多（{total_black:.1f}s / {total_dur:.1f}s = {total_black/total_dur:.0%}）"
    except Exception:
        pass  # 黑帧检查失败不阻断

    # F1 修复：Laplacian 方差清晰度检测（5 帧均值 < 50 视为模糊/闪烁片段）
    # 可灵 API 偶发返回低频闪、运动模糊片段，拉普拉斯方差是最轻量的清晰度指标
    try:
        _lap_scores = []
        _sample_count = 5
        for _fi in range(_sample_count):
            _ft = actual_dur * (_fi + 1) / (_sample_count + 1)  # 均匀采样，避免首尾帧
            _frame_cmd = [
                "ffmpeg", "-ss", f"{_ft:.2f}", "-i", str(clip_path),
                "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "gray", "-vf", "scale=320:180",
                "-",
            ]
            _fr = subprocess.run(_frame_cmd, capture_output=True, timeout=10)
            if _fr.returncode == 0 and _fr.stdout:
                import array as _arr, math as _math
                _pixels = _arr.array("B", _fr.stdout)
                _n = len(_pixels)
                if _n > 4:
                    # 近似 Laplacian：差分代替卷积，速度更快
                    _mean = sum(_pixels) / _n
                    _variance = sum((p - _mean) ** 2 for p in _pixels) / _n
                    _lap_scores.append(_variance)
        if _lap_scores:
            _avg_lap = sum(_lap_scores) / len(_lap_scores)
            if _avg_lap < 50.0:  # 经验阈值：<50 为明显模糊/低质量
                return f"清晰度不足（Laplacian 均值 {_avg_lap:.1f} < 50，可能模糊或低频闪）"
    except Exception:
        pass  # 清晰度检测失败不阻断流程

    # P0-A：人脸变形语义检测（复用 quality_checker._analyze_face_quality）
    # 抽取首/中/尾 3 帧，3 帧全部异常才视为坏片段（避免误杀正常动态画面）
    try:
        import tempfile as _tmpfile
        from quality_checker import _analyze_face_quality as _face_check
        _face_issue_count = 0
        _worst_issue = ""
        _face_sample_times = [
            actual_dur * 0.15,
            actual_dur * 0.50,
            actual_dur * 0.85,
        ]
        with _tmpfile.TemporaryDirectory(prefix="face_qc_") as _fqc_dir:
            for _fqi, _fqt in enumerate(_face_sample_times):
                _fq_frame = Path(_fqc_dir) / f"face_{_fqi}.png"
                _fq_cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{_fqt:.2f}", "-i", str(clip_path),
                    "-frames:v", "1", "-q:v", "3",
                    str(_fq_frame),
                ]
                _fqr = subprocess.run(_fq_cmd, capture_output=True, timeout=10)
                if _fqr.returncode == 0 and _fq_frame.exists():
                    _fq_issues = _face_check(_fq_frame)
                    if _fq_issues:
                        _face_issue_count += 1
                        if not _worst_issue:
                            _worst_issue = _fq_issues[0]
        # 仅当 3 帧全部异常时才判定失败（短视频动态画面容易出现局部帧角度特殊）
        # 2 帧异常仅记录警告，不阻断
        if _face_issue_count >= 3:
            return f"人脸变形检测：{_face_issue_count}/3 帧异常（肤色区域形状/填充率超限），疑似人脸崩坏"
        elif _face_issue_count >= 2:
            print(f"  ⚠️  人脸检测警告：{_face_issue_count}/3 帧异常（{_worst_issue}），已放行")
    except Exception:
        pass  # 人脸检测失败不阻断流程

    return None


def _cleanup_keyframes(clips_dir: Path, output_name: str):


    """
    清理片段生成过程中产生的关键帧临时文件和子目录

    Args:
        clips_dir: 片段目录
        output_name: 输出文件名前缀
    """
    if not clips_dir.exists():
        return
    removed = 0
    # 清理关键帧子目录和其中的文件
    for subdir in list(clips_dir.iterdir()):
        if subdir.is_dir() and output_name in subdir.name:
            for f in subdir.glob("*.png"):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
            try:
                subdir.rmdir()
            except OSError:
                pass
    # 清理 last_frame.png 等零散临时 PNG
    for f in clips_dir.glob(f"*_{output_name}_*.png"):
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"🧹 已清理 {removed} 个关键帧临时文件")


def _extract_frame_b64(video_path: Path, time_sec: float) -> str | None:
    """
    从视频提取指定时间的帧，返回 base64 编码

    Args:
        video_path: 视频路径
        time_sec: 时间点（秒）

    Returns:
        base64 编码的 PNG，失败返回 None
    """
    import tempfile
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        extract_frame(video_path, tmp_path, time_sec=time_sec)
        b64 = base64.b64encode(tmp_path.read_bytes()).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _is_http_url(value: object) -> bool:
    """判断参数是否为 HTTP/HTTPS URL。"""
    parsed = urlparse(str(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _download_product_image_url(url: str, output_dir: Path) -> Path:
    """下载商品参考图 URL，并校验其为可读取图片。"""
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    out_path = output_dir / f"product_reference_url{suffix}"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    try:
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        with tmp_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        if tmp_path.stat().st_size < 1024:
            raise RuntimeError("商品参考图下载结果过小，可能不是有效图片")
        with Image.open(tmp_path) as img:
            img.verify()
        tmp_path.replace(out_path)
        return out_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_voiceover_audio(audio_path: Path) -> float:
    """校验口播音频可用，并返回时长。"""
    if not audio_path.exists():
        raise RuntimeError(f"口播文件不存在：{audio_path}")
    size = audio_path.stat().st_size
    if size < 1024:
        raise RuntimeError(f"口播文件过小（{size} bytes）")
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0 or "audio" not in probe.stdout:
        raise RuntimeError(f"口播文件不可解码：{audio_path}")
    durations = []
    for line in probe.stdout.splitlines():
        try:
            durations.append(float(line.strip()))
        except ValueError:
            pass
    duration = max(durations) if durations else 0.0
    if duration < 0.5:
        raise RuntimeError(f"口播时长异常（{duration:.2f}s）")
    return duration


def _prepare_local_one_take_master(
    ad_script: Dict[str, Any],
    asset_index: Dict[str, Any],
    product_info: Dict[str, Any],
    requested_voice: str,
    creative_profile: Optional[Dict[str, Any]],
    reference_profile: Optional[Dict[str, Any]],
    transition_duration: float,
    output_path: Path,
) -> Dict[str, Any]:
    """Generate the only TTS request, then derive the edit timeline from its real duration."""
    from local_asset_pipeline import build_one_take_timeline

    provisional_lines = [
        {
            "text": str(segment.get("voiceover") or ""),
            "start": float(index),
            "end": float(index + 1),
            "segment": int(segment.get("segment", index)),
        }
        for index, segment in enumerate(ad_script.get("segments") or [])
    ]
    voice, voice_reason = recommend_voice_for_narration(
        product_info,
        provisional_lines,
        requested_voice=requested_voice,
        creative_profile=creative_profile,
    )
    full_text = str(ad_script.get("voiceover_full") or "").strip()
    if not full_text:
        raise RuntimeError("本地素材单条口播缺少 voiceover_full")
    synthesis_contract = build_tts_synthesis_contract(full_text, voice)
    cache_dir = output_path.parent.parent / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = synthesis_contract["sha256"][:24]
    cached_audio = cache_dir / f"{cache_key}.m4a"
    cached_manifest = cache_dir / f"{cache_key}.json"
    cache_hit = False
    if cached_audio.is_file() and cached_manifest.is_file():
        try:
            manifest = json.loads(cached_manifest.read_text(encoding="utf-8"))
            cache_hit = (
                manifest == synthesis_contract
                and _validate_voiceover_audio(cached_audio) > 0.5
            )
        except (OSError, ValueError, RuntimeError):
            cache_hit = False
    print(
        f"🎤 {'复用' if cache_hit else '前置生成'}全视频唯一 TTS 母带"
        f"（音色：{voice}，{voice_reason}）"
    )
    if not cache_hit:
        temporary_audio = cache_dir / f".{cache_key}.tmp.m4a"
        generate_tts_audio(full_text, temporary_audio, voice=voice)
        _validate_voiceover_audio(temporary_audio)
        temporary_audio.replace(cached_audio)
        cached_manifest.write_text(
            json.dumps(synthesis_contract, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    shutil.copy2(cached_audio, output_path)
    audio_duration = _validate_voiceover_audio(output_path)
    return _build_local_one_take_timeline_from_audio(
        ad_script=ad_script,
        asset_index=asset_index,
        reference_profile=reference_profile,
        transition_duration=transition_duration,
        output_path=output_path,
        audio_duration=audio_duration,
        voice=voice,
        voice_reason=voice_reason,
        source_audio_path=cached_audio,
        tts_cache_hit=cache_hit,
    )


def _build_local_one_take_timeline_from_audio(
    ad_script: Dict[str, Any],
    asset_index: Dict[str, Any],
    reference_profile: Optional[Dict[str, Any]],
    transition_duration: float,
    output_path: Path,
    audio_duration: float,
    voice: str,
    voice_reason: str,
    source_audio_path: Optional[Path] = None,
    tts_cache_hit: bool = False,
) -> Dict[str, Any]:
    """Derive the edit timeline from one already-generated continuous TTS master."""
    from local_asset_pipeline import build_one_take_timeline

    reference_outro_duration = float((reference_profile or {}).get("outro_duration") or 0.0)
    main_text = "".join(str(item.get("voiceover") or "") for item in ad_script.get("segments") or [])
    outro_text = str(ad_script.get("voiceover_outro_cue") or "")
    main_units = max(1, len(re.findall(r"[\w\u4e00-\u9fff]", main_text)))
    outro_units = len(re.findall(r"[\w\u4e00-\u9fff]", outro_text))
    estimated_outro_duration = (
        audio_duration * outro_units / max(main_units + outro_units, 1)
        if outro_units else 0.0
    )
    outro_duration = (
        min(reference_outro_duration, max(0.5, estimated_outro_duration))
        if reference_outro_duration > 0 and outro_units else 0.0
    )
    main_duration = audio_duration - outro_duration
    if main_duration <= 0.5:
        raise RuntimeError(
            f"单条口播母带 {audio_duration:.2f}s 无法覆盖 CTA {outro_duration:.2f}s 之外的主内容"
        )
    windows = (asset_index or {}).get("windows") or []
    segments = ad_script.get("segments") or []
    role_capacities: Dict[str, float] = {}
    for role in {str(segment.get("product_story_role") or "").strip() for segment in segments}:
        by_source: Dict[str, List[Tuple[float, float]]] = {}
        for window in windows:
            analysis = window.get("analysis") or {}
            if not analysis.get("usable_for_ad"):
                continue
            if role and str(analysis.get("product_story_role") or "unknown") != role:
                continue
            by_source.setdefault(str(window.get("source_path") or ""), []).append((
                float(window.get("start") or 0.0),
                float(window.get("end") or 0.0),
            ))
        capacity = 0.0
        for intervals in by_source.values():
            merged: List[List[float]] = []
            for start, end in sorted(intervals):
                if end <= start:
                    continue
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            capacity += sum(end - start for start, end in merged)
        role_capacities[role] = capacity
    cue_weights = {
        int(segment.get("segment", index)): max(
            1,
            len(re.findall(r"[\w\u4e00-\u9fff]", str(segment.get("voiceover") or ""))),
        )
        for index, segment in enumerate(segments)
    }
    role_weight_totals: Dict[str, int] = {}
    for index, segment in enumerate(segments):
        role = str(segment.get("product_story_role") or "").strip()
        role_weight_totals[role] = role_weight_totals.get(role, 0) + cue_weights[int(segment.get("segment", index))]
    max_clip_durations: Dict[int, float] = {}
    for index, segment in enumerate(segments):
        segment_index = int(segment.get("segment", index))
        role = str(segment.get("product_story_role") or "").strip()
        max_clip_durations[segment_index] = (
            role_capacities.get(role, 0.0)
            * cue_weights[segment_index]
            / max(role_weight_totals.get(role, 1), 1)
        )
    timeline = build_one_take_timeline(
        ad_script,
        main_duration=main_duration,
        outro_duration=outro_duration,
        transition_duration=transition_duration,
        max_clip_durations=max_clip_durations,
    )
    aligned_lines = align_voiceover_lines_to_audio_pauses(
        timeline["voiceover_lines"],
        output_path,
        audio_duration,
        preserve_container_edges=True,
    )
    timeline["voiceover_lines"] = aligned_lines
    main_lines = [line for line in aligned_lines if not line.get("is_outro")]
    timeline["clip_durations"] = {
        int(line["segment"]): round(float(line["end"]) - float(line["start"]), 3)
        for line in main_lines
    }
    if main_lines:
        timeline["main_duration"] = round(float(main_lines[-1]["end"]), 3)
    outro_lines = [line for line in aligned_lines if line.get("is_outro")]
    timeline["outro_duration"] = round(
        sum(float(line["end"]) - float(line["start"]) for line in outro_lines),
        3,
    )
    timeline["alignment_precision"] = (
        "measured_audio_pauses"
        if any(line.get("alignment_precision") == "measured_audio_pause" for line in aligned_lines)
        else "speech_weight_estimate"
    )
    timeline.update({
        "audio_path": str(output_path),
        "source_audio_path": str(source_audio_path or output_path),
        "audio_duration": round(audio_duration, 3),
        "voice": voice,
        "voice_reason": voice_reason,
        "mode": "single_take",
        "tts_requests": 1,
        "tts_external_requests": 0 if tts_cache_hit else 1,
        "tts_cache_hit": tts_cache_hit,
        "tempo_multiplier": 1.0,
    })
    return timeline


def _audio_waveform_similarity(
    left: Path,
    right: Path,
    sample_rate: int = 4000,
    max_lag_ms: int = 100,
) -> float:
    """Return peak mono correlation while tolerating normal codec/filter latency."""
    import numpy as np

    def _decode(path: Path) -> Any:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                "-ar", str(sample_rate), "-f", "s16le", "-",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float64)

    left_values = _decode(left)
    right_values = _decode(right)
    count = min(len(left_values), len(right_values))
    if count < sample_rate // 2:
        return 0.0
    left_centered = left_values[:count] - np.mean(left_values[:count])
    right_centered = right_values[:count] - np.mean(right_values[:count])
    max_lag = max(0, int(sample_rate * max_lag_ms / 1000))
    step = max(1, sample_rate // 1000)
    best = 0.0
    for lag in range(-max_lag, max_lag + 1, step):
        if lag >= 0:
            left_slice = left_centered[lag:]
            right_slice = right_centered[:len(left_slice)]
        else:
            right_slice = right_centered[-lag:]
            left_slice = left_centered[:len(right_slice)]
        if len(left_slice) < sample_rate // 2:
            continue
        numerator = float(np.dot(left_slice, right_slice))
        denominator = float(np.linalg.norm(left_slice) * np.linalg.norm(right_slice))
        if denominator:
            best = max(best, abs(numerator / denominator))
    return best


def _validate_voiceover_in_mix(mixed_video: Path, voiceover: Path, minimum_similarity: float = 0.15) -> float:
    similarity = _audio_waveform_similarity(mixed_video, voiceover)
    if similarity < minimum_similarity:
        raise RuntimeError(
            f"最终混音未检出口播波形（相似度 {similarity:.3f} < {minimum_similarity:.2f}）"
        )
    return similarity


def _build_scene_anchor(
    anchor_clip: Path,
    output_dir: Path,
    num_keyframes: int = 2,
) -> list[str]:
    """
    从锚点片段提取场景锚点关键帧（用于全局场景一致性）

    Args:
        anchor_clip: 锚点视频片段
        output_dir: 输出目录（用于临时文件）
        num_keyframes: 提取关键帧数量

    Returns:
        base64 编码的参考图列表
    """
    anchor_frames = []
    try:
        keyframes = extract_keyframes(
            anchor_clip,
            output_dir / "scene_anchor",
            count=num_keyframes,
        )
        for kf in keyframes:
            b64 = base64.b64encode(kf.read_bytes()).decode("utf-8")
            anchor_frames.append(f"data:image/png;base64,{b64}")
    except Exception as e:
        print(f"  ⚠️ 提取场景锚点失败：{e}")
        # fallback：提取中间帧
        mid_frame = _extract_frame_b64(anchor_clip, time_sec=2.0)
        if mid_frame:
            anchor_frames.append(mid_frame)
    return anchor_frames


def _inject_scene_consistency_prompt(
    base_prompt: str,
    is_first_clip: bool = False,
) -> str:
    """
    向 Prompt 中注入场景一致性描述

    注意：一致性描述放在 Prompt 开头，扩散模型对开头注意力最强。

    Args:
        base_prompt: 原始 Prompt
        is_first_clip: 是否是第一个片段

    Returns:
        增强后的 Prompt
    """
    scene_desc = CONSISTENCY_TEMPLATES["scene"]
    lighting_desc = CONSISTENCY_TEMPLATES["lighting"]

    if is_first_clip:
        # 第一个片段不需要"与前一段连续"的描述
        consistency = f"{lighting_desc}"
    else:
        consistency = f"{scene_desc}, {lighting_desc}, continuous shot"

    # 把一致性描述插入到 Prompt 开头（扩散模型注意力前重后轻）
    return f"{consistency}, {base_prompt}"


def input_with_default(prompt: str, default: str = "") -> str:
    """带默认值的输入"""
    if default:
        user_input = input(f"{prompt} [{default}]：").strip()
        return user_input if user_input else default
    return input(f"{prompt}：").strip()


def estimate_cost(
    mode: str = "std",
    duration_per_clip: int = 5,
    num_clips: int = 5,
    num_characters: int = 1,
    ab_versions: int = 1,
    best_of: int = 1,
    image_first_segments: int = 0,
    image_first_variants: int = 0,
    images_per_character: int = 2,
) -> dict:
    """
    估算本次生成的 API 成本（仅供参考）

    Args:
        mode: 生成模式（std/pro/4k）
        duration_per_clip: 单片段时长（秒）
        num_clips: 片段数量
        num_characters: 角色数量
        ab_versions: A/B 版本数
        best_of: 每段候选数
        image_first_segments: 图片先行覆盖的片段数
        image_first_variants: 图片先行每段候选数
        images_per_character: 每个角色的定妆照数量（默认 2 张：正面 + 全身）

    Returns:
        {
            "image_count": 图片生成次数,
            "video_seconds": 视频生成总秒数,
            "estimated_cost": 预估费用（元）,
            "breakdown": 明细列表,
        }
    """
    pricing = KLING_PRICING
    img_price = pricing["image"].get(mode, 0.05)
    vid_price = pricing["video"].get(mode, 0.30)

    best_of = max(1, int(best_of or 1))

    # 每个版本：角色定妆照 + 图片先行候选
    image_first_count = max(0, int(image_first_segments or 0)) * max(0, int(image_first_variants or 0))
    character_image_count = max(0, int(num_characters or 0)) * max(1, int(images_per_character or 1))
    image_per_version = character_image_count + image_first_count
    video_seconds_per_version = duration_per_clip * num_clips * best_of

    total_images = image_per_version * ab_versions
    total_video_seconds = video_seconds_per_version * ab_versions

    image_cost = total_images * img_price
    video_cost = total_video_seconds * vid_price
    total_cost = image_cost + video_cost

    breakdown = [
        f"角色定妆照：{character_image_count * ab_versions} 张（{num_characters} 角色 × {images_per_character} 角度/人）× {img_price:.2f} 元 = {character_image_count * ab_versions * img_price:.2f} 元",
        f"视频片段：{total_video_seconds:.0f} 秒 × {vid_price:.2f} 元/秒 = {video_cost:.2f} 元",
    ]

    if best_of > 1:
        breakdown.append(f"best-of 候选：每段 {best_of} 个候选（视频成本已按倍数计入）")
    if image_first_count > 0:
        breakdown.append(f"图片先行预检：{image_first_count * ab_versions} 张候选图（用于视频前低成本择优）")
    if ab_versions > 1:
        breakdown.append(f"A/B 版本：{ab_versions} 个版本")

    return {
        "image_count": total_images,
        "video_seconds": total_video_seconds,
        "estimated_cost": round(total_cost, 2),
        "breakdown": breakdown,
    }


def _estimate_image_first_segment_count(num_clips: int, mode: str, enabled: bool = True) -> int:
    """估算图片先行会覆盖的关键片段数，用于成本提示。"""
    if not enabled or num_clips <= 0:
        return 0
    mode = (mode or "standard").lower()
    if mode == "minimal":
        return 1
    if mode == "full":
        return num_clips
    return min(num_clips, 2)


def _get_cost_budget_limit() -> float:
    """读取单次生成预算上限。"""
    try:
        from config import QUALITY_GATE_CONFIG
        return float(QUALITY_GATE_CONFIG.get("cost_control", {}).get("max_budget", 100.0))
    except Exception:
        return 100.0


def _auto_downgrade_enabled() -> bool:
    """读取是否允许超预算时自动降级。"""
    try:
        from config import QUALITY_GATE_CONFIG
        return bool(QUALITY_GATE_CONFIG.get("cost_control", {}).get("auto_downgrade", True))
    except Exception:
        return True


def apply_low_cost_generation_policy(
    args: argparse.Namespace,
    *,
    num_clips: int,
    num_characters: int = 1,
    ab_versions: int = 1,
    budget_limit: Optional[float] = None,
) -> List[str]:
    """
    在进入真实生成前应用低成本策略。

    策略顺序按画质影响从小到大排列：
    1. best-of 降为 1，避免候选倍增；
    2. 4k -> pro -> std；
    3. 片段时长降到 5s，再最低降到 3s。
    """
    budget = _get_cost_budget_limit() if budget_limit is None else float(budget_limit)
    if budget <= 0 or not _auto_downgrade_enabled() or getattr(args, "preview", False):
        return []

    changes: List[str] = []

    def _current_cost() -> float:
        info = estimate_cost(
            mode=getattr(args, "mode", DEFAULT_MODE),
            duration_per_clip=getattr(args, "duration", DEFAULT_VIDEO_DURATION),
            num_clips=num_clips,
            num_characters=num_characters,
            ab_versions=ab_versions,
            best_of=getattr(args, "best_of", 1),
        )
        return float(info["estimated_cost"])

    if _current_cost() <= budget:
        return changes

    if getattr(args, "best_of", 1) > 1:
        old = args.best_of
        args.best_of = 1
        changes.append(f"best_of {old} -> 1")
        if _current_cost() <= budget:
            return changes

    mode_downgrade = {"4k": "pro", "pro": "std", "standard": "std"}
    while getattr(args, "mode", DEFAULT_MODE) in mode_downgrade and _current_cost() > budget:
        old = args.mode
        args.mode = mode_downgrade[old]
        changes.append(f"mode {old} -> {args.mode}")

    if _current_cost() <= budget:
        return changes

    if getattr(args, "duration", DEFAULT_VIDEO_DURATION) > 5:
        old = args.duration
        args.duration = 5
        changes.append(f"duration {old}s -> 5s")

    if _current_cost() <= budget:
        return changes

    if getattr(args, "duration", DEFAULT_VIDEO_DURATION) > 3:
        old = args.duration
        args.duration = 3
        changes.append(f"duration {old}s -> 3s")

    return changes


class GenerationSourcePlan(TypedDict):
    """CLI preflight contract for one mutually exclusive visual source workflow."""
    source: str
    label: str
    policy_changes: List[str]
    kling_cost_info: Optional[Dict[str, Any]]
    estimated_output_size: Optional[Dict[str, Any]]


def _prepare_generation_source_plan(
    args: argparse.Namespace,
    product_info: Dict[str, Any],
    *,
    ab_versions: int,
) -> GenerationSourcePlan:
    """Build either a local-edit plan or a Kling-generation plan, never a hybrid."""
    if getattr(args, "local_assets", None):
        return {
            "source": "local_assets",
            "label": "本地视频混剪",
            "policy_changes": [],
            "kling_cost_info": None,
            "estimated_output_size": None,
        }

    preview = bool(getattr(args, "preview", False))
    estimated_clips = 1 if preview else 5
    resolved_characters = build_cast_plan(product_info).get("core_characters", [])
    policy_changes = apply_low_cost_generation_policy(
        args,
        num_clips=estimated_clips,
        num_characters=len(resolved_characters) or 1,
        ab_versions=ab_versions,
    )
    estimated_mode = "std" if preview else args.mode
    cost_info = estimate_cost(
        mode=estimated_mode,
        duration_per_clip=args.duration,
        num_clips=estimated_clips,
        num_characters=len(resolved_characters) or 1,
        ab_versions=ab_versions,
        best_of=getattr(args, "best_of", 1),
        image_first_segments=_estimate_image_first_segment_count(
            estimated_clips,
            getattr(args, "image_first_mode", "standard"),
            enabled=getattr(args, "image_first", True)
            and getattr(args, "preflight_keyframe", True)
            and getattr(args, "strict", True)
            and not preview,
        ),
        image_first_variants=max(1, int(getattr(args, "image_first_variants", 2) or 1)),
    )
    from douyin_adapter import DOUYIN_CONFIG
    estimated_size = estimate_file_size(
        duration=getattr(args, "target_duration", None) or 25,
        bitrate=DOUYIN_CONFIG["bitrate"],
        audio_bitrate=DOUYIN_CONFIG.get("audio_bitrate", "160k"),
    )
    return {
        "source": "kling_generation",
        "label": "可灵 AI 视频生成",
        "policy_changes": policy_changes,
        "kling_cost_info": cost_info,
        "estimated_output_size": estimated_size,
    }


def _print_generation_source_plan(
    plan: GenerationSourcePlan,
    args: argparse.Namespace,
) -> None:
    if plan["source"] == "local_assets":
        print("🎞️ 本地视频混剪计划")
        print(f"   素材目录：{Path(args.local_assets).expanduser()}")
        print("   处理链路：素材理解 → 带货脚本与单条口播 → 智能选片 → 后期合成")
        if getattr(args, "target_duration", None):
            print(f"   时长偏好：{args.target_duration}s（最终时长由素材自然叙事决定）")
        else:
            print("   最终时长：由素材分析后的自然叙事决定")
        print("   可灵图片/视频生成：不调用")
        return

    if plan["policy_changes"]:
        print("💸 已应用低成本策略：")
        for change in plan["policy_changes"]:
            print(f"   - {change}")
    if getattr(args, "preview", False):
        print("⚡ 预览模式：使用 std 模式，仅生成 1 段快速试错")
    print_cost_estimate(plan["kling_cost_info"] or {})
    estimated_size = plan["estimated_output_size"] or {}
    print(f"📦 预估文件大小：约 {estimated_size.get('total_size_mb', 0):.1f} MB")
    if estimated_size.get("warning"):
        print(f"   ⚠️  {estimated_size['warning']}")


def _run_pre_generation_smart_decision(
    quality_gate_result: Any,
    product_info: dict,
    *,
    style: str,
    budget: Optional[float] = None,
    image_first_result: Any = None,
    preview: bool = False,
) -> Optional[Any]:
    """
    将 one_click 主流程接入智能决策引擎。

    智能决策只依赖质量门结果，不触发视频生成；它用于在昂贵视频 API
    调用前判断是否应阻断、降级或采用渐进式生成。

    预览模式下放宽限制，因为预览就是用来快速试错的。
    """
    try:
        from smart_decision_engine import run_smart_decision, print_smart_decision_report
    except Exception as e:
        print(f"⚠️  智能决策引擎不可用，跳过工作流决策：{e}")
        return None

    try:
        decision = run_smart_decision(
            quality_gate_result=quality_gate_result,
            product_category=product_info.get("type", "default"),
            style_preference=style,
            budget=_get_cost_budget_limit() if budget is None else budget,
            image_first_result=image_first_result,
        )

        # 预览模式：放宽限制，只要不是致命问题就放行
        if preview and not decision.can_proceed:
            has_fatal = any(
                "block_quality_gate" in p or "fatal" in p.issue.lower()
                for p in decision.repair_paths
            )
            if not has_fatal:
                decision.can_proceed = True
                decision.recommended_strategy = "preview_fast_track"
                decision.messages.append("⚡ 预览模式：快速通道，跳过非致命阻断")
                print("⚡ 预览模式：智能决策快速通道已启用")

        print_smart_decision_report(decision)
        if not decision.can_proceed:
            raise RuntimeError(
                f"智能决策阻止生成：预测成功率 {decision.estimated_success_rate:.1%}，"
                f"策略 {decision.recommended_strategy}"
            )
        return decision
    except RuntimeError:
        raise
    except Exception as e:
        print(f"⚠️  智能决策执行失败，继续使用质量门结果：{e}")
        return None


def _result_quality_score(quality_result: Any) -> float:
    """兼容不同质量报告对象的总分字段。"""
    if not quality_result:
        return 0.0
    if hasattr(quality_result, "overall_score"):
        return float(getattr(quality_result, "overall_score") or 0.0)
    if hasattr(quality_result, "total_score"):
        return float(getattr(quality_result, "total_score") or 0.0)
    if isinstance(quality_result, dict):
        return float(quality_result.get("overall_score") or quality_result.get("total_score") or 0.0)
    return 0.0


def _result_issues(quality_result: Any) -> List[str]:
    """兼容不同质量报告对象的问题字段。"""
    if not quality_result:
        return []
    if hasattr(quality_result, "issues"):
        return list(getattr(quality_result, "issues") or [])
    if isinstance(quality_result, dict):
        return list(quality_result.get("issues") or [])
    return []


def _record_production_workflow_completion(
    *,
    output_name: str,
    final_path: Path,
    quality_result: Any,
    product_info: dict,
    ad_script: dict,
    generation_params: dict,
    character_assets: List[dict],
    product_image_path: Optional[Path],
    character_bibles: List[dict],
    product_bible: Optional[dict],
    decision_result: Optional[Any] = None,
    asset_library: Optional[Any] = None,
    feedback_loop: Optional[Any] = None,
    experiment_tracker: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    one_click 主流程的工作流闭环：资产注册、反馈收集、实验追踪。

    这些步骤不触发视频重生成；失败时返回 warning，不影响已通过质量门的成片。
    """
    summary: Dict[str, Any] = {
        "registered_assets": [],
        "automatic_observation_recorded": False,
        "feedback_collected": False,
        "experiment_tracked": False,
        "warnings": [],
    }

    artifact_digest = hashlib.sha256()
    with final_path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            artifact_digest.update(chunk)
    artifact_id = artifact_digest.hexdigest()[:12]
    video_id = f"video_{output_name}_{artifact_id}"
    quality_score = _result_quality_score(quality_result)
    quality_issues = _result_issues(quality_result)
    rating = 5 if quality_score >= 90 else 4 if quality_score >= 80 else 3 if quality_score >= 60 else 2

    try:
        if asset_library is None:
            from asset_library import AssetLibrary
            asset_library = AssetLibrary()

        for i, char in enumerate(character_assets or []):
            image_path = Path(char.get("image_path")) if char.get("image_path") else None
            if not image_path or not image_path.exists():
                continue
            bible = character_bibles[i] if i < len(character_bibles) else {}
            asset_id = asset_library.add_character(
                image_path=image_path,
                name=bible.get("name") or char.get("name") or f"Character {i + 1}",
                bible=bible,
                tags=[product_info.get("type", "default"), "character", output_name],
            )
            if quality_score:
                asset_library.update_quality_score(asset_id, quality_score)
            summary["registered_assets"].append({"type": "character", "id": asset_id})

        if product_image_path and product_image_path.exists():
            asset_id = asset_library.add_product(
                image_path=product_image_path,
                name=product_info.get("name", "product"),
                bible=product_bible or {},
                tags=[product_info.get("type", "default"), "product", output_name],
            )
            if quality_score:
                asset_library.update_quality_score(asset_id, quality_score)
            summary["registered_assets"].append({"type": "product", "id": asset_id})
    except Exception as e:
        summary["warnings"].append(f"资产注册失败：{e}")

    try:
        if feedback_loop is None:
            from feedback_loop import FeedbackLoop
            feedback_loop = FeedbackLoop()

        summary["automatic_observation_recorded"] = bool(feedback_loop.collect_feedback(
            video_id=video_id,
            generation_params={
                **generation_params,
                "product_name": product_info.get("name", ""),
                "product_type": product_info.get("type", ""),
                "num_segments": len(ad_script.get("segments", [])) if isinstance(ad_script, dict) else 0,
            },
            rating=rating,
            issues=quality_issues,
            auto_quality_score=quality_score,
            auto_issues=quality_issues,
        ))
    except Exception as e:
        summary["warnings"].append(f"反馈收集失败：{e}")

    try:
        if experiment_tracker is None:
            from experiment_tracker import ExperimentTracker
            experiment_tracker = ExperimentTracker()

        experiment_id = f"exp_{output_name}_{artifact_id}"
        strategy = getattr(decision_result, "recommended_strategy", None) or generation_params.get("strategy", "standard")
        estimated_success_rate = getattr(decision_result, "estimated_success_rate", None)
        experiment_started = experiment_tracker.start_experiment(
            experiment_id=experiment_id,
            hypothesis=f"{product_info.get('type', 'default')} 视频生成质量闭环验证",
            params={
                **generation_params,
                "strategy": strategy,
                "estimated_success_rate": estimated_success_rate,
                "final_path": str(final_path),
            },
            video_id=video_id,
        )
        if experiment_started:
            summary["experiment_tracked"] = bool(experiment_tracker.complete_experiment(
                experiment_id=experiment_id,
                rating=rating,
                quality_score=quality_score,
            ))
            summary["experiment_id"] = experiment_id
        else:
            summary["warnings"].append(
                f"实验创建失败，未完成或覆盖已有实验：{experiment_id}"
            )
    except Exception as e:
        summary["warnings"].append(f"实验追踪失败：{e}")

    return summary


def print_cost_estimate(cost_info: dict):
    """打印成本估算"""
    print()
    print("💰 成本估算（仅供参考，以实际账单为准）")
    print("-" * 40)
    for line in cost_info["breakdown"]:
        print(f"  {line}")
    print("-" * 40)
    print(f"  预估总费用：约 {cost_info['estimated_cost']:.2f} 元")
    print()


def estimate_file_size(
    duration: float,
    bitrate: str = "6M",
    audio_bitrate: str = "160k",
) -> dict:
    """
    估算最终视频文件大小

    Args:
        duration: 视频时长（秒）
        bitrate: 视频码率（如 "6M"、"4M"）
        audio_bitrate: 音频码率（如 "160k"）

    Returns:
        {
            "video_size_mb": 视频部分大小（MB）,
            "audio_size_mb": 音频部分大小（MB）,
            "total_size_mb": 总大小（MB）,
            "warning": 预警信息（如果太大）,
        }
    """
    # 解析码率
    def _parse_bitrate(br_str: str) -> float:
        """解析码率字符串为 bps"""
        br_str = br_str.strip().upper()
        if br_str.endswith("M"):
            return float(br_str[:-1]) * 1000 * 1000
        elif br_str.endswith("K"):
            return float(br_str[:-1]) * 1000
        else:
            return float(br_str)

    video_bps = _parse_bitrate(bitrate)
    audio_bps = _parse_bitrate(audio_bitrate)

    # 文件大小 = 码率 × 时长 / 8（bit -> byte）
    video_bytes = video_bps * duration / 8
    audio_bytes = audio_bps * duration / 8
    total_bytes = video_bytes + audio_bytes

    # 加上容器开销（约 5-10%）
    total_bytes *= 1.08

    video_mb = video_bytes / (1024 * 1024)
    audio_mb = audio_bytes / (1024 * 1024)
    total_mb = total_bytes / (1024 * 1024)

    # 预警
    warning = ""
    if total_mb > 200:
        warning = f"文件较大（约 {total_mb:.0f} MB），上传可能较慢"
    elif total_mb > 100:
        warning = f"文件偏大（约 {total_mb:.0f} MB）"

    return {
        "video_size_mb": round(video_mb, 1),
        "audio_size_mb": round(audio_mb, 1),
        "total_size_mb": round(total_mb, 1),
        "warning": warning,
    }


def calc_duration_for_target(target_duration: int) -> tuple:
    """
    根据目标总时长，计算合适的片段数和单片段时长

    Args:
        target_duration: 目标总时长（秒）

    Returns:
        (num_segments, duration_per_clip, script_style_note)
    """
    # 转场总时长（估算：每个转场 0.5s）
    transition_total = lambda n: (n - 1) * 0.5

    # 预设：不同总时长对应的片段数和单段时长
    presets = {
        7:  (3, 3, "极短钩子版（3段）"),     # 3*3 - 2*0.5 = 8s，接近7s
        15: (5, 3.5, "15秒经典版（5段）"),   # 5*3.5 - 4*0.5 = 15.5s
        30: (5, 7, "30秒深度版（5段）"),     # 5*7 - 4*0.5 = 33s，稍长但可接受
        60: (7, 10, "60秒详细版（7段）"),    # 7*10 - 6*0.5 = 67s，稍长
    }

    if target_duration in presets:
        num_segs, dur, note = presets[target_duration]
        return num_segs, dur, note

    # 通用计算：默认 5 段，倒推单段时长
    num_segs = 5
    per_clip = max(2, (target_duration + transition_total(num_segs)) / num_segs)
    return num_segs, round(per_clip, 1), f"自定义 {target_duration}s（5段）"


def _fit_rhythm_template_to_net_duration(template: dict, net_duration: float) -> dict:
    """Scale segments so the post-transition timeline matches a reference duration."""
    fitted = copy.deepcopy(template)
    segments = fitted.get("segments") or []
    if not segments or net_duration <= 0:
        return fitted
    transition = float(fitted.get("transition_duration") or 0.0)
    desired_sum = net_duration + transition * max(0, len(segments) - 1)
    current_sum = sum(float(segment.get("duration") or 0.0) for segment in segments)
    if current_sum <= 0:
        return fitted
    scale = desired_sum / current_sum
    for segment in segments:
        segment["duration"] = round(float(segment.get("duration") or 0.0) * scale, 3)
    residual = desired_sum - sum(float(segment["duration"]) for segment in segments)
    segments[-1]["duration"] = round(float(segments[-1]["duration"]) + residual, 3)
    fitted["total_duration"] = round(sum(float(segment["duration"]) for segment in segments), 3)
    fitted["actual_total_duration"] = round(net_duration, 3)
    return fitted


# ============================================================
# 角色圣经 / 商品圣经（Character Bible / Product Bible）
# ============================================================
# 将零散的产品信息和角色描述统一整理为标准化资产文档，
# 从根源解决人物/商品一致性、prompt 拼接错误、多角色模糊指代问题。

class CharacterBible(TypedDict):
    """角色圣经：标准化的角色外观资产描述"""
    id: str           # 角色唯一标识，如 "char_01"
    name: str         # 角色名称，用于 prompt 中精确指代
    age: str
    gender: str
    ethnicity: str    # 肤色/族群，如 "Asian", "Caucasian"
    hair_style: str   # 发型，如 "long straight black hair"
    hair_color: str   # 发色
    outfit: str       # 服装描述
    accessories: str  # 配饰，如 "gold hoop earrings, silver watch"
    facial_features: str  # 标志性面部特征，如 "high cheekbones, small nose"
    expression_baseline: str  # 表情基调，如 "warm smile", "neutral confident"


class ProductBible(TypedDict):
    """商品圣经：标准化的商品外观资产描述"""
    name: str
    category: str
    packaging: str        # 包装描述，如 "white cylindrical bottle with gold cap"
    primary_color: str
    secondary_color: str
    shape: str            # 形状/瓶身，如 "slim cylindrical", "round jar"
    logo_description: str
    usage_context: str    # 使用场景，如 " skincare routine, bathroom counter"
    key_selling_point: str


def _normalize_character_list(characters: Any) -> List[dict]:
    """标准化角色列表，过滤无效项，保证后续成本估算和生成流程一致。"""
    if not isinstance(characters, list):
        return []

    normalized: List[dict] = []
    for idx, item in enumerate(characters, 1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"Character {chr(64 + idx)}").strip()
        description = str(item.get("description") or "").strip()
        role = str(item.get("role") or "").strip()
        character = {
            "id": str(item.get("id") or f"char_{idx:02d}").strip(),
            "name": name,
            "role": role,
            "role_type": str(item.get("role_type") or "core").strip(),
            "reference_required": bool(item.get("reference_required", True)),
            "description": description,
            "age": str(item.get("age") or "").strip(),
            "gender": str(item.get("gender") or "").strip(),
            "outfit": str(item.get("outfit") or "").strip(),
            "hair_style": str(item.get("hair_style") or "").strip(),
            "hair_color": str(item.get("hair_color") or "").strip(),
            "ethnicity": str(item.get("ethnicity") or "").strip(),
            "accessories": str(item.get("accessories") or "").strip(),
            "facial_features": str(item.get("facial_features") or "").strip(),
            "expression_baseline": str(item.get("expression_baseline") or "neutral confident").strip(),
        }
        if item.get("image_path"):
            character["image_path"] = item["image_path"]
        if character["description"] or character["age"] or character["gender"] or character["outfit"]:
            normalized.append(character)

    return normalized[:6]


def _product_context_text(product_info: dict, ad_script: Optional[dict] = None) -> str:
    parts = [
        str(product_info.get(key, ""))
        for key in (
            "name", "type", "selling_point", "audience", "style",
            "scene_description", "extra_requirements",
        )
    ]
    if ad_script:
        story_world = ad_script.get("story_world") or {}
        if isinstance(story_world, dict):
            parts.extend(str(story_world.get(key, "")) for key in ("character", "location", "emotion_arc"))
        for seg in ad_script.get("segments", []) or []:
            if isinstance(seg, dict):
                parts.extend(str(seg.get(key, "")) for key in ("scene_prompt", "subtitle", "voiceover"))
    return " ".join(parts).lower()


def _role(
    *,
    role_id: str,
    name: str,
    role: str,
    description: str,
    age: str = "",
    gender: str = "",
    outfit: str = "",
    role_type: str = "core",
    reference_required: bool = True,
    expression_baseline: str = "neutral confident",
) -> dict:
    return {
        "id": role_id,
        "name": name,
        "role": role,
        "role_type": role_type,
        "reference_required": reference_required,
        "description": description,
        "age": age,
        "gender": gender,
        "outfit": outfit,
        "expression_baseline": expression_baseline,
    }


def build_cast_plan(product_info: dict, ad_script: Optional[dict] = None) -> dict:
    """
    构建故事角色计划。

    - core_characters: 需要跨镜头保持一致并生成参考图的核心角色/主体。
    - supporting_characters: 可以出现在故事中，但不单独生成参考图的配角。
    - ambient_entities: 路人、宠物、车辆、环境人群等场景实体，只进入 prompt 约束。
    """
    explicit_core = _normalize_character_list(product_info.get("characters"))
    explicit_supporting = _normalize_character_list(product_info.get("supporting_characters"))
    explicit_ambient = product_info.get("ambient_entities")
    ambient_entities = [
        str(item).strip()
        for item in explicit_ambient
        if isinstance(explicit_ambient, list) and str(item).strip()
    ] if isinstance(explicit_ambient, list) else []

    if explicit_core:
        core = [
            c for c in explicit_core
            if c.get("reference_required", True) and c.get("role_type") not in {"supporting", "background", "ambient"}
        ]
        supporting = explicit_supporting + [
            c for c in explicit_core
            if not c.get("reference_required", True) or c.get("role_type") in {"supporting", "background", "ambient"}
        ]
        return {
            "core_characters": core[:4],
            "supporting_characters": supporting[:6],
            "ambient_entities": ambient_entities[:8],
            "rationale": "explicit characters from product_info",
        }

    text = _product_context_text(product_info, ad_script)
    core: List[dict] = []
    supporting: List[dict] = list(explicit_supporting)
    rationale = "single protagonist default"

    pet_markers = ("宠物", "猫", "狗", "犬", "猫粮", "狗粮", "pet", "cat", "dog")
    family_markers = (
        "家财", "家庭", "家人", "全屋", "房屋", "房产", "保险", "财险",
        "孩子", "父母", "亲子", "母婴", "home", "family", "insurance",
        "house", "property", "parent", "child",
    )
    # Q-Final 新增：更多产品类型的多角色配置
    beauty_markers = ("美妆", "护肤", "口红", "面膜", "粉底", "beauty", "skincare", "makeup", "cosmetic", "lipstick")
    fitness_markers = ("健身", "运动", "瑜伽", "跑步", "减肥", "塑形", "fitness", "gym", "workout", "yoga", "sports")
    food_markers = ("美食", "零食", "饮料", "咖啡", "奶茶", "火锅", "餐厅", "food", "snack", "beverage", "coffee", "drink")
    tech_markers = ("数码", "科技", "手机", "耳机", "电脑", "智能", "tech", "digital", "phone", "earphone", "laptop", "smart")
    fashion_markers = ("时尚", "服装", "穿搭", "鞋子", "包包", "配饰", "fashion", "clothing", "outfit", "shoes", "bag", "accessories")
    service_markers = ("客服", "理赔", "顾问", "医生", "老师", "教练", "advisor", "agent", "doctor", "teacher", "coach")
    social_markers = ("社交", "聚会", "派对", "约会", "分享", "social", "party", "gathering", "date", "share")
    crowd_markers = ("商场", "街头", "地铁", "办公室", "人群", "路人", "crowd", "street", "office", "mall")

    if any(marker in text for marker in pet_markers):
        core = [
            _role(
                role_id="char_01",
                name="Owner",
                role="pet owner and product user",
                description="30-year-old Chinese adult, relaxed everyday outfit, caring expression with pet",
                age="30",
                gender="person",
                outfit="relaxed everyday outfit",
            ),
            _role(
                role_id="char_02",
                name="Pet",
                role="animal protagonist",
                role_type="animal",
                description="healthy expressive household pet, clean fur, friendly natural movement",
                gender="animal",
                expression_baseline="friendly alert expression",
            ),
        ]
        rationale = "pet product needs animal subject consistency"
    elif any(marker in text for marker in family_markers):
        core = [
            _role(
                role_id="char_01",
                name="Mother",
                role="homeowner decision maker",
                description="35-year-old Chinese woman, calm and reliable, smart casual blouse, warm protective expression",
                age="35",
                gender="female",
                outfit="smart casual blouse",
                expression_baseline="calm protective smile",
            ),
            _role(
                role_id="char_02",
                name="Father",
                role="spouse and family co-decision maker",
                description="38-year-old Chinese man, neat short black hair, casual knit polo, steady and trustworthy expression",
                age="38",
                gender="male",
                outfit="casual knit polo",
                expression_baseline="steady trustworthy expression",
            ),
            _role(
                role_id="char_03",
                name="Child",
                role="family child, emotional core stake",
                description="8-year-old Chinese child, simple clean casual clothes, natural happy expression, innocent and lively",
                age="8",
                gender="child",
                outfit="simple clean casual clothes",
                expression_baseline="natural happy expression",
            ),
        ]
        is_insurance = any(m in text for m in ("保险", "财险", "insurance", "理赔", "顾问"))
        if is_insurance:
            core.append(_role(
                role_id="char_04",
                name="Insurance Advisor",
                role="professional insurance consultant",
                description="32-year-old Chinese professional, well-fitted business attire, friendly and reassuring smile, trustworthy demeanor",
                age="32",
                gender="person",
                outfit="well-fitted business attire",
                expression_baseline="friendly reassuring smile",
            ))
        rationale = "family product: both parents + child as core (child is emotional anchor for insurance/home products)"
    elif any(marker in text for marker in beauty_markers):
        # Q-Final 新增：美妆护肤类 → 主角 + 闺蜜（分享/种草场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="primary beauty product user",
                description="28-year-old Chinese woman, natural glowing skin, casual chic outfit, warm friendly expression, applying skincare product",
                age="28",
                gender="female",
                outfit="casual chic outfit",
                expression_baseline="warm friendly smile",
            ),
            _role(
                role_id="char_02",
                name="Best Friend",
                role="friend and sharing partner",
                description="26-year-old Chinese woman, fresh natural look, stylish casual outfit, curious excited expression, trying product together",
                age="26",
                gender="female",
                outfit="stylish casual outfit",
                expression_baseline="curious excited expression",
            ),
        ]
        rationale = "beauty product: user + best friend for sharing/种草场景 (two-person interaction feels more authentic)"
    elif any(marker in text for marker in fitness_markers):
        # Q-Final 新增：运动健身类 → 主角 + 教练（指导场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="fitness enthusiast and product user",
                description="27-year-old Chinese adult, athletic build, active sportswear, determined energetic expression, working out with product",
                age="27",
                gender="person",
                outfit="premium sportswear",
                expression_baseline="determined energetic expression",
            ),
            _role(
                role_id="char_02",
                name="Fitness Coach",
                role="professional trainer and guide",
                description="30-year-old Chinese adult, fit athletic body, professional fitness attire, encouraging confident expression, guiding form",
                age="30",
                gender="person",
                outfit="professional fitness attire",
                expression_baseline="encouraging confident smile",
            ),
        ]
        rationale = "fitness product: user + coach for guidance/demo场景 (expert endorsement + proper form demonstration)"
    elif any(marker in text for marker in food_markers) and not any(marker in text for marker in family_markers):
        # Q-Final 新增：美食饮料类 → 主角 + 朋友（分享场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="food lover and product user",
                description="26-year-old Chinese adult, casual trendy outfit, happy delighted expression, enjoying food/drink product",
                age="26",
                gender="person",
                outfit="casual trendy outfit",
                expression_baseline="happy delighted expression",
            ),
            _role(
                role_id="char_02",
                name="Friend",
                role="companion and sharing partner",
                description="25-year-old Chinese adult, relaxed casual wear, cheerful smiling expression, sharing food/drink experience together",
                age="25",
                gender="person",
                outfit="relaxed casual wear",
                expression_baseline="cheerful smiling expression",
            ),
        ]
        rationale = "food/beverage product: user + friend for sharing场景 (social dining feels more natural and appetizing)"
    elif any(marker in text for marker in tech_markers):
        # Q-Final 新增：数码科技类 → 主角 + 朋友（展示/推荐场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="tech enthusiast and product user",
                description="25-year-old Chinese adult, smart casual style, excited impressed expression, demonstrating tech product",
                age="25",
                gender="person",
                outfit="smart casual style",
                expression_baseline="excited impressed expression",
            ),
            _role(
                role_id="char_02",
                name="Friend",
                role="curious observer and reaction person",
                description="24-year-old Chinese adult, modern casual outfit, interested surprised expression, reacting to product demo",
                age="24",
                gender="person",
                outfit="modern casual outfit",
                expression_baseline="interested surprised expression",
            ),
        ]
        rationale = "tech product: user + friend for demo/reaction场景 (second person reaction makes product more convincing)"
    elif any(marker in text for marker in fashion_markers):
        # Q-Final 新增：时尚穿搭类 → 主角 + 闺蜜（搭配/评价场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="fashion lover and product user",
                description="27-year-old Chinese woman, stylish fashionable outfit, confident happy expression, modeling fashion item",
                age="27",
                gender="female",
                outfit="stylish fashionable outfit",
                expression_baseline="confident happy expression",
            ),
            _role(
                role_id="char_02",
                name="Best Friend",
                role="stylist companion and honest opinion",
                description="26-year-old Chinese woman, trendy chic style, admiring approving expression, giving fashion advice",
                age="26",
                gender="female",
                outfit="trendy chic style",
                expression_baseline="admiring approving expression",
            ),
        ]
        rationale = "fashion product: user + best friend for styling/feedback场景 (second person validation drives desire)"
    elif any(marker in text for marker in social_markers):
        # Q-Final 新增：社交聚会类 → 主角 + 2个朋友（多人互动场景）
        core = [
            _role(
                role_id="char_01",
                name="Main User",
                role="host and product user",
                description="28-year-old Chinese adult, warm welcoming smile, casual party outfit, socializing with product",
                age="28",
                gender="person",
                outfit="casual party outfit",
                expression_baseline="warm welcoming smile",
            ),
            _role(
                role_id="char_02",
                name="Friend A",
                role="party guest and engaged participant",
                description="27-year-old Chinese adult, lively cheerful expression, festive casual wear, interacting with product",
                age="27",
                gender="person",
                outfit="festive casual wear",
                expression_baseline="lively cheerful expression",
            ),
            _role(
                role_id="char_03",
                name="Friend B",
                role="party guest and happy participant",
                description="26-year-old Chinese adult, joyful laughing expression, stylish casual outfit, enjoying the moment",
                age="26",
                gender="person",
                outfit="stylish casual outfit",
                expression_baseline="joyful laughing expression",
            ),
        ]
        rationale = "social product: host + 2 friends for group场景 (multiple people create social proof and FOMO)"
    else:
        core = [_role(
            role_id="char_01",
            name="Main User",
            role="primary product user",
            description=(
                f"{product_info.get('age', '25')}-year-old {product_info.get('gender', 'person')} "
                f"wearing {product_info.get('outfit', 'casual everyday clothes')}"
            ),
            age=str(product_info.get("age", "25")),
            gender=str(product_info.get("gender", "person")),
            outfit=str(product_info.get("outfit", "casual everyday clothes")),
        )]

    if any(marker in text for marker in service_markers):
        supporting.append(_role(
            role_id="support_02",
            name="Service Specialist",
            role="professional support role",
            role_type="supporting",
            reference_required=False,
            description="professional service specialist, neat business casual outfit, reassuring expression",
            age="30",
            gender="person",
            outfit="neat business casual outfit",
        ))
    if any(marker in text for marker in crowd_markers):
        # Q-Final 强化：路人描述更具体自然，避免 AI 感
        # 明确说明：背景人物、自然动作、不看镜头、穿着日常服装
        ambient_entities.append(
            "a few natural background people in casual everyday clothes, "
            "doing normal everyday activities, not looking at camera, "
            "slightly out of focus in background, adds depth and realism to scene"
        )
    if any(marker in text for marker in pet_markers) and not any(c.get("name") == "Pet" for c in core):
        ambient_entities.append("household pet in the background")

    return {
        "core_characters": core[:4],
        "supporting_characters": supporting[:6],
        "ambient_entities": ambient_entities[:8],
        "rationale": rationale,
    }


def _format_cast_plan_for_prompt(cast_plan: dict) -> str:
    parts: List[str] = []
    core = cast_plan.get("core_characters") or []
    supporting = cast_plan.get("supporting_characters") or []
    ambient = cast_plan.get("ambient_entities") or []
    if core:
        parts.append("Core cast: " + "; ".join(
            f"{c.get('name')}: {c.get('description') or c.get('role')}" for c in core
        ))
    if supporting:
        parts.append("Supporting cast: " + "; ".join(
            f"{c.get('name')}: {c.get('description') or c.get('role')}" for c in supporting
        ))
    if ambient:
        parts.append("Ambient entities: " + "; ".join(str(item) for item in ambient))
    return " | ".join(parts)


def infer_characters_from_product(product_info: dict) -> List[dict]:
    """
    兼容旧调用方：只返回需要生成定妆照的核心角色。
    """
    return build_cast_plan(product_info).get("core_characters", [])


def _parse_description_to_bible(description: str, base: dict) -> CharacterBible:
    """从用户提供的描述字符串中提取结构化字段，回退到 base 默认值。"""
    desc = (description or "").lower()
    # 简单启发式提取（不引入 NLP 依赖）
    age_match = re.search(r"(\d+)[\s-]*year[\s-]*old", desc)
    age = str(age_match.group(1)) if age_match else base.get("age", "25")

    gender = base.get("gender", "person")
    if "woman" in desc or "female" in desc or "girl" in desc:
        gender = "female"
    elif "man" in desc or "male" in desc or "boy" in desc:
        gender = "male"

    ethnicity = ""
    for eth in ("asian", "caucasian", "african", "latina", "latino", "european", "indian"):
        if eth in desc:
            ethnicity = eth.capitalize()
            break

    hair_style = ""
    hair_color = ""
    hair_match = re.search(r"(long|short|medium|curly|straight|wavy)\s+([a-z]+)\s+hair", desc)
    if hair_match:
        hair_style = f"{hair_match.group(1)} {hair_match.group(2)} hair"
        hair_color = hair_match.group(2)
    elif "hair" in desc:
        hair_style = description[desc.find("hair") - 20:desc.find("hair") + 4].strip()

    return CharacterBible(
        id=base.get("id", "char_01"),
        name=base.get("name", "Character A"),
        age=age,
        gender=gender,
        ethnicity=ethnicity,
        hair_style=hair_style,
        hair_color=hair_color,
        outfit=base.get("outfit", "casual everyday clothes"),
        accessories=base.get("accessories", ""),
        facial_features=base.get("facial_features", ""),
        expression_baseline=base.get("expression_baseline", "neutral confident"),
    )


def build_character_bibles(product_info: dict, characters: Optional[list] = None) -> List[CharacterBible]:
    """
    从 product_info 和 characters 构建标准化角色圣经列表。

    Args:
        product_info: 产品信息字典（含 age, gender, outfit 等）
        characters: 角色列表，每个元素为 dict，可包含 name, description, age, gender, outfit 等

    Returns:
        List[CharacterBible]
    """
    bibles: List[CharacterBible] = []
    base_info = {
        "age": product_info.get("age", "25"),
        "gender": product_info.get("gender", "person"),
        "outfit": product_info.get("outfit", "casual everyday clothes"),
    }

    if not characters:
        # 单角色：从 product_info 构建默认主角色
        # 默认东亚面孔（抖音/中文广告面向中国市场），用户可通过 characters 参数自定义
        default_ethnicity = product_info.get("ethnicity") or "East Asian"
        bible = CharacterBible(
            id="char_01",
            name="Character A",
            age=base_info["age"],
            gender=base_info["gender"],
            ethnicity=default_ethnicity,
            hair_style="",
            hair_color="",
            outfit=base_info["outfit"],
            accessories="",
            facial_features="",
            expression_baseline="neutral confident",
        )
        bibles.append(bible)
        return bibles

    for idx, char in enumerate(characters, 1):
        char_id = char.get("id") or f"char_{idx:02d}"
        char_name = char.get("name") or f"Character {chr(64 + idx)}"
        description = char.get("description", "")

        if description:
            bible = _parse_description_to_bible(description, {
                **base_info,
                "id": char_id,
                "name": char_name,
                "outfit": char.get("outfit", base_info["outfit"]),
                "accessories": char.get("accessories", ""),
                "facial_features": char.get("facial_features", ""),
                "expression_baseline": char.get("expression_baseline", "neutral confident"),
            })
        else:
            bible = CharacterBible(
                id=char_id,
                name=char_name,
                age=char.get("age", base_info["age"]),
                gender=char.get("gender", base_info["gender"]),
                ethnicity=char.get("ethnicity", ""),
                hair_style=char.get("hair_style", ""),
                hair_color=char.get("hair_color", ""),
                outfit=char.get("outfit", base_info["outfit"]),
                accessories=char.get("accessories", ""),
                facial_features=char.get("facial_features", ""),
                expression_baseline=char.get("expression_baseline", "neutral confident"),
            )
        bibles.append(bible)

    return bibles


def build_product_bible(product_info: dict) -> ProductBible:
    """从 product_info 和 BRAND_CONFIG 构建标准化商品圣经。"""
    name = product_info.get("name", "product")
    ptype = product_info.get("type", "default")
    preset = get_preset(ptype)
    return ProductBible(
        name=name,
        category=ptype,
        packaging=BRAND_CONFIG.get("packaging_description", "consistent product packaging"),
        primary_color=BRAND_CONFIG.get("primary_color", "consistent brand colors"),
        secondary_color=BRAND_CONFIG.get("secondary_color", ""),
        shape=product_info.get("shape", ""),
        logo_description=BRAND_CONFIG.get("logo_description", "brand logo appears subtly"),
        usage_context=preset.get("scene", "natural setting"),
        key_selling_point=product_info.get("selling_point", "amazing feature"),
    )


def character_bible_to_prompt(bible: CharacterBible) -> str:
    """将角色圣经转换为标准化、信息密度高的外观描述字符串。"""
    parts: List[str] = []
    if bible.get("age") and bible.get("gender"):
        parts.append(f"{bible['age']}-year-old {bible['gender']}")
    elif bible.get("gender"):
        parts.append(bible["gender"])
    if bible.get("ethnicity"):
        parts.append(bible["ethnicity"])
    if bible.get("hair_style"):
        parts.append(bible["hair_style"])
    if bible.get("facial_features"):
        parts.append(bible["facial_features"])
    if bible.get("outfit"):
        parts.append(f"wearing {bible['outfit']}")
    if bible.get("accessories"):
        parts.append(bible["accessories"])
    if bible.get("expression_baseline"):
        parts.append(bible["expression_baseline"])
    return ", ".join(parts) if parts else "same person from reference image"


def product_bible_to_prompt(bible: ProductBible) -> str:
    """将商品圣经转换为标准化的商品外观描述字符串。"""
    parts: List[str] = [bible.get("name", "product")]
    if bible.get("packaging"):
        parts.append(bible["packaging"])
    if bible.get("shape"):
        parts.append(bible["shape"])
    if bible.get("primary_color"):
        parts.append(bible["primary_color"])
    if bible.get("logo_description"):
        parts.append(bible["logo_description"])
    return ", ".join(parts)


def generate_character_prompt(product_info: dict, bible: Optional[CharacterBible] = None) -> str:
    """生成角色定妆照 Prompt（主角色）。优先使用圣经，回退到零散字段。"""
    preset = get_preset(product_info.get("type", "default"))
    brand = BRAND_CONFIG.get("name", "brand")
    name = product_info.get("name", "product")

    if bible:
        description = character_bible_to_prompt(bible)
    else:
        age = product_info.get("age", "25")
        gender = product_info.get("gender", "女")
        style = product_info.get("style", preset["style"])
        outfit = product_info.get("outfit", "casual everyday clothes")
        description = f"{age}-year-old {gender}, {style} style, wearing {outfit}"

    prompt = (
        f"Character reference portrait for {name} advertisement, "
        f"{description}, "
        f"{preset['scene']}, "
        f"{preset['lighting']}, "
        f"half-body composition, high detail, clear facial features, "
        f"front-facing, neutral expression, 9:16 vertical, "
        f"{brand} brand aesthetic, {BRAND_CONFIG.get('primary_color', 'consistent brand colors')}"
    )
    return prompt


def generate_character_prompt_for_role(
    product_info: dict, description: str = "", bible: Optional[CharacterBible] = None
) -> str:
    """
    生成指定角色的定妆照 Prompt。优先使用圣经，回退到 description 或 product_info。

    Args:
        product_info: 产品信息字典
        description: 角色外貌描述（旧版兼容）
        bible: 角色圣经（推荐，优先使用）

    Returns:
        Prompt 字符串
    """
    preset = get_preset(product_info.get("type", "default"))
    brand = BRAND_CONFIG.get("name", "brand")
    name = product_info.get("name", "product")

    if bible:
        desc = character_bible_to_prompt(bible)
    elif description:
        desc = description
    else:
        return generate_character_prompt(product_info)

    prompt = (
        f"medium shot, full body visible from head to knees, subject filling 40-50% of frame, perfectly centered, "
        f"pure white seamless background, absolutely no objects no scenery, "
        f"soft even studio lighting, no harsh shadows, "
        f"{desc}, "
        f"front-facing, neutral natural expression, "
        f"high detail, crystal clear facial features, tack sharp focus on eyes, "
        f"9:16 vertical, character reference sheet quality"
    )
    return prompt


def generate_character_angle_prompt_for_role(
    product_info: dict, bible: Optional[CharacterBible] = None, angle: str = "full_body"
) -> str:
    """
    生成角色的多角度参考图 Prompt（全身/3/4侧面），用于提升视频中角色的3D一致性。
    性价比策略：多花0.05元生图，换视频角色一致性大幅提升。

    Args:
        product_info: 产品信息字典
        bible: 角色圣经
        angle: 角度类型 - "full_body"(全身正面) / "three_quarter"(3/4侧面) / "profile"(正侧面)

    Returns:
        Prompt 字符串
    """
    preset = get_preset(product_info.get("type", "default"))
    brand = BRAND_CONFIG.get("name", "brand")
    name = product_info.get("name", "product")

    desc = character_bible_to_prompt(bible) if bible else "person"

    angle_desc = {
        "full_body": "full-body shot, standing straight pose, showing entire body from head to feet, natural relaxed posture, body filling 70% of frame height, centered",
        "three_quarter": "three-quarter view, 45 degree angle, body slightly turned, showing both front and side profile, full body, centered composition",
        "profile": "side profile view, facing left, full body silhouette, clear outline of face and body, centered",
    }

    composition = angle_desc.get(angle, angle_desc["full_body"])

    prompt = (
        f"full body portrait, {composition}, perfectly centered, "
        f"pure white seamless background, absolutely no objects no scenery, "
        f"soft even studio lighting, no harsh shadows, "
        f"{desc}, "
        f"same person as reference, identical face and outfit and hair, "
        f"high detail, sharp focus, 9:16 vertical, "
        f"character reference sheet quality"
    )
    return prompt


# ── 参考图 Prompt 经验学习系统 ──
# 核心理念：从每次生成中学习，沉淀最优 prompt 模板，越用越准，第一次就生成高质量
_REF_EXPERIENCE_FILENAME = "ref_prompt_experience.json"
_REF_EXPERIENCE_TOP_N = 5  # 每个场景保留 TOP N 最优 prompt


def _ref_experience_key(product_info: dict, char_desc: str, angle: str) -> str:
    """生成经验库的 key：产品类型 + 角色性别 + 角度。"""
    ptype = product_info.get("type", "general") or "general"

    # 简单从描述里提取性别
    desc_lower = (char_desc or "").lower()
    if any(w in desc_lower for w in ["female", "woman", "girl", "lady", "mother", "mom", "daughter", "female", "女", "妈妈", "女儿", "女孩", "女士"]):
        gender = "female"
    elif any(w in desc_lower for w in ["male", "man", "boy", "guy", "father", "dad", "son", "male", "男", "爸爸", "儿子", "男孩", "男士"]):
        gender = "male"
    else:
        gender = "neutral"

    # 年龄段（注意：排除 year-old 中的 old）
    _desc_clean = desc_lower.replace("year-old", "").replace("years old", "")
    if any(w in _desc_clean for w in ["child", "kid", "baby", "toddler", "孩子", "小孩", "儿童", "婴儿"]):
        age_group = "child"
    elif any(w in _desc_clean for w in ["old", "senior", "elderly", "60", "70", "55", "老", "老年", "长辈", "爷爷", "奶奶"]):
        age_group = "senior"
    else:
        age_group = "adult"

    return f"{ptype}_{gender}_{age_group}_{angle}"


def _load_ref_experience(output_dir: Path) -> dict:
    """加载参考图 prompt 经验库。"""
    exp_path = output_dir / _REF_EXPERIENCE_FILENAME
    if not exp_path.exists():
        return {}
    try:
        import json
        with open(exp_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ref_experience(experience: dict, output_dir: Path) -> None:
    """保存经验库。"""
    try:
        import json
        exp_path = output_dir / _REF_EXPERIENCE_FILENAME
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(exp_path, "w", encoding="utf-8") as f:
            json.dump(experience, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _learn_from_ref_generation(
    output_dir: Path,
    product_info: dict,
    char_desc: str,
    angle: str,
    prompt: str,
    quality_score: float,
    passed: bool,
) -> None:
    """
    从一次参考图生成中学习，存入经验库。

    每个场景保留 TOP N 个质量分最高的 prompt。
    """
    if quality_score <= 0:
        return

    exp = _load_ref_experience(output_dir)
    key = _ref_experience_key(product_info, char_desc, angle)

    if key not in exp:
        exp[key] = []

    # 检查是否已有相同/高度相似的 prompt（避免重复）
    for entry in exp[key]:
        if entry["prompt"][:50] == prompt[:50]:  # 前50字相同就算相似
            if quality_score > entry["score"]:
                entry["score"] = quality_score
                entry["passed"] = passed
                entry["prompt"] = prompt
            _save_ref_experience(exp, output_dir)
            return

    # 加入新记录
    exp[key].append({
        "prompt": prompt,
        "score": round(quality_score, 3),
        "passed": passed,
    })

    # 按质量分降序排序，保留 TOP N
    exp[key].sort(key=lambda x: x["score"], reverse=True)
    exp[key] = exp[key][:_REF_EXPERIENCE_TOP_N]

    _save_ref_experience(exp, output_dir)


def _get_best_ref_prompt(
    output_dir: Path,
    product_info: dict,
    char_desc: str,
    angle: str,
    default_prompt: str,
    min_score: float = 0.6,
) -> str:
    """
    从经验库中获取同类场景下质量最高的 prompt。

    如果没有历史经验或历史最高分低于 min_score，返回默认 prompt。
    """
    exp = _load_ref_experience(output_dir)
    key = _ref_experience_key(product_info, char_desc, angle)

    if key not in exp or not exp[key]:
        return default_prompt

    best = exp[key][0]
    if best["score"] >= min_score:
        print(f"🧠 经验库命中：使用 {key} 场景下历史最优 prompt（质量分 {best['score']:.2f}）")
        return best["prompt"]

    return default_prompt


def _optimize_ref_prompt_from_quality(
    prompt: str,
    quality_result: "ReferenceImageCheckResult",
    angle: str = "front",
) -> str:
    """
    根据参考图质量检查结果，针对性优化 prompt，用于重生成。

    Args:
        prompt: 原始 prompt
        quality_result: 质量检查结果
        angle: 角度（front/full_body 等）

    Returns:
        优化后的 prompt
    """
    import re

    optimized = prompt

    # 1. 主体占比过小 → 放大主体
    if quality_result.subject_size_ratio and quality_result.subject_size_ratio < 0.2:
        if angle == "front":
            optimized = re.sub(
                r'face and upper body filling \d+% of frame',
                'extreme close-up, face and shoulders filling 85% of frame, tightly cropped',
                optimized,
                flags=re.IGNORECASE,
            )
        elif angle == "full_body":
            optimized = re.sub(
                r'body filling \d+% of frame height',
                'body filling 90% of frame height, head to toe tightly framed',
                optimized,
                flags=re.IGNORECASE,
            )
        optimized = 'huge subject, massive close-up, ' + optimized

    # 2. 背景过于复杂 → 强化纯色背景
    if quality_result.background_complexity and quality_result.background_complexity > 0.6:
        optimized = re.sub(
            r'pure white seamless background, absolutely no objects no scenery',
            'solid pure white background, completely blank empty, nothing in background, total void',
            optimized,
            flags=re.IGNORECASE,
        )
        optimized += ', no background elements whatsoever, flat white only'

    # 3. 主体偏移 → 强化居中
    if quality_result.subject_centered is False:
        optimized += ', perfectly centered composition, subject dead center'

    # 4. 水印/文字 → 加 negative 描述
    if quality_result.has_watermark:
        optimized += ', no watermark, no text, no logo, clean image'

    # 5. 人脸不清晰 → 强化面部细节
    if hasattr(quality_result, 'face_score') and quality_result.face_score and quality_result.face_score < 0.7:
        optimized += ', ultra sharp facial features, crystal clear face, hyperdetailed skin texture, tack sharp focus on eyes'

    return optimized


def _check_ref_image_quality(image_path: Path, image_type: str = "character"):
    """快速检查参考图质量，返回质量检查结果。"""
    try:
        from quality_gate import check_reference_image
        return check_reference_image(image_path, image_type)
    except Exception:
        return None


def _generate_multi_angle_character_refs(
    client: "KlingClient",
    product_info: dict,
    char_name: str,
    char_desc: str,
    char_bible: Optional[dict],
    char_ref_dir: Path,
    output_name: str,
    user_image_path: Optional[Path] = None,
    angles: Optional[List[str]] = None,
) -> dict:
    """
    生成单个角色的多角度参考图（正面 + 全身/3/4侧面）。
    性价比策略：多花0.05-0.1元生图，大幅提升视频中角色3D一致性，减少视频重抽。

    Args:
        client: KlingClient 实例
        product_info: 产品信息
        char_name: 角色名
        char_desc: 角色描述（兼容旧版）
        char_bible: 角色圣经（优先）
        char_ref_dir: 角色参考图目录
        output_name: 输出文件名前缀
        user_image_path: 用户上传的角色图（如果有则跳过生成正面图）
        angles: 要生成的角度列表，默认 ["front", "full_body"]

    Returns:
        角色参考图字典：{"name": str, "image_path": Path, "img_b64": str, "images": [Path,...], "img_b64_list": [str,...]}
    """
    if angles is None:
        angles = ["front", "full_body"]

    safe_name = char_name.replace(" ", "_")
    images = []
    b64_list = []

    # 正面图（用户上传或生成）
    if user_image_path and user_image_path.exists():
        front_path = user_image_path
        images.append(front_path)
        b64_list.append(base64.b64encode(front_path.read_bytes()).decode("utf-8"))
        print(f"✅ 角色 {char_name} 正面参考图（用户提供）：{front_path.name}")
    else:
        front_prompt = generate_character_prompt_for_role(product_info, char_desc, bible=char_bible)

        # 🧠 经验学习：先查经验库，有历史最优就直接用
        output_root = char_ref_dir.parent.parent if char_ref_dir else OUTPUT_DIR
        _desc = char_desc or (char_bible.get("description", "") if char_bible else "")
        front_prompt = _get_best_ref_prompt(
            output_dir=output_root,
            product_info=product_info,
            char_desc=_desc,
            angle="front",
            default_prompt=front_prompt,
        )

        cached_front = char_ref_dir / f"{output_name}_{safe_name}_front.png"
        front_manifest = _build_character_manifest(
            product_info=product_info,
            character={"name": char_name, "description": char_desc},
            prompt=front_prompt,
        )
        if (
            cached_front.exists()
            and cached_front.stat().st_size > 1024
            and _manifest_matches(cached_front, front_manifest)
        ):
            front_path = cached_front
            print(f"✅ 角色 {char_name} 正面参考图缓存命中：{front_path.name}")
        else:
            front_path = None
            best_path = None
            best_score = -1.0
            best_prompt = ""
            max_generate = 3
            last_error = None
            current_prompt = front_prompt
            current_save_path = cached_front

            for attempt in range(1, max_generate + 1):
                try:
                    gen_path = client.generate_character_ref(
                        prompt=current_prompt,
                        save_path=current_save_path,
                    )
                    # 质量自检
                    quality = _check_ref_image_quality(gen_path, "character")
                    score = 0.0
                    if quality:
                        subj = min(quality.subject_size_ratio or 0, 0.8) / 0.8 * 0.4
                        bg = max(0, 1.0 - (quality.background_complexity or 0.7)) / 0.7 * 0.3
                        face = (quality.face_score if hasattr(quality, 'face_score') and quality.face_score else 0) * 0.3
                        score = subj + bg + face

                        # 🧠 经验学习：每次生成都存入经验库
                        _learn_from_ref_generation(
                            output_dir=output_root,
                            product_info=product_info,
                            char_desc=char_desc or (char_bible.get("description", "") if char_bible else ""),
                            angle="front",
                            prompt=current_prompt,
                            quality_score=score,
                            passed=quality.passed,
                        )

                        if score > best_score:
                            best_score = score
                            best_path = gen_path
                            best_prompt = current_prompt

                        if quality.passed:
                            front_path = gen_path
                            _write_clip_manifest(front_path, front_manifest)
                            print(f"✅ 角色 {char_name} 正面参考图已生成（第{attempt}次，质量达标）：{front_path.name}")
                            break
                        elif attempt < max_generate:
                            optimized = _optimize_ref_prompt_from_quality(current_prompt, quality, "front")
                            if optimized != current_prompt:
                                print(f"🔧 角色 {char_name} 正面图质量不达标，优化 prompt 后第 {attempt + 1} 次生成...")
                                current_prompt = optimized
                                current_save_path = char_ref_dir / f"{output_name}_{safe_name}_front_v{attempt + 1}.png"
                                continue
                    else:
                        # 质量检查失败，直接用这张
                        front_path = gen_path
                        _write_clip_manifest(front_path, front_manifest)
                        print(f"✅ 角色 {char_name} 正面参考图已生成：{front_path.name}")
                        break
                except Exception as e:
                    last_error = e
                    if attempt < max_generate:
                        wait = 5 * attempt
                        print(f"⚠️  角色 {char_name} 正面图第 {attempt} 次失败：{e}，{wait}s 后重试")
                        time.sleep(wait)

            if front_path is None and best_path is not None:
                # 没达到完全合格，但有最佳版本，用最佳版本继续
                front_path = best_path
                _write_clip_manifest(front_path, front_manifest)
                print(f"⚠️  角色 {char_name} 正面参考图未完全达标，使用最佳版本（质量分 {best_score:.2f}）：{front_path.name}")

            if front_path is None:
                raise RuntimeError(f"角色 {char_name} 正面参考图生成失败：{last_error}")
        images.append(front_path)
        b64_list.append(base64.b64encode(front_path.read_bytes()).decode("utf-8"))

    # 额外角度图（全身、侧面等）
    for angle in angles:
        if angle == "front":
            continue
        angle_prompt = generate_character_angle_prompt_for_role(product_info, bible=char_bible, angle=angle)
        cached_angle = char_ref_dir / f"{output_name}_{safe_name}_{angle}.png"
        angle_manifest = _build_character_manifest(
            product_info=product_info,
            character={"name": char_name, "description": char_desc},
            prompt=angle_prompt,
        )
        if (
            cached_angle.exists()
            and cached_angle.stat().st_size > 1024
            and _manifest_matches(cached_angle, angle_manifest)
        ):
            angle_path = cached_angle
            print(f"✅ 角色 {char_name} {angle} 参考图缓存命中：{angle_path.name}")
        else:
            angle_path = None
            best_path = None
            best_score = -1.0
            max_generate = 2
            last_error = None
            current_prompt = angle_prompt
            current_save_path = cached_angle

            for attempt in range(1, max_generate + 1):
                try:
                    gen_path = client.generate_character_ref(
                        prompt=current_prompt,
                        save_path=current_save_path,
                    )
                    # 质量自检
                    quality = _check_ref_image_quality(gen_path, "character")
                    score = 0.0
                    if quality:
                        subj = min(quality.subject_size_ratio or 0, 0.8) / 0.8 * 0.4
                        bg = max(0, 1.0 - (quality.background_complexity or 0.7)) / 0.7 * 0.3
                        face = (quality.face_score if hasattr(quality, 'face_score') and quality.face_score else 0) * 0.3
                        score = subj + bg + face

                        if score > best_score:
                            best_score = score
                            best_path = gen_path

                        if quality.passed:
                            angle_path = gen_path
                            _write_clip_manifest(angle_path, angle_manifest)
                            print(f"✅ 角色 {char_name} {angle} 参考图已生成（第{attempt}次，质量达标）：{angle_path.name}")
                            break
                        elif attempt < max_generate:
                            optimized = _optimize_ref_prompt_from_quality(current_prompt, quality, angle)
                            if optimized != current_prompt:
                                print(f"🔧 角色 {char_name} {angle} 图质量不达标，优化 prompt 后第 {attempt + 1} 次生成...")
                                current_prompt = optimized
                                current_save_path = char_ref_dir / f"{output_name}_{safe_name}_{angle}_v{attempt + 1}.png"
                                continue
                    else:
                        angle_path = gen_path
                        _write_clip_manifest(angle_path, angle_manifest)
                        print(f"✅ 角色 {char_name} {angle} 参考图已生成：{angle_path.name}")
                        break
                except Exception as e:
                    last_error = e
                    if attempt < max_generate:
                        wait = 3 * attempt
                        print(f"⚠️  角色 {char_name} {angle} 图第 {attempt} 次失败：{e}，{wait}s 后重试")
                        time.sleep(wait)

            if angle_path is None and best_path is not None:
                angle_path = best_path
                _write_clip_manifest(angle_path, angle_manifest)
                print(f"⚠️  角色 {char_name} {angle} 参考图未完全达标，使用最佳版本（质量分 {best_score:.2f}）：{angle_path.name}")

            if angle_path is None:
                print(f"⚠️  角色 {char_name} {angle} 参考图生成失败，跳过（不影响主流程）：{last_error}")
                continue
        images.append(angle_path)
        b64_list.append(base64.b64encode(angle_path.read_bytes()).decode("utf-8"))

    return {
        "name": char_name,
        "image_path": images[0],
        "img_b64": b64_list[0],
        "images": images,
        "img_b64_list": b64_list,
    }


def _measure_mean_volume_db(path: Path) -> Optional[float]:
    """Measure mean audio level for adaptive voice/BGM balance."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", result.stderr)
        return float(match.group(1)) if match else None
    except Exception:
        return None


def _compute_bgm_mix_profile(bgm_mean_db: float, voice_mean_db: float) -> Dict[str, float]:
    """Keep music audible while maintaining a speech-first loudness gap."""
    target_gap_db = 6.0
    normalized_voice_db = -16.0
    desired_bgm_db = normalized_voice_db - target_gap_db
    gain_db = desired_bgm_db - bgm_mean_db
    base_volume = max(0.45, min(1.0, 10 ** (gain_db / 20.0)))
    return {
        "base_volume": round(base_volume, 3),
        "sidechain_ratio": 2.5,
        "target_gap_db": target_gap_db,
    }


def _probe_media_duration(path: Path, stream_selector: Optional[str] = None) -> float:
    cmd = ["ffprobe", "-v", "error"]
    if stream_selector:
        cmd += ["-select_streams", stream_selector, "-show_entries", "stream=duration"]
    else:
        cmd += ["-show_entries", "format=duration"]
    cmd += ["-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
        return float(result.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def _validate_cta_voiceover_contract(
    reference_profile: Dict[str, Any],
    voiceover_enabled: bool,
    cta_audio: Optional[Path],
) -> None:
    """Voiceover-enabled reference outros must contain a real CTA performance."""
    if not voiceover_enabled or not reference_profile.get("cta_text"):
        return
    if not cta_audio or not Path(cta_audio).exists():
        raise RuntimeError("启用口播的参考尾卡缺少 CTA 口播音频")
    duration = _probe_media_duration(Path(cta_audio), "a:0") or _probe_media_duration(Path(cta_audio))
    if duration < 0.25:
        raise RuntimeError("CTA 口播音频过短或不可解码")


def _validate_audio_covers_video(video: Path, maximum_gap: float = 0.35) -> None:
    video_duration = _probe_media_duration(video)
    audio_duration = _probe_media_duration(video, "a:0")
    if video_duration <= 0 or audio_duration <= 0 or video_duration - audio_duration > maximum_gap:
        raise RuntimeError(
            f"最终音轨未覆盖完整视频：视频 {video_duration:.2f}s，音频 {audio_duration:.2f}s"
        )


def _resolve_local_cta_background(
    *,
    local_asset_mode: bool,
    postproduction_contract: Optional[Dict[str, Any]],
    clean_video: Path,
) -> Optional[Path]:
    """Use the pre-subtitle picture layer so the tail card cannot freeze burned captions."""
    if (
        local_asset_mode
        and (postproduction_contract or {}).get("cta", {}).get("visual_mode")
        == "closing_frame_tail_card"
    ):
        return clean_video
    return None


def _append_outro_timing_to_voiceover_script(
    script_lines: List[Dict[str, Any]],
    outro_cue: str,
    main_duration: float,
    outro_duration: float,
) -> List[Dict[str, Any]]:
    """Add timing metadata for the CTA already authored inside voiceover_full."""
    combined = [dict(line) for line in script_lines]
    text = str(outro_cue or "").strip()
    if not text:
        return combined
    next_segment = max(
        (int(line.get("segment", index)) for index, line in enumerate(combined)),
        default=-1,
    ) + 1
    combined.append({
        "text": text,
        "start": float(main_duration),
        "end": float(main_duration) + float(outro_duration),
        "segment": next_segment,
        "is_outro": True,
    })
    return combined


def _mix_voiceover_with_bgm(
    video: Path,
    voiceover: Path,
    output: Path,
    bgm_ducking_volume: Optional[float] = None,
) -> Path:
    """
    将口播与视频中的 BGM 混合（#16 修复：真正的 sidechain ducking）

    使用 FFmpeg sidechaincompress：
    - 人声作为 sidechain 信号触发 BGM 压缩
    - 有人声时 BGM 自动压低约 10dB，无人声时自动恢复
    - 比固定音量降低更自然、更专业

    Args:
        video: 带 BGM 的视频
        voiceover: 口播音频
        output: 输出视频
        bgm_ducking_volume: BGM 基础音量比例（sidechain 压缩在此基础上额外压低）

    Returns:
        输出文件路径
    """
    # P1-6 修复：检查视频是否有音轨；无音轨时直接叠加口播，跳过 sidechain
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(video)],
            capture_output=True, text=True, timeout=10,
        )
        has_audio = probe.stdout.strip() != ""
    except Exception:
        has_audio = True  # 探测失败时保守地假设有音轨

    if not has_audio:
        # 无音轨：直接将口播合入，无需 sidechain
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-i", str(voiceover),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output),
        ]
        run_ffmpeg(cmd, timeout=120)
        return output

    bgm_mean_db = _measure_mean_volume_db(video)
    voice_mean_db = _measure_mean_volume_db(voiceover)
    mix_profile = _compute_bgm_mix_profile(
        bgm_mean_db if bgm_mean_db is not None else -22.0,
        voice_mean_db if voice_mean_db is not None else -16.0,
    )
    base_volume = float(bgm_ducking_volume) if bgm_ducking_volume is not None else mix_profile["base_volume"]
    sidechain_ratio = mix_profile["sidechain_ratio"]
    print(
        f"  🎚️ 智能混音：BGM {base_volume:.2f}，sidechain {sidechain_ratio:.1f}:1，"
        f"目标人声领先 {mix_profile['target_gap_db']:.1f}dB"
    )

    # P0 修复：真正的 sidechain ducking
    # [0:a] = 视频原音轨（BGM），[1:a] = 口播音频
    # sidechaincompress: 人声触发时 BGM 强力压低（ratio=12, threshold≈-26dBFS）
    # attack=5ms 快速响应，release=250ms 平滑恢复，knee=2 软拐点更自然
    # amix 后追加 alimiter 防止 BGM+口播叠加爆音
    filter_complex = (
        f"[0:a]volume={base_volume}[bgm_pre];"
        f"[1:a]loudnorm=I=-16:LRA=7:TP=-1.5,asplit=2[voice_sc][voice_mix];"
        f"[bgm_pre][voice_sc]sidechaincompress="
        f"threshold=0.08:ratio={sidechain_ratio}:attack=8:release=350:knee=3:makeup=1[bgm_duck];"
        f"[bgm_duck][voice_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amixed];"
        f"[amixed]alimiter=level_in=1:level_out=1:limit=-1dB:attack=5:release=10[aout]"
    )


    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video),
        "-i", str(voiceover),
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]

    run_ffmpeg(cmd, timeout=120)
    return output


def build_story_driven_prompts(
    ad_script: dict,
    product_info: dict,
    cinematic_style: str,
    char_refs: list,
    hook_type: str = "pain_point",
    character_bibles: Optional[List[CharacterBible]] = None,
    product_bible: Optional[ProductBible] = None,
    music_contract: Optional[dict] = None,
    rhythm_curve: Optional[Any] = None,
) -> list:
    """故事驱动的视频 Prompt 组装（改动2核心）

    以 ad_script 的故事世界为主干，按以下结构组装每段 prompt：
      [场景锚定] → [主角状态] → [叙事意图] → [电影风格元素] → [节奏驱动视觉参数] → [技术参数]

    相比旧的"模板 + scene_detail 末尾追加"方案，把故事信息前置，
    利用扩散模型"前重后轻"的注意力机制保证场景连续性和产品一致性。

    Args:
        ad_script: generate_ad_script 返回的脚本字典
        product_info: 产品信息字典
        cinematic_style: 导演风格 key（如 spielberg/hitchcock）
        char_refs: 角色参考图列表（仅用于提取角色描述）
        hook_type: 钩子类型
        character_bibles: 角色圣经列表（优先使用）
        product_bible: 商品圣经（优先使用）
        music_contract: 音乐合同（在场景锚定层注入节奏描述）
        rhythm_curve: RhythmCurve 对象（节奏分析结果，用于注入节奏驱动的视觉参数）

    Returns:
        按段落顺序的 prompt 字符串列表
    """
    segments = ad_script.get("segments", [])
    story_world = ad_script.get("story_world") or {}
    product_name = product_bible["name"] if product_bible else product_info.get("name", "product")
    cast_plan = story_world.get("cast_plan") if isinstance(story_world, dict) else None
    if not isinstance(cast_plan, dict):
        cast_plan = product_info.get("cast_plan") if isinstance(product_info.get("cast_plan"), dict) else {}

    # 提取故事世界核心描述
    location = story_world.get("location", "indoor scene, natural lighting")
    character_desc = story_world.get("character", "")
    if not character_desc:
        if cast_plan:
            character_desc = _format_cast_plan_for_prompt(cast_plan)
        elif character_bibles:
            if len(character_bibles) == 1:
                character_desc = character_bible_to_prompt(character_bibles[0])
            else:
                character_desc = ", ".join(
                    f"{b['name']}: {character_bible_to_prompt(b)}" for b in character_bibles
                )
        else:
            # 从 product_info 构建
            gender = product_info.get("gender", "person")
            age = product_info.get("age", "25")
            outfit = product_info.get("outfit", "casual clothes")
            character_desc = f"{age}-year-old {gender} wearing {outfit}"

    # 视觉承接词模板（非首段使用）
    # Q-Final 强化：加入光影一致性 + 空间连续性 + 角色动作连贯性描述
    # 扩散模型对开头注意力最强，把最强约束放在最前面
    _continuity_openers = [
        f"CONTINUITY: Same {location.split(',')[0]}, same lighting direction and color temperature, same camera perspective, seamless cut from previous shot, character action continues smoothly",
        f"CONTINUITY: Same scene, identical room setup and prop positions, same key light and shadow direction, camera moves smoothly within same space",
        f"CONTINUITY: Same {location.split(',')[0]}, same time of day and mood, consistent color palette, close-up cut within same environment, same character continues action",
        f"CONTINUITY: Same {location.split(',')[0]}, same lighting setup and atmosphere, matching shot composition, camera reframes on same character, action continues uninterrupted",
    ]

    prompts = []
    for i, seg in enumerate(segments):
        narrative = seg.get("narrative", "hook")
        scene_prompt_raw = seg.get("scene_prompt", "")
        product_visibility = seg.get("product_visibility", "absent")

        # ── 1. 场景锚定层（前置，扩散模型注意力最强区域）
        if i == 0:
            # 首段：直接声明故事世界
            scene_anchor = f"SCENE: {location}"
        else:
            # 非首段：用连续性承接词锚定同一场景
            opener = _continuity_openers[min(i - 1, len(_continuity_openers) - 1)]
            scene_anchor = f"{opener}, {location}"

        # ── 音乐合同节奏注入（利用扩散模型前重后轻注意力）──
        if music_contract:
            rhythm_tag = (
                f"{music_contract['mood']} {music_contract['genre']} energy, "
                f"{music_contract['bpm_min']}-{music_contract['bpm_max']} BPM rhythm"
            )
            scene_anchor = f"{rhythm_tag}, {scene_anchor}"

        # ── 2. 主角状态层（角色圣经特征 + 场景动作）
        char_action = ""
        raw_char_state = seg.get("scene_prompt", "")
        if "CHARACTER:" in raw_char_state:
            try:
                char_action = _extract_scene_prompt_field(raw_char_state, "CHARACTER")
            except Exception:
                char_action = ""
        # 角色一致性强化：把角色圣经的核心特征拼在动作前面
        # （Omni 无 image_fidelity，必须靠文字双重强化一致性）
        if character_bibles and len(character_bibles) > 0:
            main_bible = character_bibles[0]
            base_char_desc = character_bible_to_prompt(main_bible)
            if char_action:
                char_state = f"{base_char_desc}, {char_action}"
            else:
                char_state = base_char_desc
            # 多角色：补充次角色描述（如果有）
            if len(character_bibles) > 1:
                secondary_parts = []
                for j in range(1, min(len(character_bibles), 5)):
                    sec_bible = character_bibles[j]
                    sec_name = sec_bible.get("name", f"character {j+1}")
                    sec_desc = character_bible_to_prompt(sec_bible)
                    secondary_parts.append(f"{sec_name}: {sec_desc}")
                if secondary_parts:
                    char_state = char_state + ". Other characters in scene: " + "; ".join(secondary_parts)
        else:
            char_state = char_action if char_action else character_desc

        camera_from_script = _extract_scene_prompt_field(scene_prompt_raw, "CAMERA")
        product_from_script = _extract_scene_prompt_field(scene_prompt_raw, "PRODUCT")
        location_from_script = _extract_scene_prompt_field(scene_prompt_raw, "LOCATION")
        if location_from_script and i == 0:
            scene_anchor = f"SCENE: {location_from_script}"

        # ── 3. 产品在场层（产品圣经特征 + 在场状态）
        if product_bible:
            base_product_desc = product_bible_to_prompt(product_bible)
        else:
            base_product_desc = product_name

        if product_from_script:
            product_layer = f"{base_product_desc}, {product_from_script}"
        elif product_visibility == "prominent":
            product_layer = f"{base_product_desc}, clearly visible and prominent in frame, centered product hero shot, packaging unobstructed, product details sharp"
        elif product_visibility == "subtle":
            product_layer = f"{base_product_desc}, visible in background or partial view"
        else:
            product_layer = ""  # absent 时不强制插入产品词
        ambient_entities = cast_plan.get("ambient_entities", []) if cast_plan else []
        ambient_layer = ""
        if ambient_entities:
            ambient_layer = "background only, not main subject: " + ", ".join(
                str(item) for item in ambient_entities[:3]
            )

        # ── 4. 电影风格层（运镜+光影+构图+景深）
        camera_layer = ""
        if cinematic_style and cinematic_style != DEFAULT_CINEMATIC_STYLE:
            elements = build_cinematic_prompt_elements(cinematic_style, narrative)
            style_info = DEEP_CINEMATIC_STYLES.get(cinematic_style, {})
            style_name = style_info.get("name_en", "")
            camera_parts = []
            if style_name:
                camera_parts.append(f"{style_name}-style")
            if elements.get("shot_size"):
                camera_parts.append(elements["shot_size"])
            if elements.get("camera_movement"):
                camera_parts.append(elements["camera_movement"])
            if elements.get("lighting"):
                camera_parts.append(elements["lighting"])
            if elements.get("color_grade"):
                camera_parts.append(elements["color_grade"])
            if elements.get("dof"):
                camera_parts.append(elements["dof"])
            if camera_parts:
                camera_layer = ", ".join(camera_parts)
        else:
            # 无风格时使用段落默认镜头配置 + Q-Final 轻量级电影感增强
            # 不加特定导演风格，但加入基础电影质感：浅景深、轻微胶片颗粒、电影级调色
            from cinematic_language import build_cinematic_prompt_elements as _build
            elements = _build("", narrative)
            camera_parts = [
                v for k, v in elements.items()
                if k in ("shot_size", "camera_movement", "lighting") and v
            ]
            # Q-Final：基础电影质感（不挑风格，通用提升）
            # 浅景深+柔和散景 = 电影感第一要素
            camera_parts.append("shallow depth of field, soft bokeh")
            # 轻微胶片颗粒 = 数字感克星
            camera_parts.append("subtle film grain texture")
            # 电影级调色 = 色彩质感提升
            camera_parts.append("cinematic color grading, natural contrast")
            if camera_parts:
                camera_layer = ", ".join(camera_parts)
        if camera_from_script:
            camera_layer = f"{camera_from_script}, {camera_layer}" if camera_layer else camera_from_script

        # ── 4b. 节奏驱动视觉参数层（BPM + 情绪强度 → 运镜速度/对比度/饱和度/景深）
        rhythm_layer = ""
        if rhythm_curve and i < len(rhythm_curve.segments):
            try:
                from cinematic_language import build_rhythm_cinematic_prompt
                seg_rhythm = rhythm_curve.segments[i]
                rhythm_visual = build_rhythm_cinematic_prompt(
                    bpm=seg_rhythm.bpm,
                    emotion_level=seg_rhythm.emotion_level.value,
                    narrative_position=i,
                    total_segments=len(segments),
                )
                rhythm_parts = []
                if rhythm_visual.get("rhythm_phrase"):
                    rhythm_parts.append(rhythm_visual["rhythm_phrase"])
                if rhythm_visual.get("lighting_contrast"):
                    rhythm_parts.append(rhythm_visual["lighting_contrast"])
                if rhythm_visual.get("color_saturation"):
                    rhythm_parts.append(rhythm_visual["color_saturation"])
                if rhythm_parts:
                    rhythm_layer = ", ".join(rhythm_parts)
            except Exception:
                pass

        # ── 5. 组装最终 prompt（前重后轻顺序）
        parts = [scene_anchor]
        if char_state and char_state != location:
            parts.append(char_state)
        if product_layer:
            parts.append(product_layer)
        if ambient_layer:
            parts.append(ambient_layer)
        if camera_layer:
            parts.append(camera_layer)
        if rhythm_layer:
            parts.append(rhythm_layer)
        parts.append("9:16 vertical, high quality, cinematic")

        raw_prompt = ", ".join(p.strip().rstrip(",") for p in parts if p.strip())
        # 去重
        raw_prompt = _deduplicate_phrases(raw_prompt)
        prompts.append(raw_prompt)

    return prompts


def _extract_scene_prompt_field(scene_prompt: str, field_name: str) -> str:
    """从 LLM 场景描述中提取 LOCATION / CHARACTER / CAMERA / PRODUCT 字段。"""
    if not scene_prompt or not field_name:
        return ""
    marker = f"{field_name.upper()}:"
    upper = scene_prompt.upper()
    start = upper.find(marker)
    if start < 0:
        return ""
    raw = scene_prompt[start + len(marker):]
    next_positions = [
        raw.upper().find(f"{name}:")
        for name in ("LOCATION", "CHARACTER", "CAMERA", "PRODUCT")
        if name != field_name.upper()
    ]
    next_positions = [p for p in next_positions if p >= 0]
    end = min(next_positions) if next_positions else len(raw)
    return raw[:end].strip(" |,;")


def _compact_prompt_for_generation(prompt: str, *, max_chars: int = 1100) -> str:
    """最终调用 Kling 前压缩 Prompt，避免低价值泛词挤掉商品/动作信息。"""
    if len(prompt) <= max_chars:
        return prompt
    parts = [p.strip() for p in prompt.split(",") if p.strip()]
    if not parts:
        return prompt[:max_chars]
    low_value = {
        "high quality",
        "cinematic",
        "realistic",
        "ultra realistic",
        "professional",
        "beautiful",
    }
    kept: List[str] = []
    for part in parts:
        normalized = part.lower()
        if normalized in low_value and len(", ".join(kept + [part])) > max_chars * 0.75:
            continue
        candidate = ", ".join(kept + [part])
        if len(candidate) <= max_chars:
            kept.append(part)
    compacted = ", ".join(kept) if kept else prompt[:max_chars]
    return compacted[:max_chars].rstrip(" ,")


# ============================================================
# 首帧低成本预检（Keyframe Preflight）
# ============================================================

# 视频 Prompt 中的运镜词汇，在图片生成中无意义且可能干扰构图
_CAMERA_MOVEMENT_TERMS = {
    "dolly zoom", "push in", "pull back", "pull out", "slow push", "slow pull",
    "orbit", "orbiting", "pan", "panning", "track", "tracking", "tilt",
    "crane up", "crane down", "zoom in", "zoom out", "static shot",
    "steady cam", "steadycam", "handheld", "gimbal", "drone shot",
    "Hitchcock dolly zoom", "Kubrick one-point perspective",
}


def _sanitize_prompt_for_image_generation(prompt: str) -> str:
    """把视频 Prompt 清洗为适合图片生成的静态 Prompt，去掉运镜词汇保留内容描述。"""
    parts = [p.strip() for p in prompt.split(",") if p.strip()]
    cleaned: List[str] = []
    for part in parts:
        lowered = part.lower()
        # 去掉显式运镜短语（整段匹配）
        if any(term in lowered for term in _CAMERA_MOVEMENT_TERMS):
            continue
        # 去掉 <<<image_N>>> 标签（图片生成不需要文本绑定标签）
        if "<<<image_" in part:
            continue
        cleaned.append(part)
    return ", ".join(cleaned) if cleaned else prompt


def _preflight_keyframe_check(
    *,
    client,
    prompt: str,
    ref_images: list,
    narrative: str,
    product_image_path: Optional[Path],
    main_char_path: Optional[Path],
    save_path: Path,
    aspect_ratio: str = "9:16",
    image_fidelity: float = DEFAULT_IMAGE_FIDELITY,
    negative_prompt: str = NEGATIVE_PROMPT,
) -> Tuple[bool, List[str], Optional[Path]]:
    """
    首帧低成本预检：在付费视频生成前，用图片生成做干跑验证角色/商品一致性。

    Returns:
        (passed, issues, keyframe_path)
        passed: 是否通过预检
        issues: 未通过时的具体原因列表
        keyframe_path: 生成的首帧图片路径（通过时），失败时 None
    """
    issues: List[str] = []

    # 没有参考图时，首帧预检无法验证一致性，直接跳过
    has_product_ref = product_image_path and product_image_path.exists()
    has_char_ref = main_char_path and main_char_path.exists()
    if not has_product_ref and not has_char_ref:
        return True, [], None

    # 选择最关键的参考图传入图片生成
    # 优先按 narrative 决定：product-required 先验商品，其余先验角色
    ref_image_b64: Optional[str] = None
    ref_type: Optional[str] = None
    product_required = _is_product_required_narrative(narrative)
    if product_required and has_product_ref:
        ref_image_b64 = base64.b64encode(product_image_path.read_bytes()).decode("utf-8")
        ref_type = "subject"
    elif has_char_ref:
        ref_image_b64 = base64.b64encode(main_char_path.read_bytes()).decode("utf-8")
        ref_type = "face"
    elif has_product_ref:
        ref_image_b64 = base64.b64encode(product_image_path.read_bytes()).decode("utf-8")
        ref_type = "subject"

    # 清洗 Prompt 为静态图片版本
    image_prompt = _sanitize_prompt_for_image_generation(prompt)
    if not image_prompt:
        image_prompt = prompt

    try:
        # 网络错误自动重试（SSL EOF、连接超时、5xx 等临时错误）
        import time as _time
        _is_network_error = lambda e: any(
            kw in str(e).lower() for kw in [
                "ssl", "unexpected_eof", "connection", "timeout",
                "max retries", "eof occurred", "sslerror", "connectionerror",
                "read timed out", "500", "502", "503", "504", "429",
            ]
        )

        images = []
        image_url = None
        for _pf_attempt in range(1, 4):
            try:
                result = client.generate_image(
                    prompt=image_prompt,
                    negative_prompt=negative_prompt,
                    reference_image=ref_image_b64,
                    image_reference=ref_type,
                    image_fidelity=image_fidelity,
                    aspect_ratio=aspect_ratio,
                    resolution="1k",  # 首帧预检用 1k 足够，进一步降低成本
                    n=1,
                    wait=True,
                    timeout=90,
                )
                images = result.get("data", {}).get("task_result", {}).get("images", [])
                break
            except Exception as _pf_err:
                if _pf_attempt < 3 and _is_network_error(_pf_err):
                    _wait = 2 ** _pf_attempt
                    print(f"  ⚠️  首帧预检网络错误，{_wait}s 后重试（{_pf_attempt}/3）：{_pf_err}")
                    _time.sleep(_wait)
                    continue
                raise

        if not images:
            issues.append("首帧预检图片生成结果为空")
            return False, issues, None
        image_url = images[0].get("url")
        if not image_url:
            issues.append("首帧预检未获取图片 URL")
            return False, issues, None

        img_response = client.session.get(image_url, timeout=30)
        img_response.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(img_response.content)

        # 验证下载的是合法图片
        try:
            from PIL import Image as _PILImage
            import io as _io
            _PILImage.open(_io.BytesIO(img_response.content)).verify()
        except Exception as verify_err:
            issues.append(f"首帧预检下载内容不是有效图片：{verify_err}")
            return False, issues, None
    except Exception as e:
        # 网络错误导致的预检失败 → 跳过预检继续生成（网络问题≠质量问题）
        if _is_network_error(e):
            print(f"  ⚠️  首帧预检网络错误（{e}），跳过预检继续生成")
            return True, [], None
        issues.append(f"首帧预检图片生成失败：{e}")
        return False, issues, None

    # ── 轻量质检：与参考图比对 ──
    from quality_checker import _product_similarity, _character_similarity, _analyze_face_quality

    warnings: List[str] = []

    if product_required and has_product_ref:
        sim = _product_similarity(product_image_path, [save_path])
        if sim < 0.45:
            issues.append(f"商品首帧一致性不足（相似度 {sim:.2f} < 0.45），参考图约束可能未生效")
        elif sim < 0.60:
            warnings.append(f"商品首帧一致性较弱（相似度 {sim:.2f}），建议检查参考图质量")

    if has_char_ref and (not product_required or narrative in {"hook", "turning", "result", "review"}):
        sim = _character_similarity(main_char_path, [save_path])
        if sim < 0.40:
            issues.append(f"角色首帧一致性不足（相似度 {sim:.2f} < 0.40），参考图约束可能未生效")
        elif sim < 0.55:
            warnings.append(f"角色首帧一致性较弱（相似度 {sim:.2f}），建议检查参考图质量")

    # ── 人脸质量预检（有人物场景时）──
    # 低成本拦截明显人脸崩坏，避免浪费视频生成额度
    if narrative in {"hook", "turning", "result", "review"} and not issues:
        face_issues = _analyze_face_quality(save_path)
        if face_issues:
            # 首帧人脸有明显问题时作为警告返回（不直接拦截，避免误报）
            # 调用方可以根据警告决定是否重试首帧
            warnings.append(f"首帧人脸质量警告：{face_issues[0]}")

    if issues:
        return False, issues + warnings, save_path
    return True, warnings, save_path


def _preflight_generation_contract(
    *,
    product_info: dict,
    ad_script: dict,
    clip_prompts: List[str],
    product_image_path: Optional[Path],
    char_refs: list,
    strict_mode: bool,
) -> List[str]:
    """生成前合同预检：只拦零成本可发现的致命问题。

    评分类/建议类检查全部下沉到质量门（run_quality_gate），
    避免重复计算，保持职责单一。
    """
    issues: List[str] = []

    if not clip_prompts:
        issues.append("没有可生成的分镜 Prompt")

    for i, prompt in enumerate(clip_prompts, 1):
        if prompt.count("<<<image_") > MAX_REF_IMAGES:
            issues.append(f"分镜 {i} 引用了超过 {MAX_REF_IMAGES} 张参考图")

    if product_image_path and not Path(product_image_path).exists():
        issues.append(f"商品参考图不存在：{product_image_path}")

    for ref in char_refs or []:
        ref_path = ref.get("image_path")
        if ref_path and not Path(ref_path).exists():
            issues.append(f"角色参考图不存在：{ref_path}")

    if issues and strict_mode:
        raise RuntimeError("生成前合同预检未通过：" + "；".join(issues))

    for msg in issues:
        print(f"❌ 生成前合同问题：{msg}")
    if not issues:
        print("✅ 生成前合同预检通过：基础约束齐全，进入质量门详检")
    return issues


def _reference_strategy_for_narrative(
    narrative: str,
    *,
    product_available: bool,
    character_available: bool,
    continuity_available: bool,
    multi_character: bool = False,
    multi_angle_char: bool = False,
) -> List[str]:
    """
    按叙事任务选择最优参考图组合（性价比策略：用满5张换视频一次成功）。

    组合逻辑（MAX_REF_IMAGES=5）：
    - 人物驱动场景(hook/turning/result)：主角正面+全身(2) + 次角色(1) + 连续帧(1) + 产品(1,如有)
    - 产品驱动场景(showcase/cta/demo)：产品(1) + 主角正面+全身(2) + 连续帧(1) + 场景锚点(1)
    """
    normalized = (narrative or "").lower().strip()
    product_required = _is_product_required_narrative(normalized)
    roles: List[str] = []

    if product_required and product_available:
        roles.append("product")
        if character_available:
            roles.append("character_primary")
            if multi_angle_char:
                roles.append("character_angle")
        if continuity_available:
            roles.append("continuity")
        if multi_character and character_available:
            roles.append("character_secondary")
    else:
        if character_available:
            roles.append("character_primary")
            if multi_angle_char:
                roles.append("character_angle")
            if multi_character:
                roles.append("character_secondary")
        if continuity_available:
            roles.append("continuity")
        if product_available and normalized in {"turning", "solution", "result"}:
            roles.append("product")

    return roles[:MAX_REF_IMAGES]


def apply_cinematic_style(base_prompt: str, style_key: str, clip_type: str, narrative: str = "hook") -> str:
    """
    将电影风格注入基础 Prompt

    深度电影感版本：当风格在 DEEP_CINEMATIC_STYLES 中时，
    使用完整的镜头语言元素（景别、运镜、角度、光影、构图、景深、胶片、色彩）
    按优化后的结构重组 Prompt。

    Args:
        base_prompt: 基础 Prompt
        style_key: 电影风格键值（如 hitchcock/kubrick）
        clip_type: 片段类型（push/pull/orbit/match/light），向后兼容
        narrative: 叙事功能（hook/turning_point/showcase/result/cta），用于分镜节奏

    Returns:
        注入电影风格后的完整 Prompt
    """
    if style_key in (DEFAULT_CINEMATIC_STYLE, "none"):
        return base_prompt

    # 优先使用深度风格库
    deep_style = DEEP_CINEMATIC_STYLES.get(style_key)
    if deep_style:
        elements = build_cinematic_prompt_elements(style_key, narrative)
        style_name = deep_style.get("name_en", "")

        # Bug 1 fix: static 运镜时跳过 signature_camera，两者语义矛盾会让 AI 混乱
        # （turning_point / cta 段运镜是 locked-off static，signature 是动态描述）
        is_static_shot = "static" in elements.get("camera_movement", "").lower()
        signature_camera = (
            "" if is_static_shot
            else _get_signature_camera_move(deep_style, clip_type)
        )

        # 从 base_prompt 中提取核心主体内容（剥离镜头描述和光影描述）
        subject_content = _extract_subject_from_prompt(base_prompt)

        # #2 修复：showcase/cta 段必须以产品内容为主，电影风格只作轻量装饰
        # 其他叙事段（hook/turning_point/result）维持深度电影感顺序不变
        _product_first_narratives = {"showcase", "cta"}

        if narrative in _product_first_narratives:
            # 产品内容优先：主体 → 镜头描述（轻量）→ 光影 → 色调
            all_parts = []
            if subject_content:
                all_parts.append(subject_content)
            # 只注入景别+运镜，去掉 visual_key/film_look/composition 等纯氛围词
            camera_parts = []
            if style_name:
                camera_parts.append(f"{style_name}-style")
            if elements["shot_size"]:
                camera_parts.append(elements["shot_size"])
            if elements["camera_movement"]:
                camera_parts.append(elements["camera_movement"])
            if signature_camera:
                camera_parts.append(signature_camera)
            if camera_parts:
                all_parts.append(", ".join(camera_parts))
            if elements["lighting"]:
                all_parts.append(elements["lighting"])
            if elements["color_grade"]:
                all_parts.append(elements["color_grade"])
        else:
            # 原有深度电影感顺序（hook/turning_point/result）
            all_parts = []
            # 0. visual_key 整体视觉基调前置
            if elements.get("visual_key"):
                all_parts.append(elements["visual_key"])
            # 1. 风格标识 + 景别 + 运镜 + 角度
            camera_parts = []
            if style_name:
                camera_parts.append(f"{style_name}-style")
            if elements["shot_size"]:
                camera_parts.append(elements["shot_size"])
            if elements["camera_movement"]:
                camera_parts.append(elements["camera_movement"])
            if signature_camera:
                camera_parts.append(signature_camera)
            if elements["camera_angle"]:
                camera_parts.append(elements["camera_angle"])
            if camera_parts:
                all_parts.append(", ".join(camera_parts))
            # 2. 主体 + 动作 + 场景
            if subject_content:
                all_parts.append(subject_content)
            # 3. 光影
            if elements["lighting"]:
                all_parts.append(elements["lighting"])
            # 4. 构图 + 景深
            comp_dof = []
            if elements["composition"]:
                comp_dof.append(elements["composition"])
            if elements["dof"]:
                comp_dof.append(elements["dof"])
            if comp_dof:
                all_parts.append(", ".join(comp_dof))
            # 5. 胶片质感
            if elements["film_look"]:
                all_parts.append(elements["film_look"])
            # 6. 色彩调性
            if elements["color_grade"]:
                all_parts.append(elements["color_grade"])

        # 全文去重（去除重复的短语，如 shallow depth of field 出现两次）
        result = ", ".join(all_parts)
        result = _deduplicate_phrases(result)

        return result

    # 回退到旧版风格库（向后兼容）
    style = CINEMATIC_STYLES.get(style_key)
    if not style:
        return base_prompt

    cinematic_map = {
        "push": style.get("camera_push", ""),
        "pull": style.get("camera_pull", ""),
        "orbit": style.get("camera_orbit", ""),
        "match": style.get("transition_match", ""),
        "light": style.get("transition_light", ""),
    }

    cinematic_desc = cinematic_map.get(clip_type, "")
    if not cinematic_desc:
        return base_prompt

    return f"{cinematic_desc}, {base_prompt}"


def _extract_subject_from_prompt(prompt: str) -> str:
    """从 base_prompt 中提取主体动作和场景内容，去掉镜头描述和光影描述

    策略：找到第一个非镜头/非光影描述性的词开始截取。
    去掉常见的镜头关键词和光影关键词开头，保留剩余内容。

    Args:
        prompt: 原始 prompt 字符串

    Returns:
        提取后的主体内容字符串
    """
    # 常见的镜头描述前缀（需要去掉的）
    shot_prefixes = [
        "static shot", "slow push in", "slow pull back",
        "close-up shot", "close up", "extreme close-up",
        "medium shot", "long shot", "wide shot",
        "handheld", "tracking shot", "camera orbit around subject",
        "camera orbit around", "camera orbit",
        "slow push", "slow pull", "camera pushes", "camera pulls",
        "split screen", "cinematic framing",
        "glamour shot", "dynamic action shot",
        "macro shot", "medium close-up",
        "close-up on", "slow zoom", "static wide shot",
    ]

    # 常见的光影描述前缀（深度风格模式下需要去掉，避免与风格光影冲突）
    lighting_prefixes = [
        "natural lighting", "natural indoor lighting", "natural outdoor lighting",
        "soft lighting", "warm lighting", "cool lighting",
        "bright lighting", "dramatic lighting", "moody lighting",
        "studio lighting", "golden hour lighting", "golden hour",
        "soft natural light", "warm ambient light",
    ]

    result = prompt.strip()

    # 尝试去掉开头的镜头描述（最多去掉前 5 个匹配项）
    for _ in range(5):
        lowered = result.lower()
        matched = False
        for prefix in shot_prefixes:
            if lowered.startswith(prefix):
                result = result[len(prefix):].lstrip(" ,")
                matched = True
                break
        if not matched:
            break

    # 再去掉光影描述（可能穿插在中间，用逗号分隔的短语级去重）
    phrases = [p.strip() for p in result.split(",") if p.strip()]
    filtered_phrases = []
    for phrase in phrases:
        lowered = phrase.lower()
        is_lighting_desc = any(
            kw in lowered for kw in [
                "lighting", "light ", "light,", "lit ", "illuminated",
                "golden hour", "chiaroscuro", "neon", "backlit",
                "rim light", "key light", "fill light",
            ]
        )
        # 保留主体短语，去掉纯光影描述短语（长度 < 5 个词且含 lighting 关键词的视为纯光影描述）
        word_count = len(lowered.split())
        if is_lighting_desc and word_count <= 5:
            continue
        filtered_phrases.append(phrase)

    if filtered_phrases:
        result = ", ".join(filtered_phrases)

    return result if result else prompt


def _get_signature_camera_move(style: dict, clip_type: str) -> str:
    """从导演风格中提取标志性运镜特征词（简短版）

    用于在深度风格 Prompt 中保留导演的核心运镜辨识度，
    如希区柯克的 dolly zoom、库布里克的 one-point perspective 等。
    只返回真正独特的、不是通用运镜的特征词。

    Args:
        style: 导演风格字典（来自 DEEP_CINEMATIC_STYLES）
        clip_type: 片段类型（push/pull/orbit）

    Returns:
        标志性运镜特征词字符串（简短，2-6 个词），空串表示无独特特征
    """
    key_map = {
        "push": "camera_push",
        "pull": "camera_pull",
        "orbit": "camera_orbit",
    }
    desc_key = key_map.get(clip_type, "camera_push")
    full_desc = style.get(desc_key, "")

    if not full_desc:
        return ""

    # 通用运镜词（如果标志性特征只是这些，就跳过）
    generic_moves = {
        "push in", "push forward", "pull back", "pull out",
        "orbit", "tracking", "dolly in", "dolly out",
    }

    signature = ""
    # 策略：从冒号前的部分提取标志性特征（去掉导演名字）
    if ":" in full_desc:
        before_colon = full_desc.split(":", 1)[0].strip()
        style_name = style.get("name_en", "")
        if style_name and before_colon.lower().startswith(style_name.lower()):
            extracted = before_colon[len(style_name):].strip()
            if extracted and extracted.lower() not in generic_moves:
                signature = extracted
        elif before_colon and len(before_colon.split()) <= 6:
            if before_colon.lower() not in generic_moves:
                signature = before_colon

    # 如果没找到，从冒号后找第一个特征短语
    if not signature and ":" in full_desc:
        after_colon = full_desc.split(":", 1)[1].strip()
        if "," in after_colon:
            first_phrase = after_colon.split(",")[0].strip()
        else:
            first_phrase = after_colon.split(".")[0].strip()
        words = first_phrase.split()
        if len(words) > 6:
            first_phrase = " ".join(words[:6])
        # 检查是否是独特特征（不是通用运镜）
        if first_phrase and first_phrase.lower() not in generic_moves:
            signature = first_phrase

    # Bug 4 fix: 截断后若末尾是介词/冠词，说明句子不完整，向前裁到最近逗号或上一个完整词组
    _dangling_endings = {
        "to", "from", "with", "of", "in", "on", "at", "by", "for",
        "a", "an", "the", "and", "or", "as",
    }
    if signature:
        words = signature.split()
        while words and words[-1].lower().rstrip(",") in _dangling_endings:
            words.pop()
        signature = " ".join(words).rstrip(", ")

    # 最后过滤：如果 signature 太短（只有1个词且是通用词），跳过
    if len(signature.split()) <= 1 and signature.lower() in {
        "push", "pull", "orbit", "dolly", "track", "zoom",
    }:
        return ""

    return signature


def _deduplicate_phrases(text: str) -> str:
    """去除 Prompt 中重复的短语（不区分大小写）

    保留第一次出现的短语，移除后续重复项。
    按逗号分隔的短语级别去重。

    Args:
        text: 原始 Prompt 文本

    Returns:
        去重后的文本
    """
    phrases = [p.strip() for p in text.split(",")]
    seen = set()
    result = []

    for phrase in phrases:
        if not phrase:
            continue
        key = phrase.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(phrase)

    return ", ".join(result)


def _prompt_quality_gate(
    prompt: str,
    narrative: str,
    product_name: str,
    segment_index: int,
    max_chars: int = 1100,
) -> str:
    """Prompt 质量前置门禁（改动5）

    在 prompt 发送给可灵 API 之前执行三道检查：
    1. 去除重复短语（复用 _deduplicate_phrases）
    2. 长度超限时截断（保留前 max_chars 个字符，在逗号边界截断）
    3. showcase/result 段必须包含产品名，否则在开头补充

    Args:
        prompt: 原始 prompt
        narrative: 叙事段名称（hook/showcase/result 等）
        product_name: 产品名称
        segment_index: 片段序号（用于日志）
        max_chars: prompt 最大字符数（默认 550）

    Returns:
        处理后的 prompt
    """
    # 1. 去重
    cleaned = _deduplicate_phrases(prompt)

    # 2. 长度截断（在逗号边界截断，避免截断在词中间）
    if len(cleaned) > max_chars:
        truncated = cleaned[:max_chars]
        # 找最后一个逗号边界
        last_comma = truncated.rfind(",")
        if last_comma > max_chars // 2:
            truncated = truncated[:last_comma]
        cleaned = truncated.strip().rstrip(",")
        print(f"  ⚠️  [P-Gate] 片段 {segment_index} prompt 超长，已截断至 {len(cleaned)} 字符")

    # 3. 必要词检查：展示/结果段必须含产品名
    _product_mandatory = {"showcase", "result", "cta"}
    if (
        narrative in _product_mandatory
        and product_name
        and product_name.lower() not in cleaned.lower()
    ):
        cleaned = f"{product_name}, {cleaned}"
        print(f"  📌 [P-Gate] 片段 {segment_index}({narrative}) 已补充产品名 '{product_name}' 至 prompt 开头")

    print(f"  🔍 [P-Gate] 片段 {segment_index}({narrative}) prompt（{len(cleaned)}字）: {cleaned[:120]}{'...' if len(cleaned) > 120 else ''}")
    return cleaned


# 改动4 ────────────────────────────────────────────────────────────────────────
# 转场类型映射：按导演风格选择最匹配的转场
_STYLE_TRANSITION_MAP: dict[str, str] = {
    "hitchcock": "dissolve",       # 叠化——心理悬疑感
    "kubrick": "cut",              # 硬切——仪式感精准
    "spielberg": "push",           # 推进——情感建立
    "wong-kar-wai": "dissolve",    # 叠化——梦幻时间感
    "anderson": "whip_pan",        # 甩镜——韦斯·安德森标志
    "nolan": "cut",                # 硬切——张力
    "scorsese": "cut",             # 硬切——街头叙事节奏
    "denis-villeneuve": "dissolve",# 叠化——诗意缓慢
    "koreeda": "dissolve",         # 叠化——日常诗意
    "tarantino": "cut",            # 硬切——类型拼贴
}

# 音效包映射：按产品类型匹配对应音效风格
_PRODUCT_SFX_MAP: dict[str, str] = {
    # 英文 key（通用/外部输入兼容）
    "food": "refreshing",
    "beverage": "refreshing",
    "beauty": "soft",
    "skincare": "soft",
    "tech": "tech",
    "electronics": "tech",
    "fitness": "energetic",
    "fashion": "soft",
    "home": "neutral",
    "default": "neutral",
    # 中文 key（覆盖 config.py PRODUCT_PRESETS 所有 key）
    "食品": "refreshing",
    "饮料": "refreshing",
    "美妆": "soft",
    "小红书美妆": "soft",
    "个护": "soft",
    "数码": "tech",
    "科技": "tech",
    "健身": "energetic",
    "服装": "soft",
    "服饰": "soft",
    "家居": "neutral",
    "房产": "neutral",
    "医疗": "soft",
    "教育": "neutral",
}


def auto_match_av_style(product_info: dict, cinematic_style: str) -> dict:
    """主题自动匹配 AV 风格（改动4）

    根据导演风格和产品类型，自动推断最适合的：
    - BGM 关键词（从 DEEP_CINEMATIC_STYLES 读取，比固定列表更精准）
    - 转场类型（按风格映射）
    - 音效包（按产品类型映射）

    Args:
        product_info: 产品信息字典
        cinematic_style: 导演风格 key

    Returns:
        {
            "bgm_keywords": [...],
            "transition_type": "dissolve|cut|push|whip_pan",
            "sfx_pack": "refreshing|soft|tech|energetic|neutral",
            "mood": "..."
        }
    """
    product_type = (product_info.get("type") or "default").lower()

    # BGM 关键词：优先从导演风格库读取
    style_data = DEEP_CINEMATIC_STYLES.get(cinematic_style, {})
    bgm_keywords = style_data.get("bgm_keywords", [])
    mood = style_data.get("mood", "")

    # 若无风格或无 bgm_keywords，按产品类型兜底
    if not bgm_keywords:
        _type_bgm_fallback = {
            "food": ["refreshing", "upbeat", "cheerful"],
            "beverage": ["refreshing", "energetic"],
            "beauty": ["soft", "elegant", "feminine"],
            "skincare": ["calm", "natural", "gentle"],
            "tech": ["electronic", "modern", "dynamic"],
            "fitness": ["energetic", "motivational", "powerful"],
            "default": ["cinematic", "upbeat"],
        }
        bgm_keywords = _type_bgm_fallback.get(product_type, _type_bgm_fallback["default"])

    transition_type = _STYLE_TRANSITION_MAP.get(cinematic_style, "dissolve")
    sfx_pack = _PRODUCT_SFX_MAP.get(product_type, _PRODUCT_SFX_MAP["default"])

    return {
        "bgm_keywords": bgm_keywords,
        "transition_type": transition_type,
        "sfx_pack": sfx_pack,
        "mood": mood,
    }
# ─────────────────────────────────────────────────────────────────────────────


def generate_clip_prompts(
    product_info: dict,
    cinematic_style: str = DEFAULT_CINEMATIC_STYLE,
    clip_structure: Optional[list] = None,
    characters: Optional[list] = None,
    hook_type: str = DEFAULT_HOOK_TYPE,
    character_bibles: Optional[List[CharacterBible]] = None,
    product_bible: Optional[ProductBible] = None,
    music_contract: Optional[dict] = None,
) -> list:
    """
    生成分镜片段的 Prompts。支持角色圣经、商品圣经和音乐合同注入。

    Args:
        product_info: 产品信息字典
        cinematic_style: 电影风格键值
        clip_structure: 分镜结构配置列表（可选，默认使用 config.CLIP_STRUCTURE）
        characters: 角色名称列表（旧版兼容）
        hook_type: 钩子类型（用于替换第一个分镜的 prompt）
        character_bibles: 角色圣经列表（优先使用，解决多角色模糊描述）
        product_bible: 商品圣经（优先使用，统一商品外观描述）
        music_contract: 音乐合同（在 prompt 中注入节奏描述，让画面与音乐对齐）

    Returns:
        prompt 字符串列表
    """
    from config import CLIP_STRUCTURE

    if cinematic_style == "auto":
        try:
            from quality_gate import evolve_cinematic_style_recommendation
            product_type = product_info.get("type", "default")
            cinematic_style, evolve_info = evolve_cinematic_style_recommendation(product_type)
            method = evolve_info.get("method", "base_match")
            reason = evolve_info.get("reason", "")
            print(f"🎬 智能匹配电影风格：{cinematic_style}（进化模式：{method}）")
            if reason:
                print(f"   {reason}")
        except Exception:
            from quality_gate import smart_pick_cinematic_style
            product_type = product_info.get("type", "default")
            cinematic_style = smart_pick_cinematic_style(product_type)
            print(f"🎬 智能匹配电影风格：{cinematic_style}（产品类型：{product_type}）")

    preset = get_preset(product_info.get("type", "default"))
    name = product_info.get("name", "product")
    character = product_info.get("character", "same person from reference image")
    selling_point = product_info.get("selling_point", "amazing feature")
    brand = BRAND_CONFIG.get("name", "brand")
    brand_packaging = BRAND_CONFIG.get("packaging_description", "consistent packaging")
    brand_logo = BRAND_CONFIG.get("logo_description", "brand logo appears subtly")

    # 强一致性基础描述（所有片段共享）
    gender = product_info.get("gender", "person")
    primary_color = BRAND_CONFIG.get("primary_color", "consistent brand colors")
    character_consistency = CONSISTENCY_TEMPLATES["character"].format(gender=gender)
    product_consistency = CONSISTENCY_TEMPLATES["product"].format(
        name=name, brand_packaging=brand_packaging, brand=brand
    )
    brand_consistency = CONSISTENCY_TEMPLATES["brand"].format(
        brand=brand, primary_color=primary_color
    )

    # ── 角色描述：优先使用圣经，解决多角色模糊指代问题 ──
    if character_bibles:
        if len(character_bibles) == 1:
            character_descriptions = f"{character_bible_to_prompt(character_bibles[0])}, {character_consistency}"
        else:
            char_desc_parts = []
            for bible in character_bibles:
                char_desc_parts.append(f"{bible['name']}: {character_bible_to_prompt(bible)}")
            character_descriptions = ", ".join(char_desc_parts) + f", {character_consistency}"
    else:
        # 回退到旧版逻辑
        char_names = characters if characters else ["Character A"]
        if len(char_names) == 1:
            character_descriptions = f"{character}, {character_consistency}"
        else:
            char_desc_parts = []
            for i, cname in enumerate(char_names, 1):
                char_desc_parts.append(f"{cname}: consistent appearance, same person from reference image")
            character_descriptions = ", ".join(char_desc_parts) + f", {character_consistency}"

    # ── 商品描述：优先使用圣经 ──
    if product_bible:
        product_consistency = product_bible_to_prompt(product_bible)

    # 占位符替换上下文
    fmt_context = {
        "character": character,
        "name": name,
        "selling_point": selling_point,
        "preset_scene": preset["scene"],
        "preset_lighting": preset["lighting"],
        "demo_action": preset["demo_action"],
        "preset_result": preset["result"],
        "brand": brand,
        "brand_packaging": brand_packaging,
        "brand_logo": brand_logo,
        "character_consistency": character_descriptions,
        "product_consistency": product_consistency,
        "brand_consistency": brand_consistency,
        "product": name,
        "gender": product_info.get("gender", "person"),
    }

    # 构建基础 Prompts
    structure = clip_structure if clip_structure is not None else CLIP_STRUCTURE

    # 如果有钩子模板，替换第一个片段的 base_prompt
    hook_template = HOOK_TEMPLATES.get(hook_type)
    if hook_template and structure:
        structure = list(structure)  # 复制一份，避免修改原配置
        # #9 修复：同步写入 camera_type，不同 hook 类型触发不同镜头语言
        hook_camera = hook_template.get("camera_type", structure[0].get("camera", "push"))
        structure[0] = {
            **structure[0],
            "base_prompt": hook_template["hook_prompt"],
            "camera": hook_camera,
        }

    clips = []
    for clip_def in structure:
        base_prompt = clip_def["base_prompt"].format(**fmt_context)
        camera_type = clip_def["camera"]
        narrative = clip_def.get("narrative", "hook")
        clip_type = {
            "push": "push",
            "pull": "pull",
            "orbit": "orbit",
            # P1 修复：static 不应映射为 push，否则会向静止镜头段误射入 push-in 运镇描述
            "static": "static",
        }.get(camera_type, "push")
        final_prompt = apply_cinematic_style(base_prompt, cinematic_style, clip_type, narrative)

        # ── 音乐合同节奏注入：让画面运动与音乐 BPM/情绪对齐 ──
        if music_contract:
            rhythm_tag = (
                f"{music_contract['mood']} {music_contract['genre']} energy, "
                f"{music_contract['bpm_min']}-{music_contract['bpm_max']} BPM rhythm"
            )
            # 插入到 prompt 开头，利用扩散模型前重后轻的注意力机制
            final_prompt = f"{rhythm_tag}, {final_prompt}"

        clips.append(final_prompt)

    return clips


def generate_subtitles(
    product_info: dict,
    clip_duration: int = DEFAULT_VIDEO_DURATION,
    num_clips: int = 5,
    hook_type: str = DEFAULT_HOOK_TYPE,
    seg_indices: Optional[List[int]] = None,
) -> list:
    """
    生成字幕列表（模板化，旧版字幕生成器）

    .. deprecated::
        主流程已改用 ad_script.script_to_subtitles（支持 seg_indices 白名单）。
        此函数仅保留以兼容旧调用方，新代码不应再使用。

    Args:
        product_info: 产品信息字典
        clip_duration: 单片段时长（秒）
        num_clips: 片段数量
        hook_type: 钩子类型（用于替换第一个字幕）
        seg_indices: 实际成功的段索引白名单（0-based）。
            提供时按白名单过滤并按合并后顺序重新计算时间轴；
            None 表示不过滤（退化到旧行为）。

    Returns:
        字幕列表，每个元素包含 text/start/end
    """
    import warnings as _warnings
    _warnings.warn(
        "generate_subtitles 已废弃，请改用 ad_script.script_to_subtitles（支持 seg_indices 白名单）。",
        DeprecationWarning,
        stacklevel=2,
    )
    # 问题4：空列表保护——逻辑上不可能（success_count<2 已拦截），但防御性报错更清晰
    if seg_indices is not None and len(seg_indices) == 0:
        raise ValueError("seg_indices 不能为空列表，请传入有效的正整数段索引")

    selling_point = product_info.get("selling_point", "核心卖点")
    subtitles = []

    # 钩子字幕（第一个字幕）
    hook_template = HOOK_TEMPLATES.get(hook_type)
    hook_subtitle = hook_template.get("hook_subtitle", "你是不是也...？") if hook_template else "你是不是也...？"

    index_set = set(seg_indices) if seg_indices is not None else None
    pos_map = (
        {si: pos for pos, si in enumerate(sorted(seg_indices))}
        if seg_indices is not None
        else {}
    )

    for idx, tpl in enumerate(DEFAULT_SUBTITLE_TEMPLATE):
        seg_idx = tpl.get("segment", 0)
        if index_set is not None:
            if seg_idx not in index_set:
                continue
            pos = pos_map[seg_idx]
            seg_start = pos * clip_duration
        else:
            if seg_idx >= num_clips:
                continue
            seg_start = seg_idx * clip_duration
        text = tpl["text"].format(selling_point=selling_point)
        if idx == 0 and hook_template:
            text = hook_subtitle
        start = seg_start + clip_duration * tpl.get("ratio_start", 0)
        end = seg_start + clip_duration * tpl.get("ratio_end", 1.0)
        subtitles.append({"text": text, "start": start, "end": end})

    return subtitles


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="可灵 AI 抖音广告视频 - 一键成片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
示例：
  python one_click_create.py
  python one_click_create.py --style hitchcock
  python one_click_create.py --style kubrick --duration 8
  python one_click_create.py --product-image product.png --seed 42

一致性控制：
  --product-image  商品参考图路径（展示类片段自动使用）
  --image-fidelity 参考图 fidelity [0,1]，默认 {DEFAULT_IMAGE_FIDELITY}
  --human-fidelity 人物 fidelity [0,1]，默认 {DEFAULT_HUMAN_FIDELITY}
  --seed           随机种子基准（各片段自动递增）
        """,
    )
    parser.add_argument(
        "--style",
        default=DEFAULT_CINEMATIC_STYLE,
        choices=list(CINEMATIC_STYLES.keys()) + [DEFAULT_CINEMATIC_STYLE, "none"],
        help=f"电影风格（默认：{DEFAULT_CINEMATIC_STYLE}，auto=智能匹配，none=不使用）",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_VIDEO_DURATION,
        help=f"单片段时长（秒，默认：{DEFAULT_VIDEO_DURATION}）",
    )
    parser.add_argument(
        "--mode",
        default=DEFAULT_MODE,
        choices=["std", "pro", "4k"],
        help=f"生成模式（默认：{DEFAULT_MODE}）",
    )
    parser.add_argument(
        "--aspect-ratio",
        default=DEFAULT_ASPECT_RATIO,
        help=f"画面比例（默认：{DEFAULT_ASPECT_RATIO}）",
    )
    parser.add_argument(
        "--save",
        metavar="TEMPLATE.json",
        help="将当前产品信息和参数保存为模板 JSON",
    )
    parser.add_argument(
        "--load",
        metavar="TEMPLATE.json",
        help="从模板 JSON 加载产品信息和参数，跳过交互输入",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="指定输出名前缀；默认会复用同名生成资产",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="断点续跑：使用稳定输出名并复用已生成的角色图/片段候选（默认开启，可用 --no-resume 关闭）",
    )
    parser.add_argument(
        "--list-styles",
        action="store_true",
        help="列出所有可用的电影风格卡片",
    )
    parser.add_argument(
        "--dual-output",
        action="store_true",
        help="同时生成 9:16 和 16:9 两个版本",
    )
    parser.add_argument(
        "--target-duration",
        type=int,
        default=None,
        choices=[10, 15, 20, 25, 30, 60],
        help="目标总时长（秒），自动适配节奏模板（推荐：15/20/25/30）",
    )
    parser.add_argument(
        "--rhythm-style",
        default="moderate",
        choices=["fast", "moderate", "cinematic"],
        help="节奏风格：fast（快节奏）/ moderate（标准）/ cinematic（电影感），默认 moderate",
    )
    parser.add_argument(
        "--product-image",
        metavar="PATH",
        default=None,
        help="商品参考图路径（展示类片段自动使用，提升商品一致性）",
    )
    parser.add_argument(
        "--allow-no-product-image",
        action="store_true",
        help="允许不提供商品参考图继续生成（会降低产品露出质检可靠性）",
    )
    parser.add_argument(
        "--image-fidelity",
        type=float,
        default=DEFAULT_IMAGE_FIDELITY,
        help=f"参考图 fidelity [0,1]，默认 {DEFAULT_IMAGE_FIDELITY}",
    )
    parser.add_argument(
        "--human-fidelity",
        type=float,
        default=DEFAULT_HUMAN_FIDELITY,
        help=f"人物 fidelity [0,1]，默认 {DEFAULT_HUMAN_FIDELITY}",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子基准（各片段自动递增，保证一致性又有差异）",
    )
    parser.add_argument(
        "--hook",
        default=DEFAULT_HOOK_TYPE,
        choices=list(HOOK_TEMPLATES.keys()),
        help=f"钩子类型（默认：{DEFAULT_HOOK_TYPE}）",
    )
    parser.add_argument(
        "--list-hooks",
        action="store_true",
        help="列出所有可用的钩子模板",
    )
    parser.add_argument(
        "--voiceover",
        action="store_true",
        help="启用 AI 口播配音",
    )
    parser.add_argument(
        "--voiceover-style",
        default=DEFAULT_VOICEOVER_STYLE,
        choices=list(VOICEOVER_TEMPLATES.keys()),
        help=f"口播风格（默认：{DEFAULT_VOICEOVER_STYLE}）",
    )
    parser.add_argument(
        "--voice",
        default="auto",
        choices=["auto", *VOICE_PRESETS.keys()],
        help="音色（默认 auto：根据品类与最终口播智能选择）",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="列出所有可用的音色预设",
    )
    parser.add_argument(
        "--script-style",
        default=DEFAULT_SCRIPT_STYLE,
        choices=list(SCRIPT_STYLES.keys()),
        help=f"广告脚本风格（默认：{DEFAULT_SCRIPT_STYLE}）",
    )
    parser.add_argument(
        "--list-script-styles",
        action="store_true",
        help="列出所有可用的广告脚本风格",
    )
    parser.add_argument(
        "--ab-versions",
        type=int,
        default=1,
        help="生成 A/B 测试版本数量（1-3，默认 1）",
    )
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="严格模式：关键步骤失败时抛出异常而非静默降级（默认开启，可用 --no-strict 关闭）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制跳过 high 风险合规检测拦截（critical 级别始终拦截）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用 LLM 文案生成，强制走模板模式（覆盖 config.LLM_ENABLED）",
    )
    parser.add_argument(
        "--preview", "-p",
        action="store_true",
        help="快速预览模式：仅生成第 1 段（std 模式），保留完整后期效果（字幕/口播/BGM）",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="强制串行生成（默认并行；串行时每段用上一段尾帧，极致一致性）",
    )
    parser.add_argument(
        "--min-clips",
        type=int,
        default=3,
        help="最少成功片段数，低于此数则终止（默认 3，即 60%%）",
    )
    parser.add_argument(
        "--best-of",
        type=int,
        default=1,
        help="每个分镜最多生成候选数量（自适应 best-of），默认 1；只有首条不达标才补候选",
    )
    parser.add_argument(
        "--quality-frames",
        type=int,
        default=12,
        help="best-of 择优时的抽帧数量（默认 12）",
    )
    parser.add_argument(
        "--keep-candidates",
        action="store_true",
        help="保留 best-of 未被选中的候选片段（默认删除）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="并行生成时的最大线程数（默认 4）",
    )
    # P1-A：视频稳定化 + 去闪烁
    parser.add_argument(
        "--stabilize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启用视频稳定化 + 去闪烁（默认开启，可用 --no-stabilize 关闭）",
    )
    # P2-A：品牌开场/收尾动画
    parser.add_argument(
        "--brand-intro-outro",
        action="store_true",
        help="在成片首尾加入品牌开场（2s）和收尾动画（1.5s）",
    )
    # P2-B：A/B 测试维度
    parser.add_argument(
        "--ab-dim",
        type=str,
        default=None,
        choices=["hook", "style", "script"],
        help="A/B 测试维度（hook/style/script），与 --ab-versions 配合使用",
    )
    # P2-C：可灵 API 高级参数
    parser.add_argument(
        "--kling-model",
        type=str,
        default=None,
        help="指定可灵模型版本（如 kling-v2-master），默认使用 config 中的 KLING_VIDEO_MODEL",
    )
    parser.add_argument(
        "--multi-shot",
        action="store_true",
        help="启用可灵多镜头模式（intelligence 分镜），提升场景连贯性",
    )
    parser.add_argument(
        "--preflight-keyframe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="首帧预检：视频生成前先用低成本图片生成验证参考图一致性（默认开启，可用 --no-preflight-keyframe 关闭）",
    )
    parser.add_argument(
        "--image-first",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="图片先行：关键片段先多生图择优，再进入视频生成（默认开启，可用 --no-image-first 关闭）",
    )
    parser.add_argument(
        "--image-first-mode",
        choices=["minimal", "standard", "full"],
        default="standard",
        help="图片先行范围：minimal 只验最关键段，standard 验关键段，full 验所有段（默认 standard）",
    )
    parser.add_argument(
        "--image-first-variants",
        type=int,
        default=2,
        help="图片先行每个关键片段生成的候选图数量（默认 2）",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="手动模式：逐字段填写产品信息（默认为主题模式，输入一句话自动展开）",
    )
    parser.add_argument(
        "--local-assets",
        metavar="FOLDER",
        default=None,
        help="使用本地视频素材文件夹生成成片；跳过可灵视频生成，先分析素材再剪辑",
    )
    parser.add_argument(
        "--reference-video",
        metavar="PATH",
        default=None,
        help="同产品参考广告视频；提取带货结构、可见事实、连续口播节奏和尾卡形式",
    )
    return parser.parse_args()


def save_template(product_info: dict, args: argparse.Namespace, output_path: Path):
    """保存模板到 JSON 文件"""
    template = {
        "product_info": product_info,
        "args": {
            "style": args.style,
            "duration": args.duration,
            "mode": args.mode,
            "aspect_ratio": args.aspect_ratio,
            "dual_output": getattr(args, "dual_output", False),
            "image_fidelity": getattr(args, "image_fidelity", DEFAULT_IMAGE_FIDELITY),
            "human_fidelity": getattr(args, "human_fidelity", DEFAULT_HUMAN_FIDELITY),
            "seed": getattr(args, "seed", None),
            "product_image": str(args.product_image) if getattr(args, "product_image", None) else None,
            "allow_no_product_image": getattr(args, "allow_no_product_image", False),
            "hook_type": getattr(args, "hook", DEFAULT_HOOK_TYPE),
            "script_style": getattr(args, "script_style", DEFAULT_SCRIPT_STYLE),
            "use_voiceover": getattr(args, "voiceover", False),
            "voice": getattr(args, "voice", "auto"),
            "rhythm_style": getattr(args, "rhythm_style", "moderate"),
            "target_duration": getattr(args, "target_duration", None),
            "preview": getattr(args, "preview", False),
            "parallel": not getattr(args, "serial", False),
            "min_clips": getattr(args, "min_clips", 3),
            "best_of": getattr(args, "best_of", 1),
            "quality_frames": getattr(args, "quality_frames", 12),
            "keep_candidates": getattr(args, "keep_candidates", False),
            "max_workers": getattr(args, "max_workers", 4),
            "stabilize": getattr(args, "stabilize", True),
            "brand_intro_outro": getattr(args, "brand_intro_outro", False),
            "kling_model": getattr(args, "kling_model", None),
            "multi_shot": getattr(args, "multi_shot", False),
            "preflight_keyframe": getattr(args, "preflight_keyframe", True),
            "image_first": getattr(args, "image_first", True),
            "image_first_mode": getattr(args, "image_first_mode", "standard"),
            "image_first_variants": getattr(args, "image_first_variants", 2),
            "strict_mode": getattr(args, "strict", True),
            "force": getattr(args, "force", False),
            "no_llm": getattr(args, "no_llm", False),
            "output_name": getattr(args, "output_name", None),
            "resume": getattr(args, "resume", True),
            "local_assets": getattr(args, "local_assets", None),
            "reference_video": getattr(args, "reference_video", None),
        },
        "created_at": datetime.now().isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print(f"✅ 模板已保存：{output_path}")


def load_template(template_path: Path) -> tuple:
    """
    从 JSON 加载模板

    Returns:
        (product_info, args_dict)
    """
    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    product_info = template.get("product_info", {})
    args_dict = template.get("args", {})

    # 兼容旧模板（只有 4 个字段）
    args_dict.setdefault("dual_output", False)
    args_dict.setdefault("image_fidelity", DEFAULT_IMAGE_FIDELITY)
    args_dict.setdefault("human_fidelity", DEFAULT_HUMAN_FIDELITY)
    args_dict.setdefault("seed", None)
    args_dict.setdefault("product_image", None)
    args_dict.setdefault("allow_no_product_image", False)
    args_dict.setdefault("hook_type", DEFAULT_HOOK_TYPE)
    args_dict.setdefault("script_style", DEFAULT_SCRIPT_STYLE)
    args_dict.setdefault("use_voiceover", False)
    args_dict.setdefault("voice", "auto")
    args_dict.setdefault("rhythm_style", "moderate")
    args_dict.setdefault("target_duration", None)
    args_dict.setdefault("preview", False)
    args_dict.setdefault("parallel", True)
    args_dict.setdefault("min_clips", 3)
    args_dict.setdefault("best_of", 1)
    args_dict.setdefault("quality_frames", 12)
    args_dict.setdefault("keep_candidates", False)
    args_dict.setdefault("max_workers", 4)
    args_dict.setdefault("stabilize", True)
    args_dict.setdefault("brand_intro_outro", False)
    args_dict.setdefault("kling_model", None)
    args_dict.setdefault("multi_shot", False)
    args_dict.setdefault("preflight_keyframe", True)
    args_dict.setdefault("image_first", True)
    args_dict.setdefault("image_first_mode", "standard")
    args_dict.setdefault("image_first_variants", 2)
    args_dict.setdefault("strict_mode", True)
    args_dict.setdefault("force", False)
    args_dict.setdefault("no_llm", False)
    args_dict.setdefault("output_name", None)
    args_dict.setdefault("resume", True)
    args_dict.setdefault("local_assets", None)
    args_dict.setdefault("reference_video", None)

    return product_info, args_dict


class MusicContract(TypedDict):
    """音乐合同：脚本阶段确定的音乐策略约束"""
    bpm_min: int
    bpm_max: int
    mood: str        # upbeat, emotional, suspenseful, dreamy, cool, warm, calm
    genre: str       # electronic, orchestral, pop, lofi, jazz, acoustic
    energy: str      # high, medium, low
    recommended_pace: str  # fast, moderate, cinematic
    intro_type: str  # immediate, buildup, fade_in
    source: str
    visual_brightness: str
    visual_contrast: str
    transition_base_duration: float
    sfx_intensity: str
    semantic_tone: str
    story_role_counts: Dict[str, int]


def build_music_contract(
    product_info: dict,
    cinematic_style: str = "none",
    asset_creative_profile: Optional[Dict[str, Any]] = None,
) -> MusicContract:
    """
    从产品类型、受众和电影风格构建音乐合同。
    在脚本阶段就确定音乐策略，让分镜、口播、BGM 三方对齐。
    """
    ptype = product_info.get("type", "default")
    audience = product_info.get("audience", "18-35")

    # ── 基础映射：产品类型 → mood / genre / energy ──
    _PRODUCT_MUSIC_MAP = {
        "美妆": {"mood": "upbeat", "genre": "pop", "energy": "high"},
        "科技": {"mood": "cool", "genre": "electronic", "energy": "medium"},
        "食品": {"mood": "warm", "genre": "acoustic", "energy": "medium"},
        "家居": {"mood": "calm", "genre": "lofi", "energy": "low"},
        "服装": {"mood": "upbeat", "genre": "pop", "energy": "high"},
        "医疗": {"mood": "calm", "genre": "acoustic", "energy": "low"},
        "教育": {"mood": "warm", "genre": "acoustic", "energy": "medium"},
        "房产": {"mood": "grand", "genre": "orchestral", "energy": "medium"},
    }
    base = _PRODUCT_MUSIC_MAP.get(ptype, {"mood": "upbeat", "genre": "pop", "energy": "medium"})

    # ── 电影风格叠加 ──
    _STYLE_MOOD_OVERRIDE = {
        "hitchcock": {"mood": "suspenseful", "genre": "orchestral", "intro_type": "buildup"},
        "kubrick": {"mood": "cold", "genre": "orchestral", "intro_type": "fade_in"},
        "spielberg": {"mood": "emotional", "genre": "orchestral", "intro_type": "buildup"},
        "miyazaki": {"mood": "dreamy", "genre": "lofi", "intro_type": "fade_in"},
        "wongkarwai": {"mood": "moody", "genre": "jazz", "intro_type": "fade_in"},
        "zhangyimou": {"mood": "grand", "genre": "orchestral", "intro_type": "buildup"},
    }
    style_override = _STYLE_MOOD_OVERRIDE.get(cinematic_style, {})

    mood = style_override.get("mood", base["mood"])
    genre = style_override.get("genre", base["genre"])
    energy = str((asset_creative_profile or {}).get("energy") or base["energy"])
    intro_type = str(
        style_override.get("intro_type")
        or (asset_creative_profile or {}).get("intro_type")
        or "immediate"
    )

    # ── 受众年龄 → BPM 范围 ──
    _AUDIENCE_BPM = {
        "18-25": (120, 140),
        "25-35": (100, 120),
        "35-45": (90, 110),
        "45+": (80, 100),
    }
    bpm_min, bpm_max = _AUDIENCE_BPM.get(audience, (100, 120))

    # energy 影响 pace 和 BPM
    _ENERGY_PACE = {"high": "fast", "medium": "moderate", "low": "cinematic"}
    recommended_pace = str(
        (asset_creative_profile or {}).get("recommended_pace")
        or _ENERGY_PACE.get(energy, "moderate")
    )

    if asset_creative_profile:
        bpm_min = int(asset_creative_profile.get("bpm_min") or bpm_min)
        bpm_max = int(asset_creative_profile.get("bpm_max") or bpm_max)
    elif energy == "high":
        bpm_min = min(140, bpm_min + 10)
        bpm_max = min(150, bpm_max + 10)
    elif energy == "low":
        bpm_min = max(60, bpm_min - 10)
        bpm_max = max(80, bpm_max - 10)

    return MusicContract(
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        mood=mood,
        genre=genre,
        energy=energy,
        recommended_pace=recommended_pace,
        intro_type=intro_type,
        source=str((asset_creative_profile or {}).get("source") or "product_profile"),
        visual_brightness=str((asset_creative_profile or {}).get("brightness") or "unknown"),
        visual_contrast=str((asset_creative_profile or {}).get("contrast") or "unknown"),
        transition_base_duration=float(
            (asset_creative_profile or {}).get("transition_base_duration") or 0.4
        ),
        sfx_intensity=str((asset_creative_profile or {}).get("sfx_intensity") or "moderate"),
        semantic_tone=str((asset_creative_profile or {}).get("semantic_tone") or "product_demo"),
        story_role_counts=dict((asset_creative_profile or {}).get("story_role_counts") or {}),
    )


def _get_primary_char_for_clip(
    idx: int, clip_prompt: str, ad_script: dict, character_bibles: list
) -> int:
    """
    按分镜内容检测出场角色，返回 char_refs 中对应角色的索引。

    启发式策略（按优先级）：
    1. 检查 ad_script segment 的 scene_prompt 中是否出现角色名
    2. 检查 clip_prompt 中是否出现角色名
    3. 默认回退主角色（索引 0）
    """
    if not character_bibles or len(character_bibles) <= 1:
        return 0

    seg_i = idx - 1
    seg_text = ""
    try:
        segs = ad_script.get("segments", []) if isinstance(ad_script, dict) else []
        if 0 <= seg_i < len(segs):
            seg = segs[seg_i]
            seg_text = " ".join([
                seg.get("scene_prompt", ""),
                seg.get("dialogue", ""),
                seg.get("caption", ""),
            ])
    except Exception:
        pass

    # 合并检测文本（不区分大小写）
    haystack = (seg_text + " " + (clip_prompt or "")).lower()

    # 优先匹配非主角色（索引 > 0），如果匹配到则使用该角色
    for i in range(1, len(character_bibles)):
        name = character_bibles[i].get("name", "").lower().strip()
        if name and name in haystack:
            return i

    # 检查主角色是否被明确提到（如果主角名不在，但其他角色在，上面已返回）
    primary_name = character_bibles[0].get("name", "").lower().strip()
    if primary_name and primary_name in haystack:
        return 0

    # 默认回退主角色
    return 0


def run_generation_pipeline(
    product_info: dict,
    style: str = DEFAULT_CINEMATIC_STYLE,
    duration: int = DEFAULT_VIDEO_DURATION,
    mode: str = DEFAULT_MODE,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    output_name: str = None,
    dual_output: bool = False,
    product_image: Optional[Path] = None,
    allow_no_product_image: bool = False,
    image_fidelity: float = DEFAULT_IMAGE_FIDELITY,
    human_fidelity: float = DEFAULT_HUMAN_FIDELITY,
    seed: Optional[int] = None,
    characters: Optional[list] = None,
    output_dir: Path = OUTPUT_DIR,
    hook_type: str = DEFAULT_HOOK_TYPE,
    use_voiceover: bool = False,
    voiceover_style: str = DEFAULT_VOICEOVER_STYLE,
    voice: str = "auto",
    script_style: str = DEFAULT_SCRIPT_STYLE,
    strict_mode: bool = True,
    force: bool = False,
    target_duration: Optional[int] = None,
    rhythm_style: str = "moderate",
    parallel: bool = True,
    min_clips: int = 3,
    best_of: int = 1,
    quality_frames: int = 12,
    keep_candidates: bool = False,
    preview: bool = False,
    max_workers: int = 4,
    stabilize: bool = True,
    brand_intro_outro: bool = False,
    kling_model: Optional[str] = None,
    multi_shot: bool = False,
    preflight_keyframe: bool = True,
    image_first: bool = True,
    image_first_mode: str = "standard",
    image_first_variants: int = 2,
    local_assets: Optional[Path] = None,
    reference_video: Optional[Path] = None,
) -> dict:
    """
    核心生成流水线（无交互逻辑）

    Args:
        product_info: 产品信息字典
        style: 电影风格键值
        duration: 单片段生成时长（秒，传给可灵 API 的基础值）
        mode: 生成模式（std/pro/4k）
        aspect_ratio: 画面比例
        output_name: 输出文件名前缀（可选，自动生成时间戳）
        dual_output: 是否同时生成 16:9 版本
        product_image: 商品参考图路径（可选，展示类片段将使用）
        image_fidelity: 参考图 fidelity [0,1]，默认 0.9
        human_fidelity: 人物 fidelity [0,1]，默认 0.9
        seed: 随机种子基准（可选，各片段自动递增以保证一致性又有差异）
        characters: 角色列表，每个元素为 dict，包含：
            - name: 角色名称（用于 prompt 中固定指代）
            - description: 外貌描述
            - image_path: 可选，已有定妆照路径
        output_dir: 输出根目录，默认 OUTPUT_DIR
        hook_type: 钩子类型（question/shocking/before_after/demonstration/story/challenge/celeb_style/pain_point）
        use_voiceover: 是否启用 AI 口播配音
        voiceover_style: 口播风格（standard/emotional/energetic/professional/storytelling）
        voice: 音色（female_young/female_warm/male_pro/male_magnetic/energetic_female）
        script_style: 广告脚本风格（pain_point_solution/before_after/storytelling/demonstration/social_proof）
        force: 为 True 时跳过 high 风险合规拦截（默认 False；critical 风险始终拦截）
        target_duration: 目标总时长（秒），None 时使用 duration × 片段数 的默认计算
        rhythm_style: 节奏风格：fast / moderate / cinematic
        parallel: 是否并行生成第 2-N 段（默认 True，更快；设为 False 串行，极致一致性）
        min_clips: 最少成功片段数，低于此数则终止（默认 3，即 60%）
        best_of: 每个分镜生成候选数量（best-of），自动择优（默认 1）
        quality_frames: best-of 择优时的抽帧数量（默认 12）
        keep_candidates: 是否保留未被选中的候选片段（默认 False）
        preview: 预览模式：仅生成第 1 段（std 模式），保留完整后期效果（字幕/口播/BGM）
        max_workers: 并行生成时的最大线程数（默认 4）

    Returns:
        {
            "final_path": Path,           # 9:16 最终成片
            "wide_path": Path | None,     # 16:9 版本（dual_output=True 时）
            "output_name": str,           # 本次输出文件名前缀
        }

    Raises:
        RuntimeError: 任何步骤失败时抛出异常
    """
    if output_name is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = _safe_output_stem(product_info.get("name", "product"))
        output_name = f"{safe_name}_{timestamp}"

    output_dir = Path(output_dir)
    ensure_dirs(output_dir)
    char_ref_dir = output_dir / "character_ref"
    clips_dir = output_dir / "clips"
    final_dir = output_dir / "final"

    local_asset_mode = local_assets is not None
    client = None if local_asset_mode else KlingClient()
    local_asset_index = None
    local_asset_edit_report = None
    local_asset_frame_evidence_report = None
    asset_creative_profile = None
    local_story_contract = None
    postproduction_contract = None
    postproduction_contract_path = None
    local_one_take_timeline = None
    local_one_take_master: Optional[Path] = None
    local_edit_semantic_indices: List[int] = []
    if local_asset_mode:
        print(f"🎞️ 本地视频混剪：{Path(local_assets).expanduser()}")
        print("   素材理解驱动脚本、口播、选片与后期配置")
    reference_profile = None
    if reference_video:
        from local_asset_pipeline import analyze_reference_ad, bind_reference_profile_to_product
        print(f"🎯 分析同产品参考广告：{Path(reference_video).expanduser()}")
        reference_profile = bind_reference_profile_to_product(
            analyze_reference_ad(Path(reference_video)),
            product_info,
        )
        product_info["reference_ad_profile"] = reference_profile
        rejected_reference_claims = reference_profile.get("rejected_reference_claims") or []
        if rejected_reference_claims:
            print(f"🛡️  参考片事实交叉核验：拒绝 {len(rejected_reference_claims)} 条无独立产品证据的文字")
        if local_asset_mode and target_duration is None:
            target_duration = max(15, round(float(reference_profile.get("recommended_main_duration") or 16.0)))
        print(
            f"✅ 参考广告合同：主内容约 {reference_profile.get('recommended_main_duration', 0):.1f}s，"
            f"素材段 {reference_profile.get('recommended_material_segments', 5)}，"
            f"尾卡约 {reference_profile.get('outro_duration', 0):.1f}s"
        )

    # ── 智能电影风格匹配（零配置自动选择，自我进化）──
    # 当 style 为默认值或 auto 时，根据品类+历史成功率智能推荐
    _style_auto_triggered = style in ("auto", DEFAULT_CINEMATIC_STYLE)
    if _style_auto_triggered:
        try:
            from quality_gate import evolve_cinematic_style_recommendation
            product_type = product_info.get("type", "default")
            style, evolve_info = evolve_cinematic_style_recommendation(
                product_category=product_type,
                ad_script=None,
                story_world_desc=product_info.get("scene_description", ""),
            )
            method = evolve_info.get("method", "base_match")
            reason = evolve_info.get("reason", "")
            print(f"🎬 智能匹配电影风格：{style}（进化模式：{method}）")
            if reason:
                print(f"   {reason}")
        except Exception:
            from quality_gate import smart_pick_cinematic_style
            product_type = product_info.get("type", "default")
            style = smart_pick_cinematic_style(product_type)
            print(f"🎬 智能匹配电影风格：{style}（产品类型：{product_type}）")

    # ── 智能口播风格匹配（零配置自动选择，自我进化）──
    if voiceover_style in ("auto", "standard"):
        try:
            from quality_gate import evolve_voiceover_style_recommendation
            product_type = product_info.get("type", "default")
            voiceover_style, evolve_info = evolve_voiceover_style_recommendation(product_type)
            method = evolve_info.get("method", "base_match")
            reason = evolve_info.get("reason", "")
            print(f"🎤 智能匹配口播风格：{voiceover_style}（进化模式：{method}）")
            if reason:
                print(f"   {reason}")
        except Exception:
            from quality_gate import smart_pick_voiceover_style
            product_type = product_info.get("type", "default")
            voiceover_style = smart_pick_voiceover_style(product_type)
            print(f"🎤 智能匹配口播风格：{voiceover_style}（产品类型：{product_type}）")

    # ── 预览模式：强制 std + 仅 1 段（后期处理保留，方便预览完整效果）──
    if preview:
        mode = "std"
    effective_kling_model = kling_model or KLING_VIDEO_MODEL

    cast_plan = (
        {"core_characters": [], "supporting_characters": [], "ambient_entities": [], "rationale": "local_asset_mode"}
        if local_asset_mode
        else build_cast_plan({**product_info, "characters": characters} if characters else product_info)
    )
    core_characters = cast_plan.get("core_characters", [])
    product_info["cast_plan"] = cast_plan
    product_info["characters"] = core_characters
    product_info["supporting_characters"] = cast_plan.get("supporting_characters", [])
    product_info["ambient_entities"] = cast_plan.get("ambient_entities", [])
    if not local_asset_mode:
        print(
            f"🎭 角色计划：核心 {len(core_characters)}，"
            f"配角 {len(product_info['supporting_characters'])}，"
            f"环境实体 {len(product_info['ambient_entities'])}"
        )
        print(f"    推断依据：{cast_plan.get('rationale', 'n/a')}")

    # ============================================================
    # 第一步：生成所有角色定妆照（多角度，提升视频一致性）
    # ============================================================
    char_refs = []  # List[dict]: {"name": str, "image_path": Path, "img_b64": str, "images": [Path, ...], "img_b64_list": [str, ...]}

    # ── 构建角色圣经和商品圣经（标准化资产描述）──
    character_bibles = build_character_bibles(product_info, core_characters)
    product_bible = build_product_bible(product_info) if not local_asset_mode else None
    if not local_asset_mode:
        print(f"📖 角色圣经：{len(character_bibles)} 个角色")
        for bible in character_bibles:
            print(f"    [{bible['id']}] {bible['name']}: {character_bible_to_prompt(bible)[:60]}...")
    if product_bible:
        print(f"📦 商品圣经：{product_bible_to_prompt(product_bible)[:60]}...")

    # 生成所有角色多角度定妆照（性价比策略：多花几毛钱买一致性）
    # 每个核心角色：正面 + 全身（2张）
    # 虽然每个分镜最多只能绑定 2 个角色的参考图（API 限制），
    # 但不同分镜出场的角色不同，所有核心角色都需要有参考图备用
    # Q-Final 修复：用 core_characters（动态分析结果）而非 characters（用户原始传入）
    # 之前的 bug：用户没传 characters 时，characters=None，导致只生成 1 个默认角色
    _gen_chars = core_characters if core_characters else []
    if local_asset_mode:
        pass
    elif _gen_chars and len(_gen_chars) > 0:
        for idx, char in enumerate(_gen_chars):
            char_name = char.get("name", f"Character {chr(65 + idx)}")
            char_desc = char.get("description", "")
            char_img_path = Path(char["image_path"]) if char.get("image_path") else None
            char_bible = character_bibles[idx] if idx < len(character_bibles) else None

            try:
                char_ref = _generate_multi_angle_character_refs(
                    client=client,
                    product_info=product_info,
                    char_name=char_name,
                    char_desc=char_desc,
                    char_bible=char_bible,
                    char_ref_dir=char_ref_dir,
                    output_name=output_name,
                    user_image_path=char_img_path,
                    angles=["front", "full_body"],
                )
                char_refs.append(char_ref)
            except Exception as e:
                if idx == 0:
                    raise
                print(f"⚠️  角色 {char_name} 参考图生成失败，跳过：{e}")
    else:
        # 兼容旧版：没有任何角色时生成一个默认主角色
        default_char = {
            "name": "Character A",
            "description": f"{product_info.get('gender', 'person')} ({product_info.get('age', '25')})",
        }
        main_bible = character_bibles[0] if character_bibles else None
        try:
            char_ref = _generate_multi_angle_character_refs(
                client=client,
                product_info=product_info,
                char_name=default_char["name"],
                char_desc=default_char["description"],
                char_bible=main_bible,
                char_ref_dir=char_ref_dir,
                output_name=output_name,
                angles=["front", "full_body"],
            )
            char_refs.append(char_ref)
        except Exception as e:
            raise RuntimeError(f"默认角色参考图生成失败：{e}") from e

    if not local_asset_mode:
        total_char_images = sum(len(c.get("images", [c["image_path"]])) for c in char_refs)
        print(f"\n✅ 共加载 {len(char_refs)} 个角色，{total_char_images} 张参考图")

    # 读取商品参考图（如果提供）
    product_img_b64 = None
    product_image_path = None
    if product_image:
        if _is_http_url(product_image):
            product_image_path = _download_product_image_url(str(product_image), final_dir / f"{output_name}_refs")
            product_image = product_image_path
            print(f"🖼️ 商品参考图 URL 已下载：{product_image_path.name}")
        else:
            product_image_path = Path(product_image)
        _validate_product_image_file(product_image_path)
        product_img_b64 = base64.b64encode(product_image_path.read_bytes()).decode("utf-8")
    elif not local_asset_mode and not preview and not allow_no_product_image:
        raise RuntimeError(
            "发布级成片必须提供 --product-image，以便约束生成和质检产品露出。"
            "如仅调试或非商品视频，请显式传入 --allow-no-product-image。"
        )

    if local_asset_mode:
        print("🔎 开始分析本地素材覆盖度与视听特征...")
        from local_asset_pipeline import (
            build_local_asset_creative_profile,
            build_local_asset_index,
            build_local_asset_story_contract,
            build_material_constrained_script,
        )
        local_asset_index = build_local_asset_index(Path(local_assets))
        coverage = local_asset_index.get("coverage", {})
        asset_creative_profile = build_local_asset_creative_profile(local_asset_index)
        local_story_contract = build_local_asset_story_contract(
            local_asset_index,
            product_info,
            requested_duration=target_duration,
            preview=preview,
        )
        print(f"✅ 本地素材分析完成：可用窗口 {coverage.get('usable_windows', 0)}/{coverage.get('total_windows', 0)}")
        story_role_scores = coverage.get("story_role_scores", {})
        if story_role_scores:
            visible_story_roles = {
                key: value
                for key, value in story_role_scores.items()
                if float(value or 0) > 0
            }
            print(
                "   画面语义覆盖："
                + " / ".join(f"{key}={value:.2f}" for key, value in visible_story_roles.items())
            )
        print(
            f"   视听合同：motion={asset_creative_profile['energy']} "
            f"({asset_creative_profile['motion_score']:.2f}) / "
            f"brightness={asset_creative_profile['brightness']} / "
            f"contrast={asset_creative_profile['contrast']}"
        )
        print(
            f"   素材自然时长：{local_story_contract['natural_main_duration']:.2f}s / "
            f"{local_story_contract['recommended_segments']} 段"
        )
        if target_duration is not None and not local_story_contract["requested_duration_applied"]:
            print(
                f"   时长偏好 {target_duration}s 未强制应用："
                "保留完整素材叙事且不拉伸镜头"
            )

    # ============================================================
    # 第二步：生成广告脚本 + 分镜片段
    # ============================================================

    # ── 音乐合同：脚本阶段确定音乐策略，让分镜、口播、BGM 三方对齐 ──
    music_contract = build_music_contract(
        product_info,
        cinematic_style=style,
        asset_creative_profile=asset_creative_profile,
    )
    print(f"🎵 音乐合同：{music_contract['mood']} {music_contract['genre']}")
    print(f"    BPM：{music_contract['bpm_min']}-{music_contract['bpm_max']}，energy：{music_contract['energy']}")
    print(f"    推荐 pace：{music_contract['recommended_pace']}，intro：{music_contract['intro_type']}")

    # ── 智能段数推荐（剧情驱动，而非模板驱动）──
    # 根据产品类型、卖点数量、目标时长，智能推荐最合适的片段数量
    # 原则：剧情完整性优先，段数服务于内容，不为凑段数而凑段数
    _selling_points_raw = product_info.get("selling_point", "")
    _selling_points = [sp.strip() for sp in _selling_points_raw.replace("，", ",").split(",") if sp.strip()]
    if local_asset_mode:
        _recommended_segs = int(local_story_contract["recommended_segments"])
    else:
        _recommended_segs = recommend_segment_count(
            product_type=product_info.get("type", "default"),
            selling_points=_selling_points,
            target_duration=target_duration,
        )
        if reference_profile:
            _recommended_segs = int(reference_profile.get("recommended_material_segments") or _recommended_segs)
    # 预览模式强制 1 段
    _num_segs = 1 if preview else _recommended_segs
    if local_asset_mode:
        print(f"📐 素材理解决定段数：{_num_segs} 段")
    else:
        print(f"📐 智能段数推荐：{_num_segs} 段（基于{product_info.get('type', 'default')}品类、{len(_selling_points)}个卖点、目标时长{target_duration or '默认'}s）")

    # ── 节奏模板初始化（先选5段基准模板，再适配到目标段数）──
    # 用 5 段的总时长估算来选最接近的基准模板
    if local_asset_mode:
        _natural_total = float(local_story_contract["natural_main_duration"])
        rhythm_template = {
            "name": "本地素材自然时间轴",
            "total_duration": _natural_total,
            "actual_total_duration": _natural_total,
            "transition_duration": float(asset_creative_profile["transition_base_duration"]),
            "pace_style": str(asset_creative_profile["recommended_pace"]),
            "duration_source": "selected_local_asset_windows",
            "segments": [
                {
                    "index": int(item["segment"]),
                    "type": str(item["narrative"]),
                    "narrative": str(item["narrative"]),
                    "purpose": str(item["marketing_intent"]),
                    "duration": float(local_story_contract["segment_durations"][int(item["segment"])]),
                    "ratio": round(
                        float(local_story_contract["segment_durations"][int(item["segment"])])
                        / max(_natural_total, 0.001),
                        4,
                    ),
                }
                for item in local_story_contract["narrative_plan"]
            ],
        }
    else:
        _est_total_5seg = target_duration if target_duration is not None else duration * 5
        _effective_rhythm_style = rhythm_style
        if rhythm_style == "moderate" and music_contract["recommended_pace"] != "moderate":
            _effective_rhythm_style = music_contract["recommended_pace"]
            print(f"    节奏风格由 moderate 自动调整为 {_effective_rhythm_style}（音乐合同推荐）")
        _base_template = get_rhythm_template(
            _est_total_5seg, style=_effective_rhythm_style, product_type=product_info.get("type", "default")
        )
        if not preview and _num_segs != len(_base_template["segments"]):
            rhythm_template = adapt_rhythm_template_to_segments(_base_template, _num_segs)
            print(f"🔄 节奏模板适配：从 {len(_base_template['segments'])} 段 → {_num_segs} 段")
        else:
            rhythm_template = _base_template
        if reference_profile:
            rhythm_template = _fit_rhythm_template_to_net_duration(
                rhythm_template,
                float(reference_profile.get("recommended_main_duration") or 0.0),
            )
    _rhythm_name = rhythm_template["name"]
    _rhythm_transition = rhythm_template["transition_duration"]
    _rhythm_pace = rhythm_template["pace_style"]
    print(f"⏱️  节奏模板：{_rhythm_name}（{_rhythm_pace}，转场 {_rhythm_transition}s）")
    for seg in rhythm_template["segments"]:
        print(f"    [{seg['index']}] {seg['duration']:>5.2f}s  {seg['type']:<15s}  {seg['purpose']}")

    # P1-2: 检查节奏模板段时长是否超过可灵变速能力上限
    # 可灵生成 duration 秒，变速下限 0.5x → 单段最多可延长至 duration * 2 秒
    # 超出部分只能硬截断，实际总时长会显著低于预期，0.5x 画面也会有卡顿感
    _kling_max_seg_dur = duration * 2
    _over_limit_segs = [
        seg for seg in rhythm_template["segments"]
        if seg["duration"] > _kling_max_seg_dur
    ] if not local_asset_mode else []
    if _over_limit_segs:
        print("\n⚠️  [P1-2] 节奏模板时长警告：")
        print(f"   可灵生成时长：{duration}s，变速下限 0.5x，单段最多延长至 {_kling_max_seg_dur:.1f}s")
        for _s in _over_limit_segs:
            _gap = _s["duration"] - _kling_max_seg_dur
            print(
                f"   段 [{_s['index']}] {_s['type']}：目标 {_s['duration']:.1f}s "
                f"> 上限 {_kling_max_seg_dur:.1f}s（超出 {_gap:.1f}s）"
                f" → 实际将截断至 {_kling_max_seg_dur:.1f}s，慢速画质差"
            )
        _actual_max_total = sum(
            min(seg["duration"], _kling_max_seg_dur)
            for seg in rhythm_template["segments"]
        ) - (_rhythm_transition * (len(rhythm_template["segments"]) - 1))
        print(
            f"   预估实际总时长上限：约 {_actual_max_total:.1f}s"
            f"（目标 {rhythm_template['total_duration']}s）"
        )
        print(f"   建议：调小 --target-duration，或增大 --duration（如 --duration 10）")
        print()
        if strict_mode and not preview:
            raise RuntimeError(
                "节奏模板存在超过当前生成片段后期拉伸能力的段落，继续生成会导致截断、卡顿或字幕口播错位。"
                "请调小 --target-duration，或增大 --duration 后重新生成。"
            )

    # 生成完整广告脚本
    cast_prompt = _format_cast_plan_for_prompt(cast_plan)
    if cast_prompt:
        existing_extra = product_info.get("extra_requirements", "")
        product_info["extra_requirements"] = (
            f"{existing_extra}\n"
            f"CAST PLAN: {cast_prompt}\n"
            "Use only the core and supporting cast above. "
            "Do not invent additional named characters. "
            "Ambient entities may appear only as background detail."
        ).strip()
    if local_asset_mode:
        voice, _script_voice_reason = recommend_voice_for_narration(
            product_info,
            [],
            requested_voice=voice,
            creative_profile=asset_creative_profile,
        )
        _script_voice_rate = int(VOICE_PRESETS[voice]["rate"])
        ad_script = build_material_constrained_script(
            product_info=product_info,
            coverage=local_asset_index.get("coverage", {}) if local_asset_index else {},
            num_segments=_num_segs,
            script_style=script_style,
            asset_index=local_asset_index,
            segment_durations={
                int(segment["index"]): float(segment["duration"])
                for segment in rhythm_template["segments"]
            },
            narrative_plan_override=local_story_contract["narrative_plan"],
            narration_contract={
                "voice": voice,
                "rate": _script_voice_rate,
                "max_units_per_second": _script_voice_rate / 50.0,
                "transition_duration": float(rhythm_template["transition_duration"]),
            },
        )
    else:
        ad_script = generate_ad_script(
            product_info,
            style=script_style,
            hook_type=hook_type,
            num_segments=_num_segs,
        )
    if local_asset_mode:
        for rhythm_segment, script_segment in zip(
            rhythm_template["segments"],
            ad_script.get("segments", []),
        ):
            rhythm_segment["type"] = str(script_segment.get("narrative") or "product_showcase")
            rhythm_segment["purpose"] = str(
                script_segment.get("marketing_intent") or "value"
            )
        print("🧭 素材驱动叙事重规划：")
        for segment in rhythm_template["segments"]:
            print(
                f"    [{segment['index']}] {segment['duration']:>5.2f}s  "
                f"{segment['type']:<18s}  {segment['purpose']}"
            )
    # 将音乐合同注入 story_world，供 build_story_driven_prompts 使用
    if isinstance(ad_script, dict):
        _sw = ad_script.setdefault("story_world", {})
        if isinstance(_sw, dict):
            _sw["cast_plan"] = cast_plan
            if cast_prompt:
                _sw["character"] = cast_prompt
            _sw["music"] = {
                "mood": music_contract["mood"],
                "genre": music_contract["genre"],
                "energy": music_contract["energy"],
                "bpm_range": f"{music_contract['bpm_min']}-{music_contract['bpm_max']}",
                "intro_type": music_contract["intro_type"],
            }
    print(f"📝 广告脚本风格：{SCRIPT_STYLES.get(script_style, {}).get('name', script_style)}")
    print(f"    视频标题：{ad_script['title']}")
    print(f"    话题标签：{' '.join(ad_script['hashtags'])}")

    # 广告合规检测
    compliance_result = check_script_compliance(ad_script)
    if not compliance_result["passed"]:
        print_compliance_report(compliance_result)
        risk = compliance_result["risk_level"]
        if risk == "critical":
            raise RuntimeError(
                f"已包含最高级禁用词或敏感词（risk={risk}），"
                "请修改脚本后再运行。即使传入 --force 也无法跳过 critical 级别拦截。"
            )
        elif risk == "high":
            # 高风险：默认拦截，force=True 可跳过
            if not force:
                raise RuntimeError(
                    f"包含高风险词（risk={risk}），已中止。"
                    "如确认要发布，请修改词语或传入 --force 参数。"
                )
            print("⚠️  --force 模式：跳过高风险合规检测，请自行承担合规风险。")
        else:
            # medium 风险：提示但不拦截
            print("⚠️  检测到中风险合规问题，建议修改后再发布（当前继续处理）。")
    else:
        print("✅ 广告合规检测通过")

    # 剧情完整性校验
    _story_check = check_story_completeness(ad_script)
    _story_score_pct = int(_story_check["score"] * 100)
    print(f"🎬 剧情完整性：{_story_score_pct}%（{_story_check['total_segments']} 段）")
    if _story_check["passed"]:
        print("    ✅ 叙事弧完整：hook → turning → showcase → result → cta")
    else:
        print(f"    ⚠️  缺失叙事节拍：{', '.join(_story_check['missing_beats'])}")
    if _story_check["warnings"]:
        for _w in _story_check["warnings"]:
            print(f"    ⚠️  {_w}")
    if not _story_check["passed"] and strict_mode and not preview and not local_asset_mode:
        raise RuntimeError(
            f"剧情完整性校验失败（{_story_score_pct}%）：缺失叙事节拍 {_story_check['missing_beats']}。"
            "请优化脚本后重新生成，或使用 --no-strict 跳过校验。"
        )

    # ── 节奏曲线分析：基于脚本段落情绪 + 音乐合同 BPM 生成逐段节奏参数 ──
    rhythm_curve = None
    try:
        from rhythm_controller import RhythmController
        _rc = RhythmController()
        _segments_for_rhythm = ad_script.get("segments", []) if isinstance(ad_script, dict) else []
        if _segments_for_rhythm:
            rhythm_curve = _rc.analyze_script_rhythm(
                segments=_segments_for_rhythm,
                product_category=product_info.get("type", "default"),
            )
            print(f"🎼 节奏曲线分析完成：整体 BPM {rhythm_curve.overall_bpm}，共 {len(rhythm_curve.segments)} 段")
            for _rs in rhythm_curve.segments[:6]:
                print(f"    [{_rs.segment_index + 1}] {_rs.narrative_type:<12s}  BPM:{_rs.bpm:>3d}  强度:{_rs.emotion_level.value:<9s}  {_rs.duration:.1f}s")
    except Exception as _rhythm_err:
        print(f"⚠️  节奏曲线分析跳过：{_rhythm_err}")

    # ── 故事板预可视化验证：生成分镜结构并校验质量 ──
    storyboard = None
    if local_asset_mode:
        pass
    else:
        try:
            from storyboard_generator import StoryboardGenerator
            _sb_gen = StoryboardGenerator()
            _sb_segments = ad_script.get("segments", []) if isinstance(ad_script, dict) else []
            if _sb_segments:
                _char_roles_for_sb = []
                if character_bibles:
                    from character_analyzer import CharacterRole, CharacterType
                    for i, cb in enumerate(character_bibles):
                        _ctype = CharacterType.PROTAGONIST if i == 0 else CharacterType.SUPPORTING
                        _char_roles_for_sb.append(CharacterRole(
                            role_id=cb.get("id", f"char_{i+1:02d}"),
                            name=cb.get("name", f"角色{i+1}"),
                            character_type=_ctype,
                            description=cb.get("outfit", "") or cb.get("appearance", ""),
                            gender=cb.get("gender", "person"),
                            age_range=cb.get("age", "adult"),
                            relationship_to_protagonist="self" if i == 0 else "supporting",
                            appearance_requirements=cb.get("appearance", "") or character_bible_to_prompt(cb),
                            consistency_level=0.9 if i == 0 else 0.7,
                        ))
                _prod_bible_for_sb = product_bible if product_bible else {}
                storyboard = _sb_gen.generate_from_script(
                    ad_script=ad_script,
                    character_roles=_char_roles_for_sb if _char_roles_for_sb else None,
                    product_bible=_prod_bible_for_sb,
                    style=style if style != DEFAULT_CINEMATIC_STYLE else "cinematic",
                    character_bibles=character_bibles,
                )
                if storyboard and storyboard.shots:
                    _sb_issues = _validate_storyboard_quality(storyboard, product_info)
                    if _sb_issues:
                        print(f"📋 故事板质量预警（{len(_sb_issues)} 项）：")
                        for _sbi in _sb_issues[:5]:
                            print(f"    ⚠️  {_sbi}")
                    else:
                        print(f"📋 故事板预验证通过：{len(storyboard.shots)} 个分镜，总时长 {storyboard.total_duration:.1f}s")
        except Exception as _sb_err:
            print(f"⚠️  故事板预验证跳过：{_sb_err}")

    # 从脚本生成分镜 Prompts
    # 改动2：优先使用故事驱动 prompt 组装（build_story_driven_prompts）
    # 当 LLM 生成了 story_world 时，以故事世界为主干、电影风格为修饰层组装 prompt，
    # 保证5段场景锚定在同一时空，产品可见度精准控制。
    # 回退条件：模板模式生成（ad_script.generated_by != "llm"）时使用旧逻辑。
    _use_story_driven = (
        isinstance(ad_script, dict)
        and ad_script.get("generated_by") == "llm"
        and bool(ad_script.get("story_world"))
    )

    if _use_story_driven:
        clip_prompts = build_story_driven_prompts(
            ad_script=ad_script,
            product_info=product_info,
            cinematic_style=style,
            char_refs=char_refs,
            hook_type=hook_type,
            character_bibles=character_bibles,
            product_bible=product_bible,
            music_contract=music_contract,
            rhythm_curve=rhythm_curve,
        )
        # 长度对齐：若 LLM 段数与节奏模板段数不一致，截断或补充
        _num_expected = len(rhythm_template["segments"])
        if len(clip_prompts) < _num_expected:
            # 补充：复用最后一段
            while len(clip_prompts) < _num_expected:
                clip_prompts.append(clip_prompts[-1] if clip_prompts else "product display, natural lighting, 9:16 vertical, high quality, cinematic")
        elif len(clip_prompts) > _num_expected:
            clip_prompts = clip_prompts[:_num_expected]
        print(f"📖 故事驱动 Prompt 已生成（{len(clip_prompts)} 段，基于 story_world: {ad_script['story_world'].get('location', '')[:40]}...）")
    else:
        # 旧逻辑回退（模板模式）
        styled_prompts = generate_clip_prompts(
            product_info,
            cinematic_style=style,
            characters=[c["name"] for c in char_refs] if char_refs else None,
            hook_type=hook_type,
            character_bibles=character_bibles,
            product_bible=product_bible,
            music_contract=music_contract,
        )
        script_scenes = [seg.get("scene_prompt", "") for seg in ad_script.get("segments", [])]
        clip_prompts = []
        for i, styled in enumerate(styled_prompts):
            scene_detail = script_scenes[i] if i < len(script_scenes) else ""
            if scene_detail:
                scene_detail = scene_detail.replace(", 9:16 vertical", "").replace("9:16 vertical", "").strip().rstrip(",")
                clip_prompts.append(f"{styled}, {scene_detail}")
            else:
                clip_prompts.append(styled)

    if local_asset_mode:
        clip_prompts = [
            seg.get("visual_requirement") or seg.get("scene_prompt") or seg.get("subtitle") or "local product ad material"
            for seg in ad_script.get("segments", [])
        ]
        storyboard = None
        print(f"🎯 本地选片查询已生成（{len(clip_prompts)} 段）")

    if style != DEFAULT_CINEMATIC_STYLE:
        style_name = CINEMATIC_STYLES.get(style, {}).get("name", style)
        print(f"🎬 电影风格注入：{style_name}（影响 {len(clip_prompts)} 个片段的运镜与光影）")

    # ── 故事板验证驱动的 Prompt 增强（把预警转化为实际改进）──
    if storyboard and storyboard.shots:
        _sb_issues = _validate_storyboard_quality(storyboard, product_info)
        if _sb_issues:
            _enhanced_count = 0
            _product_name = product_info.get("name", "")
            for i in range(min(len(clip_prompts), len(ad_script.get("segments", [])))):
                seg = ad_script["segments"][i]
                narrative = str(seg.get("narrative", "")).lower()
                _original = clip_prompts[i]

                # 产品露出不足 → showcase/demo/cta 段加强产品描述
                _low_product = any("产品露出" in iss and "不足" in iss for iss in _sb_issues)
                if _low_product and _is_product_required_narrative(narrative):
                    if _product_name and _product_name.lower() not in _original.lower():
                        clip_prompts[i] = _original.rstrip(" ,") + f", {_product_name} clearly visible, product hero shot"
                        _enhanced_count += 1
                    elif "clearly visible" not in _original:
                        clip_prompts[i] = _original.rstrip(" ,") + ", product clearly visible and prominent"
                        _enhanced_count += 1

                # 场景单一 → 增加镜头运动和角度多样性
                _mono_scene = any("同一场景" in iss and "视觉单调" in iss for iss in _sb_issues)
                if _mono_scene and i > 0:
                    if "camera movement" not in _original.lower() and "slow push" not in _original.lower():
                        _varied_moves = ["slow camera pan", "subtle camera movement", "gentle camera drift"]
                        clip_prompts[i] = _original.rstrip(" ,") + f", {_varied_moves[i % len(_varied_moves)]}"
                        _enhanced_count += 1

                # 情绪单一 → 加强节奏和光影变化
                _flat_emotion = any("情绪单一" in iss for iss in _sb_issues)
                if _flat_emotion and i > 0 and i < len(clip_prompts) - 1:
                    if "contrast" not in _original.lower():
                        _contrast_variation = ["dynamic lighting contrast", "subtle contrast shift", "soft contrast variation"]
                        clip_prompts[i] = _original.rstrip(" ,") + f", {_contrast_variation[i % len(_contrast_variation)]}"
                        _enhanced_count += 1

            if _enhanced_count > 0:
                print(f"📋 故事板改进已应用：针对 {len(_sb_issues)} 项预警增强了 {_enhanced_count} 个片段的 Prompt")


    # ── 预览模式：只保留第 1 段（后期效果完整，快速预览整体效果）──
    if preview:
        clip_prompts = clip_prompts[:1]
        print(f"\n⚡ 预览模式：仅生成第 1 段（std 模式 + 完整后期效果）")
        print(f"   含字幕/口播/BGM，快速预览整体效果")
        print(f"   确认效果 OK 后，去掉 --preview 重新生成完整 5 段 pro 版本")

    if not local_asset_mode:
        _preflight_generation_contract(
            product_info=product_info,
            ad_script=ad_script,
            clip_prompts=clip_prompts,
            product_image_path=product_image_path,
            char_refs=char_refs,
            strict_mode=strict_mode and not preview,
        )

    _cost_info = None
    if not local_asset_mode:
        _cost_info = estimate_cost(
            mode=mode,
            duration_per_clip=duration,
            num_clips=len(clip_prompts),
            num_characters=len(char_refs) if char_refs else 1,
            best_of=best_of,
            image_first_segments=_estimate_image_first_segment_count(
                len(clip_prompts),
                image_first_mode,
                enabled=image_first and preflight_keyframe and strict_mode and not preview,
            ),
            image_first_variants=max(1, int(image_first_variants or 1)),
        )
        print_cost_estimate(_cost_info)

    # ── 质量前置控制（Quality Gate）──
    # 在生成视频前进行全面预检，消除质量隐患，避免成本浪费
    char_image_paths = [Path(c["image_path"]) for c in char_refs if c.get("image_path")] if char_refs else []

    if local_asset_mode:
        from types import SimpleNamespace
        quality_gate_result = SimpleNamespace(
            optimized_prompts=[],
            optimized_negative_prompt=None,
            preprocessed_reference_images=[],
            reference_checks=[],
            reference_dedup_removed=[],
            reference_sort_notes=[],
            passed=True,
        )
        print("🔍 本地选片按素材语义证据与匹配置信度执行")
    else:
        quality_gate_result = run_quality_gate(
            ad_script=ad_script,
            product_image_path=product_image_path,
            character_image_paths=char_image_paths,
            prompts=clip_prompts,
            character_bible=character_bibles[0] if character_bibles else None,
            product_bible=product_bible,
            scene_continuity_config=SCENE_CONTINUITY_CONFIG,
            num_clips=len(clip_prompts),
            duration_per_clip=duration,
            mode=mode,
        )

        print_quality_gate_report(quality_gate_result)

    # ── 自动应用 Prompt 优化 ──
    # 质量门检测出的 prompt 问题，能自动修的直接修，修完替换原始 prompt
    if quality_gate_result.optimized_prompts and len(quality_gate_result.optimized_prompts) == len(clip_prompts):
        _optimized_count = 0
        for i in range(len(clip_prompts)):
            if quality_gate_result.optimized_prompts[i] != clip_prompts[i]:
                clip_prompts[i] = quality_gate_result.optimized_prompts[i]
                _optimized_count += 1
        if _optimized_count > 0:
            print(f"🔧 已自动优化 {_optimized_count} 个片段的 Prompt（语义冲突/重复/缺词等）")

    # ── 自动应用负面词优化 ──
    effective_negative_prompt = NEGATIVE_PROMPT
    if quality_gate_result.optimized_negative_prompt:
        effective_negative_prompt = quality_gate_result.optimized_negative_prompt
        print(f"🎭 已应用优化后的负面词（{len(effective_negative_prompt.split(','))} 个精选词）")

    # ── 自动应用参考图预处理结果 ──
    if quality_gate_result.preprocessed_reference_images:
        _preprocessed_count = 0
        for info in quality_gate_result.preprocessed_reference_images:
            new_path = info.get("output_path")
            orig_path = info.get("original_path")
            if new_path and orig_path:
                for ref in char_refs:
                    if ref.get("image_path") and Path(ref["image_path"]) == orig_path:
                        ref["image_path"] = str(new_path)
                        _preprocessed_count += 1
                        break
                if product_image_path and Path(product_image_path) == orig_path:
                    product_image_path = Path(new_path)
                    _preprocessed_count += 1
        if _preprocessed_count > 0:
            print(f"🖼️  已自动预处理 {_preprocessed_count} 张参考图（裁剪/亮度/对比度优化）")

    # ── 自动应用参考图去重 + 智能排序结果 ──
    _ref_applied = False
    if quality_gate_result.reference_checks and char_refs:
        final_ref_results = quality_gate_result.reference_checks
        char_results = [r for r in final_ref_results if r.image_type == "character"]

        if char_results:
            _path_to_ref = {}
            for ref in char_refs:
                if ref.get("image_path"):
                    _path_to_ref[Path(ref["image_path"])] = ref

            new_char_refs = []
            for r in char_results:
                if r.path and r.path in _path_to_ref:
                    new_char_refs.append(_path_to_ref[r.path])

            for ref in char_refs:
                if ref not in new_char_refs:
                    new_char_refs.append(ref)

            if new_char_refs and new_char_refs != char_refs:
                old_count = len(char_refs)
                char_refs.clear()
                char_refs.extend(new_char_refs)
                new_count = len(char_refs)

                if new_count < old_count:
                    _dedup_count = old_count - new_count
                    print(f"🔍 参考图去重：移除了 {_dedup_count} 张高度相似的参考图")
                    for removed_desc in quality_gate_result.reference_dedup_removed[:3]:
                        print(f"   • {removed_desc}")
                    _ref_applied = True

                if quality_gate_result.reference_sort_notes:
                    print(f"📊 参考图已按质量智能排序（高质量图前置，AI 权重更高）")
                    _ref_applied = True

    if not quality_gate_result.passed:
        from config import QUALITY_GATE_CONFIG
        failure_behavior = QUALITY_GATE_CONFIG.get("failure_behavior", {})
        if failure_behavior.get("block_on_failure", True):
            if strict_mode:
                raise RuntimeError(
                    "质量前置检查未通过，已阻止生成，避免进入高成本视频抽卡。\n"
                    "💡 请根据上方质量报告修复问题后重试。"
                )
            if failure_behavior.get("allow_override", True):
                confirm = input("⚠️  质量前置检查未通过，是否继续生成？(y/n) [n]：").strip().lower()
                if confirm != "y":
                    print("已取消生成")
                    raise RuntimeError("质量前置检查未通过，已取消生成")
            else:
                raise RuntimeError("质量前置检查未通过，已阻止生成")

    main_char_path = Path(char_refs[0]["image_path"]) if char_refs and char_refs[0].get("image_path") else None
    approved_keyframes: Dict[int, Path] = {}
    image_first_result = None

    if image_first and preflight_keyframe and strict_mode and not preview and not local_asset_mode:
        try:
            image_first_mode_enum = ImageFirstMode((image_first_mode or "standard").lower())
        except ValueError:
            image_first_mode_enum = ImageFirstMode.STANDARD
        image_first_save_dir = clips_dir / f"{output_name}_image_first"
        image_first_result = run_image_first_strategy(
            client=client,
            ad_script=ad_script,
            clip_prompts=clip_prompts,
            product_reference_path=product_image_path,
            character_reference_path=main_char_path,
            save_dir=image_first_save_dir,
            mode=image_first_mode_enum,
            n_variants=max(1, int(image_first_variants or 1)),
            aspect_ratio=aspect_ratio,
            image_fidelity=image_fidelity,
            strict_mode=True,
            negative_prompt=effective_negative_prompt,
            product_category=product_info.get("type", "default"),
            style_preference=style,
        )
        print_image_first_report(image_first_result)
        if not image_first_result.can_proceed_to_video:
            raise RuntimeError("图片先行验证未通过，已阻止进入高成本视频生成")
        approved_keyframes = dict(image_first_result.best_keyframes)
    elif local_asset_mode:
        pass
    elif preview:
        print("⚡ 预览模式：跳过图片先行验证，直接进入视频生成")

    if local_asset_mode:
        workflow_decision_result = None
    else:
        workflow_decision_result = _run_pre_generation_smart_decision(
            quality_gate_result,
            product_info,
            style=style,
            budget=(_cost_info or {}).get("estimated_cost", _get_cost_budget_limit()),
            image_first_result=image_first_result,
            preview=preview,
        )

    clip_paths = []
    scene_anchor_frames = []  # 全局场景锚点（从第 1 段提取，所有后续段都参考）
    scene_cfg = dict(SCENE_CONTINUITY_CONFIG)  # 浅拷贝，避免污染全局配置

    # 节奏模板：转场时长从模板取（统一管理，不再分散计算）
    scene_cfg["transition_duration"] = _rhythm_transition

    # 改动4：将主题匹配的转场类型写入 scene_cfg，供后续合成步骤使用
    _av_style = auto_match_av_style(product_info, style)
    scene_cfg["transition_type"] = _av_style["transition_type"]
    print(f"🎬 AV 风格匹配：转场={_av_style['transition_type']}，音效包={_av_style['sfx_pack']}， BGM关键词={_av_style['bgm_keywords']}")

    total_clips = len(clip_prompts)
    generation_start = time.time()

    def _get_narrative_for_idx(idx: int) -> str:
        seg_i = idx - 1
        try:
            segs = ad_script.get("segments", []) if isinstance(ad_script, dict) else []
            if 0 <= seg_i < len(segs):
                v = segs[seg_i].get("narrative") or segs[seg_i].get("type") or segs[seg_i].get("purpose")
                if v:
                    return str(v)
        except Exception:
            pass
        if seg_i <= 0:
            return "hook"
        if seg_i >= 4:
            return "cta"
        return "showcase"

    def _build_ref_images(
        idx: int,
        clip_prompt: str = "",
        prev_clip_path: Optional[Path] = None,
        prev_last_frame_b64: Optional[str] = None,
    ) -> list:
        """按叙事任务选择最优参考图组合（性价比策略：用满5张换视频一次成功）。"""
        narrative = _get_narrative_for_idx(idx).lower().strip()

        # ── 多角色：按分镜内容检测出场角色，只传该角色的参考图 ──
        primary_char_idx = _get_primary_char_for_clip(
            idx, clip_prompt, ad_script, character_bibles
        )
        primary_char = char_refs[primary_char_idx] if char_refs and primary_char_idx < len(char_refs) else (char_refs[0] if char_refs else None)

        # 检测分镜中是否有多个角色出场
        haystack = (clip_prompt or "").lower()
        matched_char_indices = []
        for i, bible in enumerate(character_bibles):
            if bible.get("name", "").lower().strip() in haystack:
                matched_char_indices.append(i)
        if len(matched_char_indices) == 0 and primary_char_idx is not None:
            matched_char_indices = [primary_char_idx]

        # 构建角色→图片映射（支持多角度）
        role_to_image = {}
        if product_img_b64:
            role_to_image["product"] = f"data:image/png;base64,{product_img_b64}"

        if primary_char:
            # 根据镜头类型推荐参考图角度，智能选择主参考图
            char_images = primary_char.get("img_b64_list", [])
            recommended_angle = "front"
            try:
                from quality_gate import recommend_ref_angle
                recommended_angle = recommend_ref_angle(clip_prompt, narrative)
            except Exception:
                pass

            if len(char_images) > 1 and recommended_angle == "full_body":
                # 推荐全身图：全身图作为主参考，正面图作为辅助
                role_to_image["character_primary"] = f"data:image/png;base64,{char_images[1]}"
                role_to_image["character_angle"] = f"data:image/png;base64,{char_images[0]}"
            else:
                # 默认正面图为主
                role_to_image["character_primary"] = f"data:image/png;base64,{primary_char['img_b64']}"
                if len(char_images) > 1:
                    role_to_image["character_angle"] = f"data:image/png;base64,{char_images[1]}"
            # 次角色（分镜中出场的其他角色）
            for i in matched_char_indices:
                if i == primary_char_idx or i >= len(char_refs):
                    continue
                sec_char = char_refs[i]
                role_to_image["character_secondary"] = f"data:image/png;base64,{sec_char['img_b64']}"
                break

        approved_keyframe_path = approved_keyframes.get(idx - 1)
        if approved_keyframe_path and approved_keyframe_path.exists():
            try:
                role_to_image["approved_keyframe"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(approved_keyframe_path.read_bytes()).decode("utf-8")
                )
            except Exception:
                pass

        # 连续性参考帧
        is_first = (idx == 1)
        continuity_image = None
        if not is_first:
            if prev_clip_path and scene_cfg.get("use_previous_last_frame", True):
                try:
                    last_frame_b64 = _extract_frame_b64(prev_clip_path, time_sec=duration - 0.1)
                    if last_frame_b64:
                        continuity_image = last_frame_b64
                except Exception:
                    pass
            elif prev_last_frame_b64 and scene_cfg.get("use_previous_last_frame", True):
                continuity_image = prev_last_frame_b64
            elif scene_cfg.get("use_scene_anchor", True) and scene_anchor_frames:
                continuity_image = scene_anchor_frames[0]

        if continuity_image:
            role_to_image["continuity"] = continuity_image

        # 计算是否有多角度角色图
        multi_angle = "character_angle" in role_to_image
        multi_char = len(matched_char_indices) > 1 and "character_secondary" in role_to_image

        role_order = _reference_strategy_for_narrative(
            narrative,
            product_available=bool(product_img_b64),
            character_available=bool(primary_char),
            continuity_available=bool(continuity_image),
            multi_character=multi_char,
            multi_angle_char=multi_angle,
        )
        if "approved_keyframe" in role_to_image:
            role_order = ["approved_keyframe"] + [
                role for role in role_order if role != "approved_keyframe"
            ]

        ref_images = [
            {"role": role, "image": role_to_image[role]}
            for role in role_order
            if role in role_to_image
        ]

        # 去重（防重复图片）
        seen = set()
        deduped = []
        for item in ref_images:
            img = item.get("image", "") if isinstance(item, dict) else item
            if not img:
                continue
            if img in seen:
                continue
            seen.add(img)
            deduped.append(item)

        return deduped[:MAX_REF_IMAGES]

    def _generate_one_clip(idx: int, prompt: str, prev_clip_path: Optional[Path] = None, prev_last_frame_b64: Optional[str] = None) -> Path:
        """生成单个片段（含自动重试 + 缓存跳过），返回本地文件路径"""
        clip_path = clips_dir / f"clip_{idx:02d}_{output_name}.mp4"

        ref_images = _build_ref_images(idx, clip_prompt=prompt, prev_clip_path=prev_clip_path, prev_last_frame_b64=prev_last_frame_b64)
        narrative = _get_narrative_for_idx(idx).lower().strip()

        # 自我进化：根据历史人脸失败率动态调整 best_of 和重试强度
        # 人物场景（hook/turning/result/review）自动启用人脸质量策略
        _face_strategy = None
        _clip_best_of = best_of
        _clip_max_retries = 3
        if narrative in {"hook", "turning", "result", "review"}:
            try:
                from quality_gate import evolve_face_quality_strategy
                _face_strategy = evolve_face_quality_strategy(
                    product_category=product_info.get("type", "default"),
                    narrative_type=narrative,
                    lookback_days=30,
                )
                if _face_strategy and _face_strategy.get("confidence", 0) >= 0.5:
                    _clip_best_of = max(best_of, _face_strategy.get("best_of", 1))
                    _clip_max_retries = _face_strategy.get("max_retries", 3)
                    if _clip_best_of > best_of or _clip_max_retries > 3:
                        print(f"  🧠 自我进化：{_face_strategy.get('reason')}")
                        print(f"     best_of={best_of}→{_clip_best_of}, 重试=3→{_clip_max_retries}")
            except Exception:
                pass

        # 改动5：prompt 质量前置门禁（在 scene_consistency 和 ref_binding 之前去重）
        final_prompt = _prompt_quality_gate(
            prompt,
            narrative=narrative,
            product_name=product_info.get("name", ""),
            segment_index=idx,
        )

        if scene_cfg.get("inject_scene_prompt", True):
            # 故事驱动模式已在 prompt 开头注入场景锚定，跳过重复注入
            if not final_prompt.startswith("SCENE:") and not final_prompt.startswith("Continuing") and not final_prompt.startswith("Same scene"):
                final_prompt = _inject_scene_consistency_prompt(final_prompt, is_first_clip=(idx == 1))

        if ref_images:
            final_prompt = _bind_reference_tags_to_prompt(final_prompt, ref_images, narrative)

        # 关键帧一致性强化：如果有 approved keyframe，在 prompt 中强调首帧一致性
        has_approved_keyframe = any(item.get("role") == "approved_keyframe" for item in ref_images)
        if has_approved_keyframe:
            final_prompt = "first frame matches approved keyframe composition and lighting, " + final_prompt

        final_prompt = _compact_prompt_for_generation(final_prompt)

        clip_seed = (seed + idx - 1) if seed is not None else None
        ref_image_values = _ref_image_values(ref_images)

        clip_negative_prompt = effective_negative_prompt
        try:
            from quality_gate import enhance_negative_prompt as _enhance_neg
            _enhanced = _enhance_neg(effective_negative_prompt, final_prompt)
            if _enhanced and _enhanced != effective_negative_prompt:
                clip_negative_prompt = _enhanced
        except Exception:
            pass

        def _candidate_prompt_for_strategy(candidate_strategy: str) -> str:
            if candidate_strategy == "product_rescue" and _is_product_required_narrative(narrative):
                return _compact_prompt_for_generation(
                    f"{final_prompt}, product centered, large unobstructed packaging, exact same product color and shape as product reference"
                )
            if candidate_strategy == "character_rescue" and any(
                item.get("role") == "character" for item in ref_images if isinstance(item, dict)
            ):
                return _compact_prompt_for_generation(
                    f"{final_prompt}, same person, same hairstyle, same outfit, stable face, minimal head rotation"
                )
            return final_prompt

        final_clip_manifest = _build_clip_manifest(
            final_prompt=final_prompt,
            ref_images=ref_images,
            idx=idx,
            model=effective_kling_model,
            mode=mode,
            duration=duration,
            aspect_ratio=aspect_ratio,
            seed=clip_seed,
            negative_prompt=clip_negative_prompt,
            candidate_strategy="single",
        )
        final_clip_manifest["target_name"] = clip_path.name

        def _valid_cached_clip(target_path: Path, manifest: dict) -> bool:
            if not target_path.exists() or target_path.stat().st_size <= 100 * 1024:
                return False
            if not _manifest_matches(target_path, manifest):
                return False
            try:
                import subprocess as _sp
                _probe = _sp.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(target_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if _probe.returncode == 0 and _probe.stdout.strip():
                    _dur = float(_probe.stdout.strip())
                    return abs(_dur - duration) < 1.0
            except Exception:
                return False
            return False

        if _valid_cached_clip(clip_path, final_clip_manifest):
            print(f"  ✅ 片段 {idx} manifest 缓存命中，跳过生成")
            return clip_path

        def _generate_to_path(target_path: Path, candidate_strategy: str = "single", override_prompt: Optional[str] = None) -> Path:
            max_retries = _clip_max_retries
            last_error = None
            candidate_prompt = override_prompt if override_prompt else _candidate_prompt_for_strategy(candidate_strategy)
            target_manifest = _build_clip_manifest(
                final_prompt=candidate_prompt,
                ref_images=ref_images,
                idx=idx,
                model=effective_kling_model,
                mode=mode,
                duration=duration,
                aspect_ratio=aspect_ratio,
                seed=clip_seed,
                negative_prompt=clip_negative_prompt,
                candidate_strategy=candidate_strategy,
            )
            target_manifest["target_name"] = target_path.name
            idempotency_key = _build_video_idempotency_key(
                candidate_prompt,
                ref_image_values,
                idx,
                target_path,
                model=effective_kling_model,
                mode=mode,
                duration=duration,
                aspect_ratio=aspect_ratio,
                seed=clip_seed,
            )
            if _valid_cached_clip(target_path, target_manifest):
                print(f"  ✅ 片段 {idx} 候选缓存命中：{target_path.name}")
                return target_path

            # 首帧低成本预检：仅在第一个候选且开启预检时执行
            if preflight_keyframe and candidate_strategy == "single" and (idx - 1) not in approved_keyframes:
                _kf_path = clips_dir / f"clip_{idx:02d}_{output_name}_preflight.png"
                _kf_passed, _kf_issues, _kf_img = _preflight_keyframe_check(
                    client=client,
                    prompt=candidate_prompt,
                    ref_images=ref_images,
                    narrative=narrative,
                    product_image_path=product_image_path,
                    main_char_path=main_char_path,
                    save_path=_kf_path,
                    aspect_ratio=aspect_ratio,
                    image_fidelity=_get_fidelity_for_narrative(narrative, image_fidelity),
                    negative_prompt=clip_negative_prompt,
                )
                if not _kf_passed:
                    raise RuntimeError(
                        f"片段 {idx} 首帧预检未通过，拦截视频生成以避免浪费额度。问题：{_kf_issues[0] if _kf_issues else '未知'}"
                    )
                print(f"  ✅ 片段 {idx} 首帧预检通过")

            for attempt in range(1, max_retries + 1):
                try:
                    # 智能重试：根据上一次失败原因调整参数
                    retry_prompt = candidate_prompt
                    retry_neg = clip_negative_prompt
                    retry_seed = clip_seed
                    if attempt > 1 and last_error is not None:
                        err_str = str(last_error).lower()
                        # 清晰度不足 → 加强画质描述 + 提高 fidelity
                        if "清晰度" in err_str or "模糊" in err_str or "blurry" in err_str:
                            retry_prompt = f"{candidate_prompt}, ultra sharp, highly detailed, 8k resolution, professional photography, crisp focus"
                            retry_prompt = _compact_prompt_for_generation(retry_prompt)
                            print(f"  🔧 第 {attempt} 次重试：加强画质描述")
                        # 黑帧/全黑 → 增加亮度描述
                        elif "黑帧" in err_str or "black" in err_str:
                            retry_prompt = f"{candidate_prompt}, bright lighting, well-lit scene, key lighting"
                            retry_prompt = _compact_prompt_for_generation(retry_prompt)
                            print(f"  🔧 第 {attempt} 次重试：增加亮度描述")
                        # 人脸问题 → 分阶段梯度重试，每次都换 seed 增加多样性
                        elif "人脸" in err_str or "face" in err_str or "character" in err_str:
                            import random as _rand
                            base_seed = clip_seed or 42
                            if attempt == 2:
                                # 第2次：加强面部描述 + 负面词 + 提高人物保真
                                retry_prompt = f"{candidate_prompt}, perfect face, symmetrical features, natural expression, detailed facial features, clear facial structure, photorealistic"
                                retry_prompt = _compact_prompt_for_generation(retry_prompt)
                                retry_neg = f"{clip_negative_prompt}, deformed face, ugly, distorted features, extra limbs, bad anatomy, mutated, cross-eyed, asymmetric"
                                retry_seed = base_seed + attempt * 1000 + _rand.randint(1, 9999)
                                print(f"  🔧 第 {attempt} 次重试：加强面部描述+负面词+换 seed")
                            elif attempt == 3:
                                # 第3次：拉远镜头 + 正面视角 + 降低画面占比
                                retry_prompt = f"{candidate_prompt}, medium shot, full face visible, front view, natural lighting, professional portrait, clear features, centered composition"
                                retry_prompt = _compact_prompt_for_generation(retry_prompt)
                                retry_neg = f"{clip_negative_prompt}, deformed face, ugly, distorted features, extra limbs, close-up, extreme close-up, side profile, blurry face, cropped face"
                                retry_seed = base_seed + attempt * 10000 + _rand.randint(1, 99999)
                                print(f"  🔧 第 {attempt} 次重试：拉远镜头+正面视角+换 seed")
                            else:
                                # 第4+次：全身/中景 + 大幅换 seed + 多角度
                                retry_prompt = f"{candidate_prompt}, full body shot, natural pose, cinematic composition, wide angle, dynamic camera angle"
                                retry_prompt = _compact_prompt_for_generation(retry_prompt)
                                retry_neg = f"{clip_negative_prompt}, deformed face, ugly, distorted features, extra limbs, close-up, blurry, cropped, out of frame"
                                retry_seed = base_seed + attempt * 100000 + _rand.randint(1, 999999)
                                print(f"  🔧 第 {attempt} 次重试：全身视角+大幅换 seed")
                        # 时长异常 → 换 seed 重试
                        elif "时长" in err_str or "duration" in err_str:
                            import random as _rand
                            retry_seed = (clip_seed or 42) + attempt * 1000 + _rand.randint(1, 999)
                            print(f"  🔧 第 {attempt} 次重试：更换 seed")
                        else:
                            # 通用重试：换 seed
                            import random as _rand
                            retry_seed = (clip_seed or 42) + attempt * 1000 + _rand.randint(1, 999)
                            print(f"  🔧 第 {attempt} 次重试：更换 seed")
                    else:
                        retry_prompt = candidate_prompt
                        retry_neg = clip_negative_prompt
                        retry_seed = clip_seed

                    video_result = client.generate_video(
                        prompt=retry_prompt,
                        aspect_ratio=aspect_ratio,
                        duration=duration,
                        mode=mode,
                        reference_images=ref_image_values if ref_image_values else None,
                        image_fidelity=_get_fidelity_for_narrative(narrative, image_fidelity),
                        human_fidelity=_get_human_fidelity_for_narrative(narrative, human_fidelity),
                        seed=retry_seed,
                        negative_prompt=retry_neg,
                        idempotency_key=idempotency_key if attempt == 1 else None,
                        # P2-C 高级参数
                        model=effective_kling_model,
                        multi_shot=multi_shot,
                    )
                    video_url = video_result.get("video_url") or video_result.get("url")
                    if not video_url:
                        raise RuntimeError(f"片段 {idx} 未返回视频 URL")
                    client.download_video(video_url, target_path)

                    _qc_issue = _quick_quality_check(target_path, expected_duration=duration, idx=idx)
                    if _qc_issue:
                        try:
                            target_path.unlink(missing_ok=True)
                            _clip_manifest_path(target_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        raise RuntimeError(f"片段 {idx} 质检失败：{_qc_issue}")

                    _write_clip_manifest(target_path, target_manifest)
                    return target_path
                except Exception as e:
                    last_error = e
                    if attempt < max_retries:
                        wait = 5 * attempt
                        print(f"  ⚠️ 片段 {idx} 第 {attempt} 次尝试失败：{e}，{wait}s 后重试...")
                        time.sleep(wait)
            # 最终失败：记录到历史案例库（自我进化的数据闭环）
            try:
                from quality_gate import record_failure_case
                err_str = str(last_error)
                # 分类失败类型
                if "质检失败" in err_str:
                    if "清晰度" in err_str:
                        fail_type = "quality_low_resolution"
                    elif "黑帧" in err_str:
                        fail_type = "quality_black_frame"
                    elif "时长" in err_str:
                        fail_type = "quality_duration"
                    elif "人脸" in err_str or "face" in err_str.lower():
                        fail_type = "character_face_distortion"
                    else:
                        fail_type = "quality_check_failed"
                else:
                    fail_type = "video_generation_failed"
                record_failure_case(
                    failure_type=fail_type,
                    failure_reason=err_str[:200],
                    product_category=product_info.get("type", "default"),
                    style_preference=style,
                    num_clips=len(clip_prompts),
                    segment_index=idx,
                    narrative_type=narrative,
                    prompt_length=len(prompt),
                    has_character_ref=bool(main_char_path and main_char_path.exists()),
                    has_product_ref=bool(product_image_path and product_image_path.exists()),
                    extra={
                        "retry_count": max_retries,
                        "mode": mode,
                        "image_fidelity": image_fidelity,
                        "voiceover_style": voiceover_style,
                    },
                )
            except Exception:
                pass
            raise RuntimeError(f"片段 {idx} 生成失败（已重试 {max_retries} 次）") from last_error

        best_of_n = max(1, int(_clip_best_of or 1))
        if best_of_n <= 1:
            result = _generate_to_path(clip_path)
            # best_of=1 时也记录片段级人脸质量（自我进化数据闭环）
            if narrative in {"hook", "turning", "result", "review"}:
                try:
                    from quality_gate import record_face_quality_success
                    record_face_quality_success(
                        product_category=product_info.get("type", "default"),
                        narrative_type=narrative,
                        style_preference=style,
                        quality_score=80.0,  # best_of=1 时无详细评分，用默认值
                        face_issue_count=0,
                        extra={
                            "segment_index": idx,
                            "best_of": 1,
                            "best_strategy": "single",
                            "face_strategy_applied": _face_strategy is not None and _face_strategy.get("strategy") != "conservative_default",
                        },
                    )
                except Exception:
                    pass
            return result

        candidates: List[Path] = []
        scores: Dict[Path, float] = {}
        issues: Dict[Path, List[str]] = {}

        def _score_generated_candidate(cand_path: Path) -> Tuple[float, List[str]]:
            try:
                candidate_roles = {
                    item.get("role", "unknown") for item in ref_images if isinstance(item, dict)
                }
                product_ref_for_candidate = (
                    product_image_path
                    if product_image_path
                    and "product" in candidate_roles
                    and _is_product_required_narrative(narrative)
                    else None
                )
                character_ref_for_candidate = (
                    main_char_path
                    if main_char_path and "character" in candidate_roles
                    else None
                )
                return _score_candidate_video_quality(
                    cand_path,
                    quality_frames=int(quality_frames or 12),
                    product_reference_image=product_ref_for_candidate,
                    character_reference_image=character_ref_for_candidate,
                )
            except Exception as e:
                return 0.0, [f"质量检测失败：{e}"]

        early_stop_score = 85.0
        best_path: Optional[Path] = None
        best_strategy = "single"
        repaired_prompt: Optional[str] = None
        repair_tags: List[str] = []

        for v in range(1, best_of_n + 1):
            cand_path = clips_dir / f"clip_{idx:02d}_{output_name}_cand{v}.mp4"
            candidates.append(cand_path)

            if v == 1:
                strategy = "single"
                _generate_to_path(cand_path, candidate_strategy=strategy)
            elif v == 2 and repaired_prompt:
                # 候选2：使用失败原因驱动的精准修复 prompt
                strategy = "issue_driven_repair"
                _generate_to_path(cand_path, candidate_strategy=strategy, override_prompt=repaired_prompt)
            else:
                # 候选3+：回退到原来的通用 rescue 策略
                strategy = "product_rescue" if _is_product_required_narrative(narrative) else "character_rescue"
                _generate_to_path(cand_path, candidate_strategy=strategy)

            score, candidate_issues = _score_generated_candidate(cand_path)
            scores[cand_path] = score
            issues[cand_path] = candidate_issues
            if best_path is None or score > scores.get(best_path, 0.0):
                best_path = cand_path
                best_strategy = strategy

            print(f"  📊 片段 {idx} 候选 {v}/{best_of_n}（{strategy}）：{score:.0f} 分")

            # 候选1失败后，按具体原因生成修复 prompt
            if v == 1 and score < early_stop_score and candidate_issues:
                _primary_char_bible = character_bibles[0] if character_bibles else None
                repaired_prompt, repair_tags = _repair_prompt_by_issues(
                    prompt,
                    candidate_issues,
                    product_bible=product_bible,
                    character_bible=_primary_char_bible,
                )
                if repair_tags:
                    print(f"     🔧 候选1问题：{candidate_issues[0]}")
                    print(f"     🔧 生成修复策略：{repair_tags}")

            if score >= early_stop_score:
                print(f"  ✅ 片段 {idx} 候选已达标（≥{early_stop_score:.0f}），停止继续生成候选以节省成本")
                break

        if best_path is None:
            best_path = max(candidates, key=lambda p: scores.get(p, 0.0))
        best_score = scores.get(best_path, 0.0)
        # P1 修复：所有候选质量检测均未通过时直接失败，避免选中明显废片
        if best_score <= 0:
            raise RuntimeError(
                f"片段 {idx} 的 {best_of_n} 个候选全部未通过质量检测（最高分 {best_score:.0f}），"
                f"无法选出有效片段。主要问题：{issues.get(best_path, ['未知'])[:1]}"
            )
        print(f"  🏆 片段 {idx} 自适应 best-of：{best_score:.0f} 分（已生成 {len(candidates)}/{best_of_n} 个候选）")
        if issues.get(best_path):
            print(f"     主要问题：{issues[best_path][0]}")
        if best_strategy == "issue_driven_repair" and repair_tags:
            print(f"     修复标签：{repair_tags}")

        if best_path != clip_path:
            try:
                clip_path.unlink(missing_ok=True)
                _clip_manifest_path(clip_path).unlink(missing_ok=True)
            except Exception:
                pass
            shutil.move(str(best_path), str(clip_path))
            try:
                _clip_manifest_path(best_path).unlink(missing_ok=True)
            except Exception:
                pass
        final_clip_manifest["candidate_strategy"] = best_strategy
        _write_clip_manifest(clip_path, final_clip_manifest)

        # 记录成功案例（自我进化的数据闭环）
        try:
            from quality_gate import record_success_case
            record_success_case(
                product_category=product_info.get("type", "default"),
                style_preference=style,
                num_clips=len(clip_prompts),
                quality_score=best_score,
                seed=clip_seed,
                fidelity=image_fidelity,
                mode=mode,
                extra={
                    "segment_index": idx,
                    "narrative_type": narrative,
                    "best_strategy": best_strategy,
                    "candidates_count": len(candidates),
                    "main_issues": issues.get(best_path, []),
                    "voiceover_style": voiceover_style,
                },
            )
        except Exception:
            pass

        # 片段级人脸质量成功记录（自我进化：人脸质量策略的数据闭环）
        if narrative in {"hook", "turning", "result", "review"}:
            try:
                from quality_gate import record_face_quality_success
                _face_issues = issues.get(best_path, [])
                _face_issue_count = sum(1 for iss in _face_issues if "人脸" in iss or "face" in iss.lower())
                record_face_quality_success(
                    product_category=product_info.get("type", "default"),
                    narrative_type=narrative,
                    style_preference=style,
                    quality_score=best_score,
                    face_issue_count=_face_issue_count,
                    extra={
                        "segment_index": idx,
                        "best_of": best_of_n,
                        "best_strategy": best_strategy,
                        "face_strategy_applied": _face_strategy is not None and _face_strategy.get("strategy") != "conservative_default",
                    },
                )
            except Exception:
                pass

        if not keep_candidates:
            for p in candidates:
                if p == best_path:
                    continue
                try:
                    p.unlink(missing_ok=True)
                    _clip_manifest_path(p).unlink(missing_ok=True)
                except Exception:
                    pass

        return clip_path

    if local_asset_mode:
        if use_voiceover:
            local_one_take_master = final_dir / f"{output_name}_voiceover_master.m4a"
            local_one_take_timeline = _prepare_local_one_take_master(
                ad_script=ad_script,
                asset_index=local_asset_index or {},
                product_info=product_info,
                requested_voice=voice,
                creative_profile=asset_creative_profile,
                reference_profile=reference_profile,
                transition_duration=float(scene_cfg.get("transition_duration") or 0.0),
                output_path=local_one_take_master,
            )
            voice = str(local_one_take_timeline["voice"])
            def _apply_one_take_timeline() -> None:
                duration_by_segment = local_one_take_timeline["clip_durations"]
                for rhythm_segment in rhythm_template["segments"]:
                    segment_index = int(rhythm_segment["index"])
                    rhythm_segment["duration"] = float(duration_by_segment[segment_index])
                rhythm_template["total_duration"] = float(local_one_take_timeline["main_duration"])
                rhythm_template["actual_total_duration"] = float(local_one_take_timeline["main_duration"])
                rhythm_template["duration_source"] = "single_take_tts_timeline"

            _apply_one_take_timeline()
            print(
                f"✅ 单条口播母带：{local_one_take_timeline['audio_duration']:.2f}s，"
                f"主片 {local_one_take_timeline['main_duration']:.2f}s，"
                f"CTA {local_one_take_timeline['outro_duration']:.2f}s"
            )
            for line in local_one_take_timeline["voiceover_lines"]:
                print(
                    f"    [{line['segment']}] {line['start']:.2f}-{line['end']:.2f}s  {line['text']}"
                )
        print("\n🎞️ 本地素材选片：根据素材约束脚本自动匹配片段")
        from local_asset_pipeline import LocalAssetError, plan_and_materialize_local_clips

        if local_one_take_timeline:
            plan_and_materialize_local_clips(
                asset_index=local_asset_index or {},
                ad_script=ad_script,
                rhythm_template=rhythm_template,
                clips_dir=clips_dir / f"{output_name}_local_assets",
                final_dir=final_dir,
                output_name=output_name,
                product_info=product_info,
                plan_only=True,
                record_failure=False,
            )

        local_asset_result = plan_and_materialize_local_clips(
            asset_index=local_asset_index or {},
            ad_script=ad_script,
            rhythm_template=rhythm_template,
            clips_dir=clips_dir / f"{output_name}_local_assets",
            final_dir=final_dir,
            output_name=output_name,
            product_info=product_info,
        )
        clip_paths = local_asset_result["clip_paths"]
        successful_clip_indices = local_asset_result.get(
            "edit_indices",
            list(range(len(clip_paths))),
        )
        local_edit_semantic_indices = local_asset_result.get(
            "semantic_indices",
            list(successful_clip_indices),
        )
        local_asset_edit_report = local_asset_result.get("edit_decision_report")
        local_asset_frame_evidence_report = local_asset_result.get("frame_evidence_report")
        selected_material_segments = local_asset_result.get("selected_segments") or []
        asset_creative_profile = (
            local_asset_result.get("creative_profile") or asset_creative_profile
        )
        music_contract = build_music_contract(
            product_info,
            cinematic_style=style,
            asset_creative_profile=asset_creative_profile,
        )
        scene_cfg["transition_duration"] = float(
            asset_creative_profile["transition_base_duration"]
        )
        from postproduction_contract import (
            build_local_postproduction_contract,
            write_postproduction_contract,
        )
        postproduction_contract = build_local_postproduction_contract(
            selected_segments=selected_material_segments,
            creative_profile=asset_creative_profile,
            music_contract=music_contract,
            reference_profile=reference_profile or {},
        )
        postproduction_contract["story_contract"] = local_story_contract
        postproduction_contract_path = write_postproduction_contract(
            postproduction_contract,
            final_dir / f"{output_name}_postproduction_contract.json",
        )
        expected_segments = len(ad_script.get("segments", [])) if isinstance(ad_script, dict) else len(clip_paths)
        total_clips = len(clip_paths)
        seg_indices_for_subtitles = None
        rhythm_template = {
            **rhythm_template,
            "segments": [
                {
                    "index": int(selected["edit_index"]),
                    "duration": float(selected["target_duration"]),
                    "type": str(selected.get("narrative") or "showcase"),
                    "narrative": str(selected.get("narrative") or "showcase"),
                    "purpose": str(selected.get("product_story_role") or ""),
                    "semantic_segment": int(selected["semantic_segment"]),
                }
                for selected in selected_material_segments
            ],
            "total_duration": sum(
                float(selected["target_duration"])
                for selected in selected_material_segments
            ),
            "actual_total_duration": sum(
                float(selected["target_duration"])
                for selected in selected_material_segments
            ),
            "duration_source": "single_take_tts_edit_clip_timeline",
        }
        print(
            f"✅ 本地素材选片完成：{expected_segments} 个语义段 / "
            f"{len(clip_paths)} 个实际镜头"
        )
        print(
            f"🎛️ 选片后视听合同：{music_contract['bpm_min']}-{music_contract['bpm_max']} BPM / "
            f"{music_contract['energy']} / SFX {music_contract['sfx_intensity']}"
        )
        if local_asset_edit_report:
            print(f"📄 剪辑决策报告：{Path(local_asset_edit_report).name}")
        if local_asset_frame_evidence_report:
            print(f"🔬 素材帧证据审计：{Path(local_asset_frame_evidence_report).name}")
        print(f"🎛️ 素材驱动后期合同：{Path(postproduction_contract_path).name}")
    else:
        # ── 第 1 段：串行生成，用于提取全局场景锚点 ──
        # P1 #2（v2）：用 1-based 的 i 追踪成功段，段索引 = i-1（0-based）
        successful_clip_indices = [0]  # 第 1 段固定在此初始化；若失败则下方会抛异常
        _sep = "=" * 50
        print(f"\n{_sep}")
        print(f"🎬 片段 1/{total_clips}（串行）：{clip_prompts[0][:60]}...")
        print(_sep)
        # 问题2修复：第1段失败时用清晰的 RuntimeError，而不是让异常向上抳潮成模糊的崩溃信息
        try:
            first_clip = _generate_one_clip(1, clip_prompts[0])
        except Exception as e:
            raise RuntimeError(
                f"第 1 段视频生成失败（该段为场景锚点来源，不可跳过）：{e}"
            ) from e
        clip_paths.append(first_clip)
        elapsed = time.time() - generation_start
        print(f"  ✅ 片段 1/{total_clips} 完成 | 已用 {int(elapsed)}s")

        # 提取全局场景锚点
        if scene_cfg.get("use_scene_anchor", True):
            print("  🎬 提取全局场景锚点...")
            scene_anchor_frames = _build_scene_anchor(
                first_clip,
                clips_dir,
                num_keyframes=scene_cfg.get("anchor_keyframes", 2),
            )
            if scene_anchor_frames:
                print(f"    ✅ 场景锚点已建立：{len(scene_anchor_frames)} 张关键帧")

        # ── 第 2-N 段：并行或串行生成 ──
        # 并行模式：只使用商品/人物参考图和场景锚点，不再把第 1 段尾帧当作所有后续段的上一帧。
        # 串行模式：每段动态传前一段路径，极致一致性
        remaining_prompts = clip_prompts[1:]
        if remaining_prompts:
            failed_indices = []

            if parallel:
                print(f"\n🎬 并行生成剩余 {len(remaining_prompts)} 个片段（最大并发 {max_workers}）...")
                print("   模式：商品/人物参考图 + 全局场景锚点（不使用伪上一帧，降低冲突）")

                # 构建并行任务参数（idx 是 1-based）
                tasks = []
                for i, prompt in enumerate(remaining_prompts, 2):
                    tasks.append({"idx": i, "prompt": prompt})

                # 线程安全的进度追踪
                _status_lock = Lock()
                _clip_status: Dict[int, str] = {t["idx"]: "排队中" for t in tasks}
                _results: Dict[int, Optional[Path]] = {}

                def _parallel_worker(task: dict) -> tuple:
                    idx = task["idx"]
                    prompt = task["prompt"]
                    with _status_lock:
                        _clip_status[idx] = "生成中"
                    try:
                        path = _generate_one_clip(idx, prompt, None, None)
                        with _status_lock:
                            _clip_status[idx] = "完成"
                            _results[idx] = path
                        elapsed = time.time() - generation_start
                        done = sum(1 for v in _results.values() if v is not None)
                        total_remaining = len(tasks) - done
                        # 打印单段完成进度
                        print(f"  ✅ 片段 {idx}/{total_clips} 完成 | 已用 {int(elapsed)}s | 还剩 {total_remaining} 段")
                        return (idx, path, None)
                    except Exception as e:
                        with _status_lock:
                            _clip_status[idx] = f"失败: {e}"
                            _results[idx] = None
                        print(f"  ❌ 片段 {idx} 失败：{e}")
                        return (idx, None, e)

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(_parallel_worker, t) for t in tasks]
                    for future in as_completed(futures):
                        idx, path, err = future.result()
                        # 结果已在 worker 中写入 _results

                # 按索引顺序收集成功的片段
                for i in range(2, total_clips + 1):
                    path = _results.get(i)
                    if path is not None:
                        clip_paths.append(path)
                        successful_clip_indices.append(i - 1)
                    else:
                        failed_indices.append(i)

            else:
                # ── 串行模式（极致一致性）──
                print(f"\n🎬 串行生成剩余 {len(remaining_prompts)} 个片段（prev 动态更新）...")
                for i, prompt in enumerate(remaining_prompts, 2):
                    prev = clip_paths[-1] if clip_paths else None
                    print(f"\n{_sep}")
                    print(f"🎬 片段 {i}/{total_clips}：{prompt[:60]}...")
                    print(_sep)
                    try:
                        path = _generate_one_clip(i, prompt, prev)
                        clip_paths.append(path)
                        successful_clip_indices.append(i - 1)  # i 是 1-based，段索引 = i-1
                        elapsed = time.time() - generation_start
                        done = len(clip_paths)
                        remaining_count = total_clips - done
                        avg = elapsed / done if done > 0 else 0
                        eta = avg * remaining_count
                        eta_str = f"{int(eta//60)}分{int(eta%60)}秒" if eta > 60 else f"{int(eta)}秒"
                        print(f"  ✅ 片段 {i}/{total_clips} 完成 | 已用 {int(elapsed)}s | 预计还需 {eta_str}")
                    except Exception as e:
                        print(f"  ❌ 片段 {i} 失败：{e}")
                        failed_indices.append(i)
                        # 单段失败不立即崩溃，继续尝试后续片段

            if failed_indices:
                success_count = len(clip_paths)
                print(f"\n⚠️  {len(failed_indices)} 个片段生成失败：{failed_indices}")
                print(f"   成功片段数：{success_count}/{total_clips}")
                if strict_mode and not preview:
                    raise RuntimeError(
                        f"发布级成片要求分镜完整，但片段 {failed_indices} 生成失败。"
                        "已阻断缺段合成，避免丢失产品展示、效果证明或 CTA。"
                    )
                # 最少成功段数（默认 3，即 60%）
                min_required = max(2, min_clips)
                if success_count < min_required:
                    raise RuntimeError(
                        f"片段生成失败过多（成功 {success_count}/{total_clips}，需要 ≥{min_required} 段），无法继续合成"
                    )
                print(f"   成功片段 ≥{min_required}，继续后续合成流程（将跳过失败片段）")

        # 部分成功时，seg_indices 记录实际成功的段索引（用于字幕/口播/音效对齐）
        # 全部成功时设为 None，退化到原有逻辑，避免不必要的白名单过滤
        seg_indices_for_subtitles = (
            successful_clip_indices if len(successful_clip_indices) < total_clips else None
        )

        print(f"\n✅ 成功生成 {len(clip_paths)}/{total_clips} 个片段")

    # 清理关键帧临时文件
    _cleanup_keyframes(clips_dir, output_name)

    # ============================================================
    # 片段色调一致性匹配
    # ============================================================
    # 以第一段为参考，匹配后续片段的亮度和色偏，减少跳切感
    if len(clip_paths) > 1 and not local_asset_mode:
        print()
        print("🎨 片段色调一致性匹配...")
        color_match_dir = clips_dir / f"{output_name}_color_matched"
        matched_clips = color_match_clips(clip_paths, color_match_dir)
        if any(c != o for c, o in zip(matched_clips, clip_paths)):
            clip_paths = matched_clips
            print(f"✅ 色调匹配完成")
        else:
            print(f"   片段色调一致，无需调整")

        # P0-B：跨片段人物/肤色一致性校验（警告级，不阻断）
        # 以第 1 段为基准，比较每段中间帧最大肤色区域面积比，偏差 >40% 打印警告
        try:
            import tempfile as _cc_tmp
            from quality_checker import _detect_skin_regions as _cc_skin
            from PIL import Image as _cc_pil

            _cc_ref_ratio: float = -1.0
            _consistency_warnings: list = []

            with _cc_tmp.TemporaryDirectory(prefix="cc_qc_") as _cc_dir:
                for _cc_i, _cc_clip in enumerate([] if local_asset_mode else clip_paths):
                    try:
                        _cc_dur = _get_clip_duration(_cc_clip)
                        _cc_t = _cc_dur * 0.5
                        _cc_frame = Path(_cc_dir) / f"cc_{_cc_i}.jpg"
                        _cc_cmd = [
                            "ffmpeg", "-y",
                            "-ss", f"{_cc_t:.2f}", "-i", str(_cc_clip),
                            "-frames:v", "1", "-q:v", "4",
                            "-vf", "scale=320:-1",
                            str(_cc_frame),
                        ]
                        _ccr = subprocess.run(_cc_cmd, capture_output=True, timeout=10)
                        if _ccr.returncode != 0 or not _cc_frame.exists():
                            continue
                        _cc_img = _cc_pil.open(_cc_frame).convert("RGB")
                        _cc_regions = _cc_skin(_cc_img)
                        _cc_img_area = _cc_img.width * _cc_img.height
                        _cc_ratio = _cc_regions[0]["area"] / _cc_img_area if _cc_regions else 0.0
                        if _cc_i == 0:
                            _cc_ref_ratio = _cc_ratio
                        elif _cc_ref_ratio > 0.02 and _cc_ratio > 0.0:
                            _cc_diff = abs(_cc_ratio - _cc_ref_ratio) / _cc_ref_ratio
                            if _cc_diff > 0.40:
                                _msg = (
                                    f"片段 {_cc_i+1} 与片段 1 人物肤色区域面积差异 "
                                    f"{_cc_diff*100:.0f}%（>{40}%），人物/构图可能不一致"
                                )
                                _consistency_warnings.append(_msg)
                                print(f"   ⚠️  P0-B 一致性：{_msg}")
                    except Exception:
                        pass

            if not local_asset_mode and not _consistency_warnings:
                print("   ✅ P0-B 跨片段一致性校验通过")
        except Exception as _cc_err:
            _consistency_warnings = []
            print(f"   P0-B 一致性校验跳过（{_cc_err}）")

        # F4 修复：拼接前对各片段做直方图均衡，保证相邻片段曝光接近。
        # 问题：色调匹配只做通道增益修正，无法消除片段间整体曝光差异（如室内偏暗 vs 室外偏亮），
        # xfade dissolve 转场时亮度突变会在过渡帧中明显可见（"闪一下"）。
        # 做法：对每个片段提取中间帧的直方图，计算相对于全片段均值的曝光偏差，
        # 偏差超阈值时用 ffmpeg eq 做轻量亮度补偿（不影响已做的色调匹配）。
        try:
            _histeq_dir = clips_dir / f"{output_name}_histeq"
            _histeq_dir.mkdir(parents=True, exist_ok=True)
            _brightness_vals = []
            for _cp in clip_paths:
                try:
                    # 用 ffprobe/ffmpeg 提取中间帧亮度均值
                    _mid_t = _get_clip_duration(_cp) / 2
                    _luma_cmd = [
                        "ffmpeg", "-ss", f"{_mid_t:.2f}", "-i", str(_cp),
                        "-frames:v", "1", "-f", "rawvideo",
                        "-pix_fmt", "gray", "-vf", "scale=160:90", "-"
                    ]
                    _lr = subprocess.run(_luma_cmd, capture_output=True, timeout=10)
                    if _lr.returncode == 0 and _lr.stdout:
                        import array as _arr2
                        _px = _arr2.array("B", _lr.stdout)
                        _brightness_vals.append(sum(_px) / max(len(_px), 1))
                    else:
                        _brightness_vals.append(None)
                except Exception:
                    _brightness_vals.append(None)

            _valid_br = [b for b in _brightness_vals if b is not None]
            if len(_valid_br) >= 2:
                # 取中位数作为目标亮度（比均值抗异常值）
                _sorted_br = sorted(_valid_br)
                _target_br = _sorted_br[len(_sorted_br) // 2]
                _histeq_clips = []
                for _ci2, (_cp2, _br) in enumerate(zip(clip_paths, _brightness_vals)):
                    if _br is None:
                        _histeq_clips.append(_cp2)
                        continue
                    _diff = _target_br - _br
                    # 偏差超过 15 灰度值（满幅 255）才做补偿，避免过度处理
                    if abs(_diff) < 15:
                        _histeq_clips.append(_cp2)
                        continue
                    _eq_brightness = max(-0.08, min(0.08, _diff / 255.0))
                    _heq_path = _histeq_dir / f"{output_name}_clip_{_ci2+1:02d}_heq.mp4"
                    try:
                        _heq_cmd = [
                            "ffmpeg", "-y", "-i", str(_cp2),
                            "-vf", f"eq=brightness={_eq_brightness:.3f}",
                            "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                            "-c:a", "copy", str(_heq_path)
                        ]
                        subprocess.run(_heq_cmd, capture_output=True, timeout=120, check=True)
                        _histeq_clips.append(_heq_path)
                        print(f"   F4 直方图均衡：片段 {_ci2+1} 亮度补偿 {_diff:+.0f} ({_eq_brightness:+.3f})")
                    except Exception as _heq_err:
                        print(f"   F4 均衡失败（片段 {_ci2+1}）：{_heq_err}，跳过")
                        _histeq_clips.append(_cp2)
                clip_paths = _histeq_clips
                print(f"✅ 拼接前直方图均衡完成")
        except Exception as _f4_err:
            print(f"  ⚠️  F4 直方图均衡失败（{_f4_err}），跳过")
    elif local_asset_mode:
        print("🎨 本地素材保护：保留原始产地/原料/产品镜头色彩，不做跨场景强制匹配")

    # ============================================================
    # 片段时长适配节奏模板（裁切/变速到目标时长）
    # ============================================================
    # 策略：可灵 API 生成固定时长（默认 5s），后期裁到节奏模板指定的段时长
    # 部分成功时：按实际成功段的索引去节奏模板里找对应目标时长
    _adjusted_dir = clips_dir / f"{output_name}_rhythm_adjusted"
    _adjusted_dir.mkdir(parents=True, exist_ok=True)
    _seg_indices_list = sorted(successful_clip_indices)

    # P0-C 修复：_seg_dur_map 提前在节奏适配前初始化，这样 B4 in-place 更新才能生效。
    # 原来放在 L2715（字幕生成前）导致 B4 写入时触发 NameError 被静默吞掉，
    # 字幕始终用模板时长而非 ffprobe 实测时长。
    _seg_dur_map: dict = {s["index"]: s["duration"] for s in rhythm_template["segments"]}

    try:
        _rhythm_segs = {s["index"]: s for s in rhythm_template["segments"]}
        _adjusted_paths = []
        _any_adjusted = False

        for _pos, _clip_path in enumerate(clip_paths):
            _orig_idx = _seg_indices_list[_pos]  # 原始段索引
            _target_seg = _rhythm_segs.get(_orig_idx)
            if _target_seg is None:
                # 节奏模板里没有对应段（理论上不会发生，防御性跳过）
                _adjusted_paths.append(_clip_path)
                continue

            _target_dur = _target_seg["duration"]
            _adjusted_path = _adjusted_dir / f"{output_name}_clip_{_orig_idx:02d}_adjusted.mp4"

            # 检查是否需要调整（差异 > 0.2s 才调，避免不必要的重编码）
            try:
                _orig_dur = _get_clip_duration(_clip_path)
                if local_asset_mode:
                    if _orig_dur + 0.08 < _target_dur:
                        raise RuntimeError(
                            f"本地素材镜头 {_orig_idx} 实测 {_orig_dur:.2f}s 短于规划 "
                            f"{_target_dur:.2f}s；禁止通过变速或冻结补时"
                        )
                    _adjusted_paths.append(_clip_path)
                elif abs(_orig_dur - _target_dur) > 0.2:
                    # Bug #3 修复：透传叙事类型，让 hook 段从头裁保留开头精华帧
                    _narrative = _target_seg.get("narrative", _target_seg.get("type", "showcase"))
                    adjust_clip_duration(
                        clip_path=_clip_path,
                        output_path=_adjusted_path,
                        target_duration=_target_dur,
                        narrative=_narrative,
                    )
                    _adjusted_paths.append(_adjusted_path)
                    _any_adjusted = True
                else:
                    _adjusted_paths.append(_clip_path)
            except Exception as _adj_err:
                print(f"  ⚠️  段 {_orig_idx} 时长调整失败：{_adj_err}，使用原片段")
                _adjusted_paths.append(_clip_path)

        if _any_adjusted:
            clip_paths = _adjusted_paths
            print(f"✅ 节奏适配完成（片段已调整到节奏模板目标时长）")
            print(f"   策略：中间裁切保留核心画面，保持节奏精准")
        else:
            print(f"   片段时长与节奏模板一致，无需调整")

        # B4 修复：节奏适配后用 ffprobe 实测各片段实际时长，更新 _seg_dur_map。
        # P5 转场溢出保护原来用模板时长（_seg_dur_map），但适配后实际时长可能与模板有 ±0.1s 偏差，
        # 导致 xfade offset 超出实际片段时长，出现黑帧。
        # 用 ffprobe 实测值覆盖 _seg_dur_map，让 P5 保护基于真实时长做判断。
        try:
            for _pos2, _cp2 in enumerate(clip_paths):
                if _pos2 >= len(_seg_indices_list):
                    break
                _oidx2 = _seg_indices_list[_pos2]
                try:
                    _measured_dur = _get_clip_duration(_cp2)
                    if _measured_dur > 0:
                        _seg_dur_map[_oidx2] = _measured_dur
                except Exception:
                    pass
            print(f"   B4：已用 ffprobe 实测时长更新转场保护基准")
        except Exception as _b4_err:
            print(f"  ⚠️  B4 实测时长更新失败（{_b4_err}），继续使用模板时长")

    except Exception as e:
        print(f"⚠️  节奏适配失败：{e}，继续使用原始时长片段")

    if strict_mode and not preview:
        print("🔍 开始分段语义质量检测...")
        _check_segment_semantic_quality(
            clip_paths=clip_paths,
            successful_clip_indices=(
                local_edit_semantic_indices if local_asset_mode else _seg_indices_list
            ),
            ad_script=ad_script,
            product_image_path=product_image_path,
            main_char_path=main_char_path,
            quality_frames=quality_frames,
        )
        print("✅ 分段语义质量检测通过")

    # ============================================================
    # 第三步：拼接视频 + BGM
    # ============================================================
    merged_path = final_dir / f"{output_name}_merged.mp4"

    product_type = product_info.get("type", "default")

    # 本地素材使用选中镜头的动态分析；生成素材使用节奏模板。
    _pace_map = {"fast": "fast", "moderate": "medium", "cinematic": "slow"}
    _rhythm_pace_for_bgm = _pace_map.get(
        music_contract["recommended_pace"] if local_asset_mode else _rhythm_pace
    )
    if _rhythm_pace_for_bgm:
        pace = _rhythm_pace_for_bgm
    else:
        from config import CLIP_STRUCTURE
        pace = _detect_pace_from_clips(CLIP_STRUCTURE)

    bgm_file = None
    fallback_audio_used = False
    _actual_segment_timeline = []

    try:
        # 智能转场：真实镜头特征 + 叙事节奏 + 候选实渲染质量验证。
        _all_narratives = [
            seg.get("narrative", "showcase")
            for seg in ad_script.get("segments", [])
        ]
        # 按实际成功段过滤（对齐字幕/口播的 seg_indices 逻辑）
        if local_asset_mode:
            _success_narratives = [
                _all_narratives[index] if 0 <= index < len(_all_narratives) else "showcase"
                for index in local_edit_semantic_indices
            ]
        elif seg_indices_for_subtitles is not None:
            _success_narratives = [
                _all_narratives[i] if i < len(_all_narratives) else "showcase"
                for i in sorted(seg_indices_for_subtitles)
            ]
        else:
            _success_narratives = _all_narratives[:len(clip_paths)] if _all_narratives else ["showcase"] * len(clip_paths)

        # 节奏风格：从节奏模板取；未知值归一为 moderate，不改变质量门槛。
        _transition_style = _rhythm_pace if _rhythm_pace in ("fast", "moderate", "cinematic") else "moderate"
        _base_transition_dur = scene_cfg.get("transition_duration", 0.3)
        from intelligent_transition import (
            TransitionLearningStore,
            plan_intelligent_transitions,
            write_transition_report,
        )

        _transition_learning_store = TransitionLearningStore(
            PROJECT_ROOT / "data" / "transition_learning.db"
        )
        transition_decision_report = plan_intelligent_transitions(
            clip_paths,
            _success_narratives,
            style=_transition_style,
            base_duration=_base_transition_dur,
            work_dir=clips_dir / f"{output_name}_transition_previews",
            learning_store=_transition_learning_store,
            verification_id=output_name,
            max_total_overlap=(
                max(
                    0.0,
                    sum(_get_clip_duration(path) for path in clip_paths)
                    - float(local_one_take_timeline["main_duration"]),
                )
                if local_asset_mode and local_one_take_timeline else None
            ),
        )
        scene_transitions = normalize_transition_decisions(
            clip_paths,
            transition_decision_report["transitions"],
        )
        transition_decision_report["transitions"] = scene_transitions
        for _boundary, _decision in zip(
            transition_decision_report.get("boundaries") or [],
            scene_transitions,
        ):
            _boundary["selected"].update(_decision)

        _actual_segment_timeline = compute_segment_timeline(
            rhythm_template,
            seg_indices=seg_indices_for_subtitles,
            segment_durations=_seg_dur_map,
            transitions=scene_transitions,
        )
        _semantic_segment_timeline = (
            _collapse_edit_timeline_by_semantic(
                _actual_segment_timeline,
                local_edit_semantic_indices,
            )
            if local_asset_mode else _actual_segment_timeline
        )
        total_video_duration = (
            _actual_segment_timeline[-1]["end"]
            if _actual_segment_timeline
            else len(clip_paths) * duration
        )

        # BGM 选曲必须等智能转场定稿后再执行，目标时长和节奏来自真实渲染时间轴。
        bgm_file = pick_bgm_for_product(
            product_type,
            target_duration=total_video_duration,
            cinematic_style=style,
            pace=pace,
            music_contract=music_contract,
        )
        bgm_available = bgm_file is not None and Path(bgm_file).exists()
        if not bgm_available:
            raise RuntimeError(
                "BGM 不可用，已阻断不可发布成片；请配置可商用本地 BGM 或检查音乐下载服务"
            )
        try:
            bgm_info = get_bgm_copyright_info(bgm_file)
            print(f"🎵 BGM：{bgm_info['title']}（{bgm_info['source']}）")
            if not bgm_info["is_commercial_safe"]:
                print(f"   ⚠️  {bgm_info['warning']}")
        except Exception as _bgm_info_err:
            print(f"🎵 BGM 已选定（版权信息读取失败：{_bgm_info_err}）")

        transition_report_path = write_transition_report(
            transition_decision_report,
            final_dir / f"{output_name}_transition_decision_report.json",
        )
        if postproduction_contract:
            from video_merger import (
                _detect_beats as _bgm_detect_beats,
                _estimate_bpm as _bgm_estimate_bpm,
                _get_audio_duration as _bgm_audio_duration,
                select_bgm_segment,
            )
            _selected_bgm_bpm = _bgm_estimate_bpm(_bgm_detect_beats(Path(bgm_file)))
            _selected_bgm_segment = select_bgm_segment(
                _bgm_audio_duration(Path(bgm_file)),
                total_video_duration,
                Path(bgm_file),
                music_contract=music_contract if local_asset_mode else None,
            )
            postproduction_contract["transition"]["decisions"] = scene_transitions
            postproduction_contract["timeline"] = {
                "basis": "ffprobe_clip_durations_and_rendered_transitions",
                "edit_segments": _actual_segment_timeline,
                "segments": _semantic_segment_timeline,
                "main_duration": total_video_duration,
            }
            postproduction_contract["bgm"].update({
                "selected_file": str(bgm_file),
                "selected_title": bgm_info.get("title") if "bgm_info" in locals() else Path(bgm_file).stem,
                "detected_bpm": _selected_bgm_bpm,
                "pace": pace,
                "target_duration": total_video_duration,
                "segment_selection": _selected_bgm_segment,
            })

        print()
        print(f"🎬 智能转场序列（{_transition_style} 风格，已通过实渲染质量门）：")
        for i, boundary in enumerate(transition_decision_report["boundaries"]):
            selected = boundary["selected"]
            _from = _success_narratives[i] if i < len(_success_narratives) else "?"
            _to = _success_narratives[i + 1] if i + 1 < len(_success_narratives) else "?"
            print(
                f"   {i}→{i+1} ({_from} → {_to}): {selected['type']} "
                f"({selected['duration']:.2f}s，综合 {selected['combined_score']:.2f})"
            )
        print(f"📄 转场决策报告：{transition_report_path.name}")
        print()

        _bgm_key_times = [s["start"] for s in _actual_segment_timeline] or None

        merge_clips_ffmpeg(
            clips=clip_paths,
            output=merged_path,
            transitions=scene_transitions,
            bgm=bgm_file,
            envelope_key_times=_bgm_key_times,
            strict_transitions=True,
            music_contract=music_contract if local_asset_mode else None,
            bgm_segment=(
                _selected_bgm_segment
                if "_selected_bgm_segment" in locals()
                else None
            ),
        )
        from intelligent_transition import validate_merged_transition_boundaries

        _merged_transition_result = validate_merged_transition_boundaries(
            merged_path,
            transition_decision_report,
            store=_transition_learning_store,
            verification_id=output_name,
        )
        transition_decision_report["merged_validation"] = _merged_transition_result
        write_transition_report(transition_decision_report, transition_report_path)
        if postproduction_contract:
            postproduction_contract["transition"]["merged_validation"] = _merged_transition_result
        if not _merged_transition_result["passed"]:
            raise RuntimeError(
                "合成后转场质量门未通过："
                + ", ".join(_merged_transition_result["failed_boundaries"])
            )
        print(f"✅ 视频拼接完成：{merged_path.name}")
    except Exception as e:
        print(f"❌ 视频拼接失败：{e}")
        raise RuntimeError("视频拼接失败") from e

    # ============================================================
    # 第四步：统一调色（提前到字幕烧录前）
    # ============================================================
    # 优化 #6 修复：调色必须在字幕烧录之前对整段视频执行一次
    # 原来放在第七步（字幕/音效之后），每次字幕/音效重编码都会引入非线性色调变化，
    # 打乱已经匹配好的片段间色调一致性。现在提前到拼接后直接对 merged_path 统一调色。
    graded_path = final_dir / f"{output_name}_graded.mp4"

    try:
        color_preset = "none" if local_asset_mode else get_color_grading_for_style(style)
        print(f"🎨 应用调色预设：{color_preset}（匹配电影风格：{style}）")

        apply_color_grading(
            video=merged_path,
            output=graded_path,
            preset=color_preset,
            brand_color_tint=not local_asset_mode,
        )
        print(f"✅ 调色完成：{graded_path.name}")
    except Exception as e:
        print(f"⚠️  调色失败：{e}")
        graded_path = merged_path

    # ============================================================
    # P1-A：视频稳定化 + 去闪烁（--stabilize 启用时）
    # 放在调色后、预裁切前，确保稳定化基于已调色视频
    # ============================================================
    if stabilize:
        print()
        print("🎬 P1-A 视频稳定化 + 去闪烁...")
        _stab_path = final_dir / f"{output_name}_stabilized.mp4"
        try:
            from video_merger import stabilize_video as _stabilize_fn
            _stab_result = _stabilize_fn(graded_path, _stab_path, smoothing=10, deflicker=True)
            if _stab_result == _stab_path and _stab_path.exists():
                graded_path = _stab_path
                print(f"✅ 稳定化完成：{_stab_path.name}")
        except Exception as _stab_err:
            print(f"⚠️  稳定化跳过（{_stab_err}）")

    # ============================================================
    # P1-8：auto_trim 提前到字幕烧录前执行
    # 原来放在第八步（字幕之后），片头被裁后第一条钩子字幕会被切掉一部分。
    # 提前到调色后、字幕前，保证字幕时间轴始终相对于已裁好的视频。
    # 同时 auto_trim 使用 -c copy 流复制（速度快，不重编码），
    # 后续 export_final_video 统一做最终编码，消除原来 trim 的独立重编码。
    # ============================================================
    trimmed_for_subtitle = final_dir / f"{output_name}_trimmed_pre.mp4"
    _trim_start = 0.0
    _trim_end_amt = 0.0
    try:
        # 用 -c copy 流复制裁切（只切黑帧/冻结帧，不重编码）
        import re as _re_trim
        import subprocess as _sp_trim

        # 检测首尾异常帧时间量
        from video_merger import detect_freeze_frames as _detect_freeze
        _freeze_s, _freeze_e = _detect_freeze(graded_path)

        _black_cmd = [
            "ffmpeg", "-i", str(graded_path),
            "-vf", "blackdetect=d=0.3:pix_th=0.10",
            "-an", "-f", "null", "-",
        ]
        _black_res = _sp_trim.run(_black_cmd, capture_output=True, text=True, timeout=60)
        _black_matches = _re_trim.findall(
            r"black_start:([\d.]+)\s+black_end:([\d.]+)", _black_res.stderr
        )
        _graded_dur = _get_clip_duration(graded_path)
        _black_s = 0.0
        _black_e = 0.0
        for _bs, _be in _black_matches:
            _bsf, _bef = float(_bs), float(_be)
            if _bsf < 0.5:
                _black_s = max(_black_s, _bef)
            if _graded_dur > 0 and _bef > _graded_dur - 0.5:
                _black_e = max(_black_e, _bef - _bsf)

        _trim_start = max(_black_s, _freeze_s)
        _trim_end_amt = max(_black_e, _freeze_e)
        _trim_end = _graded_dur - _trim_end_amt if _graded_dur > 0 else _graded_dur

        if _trim_start > 0.08 or _trim_end_amt > 0.08:
            # P1 修复：改用 libx264 重编码裁切，消除 -c copy 在非关键帧处的花屏
            # -c copy 虽然快，但在 B/P 帧处裁切会产生 1~3 帧花屏，抖音开头可见
            _trim_has_audio = _has_audio_stream(graded_path)
            _recode_cmd = [
                "ffmpeg", "-y",
                "-ss", str(_trim_start),
                "-to", str(_trim_end),
                "-i", str(graded_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                "-pix_fmt", "yuv420p", "-color_range", "pc",
                "-movflags", "+faststart",
            ]
            if _trim_has_audio:
                _recode_cmd.extend(["-c:a", "copy"])
            _recode_cmd.append(str(trimmed_for_subtitle))
            run_ffmpeg(_recode_cmd, timeout=180)
            graded_path = trimmed_for_subtitle
            print(f"✂️  预裁切完成（开头 {_trim_start:.2f}s / 结尾 {_trim_end_amt:.2f}s，重编码消除花屏）")
        else:
            trimmed_for_subtitle = graded_path
    except Exception as _te:
        print(f"⚠️  预裁切失败（{_te}），继续使用原视频")
        trimmed_for_subtitle = graded_path
        _trim_start = 0.0
        _trim_end_amt = 0.0

    _rendered_main_duration = _get_clip_duration(graded_path)
    _rendered_segment_timeline = []
    _subtitle_timeline = (
        _semantic_segment_timeline
        if local_asset_mode and "_semantic_segment_timeline" in locals()
        else _actual_segment_timeline
    )
    for _timeline_item in _subtitle_timeline:
        _visible_start = max(0.0, float(_timeline_item["start"]) - _trim_start)
        _visible_end = min(
            _rendered_main_duration,
            max(0.0, float(_timeline_item["end"]) - _trim_start),
        )
        if _visible_end <= _visible_start:
            continue
        _rendered_segment_timeline.append({
            **_timeline_item,
            "start": round(_visible_start, 3),
            "end": round(_visible_end, 3),
            "duration": round(_visible_end - _visible_start, 3),
        })
    if postproduction_contract:
        postproduction_contract["timeline"].update({
            "trim_start": _trim_start,
            "trim_end": _trim_end_amt,
            "rendered_main_duration": _rendered_main_duration,
            "rendered_segments": _rendered_segment_timeline,
        })

    # ============================================================
    # 第五步：添加字幕 + 口播配音
    # ============================================================
    subtitled_path = final_dir / f"{output_name}_subtitled.mp4"

    # P1-8：字幕烧录源改为已经裁切好的 graded_path（pretrimed），
    # 保证字幕时间轴相对于已裁好的视频不会被后续 trim 切掉
    # 从广告脚本生成字幕（更丰富、更有节奏感）
    # 节奏模板驱动：每段时长从模板取，不再是均匀的 clip_duration
    actual_transition_dur = scene_cfg.get("transition_duration", 0.6)
    # P0-C 修复：_seg_dur_map 已在节奏适配前初始化，B4 已用 ffprobe 实测时长 in-place 更新。
    # 此处仅补充节奏模板中尚未被 B4 更新的 key（如节奏适配整体失败时的保底），不覆盖已有实测值。
    for _s in rhythm_template["segments"]:
        _seg_dur_map.setdefault(_s["index"], _s["duration"])
    # P1 #2（v2）：透传实际成功段索引，处理中间段失败的情况
    subtitles = script_to_subtitles(
        ad_script,
        clip_duration=duration,
        transition_duration=actual_transition_dur,
        num_clips=len(clip_paths),
        seg_indices=seg_indices_for_subtitles,
        segment_durations=_seg_dur_map,
        segment_timeline=_rendered_segment_timeline,
    )

    # 字幕时间对齐 BGM 节拍（卡点效果）
    if bgm_file and bgm_file.exists():
        # P0-3：仅在无口播时做 beat 对齐；有口播时字幕对齐交给 voiceover 接管
        # 两次对齐叠加会导致时间轴累计偏移 0.1~0.3s，口播场景字幕明显滞后
        _beat_align_needed = not use_voiceover
        if _beat_align_needed:
            subtitles = align_subtitles_to_beats(subtitles, bgm_file)

    # 生成口播配音（如果启用）
    # P0-3：有口播时，用 voiceover 对齐的字幕直接替代 beat 对齐结果（只做一次对齐），
    # 原来两次对齐叠加会导致字幕时间轴整体偏移 0.1~0.3s
    _reference_outro = bool(
        reference_profile
        and reference_profile.get("cta_text")
        and float(reference_profile.get("outro_duration") or 0.0) > 0
    )
    _defer_full_voiceover_mix = False
    _outro_voice_start: Optional[float] = None
    voiceover_enabled = False
    voiceover_audio = final_dir / f"{output_name}_voiceover.m4a"
    voice_performance: Dict[str, Any] = {}
    voiceover_subs: List[Dict[str, Any]] = []
    if use_voiceover:
        try:
            print(
                f"🎤 {'复用全视频唯一 TTS 母带' if local_one_take_master else '生成 AI 口播'}"
                f"（脚本风格：{script_style}，音色：{voice}）"
            )
            # 从广告脚本生成口播文案（比模板更丰富）
            # P1 #2（v2）：透传实际成功段索引，处理中间段失败的情况
            voiceover_script = (
                list(local_one_take_timeline["voiceover_lines"])
                if local_one_take_timeline else
                script_to_voiceover(
                    ad_script,
                    clip_duration=duration,
                    transition_duration=actual_transition_dur,
                    num_clips=len(clip_paths),
                    seg_indices=seg_indices_for_subtitles,
                    segment_durations=_seg_dur_map,
                    segment_timeline=_rendered_segment_timeline,
                    voiceover_style=voiceover_style,
                )
            )
            if local_one_take_timeline:
                _voice_reason = str(local_one_take_timeline["voice_reason"])
            else:
                voice, _voice_reason = recommend_voice_for_narration(
                    product_info,
                    voiceover_script,
                    requested_voice=voice,
                    creative_profile=asset_creative_profile if local_asset_mode else None,
                )
            print(f"  🎙️ 智能音色：{voice}（{_voice_reason}）")
            _actual_video_dur = _rendered_main_duration
            if _actual_video_dur > 0:
                total_duration = _actual_video_dur
            else:
                _vo_timeline = _rendered_segment_timeline
                total_duration = _vo_timeline[-1]["end"] if _vo_timeline else len(clip_paths) * duration
            if _reference_outro:
                if not local_one_take_timeline:
                    voiceover_script = _append_outro_timing_to_voiceover_script(
                        voiceover_script,
                        outro_cue=str(ad_script.get("voiceover_outro_cue") or ""),
                        main_duration=total_duration,
                        outro_duration=float(reference_profile["outro_duration"]),
                    )
                total_duration += float(reference_profile["outro_duration"])
            continuous_voiceover_text = None
            if local_asset_mode:
                continuous_voiceover_text = str(ad_script.get("voiceover_full") or "").strip()
            voiceover_audio, voiceover_subs = generate_full_voiceover(
                voiceover_script,
                voiceover_audio,
                voice=voice,
                total_duration=total_duration,
                continuous_narration=local_asset_mode,
                continuous_text=continuous_voiceover_text,
                performance_profile=voice_performance,
                pre_generated_audio=local_one_take_master,
            )
            if local_one_take_timeline:
                voice_performance.update({
                    "source_tempo_multiplier": float(
                        local_one_take_timeline.get("tempo_multiplier") or 1.0
                    ),
                    "tts_external_requests": int(
                        local_one_take_timeline.get("tts_external_requests") or 0
                    ),
                    "tts_cache_hit": bool(local_one_take_timeline.get("tts_cache_hit")),
                })
            _validate_voiceover_audio(voiceover_audio)
            # 用口播对齐的字幕替换原字幕（更精准）
            main_voiceover_subs = [
                item for item in voiceover_subs
                if not item.get("is_outro")
            ]
            outro_voiceover_subs = [
                item for item in voiceover_subs
                if item.get("is_outro")
            ]
            if outro_voiceover_subs:
                _outro_voice_start = float(outro_voiceover_subs[0]["start"])
            subtitles = align_subtitles_to_voiceover(subtitles, main_voiceover_subs)
            voiceover_enabled = True
            _defer_full_voiceover_mix = bool(_reference_outro)
            if postproduction_contract:
                postproduction_contract["voice"].update({
                    "selected_voice": voice,
                    "selection_reason": _voice_reason,
                    "performance": voice_performance,
                })
            print(f"✅ 口播生成完成：{voiceover_audio.name}")
        except Exception as e:
            print(f"⚠️  口播生成失败：{e}")
            voiceover_enabled = False
            if strict_mode or fallback_audio_used:
                raise RuntimeError("请求了口播但未生成有效口播音频，已阻断不可发布成片") from e

    # ============================================================
    # 无声视频检测（兜底音频已在拼接前生成，此处仅处理口播补充场景）
    # ============================================================
    # fallback 音轨只允许作为口播混音占位，不能单独成为发布音频
    if fallback_audio_used and not voiceover_enabled:
        raise RuntimeError("BGM 不可用且口播无效，fallback 底噪不能作为可发布音频")

    # 抖音平台参数也以实际渲染主片时长为准。
    _douyin_total_dur = _rendered_main_duration
    douyin_cfg = get_douyin_config(int(_douyin_total_dur))
    video_height = 1920  # 9:16 1080x1920
    _font_size_ratio = float(
        (postproduction_contract or {}).get("subtitle_style", {}).get("font_size_ratio")
        or DOUYIN_CONFIG["subtitle"]["font_size_ratio"]
    )
    _subtitle_max_units = _single_line_subtitle_capacity(
        video_width=1080,
        font_size=int(video_height * _font_size_ratio),
    )
    subtitles = optimize_subtitles_for_douyin(subtitles, video_height=video_height)
    subtitles = prepare_single_line_subtitles(subtitles, max_units=_subtitle_max_units)
    subtitles = assign_intelligent_subtitle_colors(
        graded_path,
        subtitles,
        candidates=[
            BRAND_CONFIG.get("primary_color", "#FF6B6B"),
            BRAND_CONFIG.get("accent_color", "#4ECDC4"),
            "#FFFFFF",
        ],
        fallback="#FFFFFF",
    )
    bottom_margin_ratio = DOUYIN_CONFIG["subtitle"]["bottom_margin_ratio"]
    print(f"📱 抖音优化：单行无标点字幕 + 智能对比色 + 安全区（底部 {int(bottom_margin_ratio*100)}%）")

    # 字幕动画按每条字幕所属叙事段选择；口播不再强制打字机。
    sub_animation = "fade"
    _post_segments = {
        int(item["semantic_segment"]): {"subtitle": item}
        for item in (postproduction_contract or {}).get("semantic_subtitles") or []
    }
    if not _post_segments:
        _post_segments = {
            int(item["segment"]): item
            for item in (postproduction_contract or {}).get("segments") or []
        }
    for subtitle in subtitles:
        segment_index = int(subtitle.get("segment", 0))
        narrative = _all_narratives[segment_index] if segment_index < len(_all_narratives) else "showcase"
        subtitle_contract = (_post_segments.get(segment_index) or {}).get("subtitle") or {}
        subtitle["animation"] = (
            subtitle_contract.get("animation")
            or choose_subtitle_animation(narrative, has_voiceover=voiceover_enabled)
        )
    if postproduction_contract:
        postproduction_contract["subtitles"] = [
            {
                key: subtitle.get(key)
                for key in ("segment", "text", "start", "end", "color", "animation")
            }
            for subtitle in subtitles
        ]
    print("📝 字幕布局：动画按素材选择，位置固定在抖音底部安全区")

    try:
        add_fancy_subtitles(
            video=graded_path,
            subtitles=subtitles,
            output=subtitled_path,
            font_size=int(video_height * _font_size_ratio),
            primary_color=BRAND_CONFIG.get("primary_color", "#FFFFFF"),
            accent_color=BRAND_CONFIG.get("accent_color", "#FF6B6B"),
            animation=sub_animation,
            bottom_margin_ratio=bottom_margin_ratio,
        )
        print(f"✅ 花字字幕添加完成：{subtitled_path.name}")
    except Exception as e:
        raise RuntimeError(f"字幕渲染失败，已阻断不可发布成片：{e}") from e

    # 混合口播到视频音轨（BGM 自动闪避）；口播已在字幕烧录前完成严格校验
    if voiceover_enabled and not _defer_full_voiceover_mix:
        try:
            voiced_path = final_dir / f"{output_name}_voiced.mp4"
            _mix_voiceover_with_bgm(
                video=subtitled_path,
                voiceover=voiceover_audio,
                output=voiced_path,
            )
            _voice_similarity = _validate_voiceover_in_mix(voiced_path, voiceover_audio)
            subtitled_path = voiced_path
            print(
                f"✅ 口播混合完成（相似度 {_voice_similarity:.3f}，"
                "BGM 已按实测响度自适应，sidechain 闪避）"
            )
        except Exception as e:
            if strict_mode:
                raise RuntimeError(f"口播混合失败：{e}") from e
            print(f"⚠️  口播混合失败：{e}")
    elif _defer_full_voiceover_mix:
        print("  🎙️ CTA 已并入同一次连续口播，延后到尾卡完成后统一混音")

    # ============================================================
    # 第五步：添加音效（SFX）
    # ============================================================
    sfx_path = final_dir / f"{output_name}_sfx.mp4"

    try:
        # 节奏模板驱动：转场和段时长从模板取
        transition_dur = scene_cfg.get("transition_duration", 0.6)
        # 从 ad_script 提取叙事类型，按实际成功段过滤
        _all_narratives = [seg.get("narrative", "") for seg in ad_script.get("segments", [])]
        if local_asset_mode:
            _narratives = [
                _all_narratives[index] if index < len(_all_narratives) else "showcase"
                for index in local_edit_semantic_indices
            ]
            _sfx_seg_durs = [
                _seg_dur_map.get(edit_index, float(duration))
                for edit_index in _seg_indices_list
            ]
        elif seg_indices_for_subtitles is not None:
            _narratives = [_all_narratives[i] for i in sorted(seg_indices_for_subtitles) if i < len(_all_narratives)]
            # 按实际成功段顺序构建段时长列表
            _sfx_seg_durs = [_seg_dur_map.get(i, float(duration)) for i in sorted(seg_indices_for_subtitles) if i < len(_all_narratives)]
        else:
            _narratives = _all_narratives[:len(clip_paths)]
            # 按段顺序构建时长列表
            _sfx_seg_durs = [s["duration"] for s in rhythm_template["segments"][:len(clip_paths)]]
        sfx_list = generate_sfx_timings(
            num_clips=len(clip_paths),
            clip_duration=duration,
            transition_duration=transition_dur,
            narratives=_narratives if _narratives else None,
            segment_durations=_sfx_seg_durs,
            transition_decisions=scene_transitions if local_asset_mode else None,
            segment_timeline=(
                [
                    {
                        **item,
                        "start": round(max(0.0, float(item["start"]) - _trim_start), 3),
                        "end": round(
                            min(
                                _rendered_main_duration,
                                max(0.0, float(item["end"]) - _trim_start),
                            ),
                            3,
                        ),
                        "duration": round(
                            min(
                                _rendered_main_duration,
                                max(0.0, float(item["end"]) - _trim_start),
                            ) - max(0.0, float(item["start"]) - _trim_start),
                            3,
                        ),
                    }
                    for item in _actual_segment_timeline
                ]
                if local_asset_mode else None
            ),
            sfx_intensity=music_contract.get("sfx_intensity", "moderate"),
        )

        # P1-B：帧间差分补充音效（detect_scene_cuts）
        # 用实际合并视频的场景切换点补充 whoosh，比叙事模板更精准
        if local_asset_mode:
            print("   本地素材音效：直接采用实渲染转场决策，不追加盲目 whoosh")
        else:
            try:
                from video_merger import detect_scene_cuts as _dsc
                _sc_cuts = _dsc(subtitled_path, threshold=0.35, max_cuts=15)
                _existing_times = {round(s["time"], 1) for s in sfx_list}
                for _cut_t in _sc_cuts:
                    # 避免与已有音效时间点重叠（±0.15s 内跳过）
                    _too_close = any(abs(_cut_t - _et) < 0.15 for _et in _existing_times)
                    if not _too_close:
                        sfx_list.append({"time": _cut_t, "type": "whoosh", "volume": 0.18})
                        _existing_times.add(round(_cut_t, 1))
                sfx_list.sort(key=lambda s: s["time"])
                print(f"   P1-B 帧间差分：检测到 {len(_sc_cuts)} 个场景切换点")
            except Exception as _dsc_err:
                print(f"   P1-B 帧间差分跳过（{_dsc_err}）")

        # 音效时间对齐 BGM 节拍（卡点效果）
        if bgm_file and bgm_file.exists():
            sfx_list = align_sfx_to_beats(sfx_list, bgm_file)
        if postproduction_contract:
            postproduction_contract["sfx"] = {"decisions": sfx_list}
        if sfx_list:
            # P1-5：音效时间点边界保护 —— 过滤超出视频实际时长的音效
            try:
                _sfx_vid_dur = _get_clip_duration(subtitled_path)
                sfx_list = [
                    s for s in sfx_list
                    if s.get("time", 0) < _sfx_vid_dur - 0.1
                ]
            except Exception:
                pass
            add_sfx_to_video(
                video=subtitled_path,
                output=sfx_path,
                sfx_list=sfx_list,
            )
            print(f"✅ 音效添加完成：{len(sfx_list)} 个音效")
        else:
            sfx_path = subtitled_path
    except Exception as e:
        print(f"⚠️  音效添加失败：{e}")
        if local_asset_mode and strict_mode:
            raise RuntimeError("本地素材音效智能决策失败，已阻断未完成的后期合同") from e
        sfx_path = subtitled_path

    # ============================================================
    # P1-4：第六步：生成封面图（从所有片段选最佳帧，而非固定第 1 段）
    # ============================================================
    cover_path = final_dir / f"{output_name}_cover.jpg"
    _cover_candidate_paths: List[Path] = []

    try:
        # P1-4：从所有片段中选最佳封面帧（而非固定第 1 段）
        # hook 段通常是近景特写，不一定包含产品全景或最佳视觉帧
        from PIL import Image as _PILImage, ImageFilter as _PILFilter
        import statistics as _stats

        _best_frame_path = None
        _best_score = -1.0
        _sample_ratios = [0.12, 0.25, 0.40, 0.55, 0.70, 0.85]
        # Q4 修复：缓存相邻帧用于计算帧间差（运动模糊惩罚）
        _prev_thumb_data: dict = {}  # key=(ci, ratio_idx-1) -> thumb pixel list

        for _ci, _cclip in enumerate(clip_paths):
            if not _cclip.exists():
                continue
            try:
                _cdur = _get_clip_duration(_cclip)
            except Exception:
                continue
            for _ri, _ratio in enumerate(_sample_ratios):
                _t = _cdur * _ratio
                _cand_path = final_dir / f"{output_name}_cover_c{_ci}_{int(_ratio*100)}.jpg"
                _cover_candidate_paths.append(_cand_path)
                try:
                    extract_frame(_cclip, _cand_path, time_sec=_t)
                    if _cand_path.exists():
                        with _PILImage.open(_cand_path) as _src_img:
                            _img = _src_img.convert("L")
                        _tw = 320
                        _th = int(_img.height * _tw / _img.width)
                        _thumb = _img.resize((_tw, _th), _PILImage.LANCZOS)
                        _pixels = list(_thumb.getdata())
                        _edge_vals = list(_thumb.filter(_PILFilter.FIND_EDGES).getdata())
                        _lap_var = _stats.variance(_edge_vals) if len(_edge_vals) > 1 else 0
                        _contrast = _stats.stdev(_pixels) if _tw * _th > 1 else 0
                        _base_score = _lap_var * _contrast

                        # Q4 修复：帧间差惩罚——与上一候选帧差异过大说明是运动/转场帧
                        # 平均像素差 > 30（满幅 255）时认为是高运动帧，乘以惩罚系数
                        _motion_penalty = 1.0
                        _prev_key = (_ci, _ri - 1)
                        if _prev_key in _prev_thumb_data and _pixels:
                            _prev_pixels = _prev_thumb_data[_prev_key]
                            if len(_prev_pixels) == len(_pixels):
                                _mean_diff = sum(
                                    abs(a - b) for a, b in zip(_pixels, _prev_pixels)
                                ) / len(_pixels)
                                if _mean_diff > 30:
                                    # 差异越大惩罚越重，最多惩罚到 0.3x
                                    _motion_penalty = max(0.3, 1.0 - (_mean_diff - 30) / 100)
                        _prev_thumb_data[(_ci, _ri)] = _pixels

                        _score = _base_score * _motion_penalty

                        # P1-D：肤色语义加权（复用 quality_checker._detect_skin_regions）
                        # 合理肤色区域（面积比 0.05-0.45）→ ×1.3 提权（人物清晰可见）
                        # 异常肤色（>0.5 或 aspect_ratio 超限）→ ×0.5 降权（可能崩坏）
                        try:
                            from quality_checker import _detect_skin_regions as _cov_skin
                            with _PILImage.open(_cand_path) as _src_cov:
                                _cov_img = _src_cov.convert("RGB")
                            if _cov_img.width > 320:
                                _cov_img = _cov_img.resize(
                                    (320, int(_cov_img.height * 320 / _cov_img.width)),
                                    _PILImage.LANCZOS,
                                )
                            _cov_regions = _cov_skin(_cov_img)
                            if _cov_regions:
                                _cov_area = _cov_img.width * _cov_img.height
                                _cov_r = _cov_regions[0]["area"] / _cov_area
                                _cov_ar = _cov_regions[0]["aspect_ratio"]
                                if 0.05 <= _cov_r <= 0.45 and 0.2 <= _cov_ar <= 2.5:
                                    _score *= 1.3  # 人物清晰、肤色合理
                                elif _cov_r > 0.50 or _cov_ar > 2.5 or _cov_ar < 0.2:
                                    _score *= 0.5  # 异常肤色/变形，降权
                        except Exception:
                            pass

                        if _score > _best_score:
                            _best_score = _score
                            _best_frame_path = _cand_path
                except Exception:
                    pass

        # 兜底：回退到第 1 段中间帧
        if _best_frame_path is None or not _best_frame_path.exists():
            _best_frame_path = final_dir / f"{output_name}_cover_base.jpg"
            if clip_paths and clip_paths[0].exists():
                extract_frame(clip_paths[0], _best_frame_path, time_sec=duration / 2)

        mid_frame_path = _best_frame_path

        # 生成封面
        selling_point = product_info.get("selling_point", product_info.get("name", ""))
        product_name = product_info.get("name", "")
        brand_name = BRAND_CONFIG.get("name", "")
        primary_color = BRAND_CONFIG.get("primary_color", "#FF6B6B")

        # 从广告脚本中提取 hook 文案作为封面大标题
        hook_text = ""
        tag_text = ""
        if ad_script and ad_script.get("segments"):
            hook_seg = ad_script["segments"][0]  # 第一段是 hook
            hook_text = hook_seg.get("subtitle", "")
            # 生成顶部标签（增强信任）
            tag_text = "亲测有效"

        # Logo 路径
        logo_path_str = BRAND_CONFIG.get("logo_path", "")
        logo_path = None
        if logo_path_str:
            logo_p = Path(logo_path_str)
            logo_path = logo_p if logo_p.is_absolute() else PROJECT_ROOT / logo_p
            if not logo_path.exists():
                logo_path = None

        generate_cover_image(
            base_image=mid_frame_path,
            output_path=cover_path,
            title=selling_point,
            subtitle=product_name,
            hook_text=hook_text,
            tag_text=tag_text,
            brand_name=brand_name,
            primary_color=primary_color,
            aspect_ratio=aspect_ratio,
            logo_path=logo_path,
        )
        print(f"✅ 封面生成完成：{cover_path.name}")
    except Exception as e:
        print(f"⚠️  封面生成失败：{e}")
        cover_path = None
    finally:
        _cleanup_cover_candidates(_cover_candidate_paths)

    # 第七步调色已提前到第四步（拼接后、字幕前）执行，此处不再重复

    # ============================================================
    # 第八步：品牌 Logo 水印
    # ============================================================
    logo_path_str = BRAND_CONFIG.get("logo_path", "")
    logo_cfg = BRAND_CONFIG.get("logo_watermark", {})
    logo_enabled = logo_cfg.get("enabled", False) and logo_path_str

    watermarked_path = final_dir / f"{output_name}_watermarked.mp4"
    if logo_enabled:
        logo_path = Path(logo_path_str)
        if logo_path.is_absolute():
            full_logo_path = logo_path
        else:
            full_logo_path = PROJECT_ROOT / logo_path

        if full_logo_path.exists():
            try:
                print(f"🏷️  添加品牌 Logo 水印...")
                add_logo_watermark(
                    video=sfx_path,
                    output=watermarked_path,
                    logo_path=full_logo_path,
                    position=logo_cfg.get("position", "top_right"),
                    size_ratio=logo_cfg.get("size_ratio", 0.08),
                    margin_ratio=logo_cfg.get("margin_ratio", 0.03),
                    opacity=logo_cfg.get("opacity", 0.9),
                    fade_in=logo_cfg.get("fade_in", 0.5),
                    fade_out=logo_cfg.get("fade_out", 0.5),
                )
                sfx_path = watermarked_path
            except Exception as e:
                print(f"⚠️  Logo 水印添加失败：{e}")
        else:
            print(f"⚠️  Logo 文件不存在：{full_logo_path}，跳过水印")

    # ============================================================
    # P2-A：品牌开场/收尾动画（--brand-intro-outro 启用时）
    # 插在水印之后、最终导出之前
    # ============================================================
    if brand_intro_outro or _reference_outro:
        print()
        print("🎬 P2-A 添加品牌开场/收尾动画...")
        _bio_path = final_dir / f"{output_name}_with_brand.mp4"
        try:
            from video_merger import add_brand_intro_outro as _bio_fn
            _validate_cta_voiceover_contract(
                reference_profile or {},
                voiceover_enabled=voiceover_enabled,
                cta_audio=voiceover_audio if _reference_outro else None,
            )
            _outro_main_duration = None
            if _reference_outro and voiceover_enabled and _outro_voice_start is not None:
                _main_video_duration = _get_clip_duration(sfx_path)
                _outro_main_duration = max(
                    0.5,
                    min(_main_video_duration, _outro_voice_start),
                )
                if _main_video_duration - _outro_main_duration > 0.08:
                    print(
                        f"  ✂️  CTA 连续性裁切：主片 {_main_video_duration:.2f}s → "
                        f"{_outro_main_duration:.2f}s（尾卡对齐同一条口播的 CTA 起点）"
                    )
            _bio_result = _bio_fn(
                video=sfx_path,
                output=_bio_path,
                brand_name=BRAND_CONFIG.get("name", ""),
                product_name=product_info.get("name", ""),
                cta_text=(reference_profile or {}).get("cta_text") or BRAND_CONFIG.get("cta_text", "立即体验"),
                primary_color=BRAND_CONFIG.get("primary_color", "#FF6B6B"),
                intro_duration=0.0 if _reference_outro else 2.0,
                outro_duration=float(reference_profile["outro_duration"]) if _reference_outro else 1.5,
                main_duration=_outro_main_duration,
                outro_audio=None,
                outro_bgm=bgm_file if _reference_outro else None,
                outro_background_video=_resolve_local_cta_background(
                    local_asset_mode=local_asset_mode,
                    postproduction_contract=postproduction_contract,
                    clean_video=graded_path,
                ),
                strict_material_background=bool(local_asset_mode and _reference_outro),
            )
            if _bio_result == _bio_path and _bio_path.exists():
                sfx_path = _bio_path
                if _reference_outro and voiceover_enabled:
                    _validate_audio_covers_video(sfx_path)
        except Exception as _bio_err:
            if strict_mode:
                raise RuntimeError(f"品牌开场/收尾生成失败：{_bio_err}") from _bio_err
            print(f"⚠️  品牌动画跳过（{_bio_err}）")

    if _defer_full_voiceover_mix:
        try:
            _full_voiced_path = final_dir / f"{output_name}_voiced_full.mp4"
            _mix_voiceover_with_bgm(
                video=sfx_path,
                voiceover=voiceover_audio,
                output=_full_voiced_path,
            )
            _voice_similarity = _validate_voiceover_in_mix(
                _full_voiced_path,
                voiceover_audio,
            )
            _validate_audio_covers_video(_full_voiced_path)
            sfx_path = _full_voiced_path
            print(
                f"✅ 完整连续口播统一混音完成（相似度 {_voice_similarity:.3f}，"
                "主内容与 CTA 来自同一次 TTS）"
            )
        except Exception as _full_mix_err:
            raise RuntimeError(f"完整连续口播统一混音失败：{_full_mix_err}") from _full_mix_err

    if postproduction_contract and postproduction_contract_path:
        postproduction_contract["cta"].update({
            "rendered": bool(_reference_outro),
            "main_duration": _outro_main_duration if _reference_outro else _rendered_main_duration,
            "voice_start": _outro_voice_start,
            "background_source": (
                postproduction_contract["segments"][-1]["clip_path"]
                if local_asset_mode and _reference_outro and postproduction_contract.get("segments")
                else None
            ),
        })
        write_postproduction_contract(postproduction_contract, postproduction_contract_path)

    # ============================================================
    # 导出最终成片
    # ============================================================
    final_path = final_dir / f"{output_name}_final.mp4"

    try:
        # P0-1：消除 trim 的独立重编码
        # auto_trim 已在字幕前提前执行（用 -c copy 流复制），此处直接对 sfx_path 做最终编码。
        # 不再额外调用 auto_trim_video，节省一次完整重编码。
        douyin_video_cfg = DOUYIN_CONFIG
        export_final_video(
            input_video=sfx_path,
            output=final_path,
            resolution=douyin_video_cfg["resolution"],
            fps=douyin_video_cfg["fps"],
            bitrate=douyin_video_cfg["bitrate"],
        )
        print(f"✅ 最终成片已导出：{final_path.name}")

        # #10 修复：输出文件完整性校验（存在 + 大小 + 时长）
        if not final_path.exists():
            raise RuntimeError(f"输出文件不存在：{final_path}")
        file_size = final_path.stat().st_size
        if file_size < 10_000:  # 小于 10KB 视为无效
            raise RuntimeError(f"输出文件过小（{file_size} bytes），疑似空文件：{final_path}")
        try:
            actual_dur = _get_clip_duration(final_path)
            # 最终导出只能保持其直接输入的真实时长，不能再用模板或平均转场反推。
            _expected_total = _get_clip_duration(sfx_path)
            expected_min = max(0.0, _expected_total - 0.15)
            if actual_dur < expected_min:
                raise RuntimeError(
                    f"输出视频时长异常（实际 {actual_dur:.1f}s，期望至少 {expected_min:.1f}s）"
                )
            if postproduction_contract and postproduction_contract_path:
                postproduction_contract["timeline"].update({
                    "final_input_duration": _expected_total,
                    "final_output_duration": actual_dur,
                    "export_duration_delta": actual_dur - _expected_total,
                })
                write_postproduction_contract(postproduction_contract, postproduction_contract_path)
            print(f"✅ 完整性校验通过：{file_size/1024/1024:.1f} MB，{actual_dur:.1f}s")
        except RuntimeError:
            raise
        except Exception as dur_err:
            print(f"⚠️  时长校验跳过（ffprobe 不可用）：{dur_err}")

        # ============================================================
        # AI 视频质量增强（pro/4k 模式启用）
        # ============================================================
        if mode in ("pro", "4k") and not preview:
            print()
            print("✨ AI 视频质量增强...")
            try:
                _enhancer = AIVideoEnhancer()
                _enhanced_path = final_dir / f"{output_name}_enhanced.mp4"
                _enhancements = ["denoise", "deflicker", "color_enhance"]
                if mode == "4k":
                    _enhancements.append("deblur")
                _enh_result = _enhancer.enhance_video(
                    final_path,
                    output_path=_enhanced_path,
                    enhancements=_enhancements,
                )
                if _enh_result.success and _enhanced_path.exists():
                    final_path = _enhanced_path
                    print(f"✅ 视频增强完成：{', '.join(_enhancements)}")
                else:
                    print(f"⚠️  视频增强跳过：{_enh_result.message}")
            except Exception as _enh_err:
                print(f"⚠️  视频增强失败，使用原片：{_enh_err}")

    except Exception as e:
        print(f"❌ 最终导出失败：{e}")
        raise RuntimeError("最终导出失败，已阻断中间文件被标记为成功成片") from e

    # ============================================================
    # 双版本输出（可选）
    # ============================================================
    wide_path = None
    if dual_output and aspect_ratio == "9:16":
        print()
        print("=" * 60)
        print("[+] 生成 16:9 版本...")
        print("=" * 60)

        wide_path = final_dir / f"{output_name}_16x9_final.mp4"
        try:
            convert_to_aspect_ratio(
                input_video=final_path,
                output=wide_path,
                target_aspect_ratio="16:9",
            )
            print(f"✅ 16:9 版本已生成：{wide_path.name}")
        except Exception as e:
            print(f"⚠️ 16:9 版本生成失败：{e}")
            raise RuntimeError("已请求 dual_output，但 16:9 版本生成失败") from e

    # ============================================================
    # 发布级质量门禁：放在 pipeline 内部，保证单条和批量入口都执行
    # ============================================================
    quality_result = None
    production_quality_report = None
    if not preview:
        print()
        print("🔍 开始发布级视频质量检测（初筛）...")
        quality_result = check_video_quality(
            final_path,
            num_frames=15,
            content_focus="center" if product_image_path else "default",
            require_audio=True,
            product_reference_image=product_image_path if product_image_path else None,
            character_reference_image=main_char_path if main_char_path else None,
            require_semantic_alignment=True,
        )
        print_quality_report(quality_result, final_path.name)
        if not quality_result.passed:
            raise RuntimeError("最终成片质量检测未通过，已阻断输出为成功产物")

        print()
        print("🎬 开始7大维度深度质量检测与自动修复...")
        _segments_for_quality = None
        if ad_script and ad_script.get("segments"):
            _segments_for_quality = ad_script["segments"]
        _beat_timings_for_quality = None
        if rhythm_curve and rhythm_curve.beats:
            _beat_timings_for_quality = [b.time for b in rhythm_curve.beats]

        final_path, prod_report = run_production_quality_check(
            final_path,
            product_reference=product_image_path if product_image_path else None,
            character_reference=main_char_path if main_char_path else None,
            subtitles=subtitles,
            beat_timings=_beat_timings_for_quality,
            segments=_segments_for_quality,
            cta_contract=(postproduction_contract or {}).get("cta"),
            auto_fix=True,
            platform="douyin",
        )
        production_quality_report = prod_report
        _prod_guard = ProductionQualityGuard()
        _prod_guard.print_report(prod_report, final_path.name)
        if postproduction_contract and postproduction_contract_path:
            postproduction_contract["quality"] = {
                "passed": bool(prod_report.passed),
                "overall_score": float(prod_report.overall_score),
                "critical_issues": [issue.message for issue in prod_report.get_critical_issues()],
            }
            write_postproduction_contract(postproduction_contract, postproduction_contract_path)

        # 正样本要求总体和时间一致性都达标；可归因的时间一致性失败记录为负样本。
        if transition_decision_report:
            from intelligent_transition import record_transition_outcomes

            _temporal_dimension = prod_report.dimension_scores.get("temporal")
            _transition_quality_score = min(
                float(prod_report.overall_score),
                float(_temporal_dimension.score) if _temporal_dimension else 0.0,
            )
            _learned_count = record_transition_outcomes(
                transition_decision_report,
                final_quality=_transition_quality_score,
                final_passed=bool(prod_report.passed),
                store=_transition_learning_store,
                verification_id=output_name,
                transition_failure_attributed=False,
            )
            transition_decision_report["verified_learning_records"] = _learned_count
            transition_decision_report["verified_transition_quality"] = _transition_quality_score
            transition_decision_report["learning_outcome"] = (
                "positive" if prod_report.passed and _transition_quality_score >= 80.0
                else "ignored_unattributed_failure"
            )
            write_transition_report(transition_decision_report, transition_report_path)
            print(
                f"🧠 智能转场学习：已记录 {_learned_count} 条"
                f"{transition_decision_report['learning_outcome']} 验证结果"
            )

        if not prod_report.passed:
            if strict_mode:
                raise RuntimeError(
                    f"发布级深度质量检测未通过（综合 {prod_report.overall_score:.0f}/100），"
                    f"严重问题 {len(prod_report.get_critical_issues())} 项"
                )
            else:
                print(f"⚠️  发布级深度检测得分 {prod_report.overall_score:.0f}/100，非严格模式继续")

        if wide_path:
            print()
            print("🔍 开始 16:9 版本发布级质量检测...")
            wide_quality_result = check_video_quality(
                wide_path,
                num_frames=15,
                content_focus="center" if product_image_path else "default",
                require_audio=True,
                product_reference_image=product_image_path if product_image_path else None,
                character_reference_image=main_char_path if main_char_path else None,
                require_semantic_alignment=True,
            )
            print_quality_report(wide_quality_result, wide_path.name)
            if not wide_quality_result.passed:
                raise RuntimeError("16:9 成片质量检测未通过，已阻断输出为成功产物")

            print()
            print("🎬 16:9 版本7大维度深度质量检测与自动修复...")
            wide_path, _wide_prod_report = run_production_quality_check(
                wide_path,
                product_reference=product_image_path if product_image_path else None,
                character_reference=main_char_path if main_char_path else None,
                subtitles=subtitles,
                beat_timings=_beat_timings_for_quality,
                segments=_segments_for_quality,
                auto_fix=True,
                platform="douyin",
            )
            _prod_guard.print_report(_wide_prod_report, wide_path.name)
            if not _wide_prod_report.passed and strict_mode:
                raise RuntimeError(
                    f"16:9 发布级深度质量检测未通过（综合 {_wide_prod_report.overall_score:.0f}/100）"
                )

        workflow_summary = _record_production_workflow_completion(
            output_name=output_name,
            final_path=final_path,
            quality_result=quality_result,
            product_info=product_info,
            ad_script=ad_script,
            generation_params={
                "style": style,
                "mode": mode,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "target_duration": target_duration,
                "rhythm_style": rhythm_style,
                "best_of": best_of,
                "kling_model": effective_kling_model,
                "strategy": getattr(workflow_decision_result, "recommended_strategy", "standard"),
                "transition_policy": transition_decision_report.get("policy") if transition_decision_report else None,
                "transition_types": [t["type"] for t in scene_transitions],
            },
            character_assets=char_refs,
            product_image_path=product_image_path,
            character_bibles=character_bibles,
            product_bible=product_bible,
            decision_result=workflow_decision_result,
        )
        print(
            "🔁 工作流闭环完成："
            f"资产 {len(workflow_summary['registered_assets'])}，"
            f"自动观测 {'已记录' if workflow_summary['automatic_observation_recorded'] else '未记录'}，"
            "使用者脚本反馈 未自动生成，"
            f"实验 {'已追踪' if workflow_summary['experiment_tracked'] else '未追踪'}"
        )
        for warning in workflow_summary.get("warnings", []):
            print(f"   ⚠️ {warning}")

    # ============================================================
    # 清理中间文件（只保留 _final.mp4 / _16x9_final.mp4 / _cover.jpg）
    # ============================================================
    _cleanup_intermediate_files(final_dir, output_name, final_path, wide_path)
    script_artifact_path = final_dir / f"{output_name}_script.json"
    script_artifact_path.write_text(
        json.dumps(ad_script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    final_script_sidecar = final_path.with_suffix(".script.json")
    if final_script_sidecar != script_artifact_path:
        final_script_sidecar.write_text(
            json.dumps(ad_script, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "final_path": final_path,
        "wide_path": wide_path,
        "output_name": output_name,
        "ad_script": ad_script,
        "script_artifact_path": final_script_sidecar,
        "bgm_file": bgm_file,
        "preview": preview,
        # P2：失败感知字段，让调用方知道口播/封面是否成功
        "voiceover_enabled": voiceover_enabled,
        "cover_path": cover_path,  # None 表示封面生成失败
        # P0-B：跨片段一致性警告列表（空列表 = 无问题）
        "consistency_warnings": locals().get("_consistency_warnings", []),
        "quality_result": quality_result,
        "workflow_summary": locals().get("workflow_summary", {}),
        "edit_decision_report": local_asset_edit_report,
        "frame_evidence_report": local_asset_frame_evidence_report,
        "postproduction_contract": postproduction_contract_path,
        "transition_decision_report": locals().get("transition_report_path"),
    }


def run_one_click_create(
    product_info: dict,
    args: argparse.Namespace,
    output_name: str = None,
    output_dir: Path = OUTPUT_DIR,
    characters: Optional[list] = None,
    output_name_suffix: str = None,
) -> Path:
    """
    核心：执行一键成片全流程

    Args:
        product_info: 产品信息字典
        args: 命令行参数（包含 style/duration/mode/aspect_ratio）
        output_name: 输出文件名前缀（可选）
        output_dir: 输出根目录，默认 OUTPUT_DIR
        characters: 角色列表（可选，每个元素包含 name/description/image_path）
        output_name_suffix: 输出文件名后缀（用于 A/B 多版本）

    Returns:
        最终成片路径

    Raises:
        RuntimeError: 任何步骤失败时抛出异常
    """
    if output_name is None:
        if getattr(args, "output_name", None):
            output_name = _safe_output_stem(str(args.output_name))
        elif getattr(args, "resume", False):
            output_name = build_stable_output_name(product_info, args)
            print(f"🔁 断点续跑输出名：{output_name}")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = _safe_output_stem(product_info.get("name", "product"))
            output_name = f"{safe_name}_{timestamp}"

    if output_name_suffix:
        output_name = f"{output_name}_{output_name_suffix}"

    product_image = args.product_image if getattr(args, "product_image", None) and _is_http_url(args.product_image) else (
        Path(args.product_image) if getattr(args, "product_image", None) else None
    )
    hook_type = getattr(args, "hook", DEFAULT_HOOK_TYPE)
    use_voiceover = getattr(args, "voiceover", False)
    voiceover_style = getattr(args, "voiceover_style", DEFAULT_VOICEOVER_STYLE)
    voice = getattr(args, "voice", "auto")
    script_style = getattr(args, "script_style", DEFAULT_SCRIPT_STYLE)

    if voiceover_style in ("auto", "standard"):
        try:
            from quality_gate import evolve_voiceover_style_recommendation
            product_type = product_info.get("type", "default")
            voiceover_style, _ = evolve_voiceover_style_recommendation(product_type)
        except Exception:
            from quality_gate import smart_pick_voiceover_style
            product_type = product_info.get("type", "default")
            voiceover_style = smart_pick_voiceover_style(product_type)
    resolved_cast_plan = (
        {"core_characters": [], "supporting_characters": [], "ambient_entities": [], "rationale": "local_asset_mode"}
        if getattr(args, "local_assets", None)
        else build_cast_plan({**product_info, "characters": characters} if characters else product_info)
    )
    resolved_characters = resolved_cast_plan.get("core_characters", [])
    product_info["cast_plan"] = resolved_cast_plan
    product_info["characters"] = resolved_characters
    product_info["supporting_characters"] = resolved_cast_plan.get("supporting_characters", [])
    product_info["ambient_entities"] = resolved_cast_plan.get("ambient_entities", [])

    local_asset_mode = bool(getattr(args, "local_assets", None))
    # 目标总时长适配：通过节奏模板动态调整每段时长
    target_duration = getattr(args, "target_duration", None)
    rhythm_style = getattr(args, "rhythm_style", "moderate")
    actual_duration = args.duration
    if target_duration:
        print(f"⏱️  目标总时长：{target_duration}s，节奏风格：{rhythm_style}")
        if local_asset_mode:
            print("   混剪策略：目标时长作为偏好，最终时间轴服从素材理解与自然叙事")
        else:
            print(f"   生成策略：可灵生成 {actual_duration}s 片段，后期裁切适配节奏模板")

    try:
        result = run_generation_pipeline(
            product_info=product_info,
            style=args.style,
            duration=actual_duration,
            mode=args.mode,
            aspect_ratio=args.aspect_ratio,
            output_name=output_name,
            dual_output=args.dual_output,
            product_image=product_image,
            allow_no_product_image=getattr(args, "allow_no_product_image", False),
            image_fidelity=getattr(args, "image_fidelity", DEFAULT_IMAGE_FIDELITY),
            human_fidelity=getattr(args, "human_fidelity", DEFAULT_HUMAN_FIDELITY),
            seed=getattr(args, "seed", None),
            characters=resolved_characters,
            output_dir=output_dir,
            hook_type=hook_type,
            use_voiceover=use_voiceover,
            voiceover_style=voiceover_style,
            voice=voice,
            script_style=script_style,
            strict_mode=getattr(args, "strict", True),
            force=getattr(args, "force", False),
            target_duration=target_duration,
            rhythm_style=rhythm_style,
            parallel=not getattr(args, "serial", False),
            min_clips=getattr(args, "min_clips", 3),
            best_of=getattr(args, "best_of", 1),
            quality_frames=getattr(args, "quality_frames", 12),
            keep_candidates=getattr(args, "keep_candidates", False),
            preview=getattr(args, "preview", False),
            max_workers=getattr(args, "max_workers", 4),
            stabilize=getattr(args, "stabilize", True),
            brand_intro_outro=getattr(args, "brand_intro_outro", False),
            kling_model=getattr(args, "kling_model", None),
            multi_shot=getattr(args, "multi_shot", False),
            preflight_keyframe=getattr(args, "preflight_keyframe", True),
            image_first=getattr(args, "image_first", True),
            image_first_mode=getattr(args, "image_first_mode", "standard"),
            image_first_variants=getattr(args, "image_first_variants", 2),
            local_assets=Path(args.local_assets).expanduser() if getattr(args, "local_assets", None) else None,
            reference_video=Path(args.reference_video).expanduser() if getattr(args, "reference_video", None) else None,
        )

        final_path = result["final_path"]
        print()
        print("=" * 60)
        print("🎉 一键成片完成！")
        print("=" * 60)
        print(f"📁 输出目录：{output_dir / 'final'}")
        print(f"🎬 最终成片：{final_path.name}")
        print(f"📊 文件大小：{final_path.stat().st_size / 1024 / 1024:.1f} MB")
        if result["wide_path"] and result["wide_path"].exists():
            print(f"🖥️ 16:9 版本：{result['wide_path'].name}")
        print()

        # 预览模式：跳过质量检测和发布文案，直接返回
        if result.get("preview"):
            print("⚡ 预览模式完成，跳过后期质量检测和发布文案生成")
            return final_path

        # 保存发布文案（标题 + 话题标签 + 脚本概要）
        ad_script = result.get("ad_script")
        if ad_script:
            caption_path = output_dir / "final" / f"{output_name}_发布文案.txt"
            title_options = generate_title_options(product_info, num_options=5)
            hashtag_options = generate_hashtag_options(product_info, num_options=3)

            with open(caption_path, "w", encoding="utf-8") as f:
                f.write(f"{'='*50}\n")
                f.write(f"📝 {product_info.get('name', '产品')} - 抖音发布文案\n")
                f.write(f"{'='*50}\n\n")

                f.write(f"【推荐标题】\n")
                f.write(f"{ad_script['title']}\n\n")

                f.write(f"【标题备选（{len(title_options)}个）】\n")
                for i, t in enumerate(title_options, 1):
                    f.write(f"  {i}. {t}\n")
                f.write("\n")

                f.write(f"【推荐话题标签】\n")
                f.write(f"{' '.join(ad_script['hashtags'])}\n\n")

                f.write(f"【话题标签备选（{len(hashtag_options)}组）】\n")
                for i, tags in enumerate(hashtag_options, 1):
                    f.write(f"  方案{i}: {' '.join(tags)}\n")
                f.write("\n")

                f.write(f"【脚本概要】\n")
                f.write(f"  脚本风格：{SCRIPT_STYLES.get(script_style, {}).get('name', script_style)}\n")
                for seg in ad_script["segments"]:
                    f.write(f"  [{seg['segment']+1}] {seg['narrative']}: {seg['subtitle']}\n")
                f.write("\n")

                # P0-B：跨片段一致性警告
                _cw = result.get("consistency_warnings", [])
                if _cw:
                    f.write(f"【⚠️  跨片段一致性警告（{len(_cw)} 条）】\n")
                    for _cwi, _cwmsg in enumerate(_cw, 1):
                        f.write(f"  {_cwi}. {_cwmsg}\n")
                    f.write("  建议：人工检查上述片段的人物/产品构图是否连贯\n")
                    f.write("\n")

                # BGM 版权信息
                bgm_info = get_bgm_copyright_info(result.get("bgm_file"))
                f.write(f"【BGM 信息】\n")
                f.write(f"  来源：{bgm_info['source']}\n")
                f.write(f"  标题：{bgm_info['title']}\n")
                f.write(f"  ⚠️  {bgm_info['warning']}\n")
                f.write("\n")

                f.write(f"{'='*50}\n")
                f.write(f"💡 提示：\n")
                f.write(f"   1. 标题控制在 20-30 字最佳\n")
                f.write(f"   2. 话题标签 5-7 个为宜\n")
                f.write(f"   3. 发布前建议人工复核合规性\n")
                f.write(f"   4. BGM 版权需自行核实，商用请确认授权\n")
                f.write(f"{'='*50}\n")

            print(f"📄 发布文案已保存：{caption_path.name}")
            print(f"   （含标题备选 + 话题标签 + 脚本概要）")

        if result.get("quality_result") and result["quality_result"].passed:
            print("\n✅ 视频质量检测通过")

        return final_path

    except Exception as e:
        print(f"\n❌ 生成失败：{e}")
        print(f"   💡 中间文件已保留在 {output_dir} 下，可用于排查问题")
        print(f"   输出目录：{output_dir / 'clips' /  'character_ref' / 'final'}")
        raise


def expand_theme_with_llm(theme: str, args) -> Optional[dict]:
    """
    主题模式核心：将一句主题描述交给 LLM 展开为完整的 product_info + args 参数包。

    返回格式：
        {
            "product_info": { name, type, selling_point, ingredients, origin, production_process, specifications, verified_claims, audience, style, age, gender, outfit },
            "args": { style, script_style, hook, rhythm_style, target_duration, voiceover, voice }
        }
    或 None（LLM 不可用 / 调用失败）。
    """
    from config import LLM_ENABLED
    if not LLM_ENABLED:
        return None

    try:
        from llm_client import generate_json
    except ImportError:
        return None

    system_prompt = """你是专业的短视频广告策划专家。用户会给你一句产品主题描述，
你需要根据主题自动推断出最适合的广告参数，以严格的 JSON 格式输出，不要有任何额外说明。

    输出字段说明：
    product_info:
      name: 产品名称（简洁，2-8字）
      type: 产品类型，从以下选择：美妆 食品 科技 服装 app 家居 健康 母婴 宠物 运动 default
      selling_point: 核心卖点（一句话，10-25字，突出用户利益）
      ingredients: 用户主题明确提供的产品原料列表；未明确提供则必须为空列表，禁止推断
      origin: 用户主题明确提供的产品产地；未明确提供则必须为空字符串，禁止从品类推断
      production_process: 用户主题明确提供的工艺列表；未明确提供则必须为空列表
      specifications: 用户主题明确提供的规格列表；未明确提供则必须为空列表
      verified_claims: 用户主题明确陈述并可作为广告事实使用的卖点列表；不得扩写或推断功效
      audience: 目标人群（如 18-30岁都市女性）
      style: 广告风格（如 温暖治愈、科技感、青春活力、极简高端）
      age: 主角年龄（数字，如 25）
      gender: 主角性别（女 或 男）
      outfit: 服装描述（英文，符合场景和受众，如 casual cozy sweater）
      characters: 核心角色列表，只放需要跨镜头保持一致并生成定妆照的角色/动物主体。普通单人商品 1 个；家庭/多人决策通常 2 个；宠物产品可包含主人 + 宠物。
        每个角色包含 name, role, role_type, reference_required, description, age, gender, outfit, expression_baseline。
      supporting_characters: 配角列表，不生成定妆照，例如孩子、客服、顾问、医生、店员等。每个角色同样包含 name, role, role_type=supporting, reference_required=false, description。
      ambient_entities: 环境实体列表，例如路人、人群、车辆、宠物背景、办公室同事，只作为背景描述，不作为命名主角。

args:
  style: 电影风格，从以下选一个最合适的：hitchcock kubrick spielberg aronofsky scorsese nolan anderson wong-kar-wai tarkovsky zhang-yimou koreeda tarantino jia-zhangke hou-hsiao-hsien bong-joon-ho denis-villeneuve luc-besson miyazaki
  script_style: 脚本风格，从以下选一个：pain_point_solution before_after storytelling demonstration social_proof
  hook: 开场钩子，从以下选一个：question shocking before_after demonstration story challenge celeb_style pain_point
  rhythm_style: 节奏风格，从以下选一个：fast moderate cinematic
  target_duration: 目标总时长（秒，整数，建议 15/25/30/60 之一）
  voiceover: 是否需要口播旁白（true 或 false）
  voice: 音色，从以下选一个：female_young female_warm male_pro male_magnetic energetic_female

    选择原则：
    - 温暖/情感类产品 → miyazaki / wong-kar-wai + storytelling + story
    - 科技/功能类产品 → nolan / denis-villeneuve + demonstration + shocking
    - 美妆/时尚类产品 → luc-besson / zhang-yimou + pain_point_solution + pain_point
    - 食品/生活类产品 → spielberg / koreeda + before_after + question
    - 快节奏产品 → rhythm_style=fast；沉浸式产品 → rhythm_style=cinematic
    - 需要讲解的功能性产品开启 voiceover=true，纯视觉类 voiceover=false
    - ingredients/origin/production_process/specifications/verified_claims 只能抽取用户原文事实，禁止根据常识补全
    - **角色数量由系统自动根据产品类型推断，你只需返回产品基础信息即可**
    - characters / supporting_characters / ambient_entities 字段可以留空，系统会自动填充最优角色配置"""

    prompt = f"请根据以下主题展开广告参数：\n\n{theme}"

    result = generate_json(prompt, system_prompt=system_prompt)
    if not result or "product_info" not in result:
        return None
    product_info = result["product_info"]
    normalized_theme = re.sub(r"\s+", "", theme).lower()
    for field in ("ingredients", "production_process", "specifications", "verified_claims"):
        if not isinstance(product_info.get(field), list):
            product_info[field] = []
        product_info[field] = [
            str(value).strip()
            for value in product_info[field]
            if str(value).strip() and re.sub(r"\s+", "", str(value)).lower() in normalized_theme
        ]
    if not isinstance(product_info.get("origin"), str):
        product_info["origin"] = ""
    elif re.sub(r"\s+", "", product_info["origin"]).lower() not in normalized_theme:
        product_info["origin"] = ""
    return result


def main():
    """主函数：一键成片"""
    args = parse_args()
    source_plan: Optional[GenerationSourcePlan] = None

    # --no-llm：禁用 LLM，强制走模板模式
    if args.no_llm:
        import config
        config.LLM_ENABLED = False
        print("🔧  已禁用 LLM 文案生成，使用模板模式")

    # 如果指定了 --list-styles，列出所有风格后退出
    if args.list_styles:
        print("=" * 80)
        print("🎬 可灵 AI 抖音广告视频 - 电影风格卡片库")
        print("=" * 80)
        print()
        print(f"共 {len(CINEMATIC_STYLES)} 种风格，使用方式：python one_click_create.py --style <风格名>")
        print()

        # 按中文名排序
        sorted_styles = sorted(CINEMATIC_STYLES.items(), key=lambda x: x[1]["name"])

        for key, style in sorted_styles:
            print(f"┌─ {style['name']} ({style['name_en']}) ─────────────")
            print(f"│  {style['description']}")
            print(f"│  运镜：")
            print(f"│    推进：{style['camera_push'][:80]}...")
            print(f"│    拉远：{style['camera_pull'][:80]}...")
            print(f"│    环绕：{style['camera_orbit'][:80]}...")
            print(f"│  转场：{style['transition_match'][:80]}...")
            print(f"│  光线：{style['lighting'][:80]}...")
            print(f"│  色调：{style['color'][:80]}...")
            print(f"│  情绪：{style['mood'][:80]}...")
            print(f"│")
            print(f"│  用法：python one_click_create.py --style {key}")
            print(f"└{'─' * 60}")
            print()

        sys.exit(0)

    if args.list_hooks:
        print("=" * 80)
        print("🪝 可灵 AI 抖音广告视频 - 钩子模板库")
        print("=" * 80)
        print()
        print(f"共 {len(HOOK_TEMPLATES)} 种钩子，使用方式：python one_click_create.py --hook <钩子名>")
        print()

        for key, hook in HOOK_TEMPLATES.items():
            best_for = "、".join(hook.get("best_for", []))
            print(f"┌─ {hook['name']} ({key}) ─────────────")
            print(f"│  {hook['description']}")
            print(f"│  开头文案：{hook['hook_subtitle']}")
            print(f"│  适用品类：{best_for}")
            print(f"│  情绪基调：{hook['tone']}")
            print(f"│")
            print(f"│  用法：python one_click_create.py --hook {key}")
            print(f"└{'─' * 60}")
            print()

        sys.exit(0)

    if args.list_voices:
        print("=" * 80)
        print("🎤 可灵 AI 抖音广告视频 - 音色预设库")
        print("=" * 80)
        print()
        print(f"共 {len(VOICE_PRESETS)} 种音色，使用方式：python one_click_create.py --voiceover --voice <音色名>")
        print()

        for key, voice in VOICE_PRESETS.items():
            print(f"┌─ {voice['name']} ({key}) ─────────────")
            print(f"│  系统语音：{voice['voice']}")
            print(f"│  语速：{voice['rate']} 词/分钟")
            print(f"│  音调：{voice['pitch']}")
            print(f"│")
            print(f"│  用法：python one_click_create.py --voiceover --voice {key}")
            print(f"└{'─' * 60}")
            print()

        print("\n🎬 口播风格：")
        for key, style in VOICEOVER_TEMPLATES.items():
            print(f"  - {key}: {style['name']}")
        print(f"\n用法：python one_click_create.py --voiceover --voiceover-style energetic")

        sys.exit(0)

    if args.list_script_styles:
        print("=" * 80)
        print("📝 可灵 AI 抖音广告视频 - 广告脚本风格库")
        print("=" * 80)
        print()
        print(f"共 {len(SCRIPT_STYLES)} 种脚本风格，使用方式：python one_click_create.py --script-style <风格名>")
        print()

        for key, style in SCRIPT_STYLES.items():
            print(f"┌─ {style['name']} ({key}) ─────────────")
            print(f"│  {style['description']}")
            print(f"│")
            print(f"│  用法：python one_click_create.py --script-style {key}")
            print(f"└{'─' * 60}")
            print()

        sys.exit(0)

    # 如果指定了 --load，直接从模板加载
    if args.load:
        explicit_output_name = args.output_name
        template_path = Path(args.load)
        if not template_path.exists():
            print(f"❌ 错误：模板文件不存在：{template_path}")
            sys.exit(1)

        print("=" * 60)
        print("🎬 抖音广告视频 - 一键成片（模板模式）")
        print("=" * 60)
        print(f"📄 加载模板：{template_path}")
        print()

        product_info, args_dict = load_template(template_path)
        args.style = args_dict.get("style", DEFAULT_CINEMATIC_STYLE)
        args.duration = args_dict.get("duration", DEFAULT_VIDEO_DURATION)
        args.mode = args_dict.get("mode", DEFAULT_MODE)
        args.aspect_ratio = args_dict.get("aspect_ratio", DEFAULT_ASPECT_RATIO)
        args.dual_output = args_dict.get("dual_output", False)
        args.image_fidelity = args_dict.get("image_fidelity", DEFAULT_IMAGE_FIDELITY)
        args.human_fidelity = args_dict.get("human_fidelity", DEFAULT_HUMAN_FIDELITY)
        args.seed = args_dict.get("seed", None)
        args.product_image = args_dict.get("product_image", None)
        # P1 修复：补全模板参数透传（之前只存了 9 个基础参数）
        if "hook_type" in args_dict:
            args.hook = args_dict["hook_type"]
        if "script_style" in args_dict:
            args.script_style = args_dict["script_style"]
        if "use_voiceover" in args_dict:
            args.voiceover = args_dict["use_voiceover"]
        if "voice" in args_dict:
            args.voice = args_dict["voice"]
        if "rhythm_style" in args_dict:
            args.rhythm_style = args_dict["rhythm_style"]
        if "target_duration" in args_dict:
            args.target_duration = args_dict["target_duration"]
        if "preview" in args_dict:
            args.preview = args_dict["preview"]
        if "parallel" in args_dict:
            args.serial = not args_dict["parallel"]
        if "min_clips" in args_dict:
            args.min_clips = args_dict["min_clips"]
        if "best_of" in args_dict:
            args.best_of = args_dict["best_of"]
        if "quality_frames" in args_dict:
            args.quality_frames = args_dict["quality_frames"]
        if "keep_candidates" in args_dict:
            args.keep_candidates = args_dict["keep_candidates"]
        if "max_workers" in args_dict:
            args.max_workers = args_dict["max_workers"]
        if "stabilize" in args_dict:
            args.stabilize = args_dict["stabilize"]
        if "brand_intro_outro" in args_dict:
            args.brand_intro_outro = args_dict["brand_intro_outro"]
        if "kling_model" in args_dict:
            args.kling_model = args_dict["kling_model"]
        if "multi_shot" in args_dict:
            args.multi_shot = args_dict["multi_shot"]
        if "preflight_keyframe" in args_dict:
            args.preflight_keyframe = args_dict["preflight_keyframe"]
        if "image_first" in args_dict:
            args.image_first = args_dict["image_first"]
        if "image_first_mode" in args_dict:
            args.image_first_mode = args_dict["image_first_mode"]
        if "image_first_variants" in args_dict:
            args.image_first_variants = args_dict["image_first_variants"]
        if "strict_mode" in args_dict:
            args.strict = args_dict["strict_mode"]
        if "force" in args_dict:
            args.force = args_dict["force"]
        if "no_llm" in args_dict:
            args.no_llm = args_dict["no_llm"]
        if "output_name" in args_dict:
            args.output_name = explicit_output_name or args_dict["output_name"]
        if "resume" in args_dict:
            args.resume = args_dict["resume"]
        if "allow_no_product_image" in args_dict:
            args.allow_no_product_image = args_dict["allow_no_product_image"]
        if "local_assets" in args_dict:
            args.local_assets = args_dict["local_assets"]
        if "reference_video" in args_dict:
            args.reference_video = args_dict["reference_video"]

        print("📋 已加载的参数：")
        for k, v in product_info.items():
            print(f"  {k}: {v}")
        print()
        if getattr(args, "local_assets", None):
            print("🎞️ 生成来源：本地视频混剪")
            print(f"📁 本地素材：{args.local_assets}")
        else:
            print("🎬 生成来源：可灵 AI 视频生成")
            print(f"🎥 电影风格：{args.style}")
            print(f"⏱️ 片段时长：{args.duration}s")
            print(f"🎞️ 生成模式：{args.mode}")
            if args.seed is not None:
                print(f"🌱 随机种子：{args.seed}")
            if args.product_image:
                print(f"🖼️ 商品参考图：{args.product_image}")
        if getattr(args, "reference_video", None):
            print(f"🎯 参考广告：{args.reference_video}")
        print()
        source_plan = _prepare_generation_source_plan(
            args,
            product_info,
            ab_versions=max(1, min(getattr(args, "ab_versions", 1), 3)),
        )
        _print_generation_source_plan(source_plan, args)
        print()

    else:
        # 交互式输入产品信息
        print("=" * 60)
        print("🎬 抖音广告视频 - 一键成片")
        print("=" * 60)
        print()

        # 显示电影风格
        if args.style != DEFAULT_CINEMATIC_STYLE:
            style_info = CINEMATIC_STYLES.get(args.style, {})
            print(f"🎥 电影风格：{style_info.get('name', args.style)}")
            print(f"   {style_info.get('description', '')}")
            print()

        if not getattr(args, "local_assets", None):
            _local_ans = input("是否使用本地视频素材文件夹？(y/n) [y]：").strip().lower() or "y"
            if _local_ans == "y":
                _local_path = input("请输入本地视频素材文件夹路径：").strip()
                if not _local_path:
                    print("❌ 错误：已选择本地素材模式，但未输入文件夹路径")
                    sys.exit(1)
                args.local_assets = _local_path
                args.allow_no_product_image = True
                print(f"🎞️ 已选择：本地视频混剪（{args.local_assets}）")
                print()
            else:
                print("🎬 已选择：可灵 AI 视频生成")
                print()

        # 检查环境
        # P0-1 修复：同时支持 API Key 和 JWT（AccessKey+SecretKey）两种鉴权方式
        _has_api_key = bool(KLING_API_KEY and KLING_API_KEY not in ("your_kling_api_key_here", ""))
        _has_jwt = bool(
            KLING_ACCESS_KEY and KLING_SECRET_KEY
            and KLING_ACCESS_KEY not in ("your_access_key_here", "")
            and KLING_SECRET_KEY not in ("your_secret_key_here", "")
        )
        if not getattr(args, "local_assets", None) and not _has_api_key and not _has_jwt:
            print("❌ 错误：未配置可灵 API 鉴权")
            print("  请在 .env 或 config.py 中配置以下任意一种：")
            print("  方式一（推荐）：KLING_ACCESS_KEY=ak-xxx 和 KLING_SECRET_KEY=sk-xxx")
            print("  方式二（兼容）：KLING_API_KEY=your_api_key_here")
            sys.exit(1)

        if not check_ffmpeg():
            print("❌ 错误：未安装 ffmpeg")
            print("  请先安装：brew install ffmpeg")
            sys.exit(1)

        ensure_dirs()
        print("✅ 环境检查通过")
        print()

        # ── 主题模式（默认）vs 手动模式（--manual）──────────────────
        _use_manual = getattr(args, "manual", False)

        if not _use_manual:
            print("💡 主题模式：输入一句话描述你的产品，其余参数由 AI 自动决定")
            print("   （例：一款帮助上班族缓解颈椎疼痛的按摩枕）")
            print("   输入 'm' 切换到手动填写模式")
            print()
            _theme_input = input("请输入产品主题：").strip()
            if _theme_input.lower() == "m":
                _use_manual = True
            elif not _theme_input:
                print("⚠️  未输入主题，切换到手动模式")
                _use_manual = True

            if not _use_manual:
                print()
                print("🤖 AI 正在解析主题，生成最佳参数配置...")
                _expanded = expand_theme_with_llm(_theme_input, args)
                if _expanded is None:
                    print("⚠️  LLM 不可用（未配置或调用失败），切换到手动填写模式")
                    print("   提示：在 config.py 中配置 LLM_API_KEY 和 LLM_BASE_URL 以启用主题模式")
                    print()
                    _use_manual = True
                else:
                    product_info = _expanded["product_info"]
                    # Q-Final 修复：清空 LLM 返回的角色字段，让 build_cast_plan 统一推断
                    # 避免 LLM 返回的角色数量与系统设计不一致（如家财险只给 1-2 个角色）
                    # build_cast_plan 是角色计划的唯一真源（single source of truth）
                    product_info.pop("characters", None)
                    product_info.pop("supporting_characters", None)
                    product_info.pop("ambient_entities", None)
                    if not getattr(args, "local_assets", None):
                        cast_plan = build_cast_plan(product_info)
                        product_info["cast_plan"] = cast_plan
                        product_info["characters"] = cast_plan.get("core_characters", [])
                        product_info["supporting_characters"] = cast_plan.get("supporting_characters", [])
                        product_info["ambient_entities"] = cast_plan.get("ambient_entities", [])
                    # 将 LLM 推荐的 args 参数回写到 args 对象
                    _llm_args = _expanded.get("args", {})
                    _VALID_STYLES = {
                        "hitchcock", "kubrick", "spielberg", "aronofsky", "scorsese",
                        "nolan", "anderson", "wong-kar-wai", "tarkovsky", "zhang-yimou",
                        "koreeda", "tarantino", "jia-zhangke", "hou-hsiao-hsien",
                        "bong-joon-ho", "denis-villeneuve", "luc-besson", "miyazaki",
                    }
                    _VALID_SCRIPT_STYLES = {
                        "pain_point_solution", "before_after", "storytelling",
                        "demonstration", "social_proof",
                    }
                    _VALID_HOOKS = {
                        "question", "shocking", "before_after", "demonstration",
                        "story", "challenge", "celeb_style", "pain_point",
                    }
                    _VALID_RHYTHMS = {"fast", "moderate", "cinematic"}
                    _VALID_VOICES = {
                        "female_young", "female_warm", "male_pro",
                        "male_magnetic", "energetic_female",
                    }
                    if _llm_args.get("style") in _VALID_STYLES:
                        args.style = _llm_args["style"]
                    if _llm_args.get("script_style") in _VALID_SCRIPT_STYLES:
                        args.script_style = _llm_args["script_style"]
                    if _llm_args.get("hook") in _VALID_HOOKS:
                        args.hook = _llm_args["hook"]
                    if _llm_args.get("rhythm_style") in _VALID_RHYTHMS:
                        args.rhythm_style = _llm_args["rhythm_style"]
                    if isinstance(_llm_args.get("target_duration"), int):
                        args.target_duration = _llm_args["target_duration"]
                    if isinstance(_llm_args.get("voiceover"), bool):
                        args.voiceover = _llm_args["voiceover"]
                    if _llm_args.get("voice") in _VALID_VOICES:
                        args.voice = _llm_args["voice"]
                    print("✅ AI 参数配置完成")
                    if not getattr(args, "local_assets", None):
                        print()
                        _img_input = input("🖼️  商品参考图路径或 URL（直接回车跳过）：").strip()
                        if _img_input:
                            args.product_image = _img_input
                        else:
                            args.allow_no_product_image = True

        if _use_manual:
            print("请输入产品信息（直接回车使用默认值）：")
            print()
            product_name = input_with_default("产品名称", "我的产品")
            product_type = input_with_default("产品类型（美妆/食品/科技/服装/app）", "default")
            selling_point = input_with_default("核心卖点", "卓越品质，值得拥有")
            ingredients = input("产品原料（可选，多个用逗号分隔，不确定请留空）：").strip()
            origin = input("产品产地（可选，不确定请留空）：").strip()
            production_process = input("生产工艺（可选，多个用逗号分隔，不确定请留空）：").strip()
            verified_claims = input("已核验卖点（可选，多个用逗号分隔，不确定请留空）：").strip()
            audience = input_with_default("目标人群", "18-35岁")
            style = input_with_default("广告风格", "现代简约")
            product_info = {
                "name": product_name,
                "type": product_type,
                "selling_point": selling_point,
                "ingredients": [value.strip() for value in re.split(r"[,，]", ingredients) if value.strip()],
                "origin": origin,
                "production_process": [value.strip() for value in re.split(r"[,，]", production_process) if value.strip()],
                "verified_claims": [value.strip() for value in re.split(r"[,，]", verified_claims) if value.strip()],
                "audience": audience,
                "style": style,
            }
            if not getattr(args, "local_assets", None):
                product_info.update({
                    "age": input_with_default("角色年龄", "25"),
                    "gender": input_with_default("角色性别（女/男）", "女"),
                    "outfit": input_with_default("服装描述", "casual everyday clothes"),
                })
                cast_plan = build_cast_plan(product_info)
                product_info["cast_plan"] = cast_plan
                product_info["characters"] = cast_plan.get("core_characters", [])
                product_info["supporting_characters"] = cast_plan.get("supporting_characters", [])
                product_info["ambient_entities"] = cast_plan.get("ambient_entities", [])

            print()
            print("=" * 60)
            print("📋 产品信息确认")
            print("=" * 60)
            for k, v in product_info.items():
                if k == "cast_plan" and v:
                    print(
                        f"  cast_plan: 核心 {len(v.get('core_characters', []))} / "
                        f"配角 {len(v.get('supporting_characters', []))} / "
                        f"环境实体 {len(v.get('ambient_entities', []))}"
                    )
                    print(f"    rationale: {v.get('rationale', '')}")
                elif k == "characters" and v:
                    print(f"  characters(core/ref): {len(v)} 个")
                    for char in v:
                        role = f" / {char.get('role')}" if char.get("role") else ""
                        print(f"    - {char.get('name', 'Character')}{role}: {char.get('description', '')}")
                elif k == "supporting_characters" and v:
                    print(f"  supporting_characters: {len(v)} 个")
                    for char in v:
                        role = f" / {char.get('role')}" if char.get("role") else ""
                        print(f"    - {char.get('name', 'Supporting')}{role}: {char.get('description', '')}")
                elif k == "ambient_entities" and v:
                    print(f"  ambient_entities: {', '.join(str(item) for item in v)}")
                else:
                    print(f"  {k}: {v}")
            print()

        ab_count = max(1, min(getattr(args, "ab_versions", 1), 3))
        source_plan = _prepare_generation_source_plan(
            args,
            product_info,
            ab_versions=ab_count,
        )
        _print_generation_source_plan(source_plan, args)
        print()

        confirm_label = "混剪" if source_plan["source"] == "local_assets" else "生成"
        confirm = input(f"确认开始{confirm_label}？(y/n) [y]：").strip().lower()
        if confirm and confirm != "y":
            print("已取消")
            sys.exit(0)

    if not product_info.get("name", "").strip():
        print("❌ 错误：产品名称不能为空")
        sys.exit(1)
    if not getattr(args, "local_assets", None) and not (3 <= args.duration <= 10):
        print(f"❌ 错误：--duration 必须在 3-10 秒之间（当前：{args.duration}）")
        sys.exit(1)
    if not getattr(args, "local_assets", None) and getattr(args, "best_of", 1) < 1:
        print(f"❌ 错误：--best-of 必须 ≥ 1（当前：{args.best_of}）")
        sys.exit(1)
    # ───────────────────────────────────────────────────────────────

    # 如果指定了 --save，保存当前配置模板后退出
    if args.save:
        try:
            save_template(product_info, args, Path(args.save))
            print(f"✅ 模板已保存到：{args.save}")
        except Exception as e:
            print(f"❌ 模板保存失败：{e}")
            sys.exit(1)
        sys.exit(0)

    # 执行核心流程
    try:
        ab_count = max(1, min(getattr(args, "ab_versions", 1), 3))  # 限制 1-3 个版本

        if ab_count == 1:
            run_one_click_create(product_info, args)
        else:
            # P2-B：A/B 多版本生成，支持 hook / style / script 三个维度
            import random

            ab_dim = getattr(args, "ab_dim", None) or "script"

            # ── 各维度变体候选 ──
            if ab_dim == "hook":
                from ad_script import HOOK_TYPES
                all_variants = list(HOOK_TYPES.keys()) if hasattr(__import__("ad_script"), "HOOK_TYPES") else [
                    "question", "shocking", "before_after", "demonstration",
                    "story", "challenge", "pain_point",
                ]
                # Bug7 修复：args 中 hook 维度字段名是 args.hook（parse_args 定义），不是 args.hook_type
                base_variant = getattr(args, "hook", DEFAULT_HOOK_TYPE)
                _dim_label = "hook 类型"
            elif ab_dim == "style":
                from config import CINEMATIC_STYLES
                all_variants = list(CINEMATIC_STYLES.keys()) if "CINEMATIC_STYLES" in dir(__import__("config")) else [
                    "warm_cinematic", "cool_cinematic", "vintage", "moody",
                ]
                base_variant = getattr(args, "style", "warm_cinematic")
                _dim_label = "电影风格"
            else:  # script（默认）
                all_variants = list(SCRIPT_STYLES.keys())
                base_variant = args.script_style
                _dim_label = "脚本风格"

            selected_variants = [base_variant]
            remaining = [v for v in all_variants if v != base_variant]
            random.shuffle(remaining)
            selected_variants.extend(remaining[:ab_count - 1])

            print("=" * 60)
            print(f"🔬 A/B 测试模式（维度：{_dim_label}）：将生成 {len(selected_variants)} 个版本")
            print(f"   变体列表：{', '.join(selected_variants)}")
            print("=" * 60)

            results = []

            for i, variant in enumerate(selected_variants, 1):
                version_label = f"v{i}_{ab_dim}_{variant}"
                print(f"\n\n{'='*60}")
                print(f"🎬 版本 {i}/{len(selected_variants)}（{_dim_label}：{variant}）")
                print(f"{'='*60}")

                # 临时修改对应维度参数
                # Bug7 修复：hook 维度字段名是 args.hook，与 parse_args 保持一致
                if ab_dim == "hook":
                    _orig = getattr(args, "hook", DEFAULT_HOOK_TYPE)
                    args.hook = variant
                elif ab_dim == "style":
                    _orig = args.style
                    args.style = variant
                else:
                    _orig = args.script_style
                    args.script_style = variant

                try:
                    final_path = run_one_click_create(
                        product_info,
                        args,
                        output_name_suffix=version_label,
                    )
                    results.append({
                        "version": i,
                        "dim": ab_dim,
                        "variant": variant,
                        "path": final_path,
                    })
                finally:
                    # 还原参数（Bug7 修复：hook 字段名与上方保持一致）
                    if ab_dim == "hook":
                        args.hook = _orig
                    elif ab_dim == "style":
                        args.style = _orig
                    else:
                        args.script_style = _orig

            # 汇总结果
            print("\n\n" + "=" * 60)
            print("🏆 A/B 测试生成完成！")
            print("=" * 60)
            print(f"   测试维度：{_dim_label}")
            for r in results:
                print(f"  版本 {r['version']}（{r['variant']}）→ {r['path'].name}")
            print(f"\n共生成 {len(results)} 个版本，挑最喜欢的发吧！")

    except Exception as e:
        print(f"\n❌ 生成失败：{e}")
        sys.exit(1)


def run_with_production_workflow(
    product_info: dict,
    *,
    cinematic_style: str = DEFAULT_CINEMATIC_STYLE,
    voiceover_enabled: bool = True,
) -> Path:
    """
    使用完整生产级工作流编排器生成视频（推荐，一次成功率最高）。

    包含21个步骤：质量前置、图片先行验证、智能决策、自动修复、资产注册、反馈闭环。
    """
    from workflow_orchestrator import VideoGenerationWorkflow, ExecutionMode

    print("\n" + "=" * 70)
    print("🚀 启动生产级工作流（质量前置 + 图片先行 + 智能决策）")
    print("=" * 70)

    product_info = {
        **product_info,
        "cinematic_style": cinematic_style,
    }

    target_audience = product_info.get("target_audience", "general")

    wf = VideoGenerationWorkflow(
        product_info=product_info,
        target_audience=target_audience,
    )

    wf.add_progress_callback(
        lambda step, status, progress: print(
            f"  [{status:>10}] {step} ({progress:.0%})" if progress > 0 else f"  [{status:>10}] {step}"
        )
    )

    ctx = wf.run(mode=ExecutionMode.HYBRID)

    if ctx.status.value != "completed":
        raise RuntimeError(f"工作流执行失败: {ctx.status.value}")

    final_video = ctx.data.get("production_ready_video")
    if not final_video or not Path(final_video).exists():
        final_video = ctx.data.get("output_video")

    if not final_video or not Path(final_video).exists():
        raise RuntimeError("工作流未产生输出视频")

    final_path = Path(final_video)
    print(f"\n✅ 生产级工作流完成！")
    print(f"📹 最终视频: {final_path}")

    quality_result = ctx.data.get("final_quality_result")
    if quality_result:
        print(f"🎯 综合质量分: {quality_result.overall_score:.1f}/100")

    return final_path


if __name__ == "__main__":
    main()
