"""
视频拼接模块

功能：
- 拼接多个视频片段
- 添加转场效果
- 统一调色
- 添加字幕
- 添加 BGM
- 导出最终视频

依赖：
- ffmpeg（系统安装）
- 或 moviepy（pip install）
"""

import shutil
import subprocess
import re
import math
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from config import (
    OUTPUT_RESOLUTION,
    OUTPUT_FPS,
    OUTPUT_BITRATE,
    TRANSITION_DURATION,
    SUBTITLE_FONT_SIZE,
    SUBTITLE_COLOR,
    SUBTITLE_STROKE_COLOR,
    SUBTITLE_STROKE_WIDTH,
    BGM_VOLUME,
    BGM_FADE_IN,
    BGM_FADE_OUT,
    COLOR_GRADING_PRESETS,
    DEFAULT_COLOR_GRADING,
    BRAND_CONFIG,
)


def _ffmpeg_escape_drawtext_text(text: str) -> str:
    """转义 ffmpeg drawtext 的 text 参数，避免品牌名/CTA 中的特殊字符破坏滤镜语法。"""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace("%", r"\%")
    )


def _ffmpeg_escape_filter_path(path: Path | str) -> str:
    """转义 ffmpeg 滤镜参数中的文件路径，保护空格、冒号、逗号、单引号、方括号等字符。"""
    return (
        str(path)
        .replace("\\", "/")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace(" ", r"\ ")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def _ffconcat_escape_path(path: Path | str) -> str:
    """转义 concat demuxer file 行中的路径。"""
    return str(Path(path).resolve()).replace("'", r"'\''")


def _video_vbv_args(bitrate: str = OUTPUT_BITRATE) -> List[str]:
    """返回中间编码使用的 VBV 码率约束，防止 CRF 中间文件体积失控。"""
    import re

    match = re.search(r"[\d.]+", bitrate)
    base_mbps = float(match.group()) if match else 8.0
    maxrate = f"{max(1, int(base_mbps * 1.5))}M"
    bufsize = f"{max(2, int(base_mbps * 2))}M"
    return ["-maxrate", maxrate, "-bufsize", bufsize]


def _color_range_args() -> List[str]:
    """标记 BT.709 色彩空间 + full-range，避免 AI 生成视频在不同播放器出现色差/暗部发灰。"""
    return [
        "-color_range", "pc",
        "-colorspace", "bt709",
        "-color_trc", "bt709",
        "-color_primaries", "bt709",
    ]


def _subtitle_units(text: str) -> float:
    return sum(1.0 if ord(char) > 127 else 0.55 for char in text)


@lru_cache(maxsize=256)
def _native_word_boundaries(text: str) -> frozenset[int]:
    """Return macOS NaturalLanguage word boundaries without adding a tokenizer dependency."""
    if not text or shutil.which("swift") is None:
        return frozenset()
    swift_source = r'''
import Foundation
import NaturalLanguage
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let text = String(data: data, encoding: .utf8) else { exit(1) }
let tokenizer = NLTokenizer(unit: .word)
tokenizer.string = text
var offsets: [Int] = []
tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
    offsets.append(text.distance(from: text.startIndex, to: range.upperBound))
    return true
}
print(offsets.map(String.init).joined(separator: ","))
'''
    try:
        result = subprocess.run(
            ["swift", "-e", swift_source],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return frozenset(int(value) for value in result.stdout.strip().split(",") if value)
    except (OSError, ValueError, subprocess.SubprocessError):
        return frozenset()


def _subtitle_cut_penalty(
    text: str,
    cut: int,
    target_units: float,
    word_boundaries: frozenset[int],
) -> float:
    """Score a Chinese caption cut by balance and phrase-boundary quality."""
    if cut <= 0 or cut >= len(text):
        return float("inf")
    penalty = abs(_subtitle_units(text[:cut]) - target_units)
    if word_boundaries and cut not in word_boundaries:
        penalty += 100.0
    if word_boundaries and cut in word_boundaries:
        previous = max((boundary for boundary in word_boundaries if boundary < cut), default=0)
        following = min((boundary for boundary in word_boundaries if boundary > cut), default=len(text))
        if cut - previous == 1 and following - cut == 1:
            penalty += 7.0
    left = text[cut - 1]
    right = text[cut]
    if left in "的地得和与及跟把被向给在从为是有这那一各每" or right in "的地得和与及跟把被向给在从为是有这那一各每":
        penalty += 8.0
    if left in "装型款类式" and "\u4e00" <= right <= "\u9fff":
        penalty += 12.0
    if text[max(0, cut - 2):cut] in {"不同", "各种", "多种", "每种", "这款", "那款"}:
        penalty += 12.0
    if left in "，。！？；、：,.!?;:" or right in "，。！？；、：,.!?;:":
        penalty -= 2.0
    return penalty


def _split_single_line_text(text: str, max_units: int) -> List[str]:
    cleaned = re.sub(r"\s+", "", str(text or "").replace("\\N", "").replace("\n", ""))
    if not cleaned:
        return []
    def display_units(value: str) -> float:
        return _subtitle_units(strip_subtitle_punctuation(value))

    phrases = [part for part in re.split(r"(?<=[，。！？；、：,.!?;:])", cleaned) if part]
    chunks: List[str] = []
    current = ""
    for phrase in phrases:
        if display_units(current + phrase) <= max_units:
            current += phrase
            continue
        if current:
            chunks.append(current)
            current = ""
        if display_units(phrase) > max_units:
            total_units = display_units(phrase)
            parts_needed = max(2, math.ceil(total_units / max_units))
            remaining = phrase
            for part_index in range(parts_needed - 1):
                remaining_parts = parts_needed - part_index
                target = display_units(remaining) / remaining_parts
                word_boundaries = _native_word_boundaries(remaining)
                candidates = range(1, len(remaining))
                cut = min(
                    candidates,
                    key=lambda candidate: _subtitle_cut_penalty(
                        remaining, candidate, target, word_boundaries,
                    ),
                )
                chunks.append(remaining[:cut])
                remaining = remaining[cut:]
            phrase = remaining
        if phrase:
            current = phrase
    if current:
        chunks.append(current)
    return chunks


def prepare_single_line_subtitles(
    subtitles: List[Dict[str, Any]],
    max_units: int = 11,
) -> List[Dict[str, Any]]:
    """Split timed speech into readable, strictly single-line caption phrases."""
    prepared: List[Dict[str, Any]] = []
    for subtitle in subtitles:
        spoken_chunks = (
            [str(subtitle.get("text") or "")]
            if subtitle.get("semantic_phrase")
            else _split_single_line_text(subtitle.get("text", ""), max_units)
        )
        chunks = [
            display
            for chunk in spoken_chunks
            if (display := strip_subtitle_punctuation(chunk))
        ]
        if not chunks:
            continue
        start = float(subtitle.get("start", 0.0))
        end = max(start + 0.1, float(subtitle.get("end", start + 2.0)))
        weights = [max(_subtitle_units(chunk), 1.0) for chunk in chunks]
        total_weight = sum(weights)
        cursor = start
        for index, (chunk, weight) in enumerate(zip(chunks, weights)):
            chunk_end = end if index == len(chunks) - 1 else cursor + (end - start) * weight / total_weight
            item = dict(subtitle)
            item["text"] = chunk
            item["start"] = cursor
            item["end"] = chunk_end
            highlights = item.get("highlight") or []
            item["highlight"] = [word for word in highlights if word and word in chunk]
            prepared.append(item)
            cursor = chunk_end
    return prepared


def strip_subtitle_punctuation(text: str) -> str:
    """Remove punctuation and symbols from display captions while preserving spoken copy."""
    cleaned = "".join(
        char for char in str(text or "")
        if not unicodedata.category(char).startswith(("P", "S"))
    )
    return re.sub(r"\s+", "", cleaned).strip()


def build_tail_card_display_text(product_name: str, cta_text: str) -> str:
    """Build punctuation-free tail-card copy without changing the spoken CTA source."""
    return " ".join(
        value
        for value in (
            strip_subtitle_punctuation(product_name),
            strip_subtitle_punctuation(cta_text),
        )
        if value
    )


def _relative_luminance(rgb: Tuple[int, int, int]) -> float:
    channels = []
    for value in rgb:
        channel = max(0.0, min(1.0, value / 255.0))
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def choose_readable_subtitle_color(
    background_rgb: Tuple[int, int, int],
    candidates: List[str],
    fallback: str = "#FFFFFF",
    minimum_contrast: float = 4.5,
) -> str:
    """Choose the highest-contrast candidate, falling back to white when uncertain."""
    background_luminance = _relative_luminance(background_rgb)
    scored = []
    for color in candidates:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(color or "")):
            continue
        rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        luminance = _relative_luminance(rgb)
        contrast = (max(background_luminance, luminance) + 0.05) / (min(background_luminance, luminance) + 0.05)
        scored.append((contrast, str(color).upper()))
    if not scored:
        return fallback.upper()
    contrast, color = max(scored)
    return color if contrast >= minimum_contrast else fallback.upper()


def assign_intelligent_subtitle_colors(
    video: Path,
    subtitles: List[Dict[str, Any]],
    candidates: List[str],
    fallback: str = "#FFFFFF",
) -> List[Dict[str, Any]]:
    """Sample each caption window and attach a readable per-caption color."""
    from io import BytesIO
    from PIL import Image

    colored = []
    for subtitle in subtitles:
        item = dict(subtitle)
        timestamp = (float(item.get("start", 0.0)) + float(item.get("end", 0.0))) / 2
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=270:480", "-f", "image2pipe", "-vcodec", "png", "-",
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            frame = Image.open(BytesIO(result.stdout)).convert("RGB")
            region = frame.crop((int(frame.width * 0.15), int(frame.height * 0.55), int(frame.width * 0.85), int(frame.height * 0.82)))
            background = tuple(int(value) for value in region.resize((1, 1), Image.Resampling.LANCZOS).getpixel((0, 0)))
            item["color"] = choose_readable_subtitle_color(background, candidates, fallback=fallback)
        except Exception:
            item["color"] = fallback.upper()
        colored.append(item)
    return colored


def choose_subtitle_animation(narrative: str, has_voiceover: bool = False) -> str:
    """Choose restrained animation by narrative purpose; voiceover never implies typewriter."""
    normalized = str(narrative or "").strip().lower()
    tokens = set(re.findall(r"[a-z_]+", normalized))
    if tokens & {"hook", "intro", "opening", "attention", "pain_point"}:
        return "pop"
    if tokens & {"cta", "call_to_action", "outro"}:
        return "slide"
    if tokens & {"usage_demo", "demo", "result", "proof", "highlight"}:
        return "highlight" if has_voiceover else "fade"
    return "fade"


# ============================================================
# 转场库（15 种常用 xfade 转场）
# ============================================================
# 分类：fade / slide / zoom / wipe / shape
# 情绪：smooth / dynamic / dramatic / subtle

TRANSITION_LIBRARY: Dict[str, Dict[str, Any]] = {
    "fade": {
        "name": "fade",
        "xfade_type": "fade",
        "category": "fade",
        "duration_range": (0.2, 0.6),
        "mood": "smooth",
        "best_for": ["通用", "平缓过渡"],
        "description": "经典淡入淡出，柔和不突兀",
    },
    "dissolve": {
        "name": "dissolve",
        "xfade_type": "dissolve",
        "category": "fade",
        "duration_range": (0.2, 0.8),
        "mood": "smooth",
        "best_for": ["通用", "情绪段落"],
        "description": "柔和溶解过渡，最常用的通用转场",
    },
    "fadeblack": {
        "name": "fadeblack",
        "xfade_type": "fadeblack",
        "category": "fade",
        "duration_range": (0.3, 1.0),
        "mood": "dramatic",
        "best_for": ["章节切换", "情绪沉淀", "结尾"],
        "description": "黑场过渡，电影感强，用于段落分隔",
    },
    "fadewhite": {
        "name": "fadewhite",
        "xfade_type": "fadewhite",
        "category": "fade",
        "duration_range": (0.3, 0.8),
        "mood": "dramatic",
        "best_for": ["回忆闪回", "高光时刻", "梦幻感"],
        "description": "白场过渡，高亮梦幻，用于闪回或揭示",
    },
    "slideright": {
        "name": "slideright",
        "xfade_type": "slideright",
        "category": "slide",
        "duration_range": (0.15, 0.5),
        "mood": "dynamic",
        "best_for": ["快节奏", "动态切入", "列表式内容"],
        "description": "向右滑动，动态感强，适合快节奏剪辑",
    },
    "slideleft": {
        "name": "slideleft",
        "xfade_type": "slideleft",
        "category": "slide",
        "duration_range": (0.15, 0.5),
        "mood": "dynamic",
        "best_for": ["快节奏", "动态切入", "推进感"],
        "description": "向左滑动，推进感强，适合节奏紧凑的内容",
    },
    "slideup": {
        "name": "slideup",
        "xfade_type": "slideup",
        "category": "slide",
        "duration_range": (0.15, 0.5),
        "mood": "dynamic",
        "best_for": ["上升感", "揭晓", "产品展示"],
        "description": "向上滑动，上升揭晓感，适合产品展示",
    },
    "slidedown": {
        "name": "slidedown",
        "xfade_type": "slidedown",
        "category": "slide",
        "duration_range": (0.15, 0.5),
        "mood": "dynamic",
        "best_for": ["下沉感", "引入", "列表展开"],
        "description": "向下滑动，下沉引入感，适合内容展开",
    },
    "circlecrop": {
        "name": "circlecrop",
        "xfade_type": "circlecrop",
        "category": "shape",
        "duration_range": (0.3, 0.7),
        "mood": "dramatic",
        "best_for": ["揭示", "聚焦", "电影感"],
        "description": "圆形展开，从中心向外揭示，有聚焦感",
    },
    "circleclose": {
        "name": "circleclose",
        "xfade_type": "circleclose",
        "category": "shape",
        "duration_range": (0.3, 0.7),
        "mood": "dramatic",
        "best_for": ["收束", "结尾", "聚焦点"],
        "description": "圆形收拢，向内聚焦，适合段落收束",
    },
    "zoomin": {
        "name": "zoomin",
        "xfade_type": "zoomin",
        "category": "zoom",
        "duration_range": (0.3, 0.8),
        "mood": "dynamic",
        "best_for": ["推进揭示", "深入细节", "冲击力"],
        "description": "放大推进，有深入和冲击力，适合效果揭示",
    },
    "revealleft": {
        "name": "revealleft",
        "xfade_type": "revealleft",
        "category": "zoom",
        "duration_range": (0.3, 0.8),
        "mood": "dramatic",
        "best_for": ["拉远展示", "宏大感", "环境揭示"],
        "description": "向左揭示过渡，有宏大开阔感",
    },
    # P1 修复：zoomout 作为独立条目存在。
    # ffmpeg xfade 原生不支持 zoomout；使用 fade 作为最接近「画面消退拉远」的替代。
    # 若未来 ffmpeg 支持，只需将 xfade_type 改为 "zoomout" 即可。
    "zoomout": {
        "name": "zoomout",
        "xfade_type": "fade",
        "category": "zoom",
        "duration_range": (0.4, 0.8),
        "mood": "cinematic",
        "best_for": ["拉远结尾", "宏大感", "段落收束"],
        "description": "模拟镜头拉远（ffmpeg 以 fade 实现，无法做真正 zoom-out 运动）",
    },
    "wipeleft": {
        "name": "wipeleft",
        "xfade_type": "wipeleft",
        "category": "wipe",
        "duration_range": (0.2, 0.6),
        "mood": "dynamic",
        "best_for": ["快节奏", "对比切换", "信息刷新"],
        "description": "向左擦除，干脆利落，适合对比和切换",
    },
    "wiperight": {
        "name": "wiperight",
        "xfade_type": "wiperight",
        "category": "wipe",
        "duration_range": (0.2, 0.6),
        "mood": "dynamic",
        "best_for": ["快节奏", "推进切换", "流程感"],
        "description": "向右擦除，推进流程感，适合步骤式内容",
    },
    "rectcrop": {
        "name": "rectcrop",
        "xfade_type": "rectcrop",
        "category": "shape",
        "duration_range": (0.3, 0.7),
        "mood": "subtle",
        "best_for": ["框架揭示", "产品展示", "简洁感"],
        "description": "矩形展开，简洁现代，适合框架式揭示",
    },
}


def get_transition_info(transition_type: str) -> Dict[str, Any]:
    """
    获取转场的完整信息（不存在时返回 dissolve 的兜底信息）

    Args:
        transition_type: 转场类型名

    Returns:
        转场信息字典
    """
    if transition_type in TRANSITION_LIBRARY:
        return TRANSITION_LIBRARY[transition_type]
    # 兜底：dissolve
    return TRANSITION_LIBRARY["dissolve"]


# 转场类型 → xfade transition 名的快速映射（兼容旧代码）
XFADE_TYPE_MAP = {
    name: info["xfade_type"]
    for name, info in TRANSITION_LIBRARY.items()
}


# ============================================================
# 转场智能选择
# ============================================================

# 叙事类型别名映射：将 ad_script 中各种模板的叙事类型统一映射到标准 5 种
# 标准类型：hook / turning / showcase / result / cta
_NARRATIVE_ALIAS_MAP: Dict[str, str] = {
    # 钩子/开场类 → hook
    "hook": "hook",
    "before": "hook",
    "setup": "hook",
    "intro": "hook",
    "popular": "hook",       # 爆款开头也算钩子
    # 转折/发现类 → turning
    "turning": "turning",
    "turning_point": "turning",
    "discover": "turning",
    "conflict": "turning",
    "reason": "turning",     # 讲原因也算转折引入
    # 展示/过程类 → showcase
    "showcase": "showcase",
    "process": "showcase",
    "discovery": "showcase",
    "demo": "showcase",
    "detail": "showcase",
    # 效果/结果类 → result
    "result": "result",
    "after": "result",
    "change": "result",
    "effect": "result",
    "proof": "result",
    "review": "result",
    # 行动号召 → cta
    "cta": "cta",
}


def _normalize_narrative(narrative: str) -> str:
    """将任意叙事类型归一化为标准类型（hook/turning/showcase/result/cta）。"""
    if not narrative:
        return "showcase"
    return _NARRATIVE_ALIAS_MAP.get(narrative.lower(), "showcase")


# 叙事类型 → 叙事类型的转场规则
# key: (from_narrative, to_narrative) → 候选转场列表（按优先级排序）
_NARRATIVE_TRANSITION_MAP: Dict[tuple, List[str]] = {
    # hook → turning_point：从抓眼到转折，需要动态切入
    ("hook", "turning_point"): ["slideright", "wiperight", "slideleft"],
    ("hook", "turning"): ["slideright", "wiperight", "slideleft"],
    # hook → showcase：直接进入产品展示
    ("hook", "showcase"): ["zoomin", "circlecrop", "wipeleft"],
    # turning_point → showcase：转折到产品展示，用推进揭示
    ("turning_point", "showcase"): ["zoomin", "circlecrop", "wipeleft"],
    ("turning", "showcase"): ["zoomin", "circlecrop", "wipeleft"],
    # showcase → result：展示到效果，用放大推进或圆形展开
    ("showcase", "result"): ["zoomin", "circlecrop", "fadewhite"],
    # result → cta：效果到行动，用黑场沉淀情绪
    ("result", "cta"): ["fadeblack", "dissolve", "circleclose"],
    # hook → result：直接对比
    ("hook", "result"): ["fadewhite", "zoomin", "circlecrop"],
    # turning_point → result：转折到结果
    ("turning_point", "result"): ["zoomin", "fadewhite", "circlecrop"],
    ("turning", "result"): ["zoomin", "fadewhite", "circlecrop"],
    # showcase → cta：展示直接到行动
    ("showcase", "cta"): ["fadeblack", "revealleft", "rectcrop"],
    # 通用兜底
    ("hook", "cta"): ["fadeblack", "fadewhite"],
    ("turning_point", "cta"): ["fadeblack", "dissolve"],
    ("turning", "cta"): ["fadeblack", "dissolve"],
    ("result", "result"): ["dissolve", "fade"],
    ("showcase", "showcase"): ["slideleft", "wipeleft", "slideright"],
}

# 风格 → 时长缩放系数和优先分类
_STYLE_CONFIG: Dict[str, Dict[str, Any]] = {
    "fast": {
        "duration_scale": 0.6,  # 时长乘以该系数
        "preferred_categories": ["slide", "wipe"],
        "description": "快节奏：短时长，滑动/擦除类",
    },
    "moderate": {
        "duration_scale": 1.0,
        "preferred_categories": ["fade", "slide", "wipe"],
        "description": "中等节奏：均衡",
    },
    "cinematic": {
        "duration_scale": 1.5,
        "preferred_categories": ["fade", "shape", "zoom"],
        "description": "电影感：长时长，溶解/形状/缩放类",
    },
    "default": {
        "duration_scale": 1.0,
        "preferred_categories": ["fade", "slide", "wipe"],
        "description": "默认：均衡",
    },
}


def select_transition(
    from_narrative: str = "",
    to_narrative: str = "",
    style: str = "default",
    duration: Optional[float] = None,
) -> Dict[str, Any]:
    """
    根据前后叙事类型和整体风格，智能选择转场类型和时长。

    规则：
    - 优先匹配叙事类型对的预设转场列表
    - 其次按风格偏好的分类中随机选择
    - 时长根据风格系数在推荐范围内取值
    - 显式指定 duration 时直接使用

    Args:
        from_narrative: 前一段叙事类型（hook / turning / showcase / result / cta 及其别名）
        to_narrative: 后一段叙事类型
        style: 整体风格（fast / moderate / cinematic / default）
        duration: 显式指定转场时长（秒），为 None 时自动计算

    Returns:
        转场配置字典：{"type": str, "duration": float}
    """
    style = style if style in _STYLE_CONFIG else "default"
    style_cfg = _STYLE_CONFIG[style]

    # 0. 叙事类型归一化（兼容 ad_script 中各种模板的命名）
    from_n = _normalize_narrative(from_narrative)
    to_n = _normalize_narrative(to_narrative)

    # 1. 优先匹配叙事类型对
    key = (from_n, to_n)
    candidates: List[str] = []
    if key in _NARRATIVE_TRANSITION_MAP:
        candidates = _NARRATIVE_TRANSITION_MAP[key]

    # 2. 如果没有叙事匹配或候选为空，按风格偏好分类筛选
    if not candidates:
        preferred = style_cfg["preferred_categories"]
        candidates = [
            name for name, info in TRANSITION_LIBRARY.items()
            if info["category"] in preferred
        ]
        if not candidates:
            candidates = ["dissolve"]

    # 3. 旧调用路径也必须可复现；完整智能决策由 intelligent_transition.py 执行。
    chosen_type = candidates[0]

    # 4. 计算时长
    info = TRANSITION_LIBRARY[chosen_type]
    if duration is not None:
        chosen_duration = float(duration)
    else:
        dmin, dmax = info["duration_range"]
        # 根据风格系数调整范围
        scaled_min = dmin * style_cfg["duration_scale"]
        scaled_max = dmax * style_cfg["duration_scale"]
        # 在范围内取中间偏中值
        chosen_duration = round((scaled_min + scaled_max) / 2, 3)

    # 安全兜底：时长至少 0.1s，最多 1.5s
    chosen_duration = max(0.1, min(1.5, chosen_duration))

    return {"type": chosen_type, "duration": chosen_duration}


def generate_transition_sequence(
    narratives: List[str],
    style: str = "default",
    base_duration: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    根据叙事类型序列，生成一整套转场配置。

    Args:
        narratives: 每段的叙事类型列表（长度为片段数）
        style: 整体风格（fast / moderate / cinematic / default）
        base_duration: 基础转场时长（秒），为 None 时自动计算

    Returns:
        转场配置列表（长度 = len(narratives) - 1）
    """
    if len(narratives) < 2:
        return []

    transitions = []
    for i in range(len(narratives) - 1):
        trans = select_transition(
            from_narrative=narratives[i],
            to_narrative=narratives[i + 1],
            style=style,
            duration=base_duration,
        )
        transitions.append(trans)
    return transitions


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否已安装"""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# 常见中文字体候选路径（跨平台）
_COMMON_CJK_FONT_PATHS = [
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/Library/Fonts/NotoSansCJKsc-Regular.otf",
    # Linux
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def find_system_font() -> str | None:
    """
    跨平台查找可用中文字体

    Returns:
        找到的字体文件路径，如果没找到返回 None
    """
    for font_path in _COMMON_CJK_FONT_PATHS:
        if Path(font_path).exists():
            return font_path

    # P0 修复：找不到字体时给出明确的安装指引，避免服务器部署出现字幕豆腐块
    print(
        "\n"
        "❌ 未找到中文字体！字幕将显示为方块（豆腐块）。\n"
        "   请安装中文字体后重试：\n"
        "   • Ubuntu/Debian: sudo apt-get install -y fonts-noto-cjk\n"
        "   • CentOS/RHEL:   sudo yum install -y google-noto-sans-cjk-ttc-fonts\n"
        "   • Alpine/Docker: apk add --no-cache font-noto-cjk\n"
        "   • macOS:         字体应已内置，请检查 /System/Library/Fonts/\n"
        "\n"
        "   也可手动下载 NotoSansCJK-Regular.ttc 放到以下任意路径：\n"
        "   /usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc\n"
    )
    return None


# 常见粗体中文字体候选路径
_BOLD_CJK_FONT_PATHS = [
    # macOS
    "/System/Library/Fonts/PingFang.ttc",  # PingFang 包含多种字重
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/NotoSansCJKsc-Bold.otf",
    # Linux
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    # Windows
    "C:/Windows/Fonts/msyhbd.ttc",  # 微软雅黑粗体
    "C:/Windows/Fonts/simhei.ttf",
]


def _get_font_path(weight: str = "regular") -> str:
    """
    获取指定字重的字体路径

    Args:
        weight: 字重（regular / bold）

    Returns:
        字体文件路径，如果没找到粗体则 fallback 到常规字体
    """
    if weight == "bold":
        for font_path in _BOLD_CJK_FONT_PATHS:
            if Path(font_path).exists():
                return font_path
        # fallback 到常规字体
        regular = find_system_font()
        if regular:
            return regular

    # regular 或 fallback
    regular = find_system_font()
    if regular:
        return regular

    # 最后的兜底：返回一个空字符串（FFmpeg 会用默认字体）
    return ""


def _get_font_postscript_name(font_path: str) -> str:
    """
    获取字体文件的 Family 名称（供 ASS/libass 使用）。

    优先级：
    1. known_map 硬编码映射（最快，最可靠）
    2. fonttools 读取（需要 pip install fonttools，精确）
    3. fc-list 系统命令（Linux，解析 family 字段）
    4. fallback：文件名 stem

    Args:
        font_path: 字体文件路径

    Returns:
        字体 Family 名称字符串
    """
    if not font_path or not Path(font_path).exists():
        return "Arial"

    # 1. 常见字体文件名 → PostScript Family 名映射
    # 注意：TTC 字重选择：libass 默认取 TTC 第一个 face（通常最细），
    # 所以 PingFang.ttc 默认是 Thin。直接指定具体字重名让 libass 选对 face。
    known_map = {
        "PingFang.ttc": "PingFang SC Semibold",  # 指定 Semibold 避免 Thin 字重
        "STHeiti Light.ttc": "Heiti SC",
        "STHeiti Medium.ttc": "Heiti SC Medium",
        "NotoSansCJKsc-Regular.otf": "Noto Sans CJK SC",
        "NotoSansCJKsc-Bold.otf": "Noto Sans CJK SC Bold",
        "NotoSansCJK-Regular.ttc": "Noto Sans CJK SC",
        "NotoSansCJK-Bold.ttc": "Noto Sans CJK SC Bold",
        "msyh.ttc": "Microsoft YaHei",
        "msyhbd.ttc": "Microsoft YaHei Bold",
        "simhei.ttf": "SimHei",
        "wqy-zenhei.ttc": "WenQuanYi Zen Hei",
        "DroidSansFallbackFull.ttf": "Droid Sans Fallback",
        "Arial Unicode.ttf": "Arial Unicode MS",
    }
    font_name = Path(font_path).name
    if font_name in known_map:
        return known_map[font_name]

    # 2. fonttools（精确读取 name record 4 = Full Name 或 1 = Family）
    try:
        from fontTools.ttLib import TTFont  # pip install fonttools
        tt = TTFont(font_path, lazy=True)
        name_table = tt["name"]
        # 优先取 nameID=4（Full name），其次 nameID=1（Family）
        for name_id in (4, 1):
            record = name_table.getName(name_id, 3, 1, 0x0409)  # Windows English
            if record:
                return record.toUnicode()
        tt.close()
    except Exception as e:
        print(f"⚠️  ffprobe 获取视频尺寸失败，使用默认 1080x1920：{e}")

    # 3. fc-list（Linux / macOS brew fontconfig）
    try:
        result = subprocess.run(
            ["fc-list", font_path, "family"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # fc-list 输出格式: "PingFang SC,苹方-简"，取第一个 token
            return result.stdout.strip().split(",")[0].strip()
    except Exception as e:
        print(f"⚠️  ffprobe 获取视频时长失败，水印淡出将按默认逻辑处理：{e}")

    # 4. fallback：文件名（去掉扩展名）
    return Path(font_path).stem


def run_ffmpeg(cmd: List[str], timeout: int = 300, retries: int = 2) -> subprocess.CompletedProcess:
    """
    执行 ffmpeg 命令（带自动重试）

    Args:
        cmd: ffmpeg 命令参数列表
        timeout: 超时时间（秒）
        retries: 失败重试次数（默认 2 次，加上首次共 3 次尝试）

    Returns:
        执行结果

    Raises:
        RuntimeError: 所有重试都失败时抛出
    """
    import time

    cmd_str = " ".join(cmd)
    last_error = None
    last_stderr = ""
    last_result = None

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 2 * attempt
            print(f"[FFmpeg] 第 {attempt} 次重试（等待 {wait}s）...")
            time.sleep(wait)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return result

            last_stderr = result.stderr or ""
            last_result = result
            last_error = last_stderr[-500:] if last_stderr else "unknown error"
            print(f"[FFmpeg] 失败（第 {attempt + 1} 次）：{last_error[:200]}")

        except subprocess.TimeoutExpired as e:
            last_error = f"timeout after {timeout}s"
            last_stderr = str(e.stderr) if e.stderr else ""
            print(f"[FFmpeg] 超时（第 {attempt + 1} 次）")

        except Exception as e:
            last_error = str(e)
            last_stderr = ""
            print(f"[FFmpeg] 异常（第 {attempt + 1} 次）：{e}")

    # 所有重试都失败，打印完整 stderr 末尾供调试
    if last_stderr:
        print("\n" + "=" * 60)
        print("[FFmpeg] 完整错误日志（末尾 2000 字符）：")
        print("-" * 60)
        print(last_stderr[-2000:])
        print("=" * 60 + "\n")

    # 抛出最后一次的错误
    raise RuntimeError(f"FFmpeg 执行失败（已重试 {retries} 次）：{last_error}")


def _get_clip_duration(clip_path: Path) -> float:
    """获取视频片段时长（秒）

    Raises:
        RuntimeError: ffprobe 执行失败或无法解析时长时抛出
    """
    if not clip_path.exists():
        raise RuntimeError(f"视频文件不存在：{clip_path}")
    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        duration_str = result.stdout.strip()
        if not duration_str:
            raise RuntimeError(f"ffprobe 返回空结果，视频可能已损坏：{clip_path}")
        return float(duration_str)
    except (subprocess.CalledProcessError, ValueError) as e:
        raise RuntimeError(f"获取视频时长失败 {clip_path.name}：{e}") from e


def adjust_clip_duration(
    clip_path: Path,
    output_path: Path,
    target_duration: float,
    method: str = "auto",
    narrative: str = "showcase",
) -> Path:
    """
    调整视频片段时长到目标时长（裁切或变速）。

    策略：
      - target < 原时长：
        - hook/intro 叙事段：从尾部裁（start=0），保留开头精华帧
        - 其他叙事段：从中间裁，保留核心画面
      - target > 原时长：使用 setpts+atempo 慢速延长
      - method="auto" 自动选择；method="crop" 强制裁切；method="speed" 强制变速

    Args:
        clip_path: 输入视频路径
        output_path: 输出视频路径
        target_duration: 目标时长（秒）
        method: 调整方式：auto / crop / speed
        narrative: 叙事类型（hook/intro/showcase/...），影响裁切策略

    Returns:
        输出文件路径（即 output_path）

    Raises:
        RuntimeError: 调整失败时抛出
    """
    if target_duration <= 0:
        raise ValueError(f"目标时长必须大于 0，收到：{target_duration}")
    if not clip_path.exists():
        raise RuntimeError(f"视频文件不存在：{clip_path}")

    original_duration = _get_clip_duration(clip_path)
    diff = abs(original_duration - target_duration)

    # 差异在 0.1 秒以内，直接拷贝（避免不必要的重编码）
    if diff < 0.1:
        shutil.copy2(clip_path, output_path)
        return output_path

    # 自动选择策略
    if method == "auto":
        method = "crop" if target_duration < original_duration else "speed"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        has_audio = _has_audio_stream(clip_path)
        if method == "crop":
            # Bug #3 修复：裁切策略按叙事类型区分
            # hook/intro 段：AI 生成视频开头最精彩，从尾部裁（start=0 保留开头）
            # 其他叙事段：从中间裁，保留画面核心内容
            _HOOK_NARRATIVES = {"hook", "intro", "opening"}
            if narrative.lower() in _HOOK_NARRATIVES:
                start = 0.0  # 从头开始，裁掉尾部
            else:
                start = (original_duration - target_duration) / 2  # 从中间裁
            start = max(0, start)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(clip_path),
                "-ss", f"{start:.3f}",
                "-t", f"{target_duration:.3f}",
                "-map", "0:v:0",
            ]
            if has_audio:
                cmd.extend(["-map", "0:a:0"])
            cmd.extend([
                "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            ])
            if has_audio:
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])
            else:
                cmd.append("-an")
            cmd.extend([
                "-movflags", "+faststart",
                str(output_path),
            ])
            run_ffmpeg(cmd, timeout=120, retries=1)

        elif method == "speed":
            # 变速调整：setpts 控制视频速度，atempo 控制音频速度
            # atempo 单次范围 0.5-2.0，需要多级串联才能超出范围
            raw_speed_ratio = original_duration / target_duration
            # 限制变速范围在 0.5x ~ 2.0x 之间，避免画质/音质严重损失
            speed_ratio = max(0.5, min(2.0, raw_speed_ratio))

            # P1-7：当目标时长超过原始时长 2 倍（需要 <0.5x 慢速）时，
            # 先变速到 0.5x，再 freeze 最后一帧补足剩余差值，
            # 防止超限后直接 clamp 导致成片比目标时长短。
            freeze_extra = 0.0
            if raw_speed_ratio < 0.5:
                # 0.5x 变速后实际输出时长
                stretched_duration = original_duration / 0.5
                freeze_extra = max(0.0, target_duration - stretched_duration)

            video_filter = f"setpts={1/speed_ratio}*PTS"
            cmd = ["ffmpeg", "-y", "-i", str(clip_path)]
            if has_audio:
                audio_filter = _build_atempo_filter(speed_ratio)
                cmd.extend([
                    "-filter_complex",
                    f"[0:v]{video_filter}[v];[0:a]{audio_filter}[a]",
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                    "-c:a", "aac", "-b:a", "192k",
                ])
            else:
                cmd.extend([
                    "-vf", video_filter,
                    "-map", "0:v:0",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                    "-an",
                ])
            cmd.extend([
                "-movflags", "+faststart",
                str(output_path),
            ])
            run_ffmpeg(cmd, timeout=180, retries=1)

            # P1-7：freeze 补帧：如果慢速被 clamp，在末尾 loop 最后一帧补足剩余时长
            if freeze_extra > 0.05 and output_path.exists():
                freeze_out = output_path.parent / f"{output_path.stem}_freeze{output_path.suffix}"
                _freeze_dur = round(freeze_extra, 3)
                freeze_cmd = [
                    "ffmpeg", "-y",
                    "-i", str(output_path),
                    "-vf", f"tpad=stop_mode=clone:stop_duration={_freeze_dur}",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                ]
                if _has_audio_stream(output_path):
                    freeze_cmd.extend([
                        "-af", f"apad=whole_dur={target_duration}",
                        "-c:a", "aac", "-b:a", "192k",
                    ])
                else:
                    freeze_cmd.append("-an")
                freeze_cmd.extend(["-movflags", "+faststart", str(freeze_out)])
                try:
                    run_ffmpeg(freeze_cmd, timeout=120, retries=1)
                    if freeze_out.exists() and freeze_out.stat().st_size > 1000:
                        shutil.move(str(freeze_out), str(output_path))
                except Exception as _fe:
                    print(f"  ⚠️  freeze 补帧失败（{_fe}），使用 0.5x 变速版本")
                    try:
                        freeze_out.unlink(missing_ok=True)
                    except Exception:
                        pass

        else:
            raise ValueError(f"未知的调整方式：{method}")

        # 校验输出文件
        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise RuntimeError(f"时长调整输出文件异常：{output_path}")

        return output_path

    except Exception as e:
        # 兜底：失败时直接拷贝原文件，不中断主流程
        print(f"  ⚠️  时长调整失败（目标 {target_duration:.2f}s，原 {original_duration:.2f}s）：{e}")
        print("     回退到原文件...")
        shutil.copy2(clip_path, output_path)
        return output_path


def _build_atempo_filter(speed_ratio: float) -> str:
    """
    构建 atempo 滤镜链（atempo 单次范围 0.5-2.0，超出时用多级串联）。

    Args:
        speed_ratio: 目标播放速率（>1 加速，<1 减速）

    Returns:
        FFmpeg atempo 滤镜字符串，如 "atempo=1.5,atempo=1.2"
    """
    # 限制在合理范围
    rate = max(0.25, min(4.0, speed_ratio))
    remaining = rate
    filters = []

    # 多级串联，每级在 0.5-2.0 之间
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.3f}")

    return ",".join(filters)


def _has_audio_stream(video_path: Path) -> bool:
    """检测视频文件是否包含音频流"""
    if not video_path.exists():
        return False
    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def extract_last_frame(clip_path: Path, output_path: Path) -> Path:
    """
    提取视频最后一帧

    Args:
        clip_path: 视频文件路径
        output_path: 图片保存路径

    Returns:
        保存的图片路径
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-sseof", "-1",
        "-i", str(clip_path),
        "-q:v", "2",
        str(output_path),
    ]
    run_ffmpeg(cmd, timeout=30)
    return output_path


def extract_frame(clip_path: Path, output_path: Path, time_sec: float = 1.0) -> Path:
    """
    提取视频指定时间点的帧

    Args:
        clip_path: 视频文件路径
        output_path: 图片保存路径
        time_sec: 提取第几秒的帧

    Returns:
        保存的图片路径
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(time_sec),
        "-i", str(clip_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path),
    ]
    run_ffmpeg(cmd, timeout=30)
    return output_path


def generate_fallback_audio(
    output_path: Path,
    duration: float,
    sample_rate: int = 44100,
) -> Path:
    """
    生成兜底音频（粉红噪声 + 缓慢低通滤波，模拟环境底噪，不那么刺耳）

    当 BGM 和口播都失败时，用这个避免完全无声。
    音量极低（-40 LUFS 左右），几乎听不见，但能让视频"有声音"，
    防止抖音算法因为完全无声而降低推荐。

    Args:
        output_path: 输出音频路径
        duration: 时长（秒）
        sample_rate: 采样率

    Returns:
        输出文件路径
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 用 ffmpeg 生成粉红噪声 + 低通滤波 + 极低音量
    # 粉红噪声比白噪声更柔和，低通后更像环境底噪
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anoisesrc=d={duration}:c=pink:r={sample_rate}:a=0.02",
        "-af", "lowpass=f=800,volume=-45dB",
        "-ac", "2",
        "-ar", str(sample_rate),
        "-c:a", "aac",
        "-b:a", "64k",
        str(output_path),
    ]
    run_ffmpeg(cmd, timeout=60)
    return output_path


def add_logo_watermark(
    video: Path,
    output: Path,
    logo_path: Path,
    position: str = "top_right",
    size_ratio: float = 0.08,
    margin_ratio: float = 0.03,
    opacity: float = 0.9,
    fade_in: float = 0.5,
    fade_out: float = 0.5,
) -> Path:
    """
    为视频添加品牌 Logo 水印

    Args:
        video: 输入视频
        output: 输出视频
        logo_path: Logo 图片路径（PNG 透明底）
        position: 位置（top_left / top_right / bottom_left / bottom_right）
        size_ratio: Logo 宽度占视频宽度的比例
        margin_ratio: 边距比例（相对视频宽/高）
        opacity: 透明度 0-1
        fade_in: 淡入时长（秒）
        fade_out: 淡出时长（秒）

    Returns:
        输出文件路径
    """
    if not logo_path.exists():
        print(f"⚠️  Logo 文件不存在：{logo_path}，跳过水印")
        shutil.copy2(video, output)
        return output

    # 获取视频尺寸
    video_w, video_h = 1080, 1920  # 默认竖屏
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(video),
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        if probe_result.returncode == 0 and probe_result.stdout.strip():
            wh = probe_result.stdout.strip().split(",")
            if len(wh) == 2:
                video_w, video_h = int(wh[0]), int(wh[1])
    except Exception:
        pass

    # 获取视频时长
    duration = 0.0
    try:
        dur_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
        dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
        if dur_result.returncode == 0 and dur_result.stdout.strip():
            duration = float(dur_result.stdout.strip())
    except Exception:
        pass

    # Logo 目标宽度
    logo_target_w = int(video_w * size_ratio)
    margin_x = int(video_w * margin_ratio)
    margin_y = int(video_h * margin_ratio)

    # 计算 overlay 位置表达式
    # 注意：overlay 中可以用 W/H 引用主视频宽高，用 w/h 引用 Logo 宽高
    if position == "top_left":
        x_expr = str(margin_x)
        y_expr = str(margin_y)
    elif position == "top_right":
        x_expr = f"W-w-{margin_x}"
        y_expr = str(margin_y)
    elif position == "bottom_left":
        x_expr = str(margin_x)
        y_expr = f"H-h-{margin_y}"
    elif position == "bottom_right":
        x_expr = f"W-w-{margin_x}"
        y_expr = f"H-h-{margin_y}"
    else:
        x_expr = f"W-w-{margin_x}"
        y_expr = str(margin_y)

    # 淡出开始时间
    fade_out_start = max(0, duration - fade_out) if duration > 0 else 5

    # 滤镜链：缩放 → 透明度 → 淡入 → 淡出 → 叠加
    filter_complex = (
        f"[1:v]scale={logo_target_w}:-1,"
        f"format=rgba,"
        f"colorchannelmixer=aa={opacity},"
        f"fade=t=in:st=0:d={fade_in}:alpha=1,"
        f"fade=t=out:st={fade_out_start}:d={fade_out}:alpha=1[logo];"
        f"[0:v][logo]overlay=x={x_expr}:y={y_expr}:format=auto[vout]"
    )

    # 水印 overlay 只修改视频滤镜，视频流用 libx264 重编码保持兼容性
    # 注：overlay 滤镜无法与 -c:v copy 共存，此处为必要重编码，但已是最后水印步骤
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video),
        "-i", str(logo_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    # #6 修复：无音轨保护，没有音频流时不映射 0:a
    if _has_audio_stream(video):
        cmd.extend(["-map", "0:a", "-c:a", "copy"])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",   # 与主流水线统一为 CRF 16（原 18 偏松）
        *_video_vbv_args(),
        "-movflags", "+faststart",
        str(output),
    ])

    try:
        run_ffmpeg(cmd, timeout=300)
        print(f"✅ Logo 水印已添加（{position}）")
    except Exception as e:
        print(f"⚠️  Logo 水印添加失败：{e}")
        shutil.copy2(video, output)

    return output


def generate_cover_image(
    base_image: Path,
    output_path: Path,
    title: str,
    subtitle: str = "",
    hook_text: str = "",
    tag_text: str = "",
    brand_name: str = "",
    primary_color: str = "#FF6B6B",
    aspect_ratio: str = "9:16",
    logo_path: Path = None,
) -> Path:
    """
    生成抖音风格封面图（Pillow 实现）

    封面元素（从顶部到底部）：
    - 顶部标签（可选，如"真实测评"/"亲测有效"，增强信任）
    - Hook 大标题（抓人眼球的痛点/问句，最醒目）
    - 副标题（产品名/辅助信息）
    - 底部品牌角标 + Logo

    Args:
        base_image: 基础图片路径（视频帧）
        output_path: 输出封面路径
        title: 主标题（核心卖点/钩子）
        subtitle: 副标题（产品名等）
        hook_text: Hook 文案（大字标题，最抓人眼球的一句话）
        tag_text: 顶部标签文字（如"亲测有效"/"真实分享"）
        brand_name: 品牌名
        primary_color: 品牌主色（HEX，如 #FF6B6B）
        aspect_ratio: 画面比例（9:16 / 16:9 / 1:1）
        logo_path: Logo 图片路径（可选，PNG 透明底）

    Returns:
        封面图片路径
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    # 确定画布尺寸
    if aspect_ratio == "9:16":
        canvas_w, canvas_h = 1080, 1920
    elif aspect_ratio == "16:9":
        canvas_w, canvas_h = 1920, 1080
    elif aspect_ratio == "1:1":
        canvas_w, canvas_h = 1080, 1080
    else:
        canvas_w, canvas_h = 1080, 1920

    # 1. 打开基础图，cover 模式填满画布
    try:
        with Image.open(base_image) as src:
            base = src.convert("RGB")
    except Exception:
        # 兜底：纯色背景
        base = Image.new("RGB", (canvas_w, canvas_h), "#333333")

    # Cover 缩放 + 中心裁剪
    base_ratio = base.width / base.height
    canvas_ratio = canvas_w / canvas_h
    if base_ratio > canvas_ratio:
        # 图更宽，以高为准缩放，裁两边
        new_h = canvas_h
        new_w = int(new_h * base_ratio)
    else:
        # 图更高，以宽为准缩放，裁上下
        new_w = canvas_w
        new_h = int(new_w / base_ratio)
    base = base.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - canvas_w) // 2
    top = (new_h - canvas_h) // 2
    base = base.crop((left, top, left + canvas_w, top + canvas_h))

    img = base.copy()
    draw = ImageDraw.Draw(img, "RGBA")

    # 2. P2-12：全画面半透明底层遮罩（alpha=55，约 22%）
    # 解决中间区域（顶部 45%~底部 25% 之间约 30% 高度）没有任何遮罩，
    # 背景亮色时文字完全看不清的问题。
    full_overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 55))
    img.paste(full_overlay, (0, 0), full_overlay)

    # 顶部渐变遮罩（从上到下，半透明黑→透明）
    gradient_height = int(canvas_h * 0.45)
    # 高效方式：生成 1px 宽的渐变条，再 resize 拉宽
    grad_strip = Image.new("RGBA", (1, gradient_height))
    grad_px = grad_strip.load()
    for y in range(gradient_height):
        alpha = int((1 - y / gradient_height) * 180)  # 顶部 180/255 不透明度
        grad_px[0, y] = (0, 0, 0, alpha)
    grad_full = grad_strip.resize((canvas_w, gradient_height), Image.NEAREST)
    img.paste(grad_full, (0, 0), grad_full)

    # 底部渐变遮罩
    bottom_gradient_height = int(canvas_h * 0.25)
    bottom_strip = Image.new("RGBA", (1, bottom_gradient_height))
    bottom_px = bottom_strip.load()
    for y in range(bottom_gradient_height):
        alpha = int((y / bottom_gradient_height) * 150)
        bottom_px[0, y] = (0, 0, 0, alpha)
    bottom_full = bottom_strip.resize((canvas_w, bottom_gradient_height), Image.NEAREST)
    img.paste(bottom_full, (0, canvas_h - bottom_gradient_height), bottom_full)

    # 字体配置（复用全局字体查找逻辑，确保跨平台一致）
    def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        """加载字体，复用 _get_font_path 确保跨平台一致"""
        font_path = _get_font_path("bold" if bold else "regular")
        if font_path and Path(font_path).exists():
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass
        # 兜底：默认字体
        return ImageFont.load_default()

    # 3. 顶部标签（可选，增强信任/紧迫感）
    current_y = int(canvas_h * 0.12)
    if tag_text:
        tag_size = int(canvas_w * 0.035)
        tag_font = _load_font(tag_size, bold=True)

        bbox_tag = draw.textbbox((0, 0), tag_text, font=tag_font)
        tag_w = bbox_tag[2] - bbox_tag[0]
        tag_h = bbox_tag[3] - bbox_tag[1]
        tag_x = (canvas_w - tag_w) // 2

        # 品牌色圆角背景（用矩形模拟）
        pad_x = int(tag_size * 0.6)
        pad_y = int(tag_size * 0.2)
        bg_x1 = tag_x - pad_x
        bg_y1 = current_y - pad_y
        bg_x2 = tag_x + tag_w + pad_x
        bg_y2 = current_y + tag_h + pad_y

        draw.rounded_rectangle(
            [bg_x1, bg_y1, bg_x2, bg_y2],
            radius=int(tag_size * 0.4),
            fill=primary_color,
        )

        draw.text(
            (tag_x, current_y),
            tag_text,
            font=tag_font,
            fill="white",
        )
        current_y = bg_y2 + int(canvas_h * 0.02)

    # 4. 主标题（Hook 文案，最大最醒目）
    # 如果有 hook_text，用 hook 做大标题，title 做中标题
    main_title = hook_text if hook_text else title
    title_size = int(canvas_w * 0.11)  # 宽度的 11%，更醒目
    title_font = _load_font(title_size, bold=True)

    # 自动换行（标题太长时拆成两行）
    def _wrap_text(text: str, font, max_width: int) -> list:
        """简单中文换行"""
        lines = []
        current_line = ""
        for char in text:
            test_line = current_line + char
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = char
        if current_line:
            lines.append(current_line)
        return lines

    max_title_width = int(canvas_w * 0.85)
    title_lines = _wrap_text(main_title, title_font, max_title_width)

    # 计算总标题高度
    line_height = int(title_size * 1.2)
    total_title_h = line_height * len(title_lines)

    # 如果标题太多，适当缩小字号
    if len(title_lines) > 2:
        title_size = int(canvas_w * 0.09)
        title_font = _load_font(title_size, bold=True)
        title_lines = _wrap_text(main_title, title_font, max_title_width)
        line_height = int(title_size * 1.2)
        total_title_h = line_height * len(title_lines)

    title_y = current_y

    # 绘制每一行标题
    for i, line in enumerate(title_lines):
        bbox = draw.textbbox((0, 0), line, font=title_font, stroke_width=4)
        line_w = bbox[2] - bbox[0]
        line_x = (canvas_w - line_w) // 2
        line_y = title_y + i * line_height

        # 阴影
        shadow_offset = max(3, title_size // 25)
        draw.text(
            (line_x + shadow_offset, line_y + shadow_offset),
            line,
            font=title_font,
            fill=(0, 0, 0, 200),
            stroke_width=4,
            stroke_fill=(0, 0, 0, 220),
        )

        # 标题正文（白色 + 黑色描边）
        stroke_w = max(3, title_size // 16)
        draw.text(
            (line_x, line_y),
            line,
            font=title_font,
            fill="white",
            stroke_width=stroke_w,
            stroke_fill="black",
        )

    current_y = title_y + total_title_h + int(canvas_h * 0.015)

    # 5. 副标题/产品名（如果有 hook_text，title 降级为副标题；否则 subtitle 是副标题）
    sub_title = title if hook_text else subtitle
    if sub_title and sub_title != main_title:
        sub_size = int(canvas_w * 0.045)
        sub_font = _load_font(sub_size, bold=False)

        bbox_sub = draw.textbbox((0, 0), sub_title, font=sub_font)
        sub_w = bbox_sub[2] - bbox_sub[0]
        sub_x = (canvas_w - sub_w) // 2
        sub_y = current_y

        # 品牌色短横线装饰（左右各一条）
        line_w = int(canvas_w * 0.08)
        line_h = max(2, int(sub_size * 0.08))
        line_y = sub_y + int(sub_size * 0.5)
        gap = int(canvas_w * 0.03)

        draw.rectangle(
            [(canvas_w // 2 - sub_w // 2 - gap - line_w, line_y),
             (canvas_w // 2 - sub_w // 2 - gap, line_y + line_h)],
            fill=primary_color,
        )
        draw.rectangle(
            [(canvas_w // 2 + sub_w // 2 + gap, line_y),
             (canvas_w // 2 + sub_w // 2 + gap + line_w, line_y + line_h)],
            fill=primary_color,
        )

        draw.text(
            (sub_x, sub_y),
            sub_title,
            font=sub_font,
            fill="white",
            stroke_width=max(1, sub_size // 15),
            stroke_fill="black",
        )
        current_y = sub_y + sub_size + int(canvas_h * 0.02)

    # 6. 品牌角标（左下角） + Logo（右下角）
    # 抖音信息流底部 UI 约占 20-25%，品牌信息上移到 78% 位置确保可见
    # 右侧点赞栏约占 8-10% 宽度，右边距增加到 12%
    safe_bottom_ratio = 0.78  # 底部安全区上界（距顶部 78%）
    safe_right_margin = 0.12  # 右侧安全边距

    if brand_name:
        brand_size = int(canvas_w * 0.035)
        brand_font = _load_font(brand_size, bold=True)
        brand_x = int(canvas_w * 0.06)
        brand_y = int(canvas_h * safe_bottom_ratio)

        # 品牌色装饰条
        bar_w = int(canvas_w * 0.01)
        bar_h = int(brand_size * 1.2)
        draw.rectangle(
            [(brand_x, brand_y - 2), (brand_x + bar_w, brand_y + bar_h)],
            fill=primary_color,
        )

        # 品牌文字
        draw.text(
            (brand_x + bar_w + 15, brand_y),
            brand_name,
            font=brand_font,
            fill="white",
            stroke_width=max(1, brand_size // 15),
            stroke_fill="black",
        )

    # 右下角 Logo（如果提供）
    if logo_path and logo_path.exists():
        try:
            with Image.open(logo_path) as src:
                logo_img = src.convert("RGBA")
            logo_target_w = int(canvas_w * 0.15)
            logo_ratio = logo_target_w / logo_img.width
            logo_target_h = int(logo_img.height * logo_ratio)
            logo_img = logo_img.resize((logo_target_w, logo_target_h), Image.LANCZOS)

            logo_x = canvas_w - logo_target_w - int(canvas_w * safe_right_margin)
            logo_y = int(canvas_h * safe_bottom_ratio)

            img.paste(logo_img, (logo_x, logo_y), logo_img)
        except Exception:
            pass

    # 保存
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=95)
    return output_path


def extract_keyframes(clip_path: Path, output_dir: Path, count: int = 3) -> List[Path]:
    """
    提取视频的多个关键帧（均匀分布）

    Args:
        clip_path: 视频文件路径
        output_dir: 输出目录
        count: 提取帧数（默认 3：开始、中间、结束）

    Returns:
        保存的图片路径列表
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = _get_clip_duration(clip_path)
    frame_paths = []

    for i in range(count):
        # 均匀分布：0%, 50%, 100%（对于 count=3）
        # 对于 count=5：0%, 25%, 50%, 75%, 100%
        t = (duration / max(count - 1, 1)) * i
        frame_path = output_dir / f"frame_{i:02d}_{clip_path.stem}.png"

        cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(t),
            "-i", str(clip_path),
            "-q:v", "2",
            "-frames:v", "1",
            str(frame_path),
        ]
        run_ffmpeg(cmd, timeout=30)
        frame_paths.append(frame_path)

    return frame_paths


def create_transition_ffmpeg(
    clip1: Path,
    clip2: Path,
    output: Path,
    transition_type: str = "fade",
    duration: float = TRANSITION_DURATION,
) -> Path:
    """
    使用 ffmpeg xfade 创建两个片段之间的转场效果

    Args:
        clip1: 第一个片段
        clip2: 第二个片段
        output: 输出路径
        transition_type: 转场类型（fade/fadeblack/fadewhite/dissolve）
        duration: 转场时长（秒）

    Returns:
        输出文件路径
    """
    xfade_type = XFADE_TYPE_MAP.get(transition_type, "fade")

    clip1_duration = _get_clip_duration(clip1)
    clip2_duration = _get_clip_duration(clip2)
    duration = min(duration, min(clip1_duration, clip2_duration) * 0.45)
    if duration <= 0:
        raise RuntimeError(f"片段时长异常，无法创建转场：{clip1.name} / {clip2.name}")
    offset = clip1_duration - duration

    # P0 修复：检测两个片段是否都有音轨，避免 acrossfade 引用不存在的音频流导致崩溃
    clip1_has_audio = _has_audio_stream(clip1)
    clip2_has_audio = _has_audio_stream(clip2)
    both_have_audio = clip1_has_audio and clip2_has_audio

    if both_have_audio:
        filter_complex = (
            f"[0:v]scale={OUTPUT_RESOLUTION.replace('x', ':')}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_RESOLUTION.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS},format=yuv420p,setsar=1,"
            f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB)[v0n];"
            f"[1:v]scale={OUTPUT_RESOLUTION.replace('x', ':')}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_RESOLUTION.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS},format=yuv420p,setsar=1,"
            f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB)[v1n];"
            f"[v0n][v1n]xfade=transition={xfade_type}:duration={duration}:offset={offset}[vout];"
            f"[0:a][1:a]acrossfade=d={duration}:c1=tri:c2=tri[aout]"
        )
        audio_map = ["[aout]"]
    else:
        # 至少一个片段无音轨：只做视频转场，音频按需合并或丢弃
        filter_parts_v = (
            f"[0:v]scale={OUTPUT_RESOLUTION.replace('x', ':')}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_RESOLUTION.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS},format=yuv420p,setsar=1,"
            f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB)[v0n];"
            f"[1:v]scale={OUTPUT_RESOLUTION.replace('x', ':')}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_RESOLUTION.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS},format=yuv420p,setsar=1,"
            f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB)[v1n];"
            f"[v0n][v1n]xfade=transition={xfade_type}:duration={duration}:offset={offset}[vout]"
        )
        if clip1_has_audio and not clip2_has_audio:
            # clip1 有音，clip2 无：给 clip2 补静音再 acrossfade
            filter_complex = (
                filter_parts_v + ";"
                f"aevalsrc=0:d={clip2_duration},aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo[sil2];"
                f"[0:a][sil2]acrossfade=d={duration}:c1=tri:c2=tri[aout]"
            )
            audio_map = ["[aout]"]
        elif not clip1_has_audio and clip2_has_audio:
            # clip1 无音，clip2 有：给 clip1 补静音再 acrossfade
            filter_complex = (
                filter_parts_v + ";"
                f"aevalsrc=0:d={clip1_duration},aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo[sil1];"
                f"[sil1][1:a]acrossfade=d={duration}:c1=tri:c2=tri[aout]"
            )
            audio_map = ["[aout]"]
        else:
            # 两个都无音轨：只输出视频
            filter_complex = filter_parts_v
            audio_map = []

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(clip1),
        "-i", str(clip2),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    for am in audio_map:
        cmd.extend(["-map", am])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",
        *_video_vbv_args(),
        "-pix_fmt", "yuv420p", *_color_range_args(),
    ])
    if audio_map:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    cmd.append(str(output))

    run_ffmpeg(cmd)
    return output


def normalize_transition_decisions(
    clips: List[Path],
    transitions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return the exact frame-aligned transition durations the renderer will use."""
    if len(transitions) != max(0, len(clips) - 1):
        raise ValueError(
            f"转场数量必须等于镜头数减一：收到 {len(transitions)} 个转场 / {len(clips)} 个镜头"
        )
    durations = [_get_clip_duration(clip) for clip in clips]
    normalized = []
    for index, transition in enumerate(transitions):
        item = dict(transition)
        transition_type = str(item.get("type") or "none").lower()
        requested = item.get("duration")
        requested_duration = 0.0 if requested is None else float(requested)
        if transition_type in {"none", "cut"}:
            render_duration = 0.0
        else:
            render_duration = min(
                requested_duration,
                min(durations[index], durations[index + 1]) * 0.45,
            )
            if render_duration <= 0:
                raise ValueError(f"边界 {index} 的 {transition_type} 转场时长必须大于 0")
            render_duration = round(render_duration * OUTPUT_FPS) / OUTPUT_FPS
        item["requested_duration"] = requested_duration
        item["duration"] = render_duration
        normalized.append(item)
    return normalized


def _merge_with_transitions(
    clips: List[Path],
    output: Path,
    transitions: List[Dict[str, Any]],
    bgm: Optional[Path] = None,
    envelope_key_times: Optional[List[float]] = None,
    music_contract: Optional[Dict[str, Any]] = None,
    bgm_segment: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    使用 xfade 链实现多片段真正的交叉转场拼接

    支持 15 种转场类型（详见 TRANSITION_LIBRARY）：
    fade / dissolve / fadeblack / fadewhite /
    slideright / slideleft / slideup / slidedown /
    circlecrop / circleclose / zoomin / zoomout /
    wipeleft / wiperight / rectcrop

    Args:
        clips: 视频片段路径列表（>=2）
        output: 输出文件路径
        transitions: 转场配置列表（长度 = len(clips) - 1），
            每个元素包含 type（转场类型）和 duration（时长秒）
        bgm: BGM 文件路径

    Returns:
        输出文件路径
    """
    transitions = normalize_transition_decisions(clips, transitions)
    durations = [_get_clip_duration(clip) for clip in clips]
    # P0 修复：必须所有片段都有音轨才能用 acrossfade；any() 会导致引用无音轨片段的 [N:a] 崩溃
    clip_audio_flags = [_has_audio_stream(clip) for clip in clips]
    has_audio = all(clip_audio_flags)

    # 统一每个 clip 的分辨率、帧率和像素格式（xfade 前置归一化）
    norm_parts = []
    for i in range(len(clips)):
        norm_label = f"[vn{i:02d}]"
        norm_parts.append(
            f"[{i}:v]scale={OUTPUT_RESOLUTION.replace('x', ':')}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_RESOLUTION.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS},"
            f"format=yuv420p,"
            f"setsar=1,settb=expr=1/{OUTPUT_FPS},"
            f"setpts=N/({OUTPUT_FPS}*TB){norm_label}"
        )

    # 构建视频 xfade 滤镜链（使用归一化后的 clip）
    # O6 修复：用整数帧累积计算 offset，消除 5 段浮点累积后 ±0.05s 的时序误差
    video_parts = []
    current_vlabel = "[vn00]"
    current_duration_frames = round(durations[0] * OUTPUT_FPS)

    for i in range(len(clips) - 1):
        transition_name = str(transitions[i].get("type") or "none").lower()
        if transition_name in {"none", "cut"}:
            next_vlabel = f"[v{i:02d}]"
            video_parts.append(
                f"{current_vlabel}[vn{i+1:02d}]concat=n=2:v=1:a=0,"
                f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB){next_vlabel}"
            )
            current_vlabel = next_vlabel
            current_duration_frames += round(durations[i + 1] * OUTPUT_FPS)
            continue

        trans_type = XFADE_TYPE_MAP.get(transition_name, "fade")
        trans_duration = float(transitions[i]["duration"])
        if trans_duration <= 0:
            raise RuntimeError(f"片段时长异常，无法创建转场：{clips[i].name} / {clips[i + 1].name}")
        # 整数帧计算：先转帧数再转回秒，消除浮点累积误差
        trans_frames = round(trans_duration * OUTPUT_FPS)
        # offset 保护：防止 trans_frames >= dur_frames 时 ffmpeg 报错
        raw_offset_frames = current_duration_frames - trans_frames
        offset_frames = max(0, raw_offset_frames)
        offset = offset_frames / OUTPUT_FPS
        # 精确转场时长（按帧对齐）
        trans_duration_aligned = trans_frames / OUTPUT_FPS

        next_vlabel = f"[v{i:02d}]"
        video_parts.append(
            f"{current_vlabel}[vn{i+1:02d}]xfade=transition={trans_type}:"
            f"duration={trans_duration_aligned:.4f}:offset={offset:.4f},"
            f"settb=expr=1/{OUTPUT_FPS},setpts=N/({OUTPUT_FPS}*TB){next_vlabel}"
        )
        current_vlabel = next_vlabel
        current_duration_frames += round(durations[i + 1] * OUTPUT_FPS) - trans_frames

    filter_parts = norm_parts + video_parts

    # 构建音频滤镜链
    # P0 修复：has_audio=True 时所有片段都有音轨（all() 保证），可安全用 acrossfade
    # 若部分片段无音轨（has_audio=False），先为各无音轨片段补静音流，再构建 acrossfade 链
    current_alabel = None
    any_audio = any(clip_audio_flags)
    if any_audio:
        audio_parts = []
        # 为无音轨的片段插入静音流
        for i, (clip, flag) in enumerate(zip(clips, clip_audio_flags)):
            if not flag:
                sil_dur = _get_clip_duration(clip)
                audio_parts.append(
                    f"aevalsrc=0:d={sil_dur:.4f},aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo[sil{i:02d}]"
                )
        # 构建 acrossfade 链，无音轨片段使用补丁静音标签
        # P1-3 修复：acrossfade 要求两路输入在交叉点对齐。
        # 直接 [A][B]acrossfade 会从各自流的起始点开始淡化，导致 B 段音频比画面提前切入。
        # 修复：用 acrossfade 的 nb_samples 参数（即 d 参数）精确控制淡化，
        # 但更根本的是：把整条音频链改成 concat 模式，先拼接再整体处理，
        # 而非逐对 acrossfade（逐对模式每次都从流头开始，无法做全局时间偏移）。
        # 实用方案：保留现有 acrossfade 链结构，但对每段音频预先用 atrim+asetpts
        # 截取"转场前 d 秒"和"转场后 d 秒"，保证 acrossfade 两端对齐。
        # 最简方案（无副作用）：直接用 concat 拼接代替 acrossfade，
        # 对拼接点两侧各做 afade，效果等同但时间轴严格对齐。
        def _alabel(i: int) -> str:
            return f"[sil{i:02d}]" if not clip_audio_flags[i] else f"[{i}:a]"

        # 构建音频 concat：先为每个片段做 atrim（保留片段对应时长），再 concat 拼接
        # 比逐对 acrossfade 更可靠：时间轴严格对齐，无提前/滞后问题
        audio_concat_parts = []
        concat_labels = []
        for i in range(len(clips)):
            alabel = _alabel(i)
            trimmed = f"[at{i:02d}]"
            clip_dur = durations[i]
            audio_concat_parts.append(
                f"{alabel}atrim=duration={clip_dur:.4f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo{trimmed}"
            )
            concat_labels.append(trimmed)

        # concat 拼接所有片段
        concat_in = "".join(concat_labels)
        n = len(clips)
        audio_concat_parts.append(f"{concat_in}concat=n={n}:v=0:a=1[acatraw]")

        # 在拼接点两侧各做 afade 模拟转场淡化（等效于对齐的 acrossfade）
        fade_filters = []
        cumulative_a = 0.0
        for i in range(len(clips) - 1):
            trans_d = float(transitions[i].get("duration") or 0.0)
            if trans_d <= 0:
                cumulative_a += durations[i]
                continue
            fade_out_start = cumulative_a + durations[i] - trans_d
            fade_in_start  = cumulative_a + durations[i]
            if fade_out_start > 0:
                fade_filters.append(
                    f"afade=t=out:st={fade_out_start:.4f}:d={trans_d:.4f}:curve=tri"
                )
            fade_filters.append(
                f"afade=t=in:st={fade_in_start:.4f}:d={trans_d:.4f}:curve=tri"
            )
            cumulative_a += durations[i]

        if fade_filters:
            audio_concat_parts.append(
                f"[acatraw]{','.join(fade_filters)}[acatfaded]"
            )
            current_alabel = "[acatfaded]"
        else:
            current_alabel = "[acatraw]"

        # P0 修复：将音频精确截断到视频总时长（concat 输出比 xfade 视频长 sum(trans_d)）
        total_video_duration = sum(durations) - sum(float(t.get("duration") or 0.0) for t in transitions)
        audio_trimmed = "[acattrim]"
        audio_concat_parts.append(
            f"{current_alabel}atrim=duration={total_video_duration:.4f},asetpts=PTS-STARTPTS{audio_trimmed}"
        )
        current_alabel = audio_trimmed

        filter_parts += audio_concat_parts

    # BGM 混音（智能选段 + 淡入淡出）
    bgm_input_index = len(clips)
    if bgm and bgm.exists():
        total_duration = sum(durations) - sum(float(t.get("duration") or 0.0) for t in transitions)
        # Bug #4 修复：直接传入正确的 input_label，消除脆弱的 string replace
        bgm_filter = _build_bgm_audio_filter(
            total_duration,
            bgm_path=bgm,
            input_label=f"[{bgm_input_index}:a]",
            num_clips=len(clips),
            envelope_key_times=envelope_key_times,
            music_contract=music_contract,
            bgm_segment=bgm_segment,
        )
        filter_parts.append(bgm_filter)
        if any_audio and current_alabel:
            filter_parts.append(
                f"[bgm]{current_alabel}amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]"
            )
        else:
            filter_parts.append("[bgm]anull[outa]")
    elif any_audio and current_alabel:
        # P1 修复：无 BGM 但片段有音频时，必须将 current_alabel 路由到 [outa]
        filter_parts.append(f"{current_alabel}anull[outa]")

    filter_complex = ";".join(filter_parts)

    # 构建输入参数
    inputs = []
    for clip in clips:
        inputs.extend(["-i", str(clip)])
    if bgm and bgm.exists():
        inputs.extend(["-i", str(bgm)])

    video_map = current_vlabel
    has_output_audio = (bgm and bgm.exists()) or any_audio
    audio_map = "[outa]" if has_output_audio else None

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", video_map,
    ]
    if audio_map:
        cmd.extend(["-map", audio_map])
    # norm_parts 已在 filter_complex 中将各片段精确归一化到 OUTPUT_RESOLUTION，
    # 无需再加 -vf scale+pad（否则会对已归一化的输出流做二次 scale，引入额外压缩损耗）
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",
        *_video_vbv_args(),
        "-r", str(OUTPUT_FPS),
        "-pix_fmt", "yuv420p", *_color_range_args(),
    ])
    if audio_map:
        cmd.extend([
            "-c:a", "aac",
            "-b:a", "192k",
        ])
    cmd.extend([
        "-movflags", "+faststart",
        str(output),
    ])

    run_ffmpeg(cmd, timeout=600)
    return output


def _merge_simple_concat(
    clips: List[Path],
    output: Path,
    bgm: Optional[Path] = None,
    envelope_key_times: Optional[List[float]] = None,
    music_contract: Optional[Dict[str, Any]] = None,
    bgm_segment: Optional[Dict[str, Any]] = None,
) -> Path:
    """简单拼接（无转场或回退方案）"""
    if not clips:
        raise ValueError("没有可拼接的视频片段")

    if len(clips) == 1:
        shutil.copy2(clips[0], output)
        return output

    has_audio = any(_has_audio_stream(clip) for clip in clips)
    # P1 修复：concat demuxer 要求所有输入流结构完全一致，音频不一致时回退到 filter_complex
    has_audio_flags = [_has_audio_stream(clip) for clip in clips]
    if any(has_audio_flags) and not all(has_audio_flags):
        return _merge_with_transitions(
            clips, output,
            transitions=[{"type": "fade", "duration": 0.0} for _ in range(len(clips) - 1)],
            bgm=bgm,
            envelope_key_times=envelope_key_times,
            music_contract=music_contract,
            bgm_segment=bgm_segment,
        )

    concat_file = output.parent / "concat_list.txt"
    try:
        with open(concat_file, "w", encoding="utf-8") as f:
            for clip in clips:
                f.write(f"file '{_ffconcat_escape_path(clip)}'\n")

        # R3 修复：用 scale+pad 替换 -s，防止非标尺寸画面拉伸变形
        _rw, _rh = OUTPUT_RESOLUTION.split("x")
        _scale_pad = (
            f"scale={_rw}:{_rh}:force_original_aspect_ratio=decrease,"
            f"pad={_rw}:{_rh}:(ow-iw)/2:(oh-ih)/2:black"
        )

        if bgm and bgm.exists():
            # P0 修复：-vf 与 -filter_complex 不能共存，有 BGM 时把视频归一化也放进 filter_complex
            total_duration = sum(_get_clip_duration(c) for c in clips)
            bgm_filter = _build_bgm_audio_filter(
                total_duration,
                bgm_path=bgm,
                num_clips=len(clips),
                envelope_key_times=envelope_key_times,
                music_contract=music_contract,
                bgm_segment=bgm_segment,
            )
            if has_audio:
                fcx = (
                    f"[0:v]{_scale_pad}[vnorm];{bgm_filter};"
                    f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]"
                )
            else:
                fcx = f"[0:v]{_scale_pad}[vnorm];{bgm_filter};[bgm]anull[outa]"
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-i", str(bgm),
                "-filter_complex", fcx,
                "-map", "[vnorm]",
                "-map", "[outa]",
                "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                *_video_vbv_args(),
                "-r", str(OUTPUT_FPS),
                "-pix_fmt", "yuv420p", *_color_range_args(),
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c:v", "libx264",
                "-preset", "slow",
                "-crf", "16",
                *_video_vbv_args(),
                "-r", str(OUTPUT_FPS),
                "-vf", _scale_pad,
                "-pix_fmt", "yuv420p", *_color_range_args(),
            ]
            if has_audio:
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        cmd.append(str(output))

        run_ffmpeg(cmd, timeout=600)
    finally:
        if concat_file.exists():
            concat_file.unlink()

    return output


def _get_video_color_stats(video_path: Path, sample_frames: int = 5) -> Dict[str, float]:
    """
    获取视频的颜色统计信息（平均亮度、色偏）

    从视频中均匀抽取多帧分析，避免单帧偶然性导致的匹配偏差。

    Args:
        video_path: 视频路径
        sample_frames: 采样帧数，默认 5 帧

    Returns:
        包含 brightness / r_avg / g_avg / b_avg 的字典
    """
    stats = {"brightness": 128.0, "r_avg": 128.0, "g_avg": 128.0, "b_avg": 128.0}

    try:
        import tempfile
        import subprocess
        from PIL import Image, ImageStat

        # 先获取视频时长
        duration = 0.0
        try:
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
            duration = float(result.stdout.strip())
        except Exception as e:
            print(f"⚠️  ffprobe 获取视频时长失败，颜色统计使用 5s 保底：{e}")
            duration = 5.0

        with tempfile.TemporaryDirectory() as tmpdir:
            r_total = g_total = b_total = 0.0
            valid_frames = 0

            for i in range(sample_frames):
                # 均匀分布采样点，避开首尾 10%
                t = duration * (0.1 + 0.8 * i / max(sample_frames - 1, 1))
                t = max(0.1, min(duration - 0.1, t))

                frame_path = Path(tmpdir) / f"frame_{i:02d}.png"
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{t:.2f}",
                    "-i", str(video_path),
                    "-vframes", "1",
                    str(frame_path),
                ]
                try:
                    run_ffmpeg(cmd, timeout=15)
                except Exception:
                    continue

                if frame_path.exists():
                    with Image.open(frame_path) as src:
                        img = src.convert("RGB")
                    if img.width > 320:
                        img = img.resize((320, int(img.height * 320 / img.width)), Image.LANCZOS)
                    stat = ImageStat.Stat(img)
                    r_total += stat.mean[0]
                    g_total += stat.mean[1]
                    b_total += stat.mean[2]
                    valid_frames += 1

            if valid_frames > 0:
                stats["r_avg"] = r_total / valid_frames
                stats["g_avg"] = g_total / valid_frames
                stats["b_avg"] = b_total / valid_frames
                stats["brightness"] = (stats["r_avg"] + stats["g_avg"] + stats["b_avg"]) / 3
    except Exception as e:
        print(f"⚠️  ffprobe 获取字幕源视频信息失败，使用默认 1080x1920：{e}")

    return stats


def _build_color_match_filter(ref_stats: Dict[str, float], target_stats: Dict[str, float]) -> str:
    """
    构建颜色匹配的 FFmpeg 滤镜字符串

    将 target_stats 的视频调整到 ref_stats 的亮度和色偏。
    使用 colorchannelmixer 应用 RGB 通道增益，配合 eq 调整整体亮度。

    Args:
        ref_stats: 参考（目标）颜色统计
        target_stats: 当前视频颜色统计

    Returns:
        FFmpeg 滤镜字符串
    """
    # 色偏修正：计算 RGB 三个通道的相对增益
    r_ratio = (ref_stats["r_avg"] / target_stats["r_avg"]) if target_stats["r_avg"] > 0 else 1.0
    g_ratio = (ref_stats["g_avg"] / target_stats["g_avg"]) if target_stats["g_avg"] > 0 else 1.0
    b_ratio = (ref_stats["b_avg"] / target_stats["b_avg"]) if target_stats["b_avg"] > 0 else 1.0

    # P1-6：限制修正强度（收窄到 ±15%，防止与后续 apply_color_grading 对冲）
    # 原来 ±30% 过强，调色滤镜再叠一层会重新拉开片段间色差
    r_ratio = max(0.85, min(1.15, r_ratio))
    g_ratio = max(0.88, min(1.13, g_ratio))
    b_ratio = max(0.85, min(1.15, b_ratio))

    # 亮度差：通过 G 通道增益的偏差来估算整体亮度调整
    # colorchannelmixer 已经处理了通道增益，亮度用 eq 微调
    bright_diff = (ref_stats["brightness"] - target_stats["brightness"]) / 255.0
    # P1-6：收窄亮度修正范围（±10% 而非 ±15%），减少与调色滤镜叠加时的对冲
    bright_diff = max(-0.10, min(0.10, bright_diff))

    # 如果差异太小，不做修正
    if (abs(r_ratio - 1.0) < 0.02 and abs(g_ratio - 1.0) < 0.02
            and abs(b_ratio - 1.0) < 0.02 and abs(bright_diff) < 0.01):
        return ""

    filters = []

    # P2 修复：将 RGB 增益修正和色温修正合并为单个 colorchannelmixer
    # 原来串联两个 colorchannelmixer，等效修正强度约 ±25%，超出预期的 ±15% 限制
    # 合并后：最终通道增益 = rgb_ratio * temp_ratio（矩阵直接相乘），修正强度精确可控
    rb_diff = ref_stats["r_avg"] - ref_stats["b_avg"]
    rb_diff_tgt = target_stats["r_avg"] - target_stats["b_avg"]
    temperature_shift = (rb_diff - rb_diff_tgt) / 255.0
    temperature_shift = max(-0.12, min(0.12, temperature_shift))

    # 合并后的最终通道增益（RGB 修正 × 色温修正）
    t_r = 1.0 + temperature_shift * 0.5 if abs(temperature_shift) >= 0.02 else 1.0
    t_b = 1.0 - temperature_shift * 0.5 if abs(temperature_shift) >= 0.02 else 1.0
    final_r = r_ratio * t_r
    final_g = g_ratio
    final_b = b_ratio * t_b

    # 再次限制合并后增益范围，防止矩阵相乘后超出
    final_r = max(0.82, min(1.18, final_r))
    final_g = max(0.85, min(1.15, final_g))
    final_b = max(0.82, min(1.18, final_b))

    if abs(final_r - 1.0) >= 0.02 or abs(final_g - 1.0) >= 0.02 or abs(final_b - 1.0) >= 0.02:
        filters.append(
            f"colorchannelmixer="
            f"rr={final_r:.3f}:rg=0:rb=0:ra=0:"
            f"gr=0:gg={final_g:.3f}:gb=0:ga=0:"
            f"br=0:bg=0:bb={final_b:.3f}:ba=0"
        )

    # 再用 eq 微调整体亮度
    if abs(bright_diff) >= 0.01:
        filters.append(f"eq=brightness={bright_diff:.3f}")

    return ",".join(filters) if filters else ""


def color_match_clips(
    clips: List[Path],
    output_dir: Path,
) -> List[Path]:
    """
    对多个视频片段做颜色匹配（以全片段平均色调为参考基准）

    O1 优化：改为以所有片段的平均亮度/色温作为参考，
    而非只用第一段（Hook 段通常是近景特写，不代表全片基调）。

    Args:
        clips: 原始片段路径列表
        output_dir: 输出目录

    Returns:
        颜色匹配后的片段路径列表
    """
    if len(clips) <= 1:
        return clips

    output_dir.mkdir(parents=True, exist_ok=True)

    # O1：计算所有片段的颜色统计，取平均作为参考基准
    all_stats = []
    for clip in clips:
        try:
            all_stats.append(_get_video_color_stats(clip))
        except Exception:
            all_stats.append(None)

    valid_stats = [s for s in all_stats if s is not None]
    if not valid_stats:
        return clips

    # P1-6 修复：改用中位数而非均值，防止 Hook 近景特写拉偏全片基准
    # Hook 段通常是极近景人脸，肤色偏暖，均值会把整批片段强制加暖
    def _median(vals):
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0

    ref_stats = {
        "brightness": _median([s["brightness"] for s in valid_stats]),
        "r_avg": _median([s["r_avg"] for s in valid_stats]),
        "g_avg": _median([s["g_avg"] for s in valid_stats]),
        "b_avg": _median([s["b_avg"] for s in valid_stats]),
    }

    print(f"🎨 片段颜色匹配（参考：全片平均，亮度 {ref_stats['brightness']:.0f}）")

    result = []
    for i, (clip, clip_stats) in enumerate(zip(clips, all_stats), 1):
        if clip_stats is None:
            result.append(clip)
            continue

        filter_str = _build_color_match_filter(ref_stats, clip_stats)

        if not filter_str:
            result.append(clip)
            continue

        matched_path = output_dir / f"clip_{i:02d}_colormatched.mp4"
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(clip),
                "-vf", filter_str,
                "-c:v", "libx264",
                "-preset", "slow",
                "-crf", "16",  # 与主流水线统一 CRF 16，减少重编码累积损耗
                "-pix_fmt", "yuv420p", *_color_range_args(),
                "-c:a", "copy",
                str(matched_path),
            ]
            run_ffmpeg(cmd, timeout=120)
            result.append(matched_path)
            bright_diff = ref_stats["brightness"] - clip_stats["brightness"]
            print(f"   片段 {i}：亮度差 {bright_diff:+.0f}，已匹配")
        except Exception as e:
            print(f"   片段 {i}：颜色匹配失败（{e}），使用原片段")
            result.append(clip)

    return result


def merge_clips_ffmpeg(
    clips: List[Path],
    output: Path,
    transitions: Optional[List[Dict[str, Any]]] = None,
    bgm: Optional[Path] = None,
    subtitles: Optional[List[Dict[str, Any]]] = None,
    envelope_key_times: Optional[List[float]] = None,
    strict_transitions: bool = False,
    music_contract: Optional[Dict[str, Any]] = None,
    bgm_segment: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    使用 ffmpeg 拼接多个视频片段，支持真正的交叉转场

    Args:
        clips: 视频片段路径列表
        output: 输出文件路径
        transitions: 转场配置列表，每个元素包含：
            - type: 转场类型（fade/dissolve/fadeblack/fadewhite/slideright/slideleft/
              slideup/slidedown/circlecrop/circleclose/zoomin/zoomout/wipeleft/wiperight/rectcrop）
            - duration: 转场时长（秒）
        bgm: BGM 文件路径
        subtitles: （已废弃，由 add_subtitles_ffmpeg 单独处理）字幕配置列表
        envelope_key_times: Q4 优化：节奏模板的实际段落边界时间点（秒）列表。
            传入时用于构建精确的 BGM 音量包络曲线，不传则默认均分估算。
        strict_transitions: 智能转场模式。要求数量精确匹配，合成失败时直接阻断。

    Returns:
        输出文件路径
    """
    del subtitles  # 已废弃，保留参数仅用于向后兼容
    if not clips:
        raise ValueError("没有可拼接的视频片段")

    # 智能转场必须使用逐边界决策，禁止循环复用或失败后降级。
    if transitions:
        try:
            num_transitions_needed = len(clips) - 1
            if strict_transitions and len(transitions) != num_transitions_needed:
                raise RuntimeError(
                    f"智能转场数量不匹配：需要 {num_transitions_needed}，实际 {len(transitions)}"
                )
            full_transitions = (
                list(transitions)
                if strict_transitions
                else [transitions[i % len(transitions)] for i in range(num_transitions_needed)]
            )
            return _merge_with_transitions(
                clips,
                output,
                full_transitions,
                bgm,
                envelope_key_times=envelope_key_times,
                music_contract=music_contract,
                bgm_segment=bgm_segment,
            )
        except Exception as e:
            if strict_transitions:
                raise RuntimeError(f"智能转场合成失败，已阻断输出：{e}") from e
            print(f"⚠️ 转场拼接失败（{e}），回退到简单拼接")

    if strict_transitions and len(clips) > 1:
        raise RuntimeError("智能转场决策缺失，已阻断输出")

    # 简单拼接（无转场或转场失败时回退）
    return _merge_simple_concat(
        clips,
        output,
        bgm,
        envelope_key_times=envelope_key_times,
        music_contract=music_contract,
        bgm_segment=bgm_segment,
    )


def add_subtitles_ffmpeg(
    video: Path,
    subtitles: List[Dict[str, Any]],
    output: Path,
    font_size: int = SUBTITLE_FONT_SIZE,
    bottom_margin: int = 200,
) -> Path:
    """
    使用 ffmpeg 添加字幕

    Args:
        video: 输入视频
        subtitles: 字幕列表，每个元素包含：
            - text: 字幕文本
            - start: 开始时间（秒）
            - end: 结束时间（秒）
            - x/y: 位置（可选）
        output: 输出路径
        font_size: 字号
        bottom_margin: 底部边距（像素），抖音竖屏建议 200-280px，防止被小黄车/文案遮挡

    Returns:
        输出文件路径
    """
    if not subtitles:
        shutil.copy2(video, output)
        return output

    # 生成 SRT 字幕文件
    # 唯一文件名，避免多版本并行时冲突
    srt_path = output.parent / f"{output.stem}_subtitles.srt"
    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            for idx, sub in enumerate(subtitles, 1):
                start = sub.get("start", 0)
                end = sub.get("end", start + 2)
                text = _escape_srt_text(sub.get("text", ""))

                # 格式化时间
                start_str = format_srt_time(start)
                end_str = format_srt_time(end)

                f.write(f"{idx}\n")
                f.write(f"{start_str} --> {end_str}\n")
                f.write(f"{text}\n\n")

        # 使用 ffmpeg 烧录字幕（跨平台字体处理）
        font_path = find_system_font()
        subtitle_filter = f"subtitles={_ffmpeg_escape_filter_path(srt_path)}"
        if font_path:
            escaped_font_path = _ffmpeg_escape_filter_path(font_path)
            # P0 修复：Outline 从 5 降到 2，避免字幕描边过粗影响画面观感
            subtitle_filter += f":force_style='FontSize={font_size},PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Shadow=3,Alignment=2,MarginV={bottom_margin},FontFile={escaped_font_path}'"
        else:
            subtitle_filter += f":force_style='FontSize={font_size},PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Shadow=3,Alignment=2,MarginV={bottom_margin}'"

        # Bug #2 修复（SRT 路径）：同样补充 -c:v libx264
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video),
            "-vf", subtitle_filter,
            "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            *_video_vbv_args(),
            "-pix_fmt", "yuv420p", *_color_range_args(),
        ]
        # P1 修复：输入无音轨时 -c:a copy 会导致 ffmpeg 崩溃
        if _has_audio_stream(video):
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.append("-an")
        cmd.append(str(output))

        run_ffmpeg(cmd, timeout=300)
    finally:
        if srt_path.exists():
            srt_path.unlink()

    return output


def format_srt_time(seconds: float) -> str:
    """格式化 SRT 时间戳"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _escape_srt_text(text: str) -> str:
    """
    转义 SRT 字幕文本中的特殊字符，防止破坏 SRT 格式

    - 移除单独成行的纯数字（避免与序号混淆）
    - 替换行内 --> 为 ➔（避免与时间戳混淆）
    """
    lines = text.split("\n")
    escaped = []
    for line in lines:
        if line.strip().isdigit():
            line = line.strip() + "."
        line = line.replace("-->", "➔")
        escaped.append(line)
    return "\n".join(escaped)


def add_fancy_subtitles(
    video: Path,
    subtitles: List[Dict[str, Any]],
    output: Path,
    font_size: int = SUBTITLE_FONT_SIZE,
    primary_color: str = "#FFFFFF",
    accent_color: str = "#FF6B6B",
    animation: str = "pop",
    bottom_margin_ratio: float = 0.22,
) -> Path:
    """
    添加花字动效字幕（ASS 格式）

    支持的动效：
    - pop：逐字弹出 + 弹跳效果（抖音风格）
    - slide：从下往上滑入 + 淡入
    - fade：淡入淡出
    - highlight：关键词变色放大
    - typewriter：打字机逐字出现（模拟语音朗读节奏，口播场景推荐）

    Args:
        video: 输入视频
        subtitles: 字幕列表，每个元素包含 text/start/end/highlight（高亮词）
        output: 输出路径
        font_size: 基础字号
        primary_color: 主文字颜色（HEX）
        accent_color: 强调色/高亮色（HEX）
        animation: 动画类型（pop/slide/fade/highlight）
        bottom_margin_ratio: 底部边距比例（相对视频高度），抖音建议 0.22+，防止被小黄车/购物车遮挡

    Returns:
        输出文件路径
    """
    if not subtitles:
        shutil.copy2(video, output)
        return output

    font_path = find_system_font()

    # HEX -> ASS BGR 颜色格式（&HAABBGGRR）
    def _hex_to_ass_color(hex_color: str, alpha: str = "00") -> str:
        hex_color = hex_color.lstrip("#")
        r = hex_color[0:2]
        g = hex_color[2:4]
        b = hex_color[4:6]
        return f"&H{alpha}{b}{g}{r}"

    ass_primary = _hex_to_ass_color(primary_color, "00")
    ass_accent = _hex_to_ass_color(accent_color, "00")
    ass_outline = _hex_to_ass_color("#000000", "00")
    ass_shadow = _hex_to_ass_color("#000000", "50")  # 更深的阴影（alpha=80 更不透明）

    # 获取视频分辨率和时长，用于计算字幕位置和边界保护
    video_w, video_h = 1080, 1920  # 默认竖屏
    video_duration = 0.0
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        if probe_result.returncode == 0 and probe_result.stdout.strip():
            lines = probe_result.stdout.strip().split("\n")
            if len(lines) >= 2:
                video_w = int(lines[0])
                video_h = int(lines[1])
            if len(lines) >= 3 and lines[2].strip():
                video_duration = float(lines[2].strip())
    except Exception:
        pass

    # 字幕时间轴保护：裁剪超出视频时长的字幕
    if video_duration > 0:
        safe_end = video_duration - 0.05  # 留 50ms 余量
        clipped_subs = []
        for sub in subtitles:
            start = sub.get("start", 0)
            end = sub.get("end", start + 2)
            if start >= safe_end:
                continue  # 完全在外面，跳过
            if end > safe_end:
                end = safe_end
            if end - start < 0.1:
                continue  # 太短了，跳过
            new_sub = dict(sub)
            new_sub["start"] = max(0, start)
            new_sub["end"] = end
            clipped_subs.append(new_sub)
        if len(clipped_subs) != len(subtitles):
            print(f"  ✂️  字幕裁剪：{len(subtitles)} → {len(clipped_subs)} 条（超出视频时长）")
            subtitles = clipped_subs

    # 计算安全边距（先算边距，再基于实际边距算安全宽度）
    margin_v = int(video_h * bottom_margin_ratio)
    margin_lr = int(video_w * 0.15)  # 左右各留 15%，防止被抖音右侧点赞栏（约 8%）遮挡

    # 字号按视频宽度自适应（基准：1080 宽 -> font_size）
    scale = video_w / 1080.0
    actual_font_size = int(font_size * scale)
    highlight_size = int(actual_font_size * 1.3)  # 高亮字放大 30%

    # 长文本自动缩小字号，避免单行超出安全宽度
    # 必须用实际渲染的 margin_lr 来算安全宽度，否则字号算大了会超出
    safe_text_width = video_w - 2 * margin_lr
    max_line_len = 0
    for sub in subtitles:
        text = sub.get("text", "")
        # 粗略估算字符宽度（中文=1，英文数字=0.5）
        width_estimate = sum(1.0 if ord(c) > 127 else 0.5 for c in text)
        if width_estimate > max_line_len:
            max_line_len = width_estimate

    if max_line_len > 0 and actual_font_size * max_line_len > safe_text_width:
        new_size = int(safe_text_width / max_line_len)
        new_size = max(new_size, int(actual_font_size * 0.5))  # 最多缩小 50%（长文本宁小勿超）
        print(f"  📏 长文本自适应字号：{actual_font_size} → {new_size}（最长 {max_line_len:.0f} 字）")
        actual_font_size = new_size
        highlight_size = int(actual_font_size * 1.3)

    # 构建 ASS 字幕文件（使用 output stem 命名，避免多版本并行时文件冲突）
    ass_path = output.parent / f"{output.stem}_fancy_subs.ass"

    # 使用字体 PostScript 名称（TTC 字体集合需要内部名称才能被 libass 正确识别）
    font_display_name = _get_font_postscript_name(font_path) if font_path else "Arial"

    # ASS header
    ass_header = f"""[Script Info]
Title: Fancy Subtitles
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_display_name},{actual_font_size},{ass_primary},{ass_primary},{ass_outline},{ass_shadow},1,0,0,0,100,100,0,0,1,2,2,2,{margin_lr},{margin_lr},{margin_v},1
Style: Highlight,{font_display_name},{highlight_size},{ass_accent},{ass_accent},{ass_outline},{ass_shadow},1,0,0,0,100,100,0,0,1,2,2,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def _format_ass_time(seconds: float) -> str:
        """格式化 ASS 时间戳 H:MM:SS.cc"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        centisecs = int((seconds % 1) * 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

    ass_lines = []
    for idx, sub in enumerate(subtitles, 1):
        start = sub.get("start", 0)
        end = sub.get("end", start + 2)
        text = sub.get("text", "")
        highlight_words = sub.get("highlight", [])
        duration = max(end - start, 0.1)
        item_margin_v = margin_v

        if "\n" in text or "\\N" in text or _subtitle_units(text) * actual_font_size > safe_text_width:
            raise ValueError(
                f"subtitle exceeds single-line safe width: {text!r}; "
                "run prepare_single_line_subtitles before rendering"
            )

        start_str = _format_ass_time(start)
        end_str = _format_ass_time(end)
        item_color = str(sub.get("color") or primary_color)
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", item_color):
            item_color = "#FFFFFF"
        color_tag = f"{{\\c{_hex_to_ass_color(item_color, '00')}}}"

        item_animation = sub.get("animation", animation)
        if item_animation == "typewriter":
            # 打字机效果：逐行 Dialogue 实现，每行多显示一个字
            # O3 修复：打字速度基于字符数自适应，每字约 80ms（~750字/分钟），
            # 避免短字幕打完后静止过久，或长字幕来不及打完
            chars = list(text.replace("\n", "\\N"))
            # 每字 80ms，但总打字时长不超过 duration 的 70%，也不少于 30%
            char_ms = 80
            ideal_type = len(chars) * char_ms / 1000.0
            type_duration = max(duration * 0.3, min(ideal_type, duration * 0.7))
            hold_duration = duration - type_duration
            char_delay = type_duration / max(len(chars), 1)

            # P0 修复：保持阶段的全文本需先注入高亮标签，再逐字打出
            def _inject_highlight(raw: str, hw_list: list) -> str:
                """对原始文本注入 ASS 高亮样式标签"""
                result = raw
                for hw in hw_list:
                    if hw and hw in result:
                        tagged = f"{{\\rHighlight}}{hw}{{\\r}}"
                        result = result.replace(hw, tagged, 1)
                return result

            full_with_hl = _inject_highlight("".join(chars), highlight_words)
            # 打字阶段：逐字出现（不含高亮标签，避免标签字符被逐字切开）
            for i in range(1, len(chars) + 1):
                char_start = start + (i - 1) * char_delay
                char_end = start + i * char_delay
                if char_end - char_start < 0.02:
                    continue
                partial_text = "".join(chars[:i])
                cs = _format_ass_time(char_start)
                ce = _format_ass_time(char_end)
                ass_lines.append(
                    f"Dialogue: 0,{cs},{ce},Default,,0,0,{item_margin_v},,{color_tag}{partial_text}"
                )

            # 保持阶段：全部显示（含高亮）直到结束
            if hold_duration > 0.05 and chars:
                hold_start = start + type_duration
                hs = _format_ass_time(hold_start)
                ass_lines.append(
                    f"Dialogue: 0,{hs},{end_str},Default,,0,0,{item_margin_v},,{color_tag}{full_with_hl}"
                )

        elif item_animation == "pop":
            # P2-11：pop 动画锚点修复
            # 加 \an2（底部中心对齐），使缩放从中心展开而非左上角，防止字幕视觉跑位
            anim_tag = (
                f"{{\\an2\\fscx1\\fscy1\\t(0,150,\\fscx120\\fscy120)\\t(150,250,\\fscx100\\fscy100)}}"
            )
            _t = text.replace(chr(10), "\\N")
            # 注入高亮标签
            for hw in highlight_words:
                if hw and hw in _t:
                    _t = _t.replace(hw, f"{{\\rHighlight\\fscx130\\fscy130}}{hw}{{\\r}}", 1)
            display_text = f"{anim_tag}{color_tag}{_t}"
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,{item_margin_v},,{display_text}"
            )

        elif item_animation == "slide":
            slide_dist = int(video_h * 0.03)
            center_x = video_w // 2
            base_y = video_h - item_margin_v
            anim_tag = (
                f"{{\\move({center_x},{base_y + slide_dist},{center_x},{base_y},0,300)"
                f"\\alpha&HFF&\\t(0,300,\\alpha&H00&)}}"
            )
            _t = text.replace(chr(10), "\\N")
            display_text = f"{anim_tag}{color_tag}{_t}"
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,{item_margin_v},,{display_text}"
            )

        elif item_animation == "fade":
            fade_in = min(0.3, duration * 0.1)
            fade_out = min(0.3, duration * 0.1)
            anim_tag = (
                f"{{\\alpha&HFF&\\t(0,{int(fade_in*1000)},\\alpha&H00&)"
                f"\\t({int((duration-fade_out)*1000)},{int(duration*1000)},\\alpha&HFF&)}}"
            )
            _t = text.replace(chr(10), "\\N")
            display_text = f"{anim_tag}{color_tag}{_t}"
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,{item_margin_v},,{display_text}"
            )

        elif item_animation == "highlight":
            display_text = text
            for hw in highlight_words:
                if hw in display_text:
                    tagged = f"{{\\rHighlight\\fscx130\\fscy130}}{hw}{{\\r}}"
                    display_text = display_text.replace(hw, tagged)
            _t = display_text.replace(chr(10), "\\N")
            display_text = f"{{\\alpha&HFF&\\t(0,200,\\alpha&H00&)}}{color_tag}{_t}"
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,{item_margin_v},,{display_text}"
            )

        else:
            display_text = color_tag + text.replace(chr(10), "\\N")
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,{item_margin_v},,{display_text}"
            )

    ass_content = ass_header + "\n".join(ass_lines) + "\n"

    try:
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # FFmpeg 烧录 ASS 字幕
        if font_path:
            subtitle_filter = (
                f"ass={_ffmpeg_escape_filter_path(ass_path)}:"
                f"fontsdir={_ffmpeg_escape_filter_path(Path(font_path).parent)}"
            )
        else:
            subtitle_filter = f"ass={_ffmpeg_escape_filter_path(ass_path)}"

        # Bug #2 修复：补充 -c:v libx264，防止 ffmpeg 回退到低质量 mpeg4 编码器
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video),
            "-vf", subtitle_filter,
            "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            *_video_vbv_args(),
            "-pix_fmt", "yuv420p", *_color_range_args(),
        ]
        # P1 修复：输入无音轨时 -c:a copy 会导致 ffmpeg 崩溃
        if _has_audio_stream(video):
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.append("-an")
        cmd.append(str(output))

        run_ffmpeg(cmd, timeout=300)
    finally:
        if ass_path.exists():
            ass_path.unlink()

    return output


def _get_audio_duration(audio_path: Path) -> float:
    """获取音频文件时长（秒），带 LRU 缓存"""
    from utils_ffprobe import get_audio_duration
    return get_audio_duration(str(audio_path))


def _analyze_loudness(audio_path: Path, window_sec: float = 1.0) -> list[float]:
    """
    分析音频的响度曲线（按时间窗口采样 RMS）

    使用 FFmpeg 输出 PCM 数据，在 Python 中按窗口计算 RMS。
    比 astats 更可控，窗口大小精确。

    Args:
        audio_path: 音频文件路径
        window_sec: 每个分析窗口的时长（秒），默认 1 秒

    Returns:
        响度列表，每个元素是一个窗口的 RMS dB 值
        失败返回空列表
    """
    if not audio_path.exists():
        return []

    # 小窗口（节拍检测）用高采样率，大窗口用低采样率
    if window_sec < 0.1:
        sample_rate = 22050  # 节拍检测用更高采样率，精度更好
    else:
        sample_rate = 8000  # 整体响度分析用低采样率，速度快

    # 输出 8kHz 单声道 16-bit PCM 到 stdout
    cmd = [
        "ffmpeg",
        "-i", str(audio_path),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return []

    if not result.stdout:
        return []

    import array
    import math

    samples_per_window = int(sample_rate * window_sec)
    # 批量解析 16-bit signed int 小端
    samples = array.array("h")
    samples.frombytes(result.stdout)
    num_samples = len(samples)
    if num_samples == 0:
        return []

    rms_values = []
    for i in range(0, num_samples, samples_per_window):
        end = min(i + samples_per_window, num_samples)
        chunk = samples[i:end]
        if len(chunk) < 2:
            break
        sum_sq = sum(s * s for s in chunk)
        rms_linear = math.sqrt(sum_sq / len(chunk))
        # 转换为 dB（满幅参考 32768）
        if rms_linear > 0:
            rms_db = 20 * math.log10(rms_linear / 32768.0)
        else:
            rms_db = -100.0
        rms_values.append(rms_db)

    return rms_values


def _find_chorus_segment(
    audio_path: Path,
    target_duration: float,
    window_sec: float = 1.0,
) -> Optional[float]:
    """
    基于响度分析定位副歌（最响的连续片段）

    副歌通常是整首歌中响度最高、能量最强的段落。
    通过滑动窗口找到平均 RMS 最高的连续片段，作为副歌起始点。

    Args:
        audio_path: 音频文件路径
        target_duration: 需要的片段长度（秒）
        window_sec: 分析窗口大小（秒）

    Returns:
        副歌片段的起始时间（秒），分析失败返回 None
    """
    rms_values = _analyze_loudness(audio_path, window_sec)
    if not rms_values:
        return None

    total_windows = len(rms_values)
    target_windows = max(1, int(target_duration / window_sec))

    # 需要的窗口数超过总窗口数：从头开始（没法选段）
    if target_windows >= total_windows:
        return 0.0

    # 滑动窗口找平均 RMS 最高的连续片段
    # 但跳过前 10%（intro）和后 10%（outro），副歌通常在中间
    skip_front = max(1, int(total_windows * 0.1))
    skip_back = max(1, int(total_windows * 0.1))
    search_start = skip_front
    search_end = total_windows - target_windows - skip_back

    if search_end <= search_start:
        # 可搜索范围太小，直接用 20% 启发式
        return None

    best_start = search_start
    best_avg_rms = -999.0

    # 计算初始窗口和
    current_sum = sum(rms_values[search_start : search_start + target_windows])

    for i in range(search_start, search_end + 1):
        avg_rms = current_sum / target_windows
        if avg_rms > best_avg_rms:
            best_avg_rms = avg_rms
            best_start = i

        # 滑动到下一个窗口
        if i + target_windows < total_windows:
            current_sum += rms_values[i + target_windows] - rms_values[i]

    # 转换回秒数
    start_sec = best_start * window_sec
    return start_sec


def _detect_beats(
    audio_path: Path,
    window_sec: float = 0.02,
    threshold_ratio: float = 1.35,
) -> list[float]:
    """
    改进版节拍检测（基于能量突变 onset detection）

    改进点：
    - 更小的窗口（20ms），更高时间分辨率
    - 结合能量差（差分）检测，比单纯比例更准确
    - 自适应阈值：基于局部统计的动态阈值
    - 最小间隔约束，防止误检

    Args:
        audio_path: 音频文件路径
        window_sec: 分析窗口大小（秒），默认 20ms
        threshold_ratio: 能量突变阈值（相对于局部均值的倍数）

    Returns:
        节拍时间点列表（秒），按时间排序
    """
    rms_values = _analyze_loudness(audio_path, window_sec)
    if not rms_values:
        return []

    import math

    # 转换为线性振幅
    linear_energy = [10 ** (rms / 20.0) for rms in rms_values]

    # 计算能量差分（onset strength）
    n = len(linear_energy)
    onset_strength = [0.0] * n
    for i in range(1, n):
        diff = linear_energy[i] - linear_energy[i - 1]
        onset_strength[i] = max(0.0, diff)

    # 滑动窗口计算局部均值和标准差
    beats = []
    local_window = 40  # 40 个窗口 = 0.8 秒（20ms 窗口）
    min_beats_interval = 0.15  # 最小节拍间隔 150ms（最快 400 BPM）

    for i in range(local_window, n - 1):
        # 局部平均 onset strength
        local_onset = onset_strength[i - local_window:i]
        local_avg = sum(local_onset) / local_window
        if local_avg <= 0:
            continue

        # 局部标准差
        local_var = sum((x - local_avg) ** 2 for x in local_onset) / local_window
        local_std = math.sqrt(local_var)

        # 动态阈值：均值 + threshold_ratio * 标准差
        threshold = local_avg + threshold_ratio * local_std

        # 当前 onset 超过阈值，且比左右邻居都大（局部峰值）
        if (onset_strength[i] > threshold
                and onset_strength[i] >= onset_strength[i - 1]
                and onset_strength[i] >= onset_strength[i + 1]):
            beat_time = i * window_sec
            if not beats or beat_time - beats[-1] > min_beats_interval:
                beats.append(beat_time)

    # P2-13：过滤 hi-hat 误检
    # 将间隔 < 0.25s 的连续节拍组只保留 onset_strength 最强的一个，
    # 消除高帽鼓（hi-hat）细密节拍干扰，确保字幕/音效卡点时间准确。
    if beats:
        clustered = [beats[0]]
        cluster_start_idx = 0  # beats 中当前簇起始索引
        cluster_best_onset = onset_strength[int(beats[0] / window_sec)] if beats else 0.0
        cluster_best_time = beats[0]

        for b in beats[1:]:
            if b - clustered[-1] < 0.25:
                # 同一簇：保留 onset 最强的节拍
                b_idx = min(int(b / window_sec), n - 1)
                b_onset = onset_strength[b_idx]
                prev_idx = min(int(clustered[-1] / window_sec), n - 1)
                if b_onset > onset_strength[prev_idx]:
                    clustered[-1] = b
            else:
                clustered.append(b)

        beats = clustered

    return beats


def _estimate_bpm(beats: list[float]) -> float:
    """
    根据节拍点估算 BPM（每分钟节拍数）

    Args:
        beats: 节拍时间点列表

    Returns:
        BPM 值，节拍太少返回 0
    """
    if len(beats) < 4:
        return 0.0

    # 计算相邻节拍的间隔，取中位数（比平均更抗干扰）
    intervals = [beats[i + 1] - beats[i] for i in range(len(beats) - 1)]
    intervals.sort()
    median_interval = intervals[len(intervals) // 2]

    if median_interval <= 0:
        return 0.0

    bpm = 60.0 / median_interval
    # 合理范围：60-180 BPM
    if bpm < 60 or bpm > 200:
        # 可能是倍频/半频错误，尝试调整
        if bpm > 200:
            bpm /= 2
        elif bpm < 60:
            bpm *= 2
    return bpm


def _align_to_beat(
    time_point: float,
    beats: list[float],
    max_offset: float = 0.3,
) -> float:
    """
    将时间点对齐到最近的节拍

    Args:
        time_point: 原始时间点
        beats: 节拍时间点列表（已排序）
        max_offset: 最大偏移量（秒），太远就不对齐了

    Returns:
        对齐后的时间点
    """
    if not beats:
        return time_point

    # 二分查找最近的节拍
    import bisect
    idx = bisect.bisect_left(beats, time_point)

    candidates = []
    if idx < len(beats):
        candidates.append(beats[idx])
    if idx > 0:
        candidates.append(beats[idx - 1])

    if not candidates:
        return time_point

    nearest = min(candidates, key=lambda t: abs(t - time_point))
    offset = abs(nearest - time_point)

    if offset <= max_offset:
        return nearest
    return time_point


def align_subtitles_to_beats(
    subtitles: list,
    bgm_path: Path,
    max_offset: float = 0.25,
) -> list:
    """
    将字幕出现时间对齐到 BGM 的节拍

    Args:
        subtitles: 字幕列表，每个元素包含 text/start/end
        bgm_path: BGM 文件路径（用于检测节拍）
        max_offset: 最大偏移量（秒），太远就不对齐

    Returns:
        对齐后的字幕列表
    """
    if not subtitles or not bgm_path or not bgm_path.exists():
        return subtitles

    beats = _detect_beats(bgm_path)
    if not beats:
        return subtitles

    # O4 修复：BPM < 80 时跳过对齐——轻音乐/钢琴曲每个音符都被检测为节拍，
    # 对齐后字幕会高频小幅抖动，反而比不对齐更差
    bpm = _estimate_bpm(beats)
    if bpm > 0 and bpm < 80:
        return subtitles

    aligned = []
    for sub in subtitles:
        orig_start = sub["start"]
        orig_end = sub["end"]
        orig_duration = orig_end - orig_start
        # Q5 修复：只对齐字幕出现时间（start），结束时间按原时长顺延
        # 人眼对字幕消失时机不敏感，对齐 end 反而可能缩短有效展示时长
        new_start = _align_to_beat(orig_start, beats, max_offset)
        new_end = new_start + orig_duration
        aligned.append({**sub, "start": new_start, "end": new_end})

    return aligned


def align_sfx_to_beats(
    sfx_list: list,
    bgm_path: Path,
    max_offset: float = 0.2,
) -> list:
    """
    将音效时间对齐到 BGM 的节拍

    Args:
        sfx_list: 音效列表
        bgm_path: BGM 文件路径
        max_offset: 最大偏移量（秒）

    Returns:
        对齐后的音效列表
    """
    if not sfx_list or not bgm_path or not bgm_path.exists():
        return sfx_list

    beats = _detect_beats(bgm_path)
    if not beats:
        return sfx_list

    aligned = []
    for sfx in sfx_list:
        new_time = (
            float(sfx["time"])
            if sfx.get("locked_to_visual")
            else _align_to_beat(sfx["time"], beats, max_offset)
        )
        aligned.append({
            **sfx,
            "time": new_time,
        })

    return aligned


def _pick_bgm_segment(
    bgm_duration: float,
    video_duration: float,
    bgm_path: Optional[Path] = None,
) -> tuple[float, bool, float]:
    """
    智能选择 BGM 片段的起始点和处理策略

    优先策略：基于响度分析定位副歌（最响的片段）
    回退策略：基于流行音乐结构的启发式（20% 位置）

    Args:
        bgm_duration: BGM 总时长（秒）
        video_duration: 视频总时长（秒）
        bgm_path: BGM 文件路径（用于响度分析，可选）

    Returns:
        (start_time, need_loop, segment_duration)
        - start_time: BGM 起始点（秒）
        - need_loop: 是否需要循环
        - segment_duration: 单次片段时长（秒，循环时用）
    """
    selected = select_bgm_segment(bgm_duration, video_duration, bgm_path)
    return (
        float(selected["start_time"]),
        bool(selected["need_loop"]),
        float(selected["segment_duration"]),
    )


def select_bgm_segment(
    bgm_duration: float,
    video_duration: float,
    bgm_path: Optional[Path] = None,
    music_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Select an auditable music window from actual audio and the AV contract."""
    if bgm_duration <= 0:
        return {
            "start_time": 0.0,
            "need_loop": False,
            "segment_duration": 0.0,
            "strategy": "empty_audio",
            "average_loudness_db": None,
        }

    ratio = bgm_duration / max(video_duration, 0.1)
    material_driven = str((music_contract or {}).get("source") or "") in {
        "local_asset_analysis",
        "selected_local_assets",
    }

    if ratio > 1.2 and material_driven and bgm_path:
        loudness = _analyze_loudness(bgm_path, 1.0)
        window_size = max(1, min(len(loudness), math.ceil(video_duration)))
        windows = [
            {
                "start_time": float(start),
                "average_loudness_db": sum(loudness[start:start + window_size]) / window_size,
                "loudness_range_db": max(loudness[start:start + window_size]) - min(loudness[start:start + window_size]),
            }
            for start in range(0, max(1, len(loudness) - window_size + 1))
            if start + video_duration <= bgm_duration + 0.05
        ]
        if windows:
            energy = str((music_contract or {}).get("energy") or "medium")
            quantile = {"low": 0.25, "medium": 0.50, "high": 1.0}.get(energy, 0.50)
            ordered_levels = sorted(item["average_loudness_db"] for item in windows)
            target_index = round((len(ordered_levels) - 1) * quantile)
            target_level = ordered_levels[target_index]
            selected = min(
                windows,
                key=lambda item: (
                    abs(item["average_loudness_db"] - target_level),
                    item["loudness_range_db"],
                    item["start_time"],
                ),
            )
            start_time = selected["start_time"]
            beats = _detect_beats(bgm_path)
            if beats:
                start_time = _align_to_beat(start_time, beats, max_offset=0.5)
            return {
                "start_time": round(start_time, 3),
                "need_loop": False,
                "segment_duration": video_duration,
                "strategy": "contract_energy_window",
                "average_loudness_db": round(selected["average_loudness_db"], 3),
                "loudness_range_db": round(selected["loudness_range_db"], 3),
                "contract_energy": energy,
            }

    # 1. BGM 比视频长很多：选段使用
    if ratio > 1.2:
        # 优先用响度分析找副歌
        if bgm_path and bgm_path.exists():
            chorus_start = _find_chorus_segment(bgm_path, video_duration)
            if chorus_start is not None and chorus_start + video_duration <= bgm_duration:
                # P0-2：将副歌起始点对齐到最近节拍，确保从强拍开始，消除节奏断裂感
                beats = _detect_beats(bgm_path)
                if beats:
                    chorus_start = _align_to_beat(chorus_start, beats, max_offset=0.5)
                print(f"  🎯 副歌定位：{chorus_start:.1f}s 处（已对齐节拍）")
                return {
                    "start_time": chorus_start,
                    "need_loop": False,
                    "segment_duration": video_duration,
                    "strategy": "loudest_chorus",
                    "average_loudness_db": None,
                }

        # 回退：从 20% 处开始（大多数流行音乐 intro 占 10-15%，20% 处进入主歌/副歌）
        start_time = bgm_duration * 0.2
        if start_time + video_duration > bgm_duration:
            start_time = max(bgm_duration - video_duration, 0)
        return {
            "start_time": start_time,
            "need_loop": False,
            "segment_duration": video_duration,
            "strategy": "structural_main_section",
            "average_loudness_db": None,
        }

    # 2. BGM 比视频略长或差不多：整首使用
    if ratio > 1.0:
        return {
            "start_time": 0.0,
            "need_loop": False,
            "segment_duration": bgm_duration,
            "strategy": "full_track",
            "average_loudness_db": None,
        }

    # 3. BGM 比视频短：循环使用
    # 优先用响度分析找最燃的循环起点（跳过 intro 的最响点）
    loop_start = 0.0
    if bgm_path and bgm_path.exists():
        # 找一个最响的点作为循环起点（但至少从 5% 开始，跳过纯 intro）
        rms_values = _analyze_loudness(bgm_path, 1.0)
        if rms_values:
            skip_front = max(1, int(len(rms_values) * 0.05))
            if skip_front < len(rms_values):
                # 找前半段响度最高的点作为循环起点
                mid = len(rms_values) // 2
                search_range = rms_values[skip_front:mid]
                if search_range:
                    best_idx = search_range.index(max(search_range)) + skip_front
                    loop_start = best_idx * 1.0

    # 回退：从 10% 处开始（跳过 intro 的空白/渐入）
    if loop_start <= 0:
        loop_start = bgm_duration * 0.1

    loop_duration = bgm_duration - loop_start

    # P1 修复：BPM 校验——确认所选循环段节拍是否合理
    if bgm_path and bgm_path.exists():
        _warn_bgm_bpm(bgm_path, loop_start, loop_duration)

    return {
        "start_time": loop_start,
        "need_loop": True,
        "segment_duration": loop_duration,
        "strategy": "energy_loop",
        "average_loudness_db": None,
    }


def _warn_bgm_bpm(
    bgm_path: Path,
    start_sec: float,
    segment_duration: float,
    target_min: float = 90.0,
    target_max: float = 160.0,
) -> None:
    """
    P1 修复：BGM BPM 校验警告

    抖音广告主流节奏范围：90-160 BPM
    - 快节奏（种草/展示类）：120-140 BPM
    - 中节奏（故事类）：100-120 BPM
    - 慢节奏（情感类）：80-100 BPM

    检测所选片段的 BPM，若偏出合理范围则打印警告，
    帮助制作方决定是否更换 BGM。

    Args:
        bgm_path: BGM 文件路径
        start_sec: 所选片段起始点（秒）
        segment_duration: 所选片段时长（秒）
        target_min: 目标 BPM 下限
        target_max: 目标 BPM 上限
    """
    try:
        beats = _detect_beats(bgm_path)
        if not beats:
            return

        # 只取所选片段范围内的节拍
        end_sec = start_sec + segment_duration
        segment_beats = [b for b in beats if start_sec <= b <= end_sec]

        if len(segment_beats) < 4:
            # 节拍太少，无法可靠估算
            return

        # 用中位数间隔计算 BPM（抗干扰）
        intervals = sorted([
            segment_beats[i + 1] - segment_beats[i]
            for i in range(len(segment_beats) - 1)
        ])
        median_interval = intervals[len(intervals) // 2]
        if median_interval <= 0:
            return

        bpm = 60.0 / median_interval
        # 倍频修正
        if bpm > 200:
            bpm /= 2
        elif bpm < 45:
            bpm *= 2

        if bpm < target_min:
            print(
                f"  ⚠️  BGM BPM 偏低（{bpm:.0f} BPM < {target_min:.0f}）"
                f"——节奏可能偏慢，建议换用 {target_min:.0f}-{target_max:.0f} BPM 的 BGM"
            )
        elif bpm > target_max:
            print(
                f"  ⚠️  BGM BPM 偏高（{bpm:.0f} BPM > {target_max:.0f}）"
                f"——节奏可能过快，建议换用 {target_min:.0f}-{target_max:.0f} BPM 的 BGM"
            )
        else:
            print(f"  ✅  BGM BPM 检测：{bpm:.0f} BPM（节奏匹配）")
    except Exception:
        pass  # BPM 检测失败不阻断主流程


def _build_volume_envelope(
    video_duration: float,
    base_volume: float = BGM_VOLUME,
    num_segments: int = 5,
    key_times: Optional[List[float]] = None,
) -> str:
    """
    构建叙事弧光音量包络（五段式）

    广告视频五段式结构对应的 BGM 音量曲线：
    - 第1段（Hook）：0.6 → 0.85（渐强，抓注意力但不炸）
    - 第2段（转折）：0.85 → 1.0（继续往上推）
    - 第3段（展示/高潮）：1.0（满音量）
    - 第4段（结果）：0.95（保持高位）
    - 第5段（CTA）：0.95 → 0.5（收尾渐弱）

    所有音量值相对于 base_volume 做缩放。

    Args:
        video_duration: 视频总时长（秒）
        base_volume: 基础音量（0-1），即 1.0 相对值对应的实际音量
        num_segments: 段落数，默认 5 段
        key_times: Q4 优化：节奏模板的实际段落边界时间点列表（秒）。
            提供时直接用这些时间点替代均分估算，消除不等长段落时包络时间轴偏移。
            None 时退化到按 num_segments 均分（向后兼容）。

    Returns:
        FFmpeg volume 滤镜表达式（eval 模式）
    """
    # Q4 修复：优先使用节奏模板的精确时间点，而非均分
    if key_times and len(key_times) >= 2:
        # key_times 是各段起始时间（段边界），用于定位音量包络的关键点
        # 关键时间点数量决定使用几段式包络
        n = len(key_times)
        # 归一化的音量曲线：渐强 → 高潮 → 渐弱
        _vol_curve = [0.60, 0.85, 1.00, 1.00, 0.95, 0.50]
        # 从归一化曲线中均匀采样 n+1 个值（首段到末尾+1）
        import math as _math
        sampled_vols = []
        for i in range(n + 1):
            idx = i / max(n, 1) * (len(_vol_curve) - 1)
            lo = int(idx)
            hi = min(lo + 1, len(_vol_curve) - 1)
            frac = idx - lo
            sampled_vols.append(_vol_curve[lo] * (1 - frac) + _vol_curve[hi] * frac)
        key_points = list(zip(key_times + [video_duration], sampled_vols))
    else:
        seg_dur = video_duration / max(num_segments, 1)
        # O4 修复：按实际段数动态生成关键点，避免片段失败时包络时间轴错位
        # (时间点, 相对音量 0-1)
        if num_segments >= 5:
            key_points = [
                (0.0,              0.60),
                (seg_dur * 1,      0.85),
                (seg_dur * 2,      1.00),
                (seg_dur * 3,      1.00),
                (seg_dur * 4,      0.95),
                (video_duration,   0.50),
            ]
        elif num_segments == 4:
            key_points = [
                (0.0,              0.65),
                (seg_dur * 1,      0.90),
                (seg_dur * 2,      1.00),
                (seg_dur * 3,      0.95),
                (video_duration,   0.50),
            ]
        elif num_segments == 3:
            key_points = [
                (0.0,              0.70),
                (seg_dur * 1,      1.00),
                (seg_dur * 2,      0.95),
                (video_duration,   0.50),
            ]
        else:
            # 1-2 段：简单渐弱
            key_points = [
                (0.0,            0.80),
                (video_duration, 0.60),
            ]

    # 构建 FFmpeg volume 表达式（eval 模式）
    # 在每两个相邻关键点之间做线性插值
    # 表达式格式：if(cond, true_val, if(cond2, true_val2, ...))
    # 线性插值：v1 + (v2 - v1) * (t - t1) / (t2 - t1)

    def _lerp_expr(t1, v1, t2, v2):
        """两点之间的线性插值表达式"""
        if abs(t2 - t1) < 0.001:
            return f"{v1 * base_volume:.4f}"
        slope = (v2 - v1) / (t2 - t1)
        return f"{v1 * base_volume:.4f} + {slope * base_volume:.6f} * (t - {t1:.3f})"

    # 从后往前构建 if 嵌套
    expr = f"{key_points[-1][1] * base_volume:.4f}"
    for i in range(len(key_points) - 2, -1, -1):
        t1, v1 = key_points[i]
        t2, v2 = key_points[i + 1]
        lerp = _lerp_expr(t1, v1, t2, v2)
        expr = f"if(lt(t,{t2:.3f}), {lerp}, {expr})"

    return f"volume=eval=frame:volume='{expr}'"


def _build_bgm_audio_filter(
    video_duration: float,
    bgm_path: Optional[Path] = None,
    volume: float = BGM_VOLUME,
    fade_in: float = BGM_FADE_IN,
    fade_out: float = BGM_FADE_OUT,
    use_envelope: bool = True,
    input_label: str = "[1:a]",
    num_clips: int = 5,
    envelope_key_times: Optional[List[float]] = None,
    music_contract: Optional[Dict[str, Any]] = None,
    bgm_segment: Optional[Dict[str, Any]] = None,
) -> str:
    # Bug #4 修复：新增 input_label 参数，调用方直接传入正确的输入流标签，
    # 消除之前用 string replace 修改 [1:a] 的脆弱做法（多处引用时会漏替换）
    """
    构建 BGM 音频处理滤镜链（智能选段 + 音量包络 + 淡入淡出）

    智能选段策略（优先响度分析定位副歌，失败则启发式）：
    - BGM 比视频长很多：定位副歌片段，从副歌开始取
    - BGM 比视频略长：整首使用
    - BGM 比视频短：从最响处开始循环

    音量包络：五段式叙事弧光（Hook渐强→高潮→CTA渐弱）

    Args:
        video_duration: 视频总时长（秒）
        bgm_path: BGM 文件路径（用于响度分析，可选）
        volume: BGM 基础音量（0-1），音量包络在此基础上缩放
        fade_in: 开头淡入时长（秒）
        fade_out: 结尾淡出时长（秒）
        use_envelope: 是否启用叙事弧光音量包络

    Returns:
        FFmpeg filter_complex 片段（输入为 [1:a]，输出为 [bgm]）
    """
    # 如果有 BGM 文件路径，做智能选段；否则 fallback 到简单循环
    bgm_duration = _get_audio_duration(bgm_path) if bgm_path else 0.0

    if bgm_duration > 0:
        selected_segment = bgm_segment or select_bgm_segment(
            bgm_duration,
            video_duration,
            bgm_path,
            music_contract=music_contract,
        )
        start_time = float(selected_segment["start_time"])
        need_loop = bool(selected_segment["need_loop"])
        segment_duration = float(selected_segment["segment_duration"])
    else:
        start_time, need_loop, segment_duration = 0.0, True, 0.0

    filter_parts = []

    # 第一步：裁切起始点（跳过 intro）
    if start_time > 0:
        filter_parts.append(f"atrim=start={start_time}")
        filter_parts.append("asetpts=PTS-STARTPTS")
        # P2-14：chorus trim 后必须补 afade 入点，防止从副歌直接硬切进入有断拍感
        filter_parts.append(f"afade=t=in:st=0:d={fade_in}")

    # 第二步：循环（如果需要）
    if need_loop:
        # B2 修复：aloop 生成超长流后，串联多个 afade 仅第一个接缝有效（后续 afade 的 st
        # 相对于已被前一个 afade 修改的流，PTS 不会重置，st 超出流头后被静默忽略）。
        # 改为：aloop 生成足够长的流，再用单个 aecho 模拟接缝淡化，或更简单地用
        # areverse+concat 方案。最稳健的无 click 方案：对每个副本独立输入再 concat+acrossfade。
        # 实用折衷（保持单 filter_complex）：aloop 后仅做整体 afade in/out，
        # 接缝 click 声用 aresample（SoX highpass=0 去直流）+ alimiter 消除。
        if segment_duration > 0 and video_duration > 0:
            loop_count = int(video_duration / segment_duration) + 2
            # 用 aloop 生成足够长的流，然后用 asplit 做每个接缝点的 volume 软拐角消除 click
            # 最简有效：在 aloop 后加 aresample 去直流分量（click 声本质是直流跳变），
            # 再加 alimiter 防止极端接缝导致的瞬间削波
            loop_filter = (
                f"aloop=loop={loop_count}:size=2e9,"
                f"aresample=async=1:first_pts=0,"  # 重采样消除接缝处的 PTS 不连续
                f"alimiter=limit=0.95:attack=1:release=5"  # 限幅消除接缝 click 尖峰
            )
            filter_parts.append(loop_filter)
        else:
            filter_parts.append("aloop=loop=-1:size=2e9")


    # 第三步：叙事弧光音量包络（Q4：优先用节奏模板精确时间点，防止不等长段包络偏移）
    if use_envelope and video_duration > 0:
        envelope = _build_volume_envelope(
            video_duration, volume, num_clips,
            key_times=envelope_key_times,
        )
        filter_parts.append(envelope)
    else:
        # 简单的固定音量
        filter_parts.append(f"volume={volume}")

    # 第四步：整体淡出（淡入已在 trim 后直接处理，这里只加淡出）
    # P2-14：如果已在 trim 后加了淡入，避免重复叠加两层淡入导致音量双重衰减
    fade_out_start = max(video_duration - fade_out, 0)
    if start_time <= 0:
        # 没有 trim（从头开始），正常加淡入
        filter_parts.append(f"afade=t=in:st=0:d={fade_in}")
    filter_parts.append(f"afade=t=out:st={fade_out_start}:d={fade_out}:curve=qsin")

    # 第五步：精确裁切到视频时长
    filter_parts.append(f"atrim=duration={video_duration}")
    filter_parts.append("asetpts=PTS-STARTPTS")

    return f"{input_label}{','.join(filter_parts)}[bgm]"


def add_bgm_ffmpeg(
    video: Path,
    bgm: Path,
    output: Path,
    volume: float = BGM_VOLUME,
    fade_in: float = BGM_FADE_IN,
    fade_out: float = BGM_FADE_OUT,
) -> Path:
    """
    为视频添加 BGM（智能选段 + 淡入淡出）

    智能选段策略：
    - BGM 只是略长：整首使用
    - BGM 比较长：从 20% 处开始取（跳过 intro，直接进主旋律）
    - BGM 比视频短：从 10% 处开始循环（跳过 intro）

    Args:
        video: 输入视频
        bgm: BGM 文件
        output: 输出路径
        volume: BGM 音量（0-1）
        fade_in: 淡入时长（秒）
        fade_out: 淡出时长（秒）

    Returns:
        输出文件路径
    """
    video_duration = _get_clip_duration(video)
    bgm_filter = _build_bgm_audio_filter(video_duration, bgm, volume, fade_in, fade_out)
    has_audio = _has_audio_stream(video)

    # P1 #4：无音轨时直接输出 BGM，避免 [0:a] 引用不存在的流
    if has_audio:
        audio_filter = f"{bgm_filter};[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]"
    else:
        audio_filter = f"{bgm_filter};[bgm]anull[outa]"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video),
        "-i", str(bgm),
        "-filter_complex", audio_filter,
        "-map", "0:v",
        "-map", "[outa]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]

    run_ffmpeg(cmd, timeout=300)
    return output


def convert_to_aspect_ratio(
    input_video: Path,
    output: Path,
    target_aspect_ratio: str = "16:9",
) -> Path:
    """
    转换视频画幅比例

    Args:
        input_video: 输入视频
        output: 输出路径
        target_aspect_ratio: 目标画幅比例（如 16:9、1:1）

    Returns:
        输出文件路径
    """
    # 解析目标比例
    w_ratio, h_ratio = map(int, target_aspect_ratio.split(":"))
    target_w = 1920
    target_h = int(target_w * h_ratio / w_ratio)

    # 对于 9:16 -> 16:9 的转换：
    # 先 scale 到目标宽度，高度自动按比例
    # 然后从中间裁切出目标高度
    # 这样可以保留画面中心的内容

    # 如果是 9:16 转 16:9，需要裁切上下部分
    # 如果是 16:9 转 9:16，需要裁切左右部分
    # 如果是 1:1，需要裁切成正方形

    if target_aspect_ratio == "16:9":
        # 9:16 (1080x1920) -> 16:9 (1920x1080)
        # scale 到宽度 1920，然后裁切中间 1080 高度
        vf_filter = (
            "scale=1920:-1,"
            "crop=1920:1080:(iw-1920)/2:(ih-1080)/2"
        )
    elif target_aspect_ratio == "1:1":
        # 裁切为正方形，从中间裁切
        vf_filter = (
            "scale=1080:-1,"
            "crop=1080:1080:(iw-1080)/2:(ih-1080)/2"
        )
    else:
        # 通用转换：scale 到目标宽度，然后裁切
        vf_filter = (
            f"scale={target_w}:-1,"
            f"crop={target_w}:{target_h}:(iw-{target_w})/2:(ih-{target_h})/2"
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_video),
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",
        *_video_vbv_args(),
        "-r", str(OUTPUT_FPS),
        "-pix_fmt", "yuv420p", *_color_range_args(),
    ]
    if _has_audio_stream(input_video):
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.append("-an")
    cmd.append(str(output))

    run_ffmpeg(cmd, timeout=300)
    return output


# ============================================================
# 音效设计（SFX）
# ============================================================
# 用 FFmpeg 合成音效，无需外部资源

def _generate_whoosh(duration: float = 0.5, output_path: Path = None) -> Path:
    """
    生成转场 whoosh 音效（嗖的一声）

    白噪声 → 带通滤波器（频率从低到高扫频）→ 音量包络（淡入淡出）

    Args:
        duration: 音效时长（秒）
        output_path: 输出路径（可选）

    Returns:
        音效文件路径
    """
    if output_path is None:
        from config import PROJECT_ROOT, SFX_CACHE_DIR
        output_dir = PROJECT_ROOT / SFX_CACHE_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"whoosh_{duration}s.wav"

    if output_path.exists():
        return output_path

    # 粉噪声 + 带通 + 淡入淡出包络
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=d={duration}:c=pink:r=44100:a=0.5",
        "-filter_complex", (
            f"[0:a]"
            f"bandpass=f=800:width_type=h:width=2000,"
            f"afade=t=in:st=0:d={duration*0.3},"
            f"afade=t=out:st={duration*0.5}:d={duration*0.5}"
            f"[out]"
        ),
        "-map", "[out]",
        str(output_path),
    ]

    try:
        run_ffmpeg(cmd, timeout=30)
        return output_path
    except Exception:
        return None


def _generate_ding(duration: float = 0.3, freq: float = 1200, output_path: Path = None) -> Path:
    """
    生成强调 ding 音效（叮咚/叮的一声）

    正弦波 + 指数衰减 → 清脆的提示音

    Args:
        duration: 音效时长（秒）
        freq: 频率（Hz），越高越尖锐
        output_path: 输出路径（可选）

    Returns:
        音效文件路径
    """
    if output_path is None:
        from config import PROJECT_ROOT, SFX_CACHE_DIR
        output_dir = PROJECT_ROOT / SFX_CACHE_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"ding_{int(freq)}Hz_{duration}s.wav"

    if output_path.exists():
        return output_path

    # 正弦波 + 快速淡出 = 叮咚声
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-filter_complex", (
            f"[0:a]"
            f"afade=t=out:st=0:d={duration}"  # 从一开始就淡出，形成指数衰减效果
            f"[out]"
        ),
        "-map", "[out]",
        str(output_path),
    ]

    try:
        run_ffmpeg(cmd, timeout=30)
        return output_path
    except Exception:
        return None


def _generate_impact(duration: float = 0.4, output_path: Path = None) -> Path:
    """
    生成冲击/转场重击音效（咚的一声）

    低频正弦波 + 快速衰减 → 有力量感的转场音

    Args:
        duration: 音效时长（秒）
        output_path: 输出路径（可选）

    Returns:
        音效文件路径
    """
    if output_path is None:
        from config import PROJECT_ROOT, SFX_CACHE_DIR
        output_dir = PROJECT_ROOT / SFX_CACHE_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"impact_{duration}s.wav"

    if output_path.exists():
        return output_path

    # 低频 + 快速淡出 = 重击感
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=80:duration={duration}",
        "-filter_complex", (
            f"[0:a]"
            f"lowpass=f=200,"
            f"volume=2,"
            f"afade=t=out:st=0:d={duration}"
            f"[out]"
        ),
        "-map", "[out]",
        str(output_path),
    ]

    try:
        run_ffmpeg(cmd, timeout=30)
        return output_path
    except Exception:
        return None

def add_brand_intro_outro(
    video: Path,
    output: Path,
    brand_name: str = "",
    product_name: str = "",
    cta_text: str = "立即体验",
    primary_color: str = "#FF6B6B",
    intro_duration: float = 2.0,
    outro_duration: float = 1.5,
    main_duration: Optional[float] = None,
    outro_audio: Optional[Path] = None,
    outro_bgm: Optional[Path] = None,
    outro_background_video: Optional[Path] = None,
    strict_material_background: bool = False,
    resolution: str = "1080x1920",
    fps: int = 30,
) -> Path:
    """
    P2-A：在视频首尾添加品牌开场/收尾动画。

    开场（intro_duration）：品牌色全屏背景 + 品牌名淡入
    收尾（outro_duration）：品牌色背景 + 产品名 + CTA 文字

    使用 ffmpeg lavfi + drawtext + concat 实现，无额外依赖。
    失败时静默返回原视频，不影响主流程。

    Args:
        video: 主视频路径
        output: 输出路径
        brand_name: 品牌名称
        product_name: 产品名称
        cta_text: CTA 文字（如「立即体验」）
        primary_color: 品牌主色（HEX），转换为 RGB 用于 ffmpeg
        intro_duration: 开场时长（秒）
        outro_duration: 收尾时长（秒）
        main_duration: 主片进入尾卡前的保留时长；None 表示保留完整主片
        resolution: 分辨率（如 1080x1920）
        fps: 帧率

    Returns:
        输出文件路径（失败时返回原 video 路径）
    """
    import tempfile as _bio_tmp
    import shutil as _bio_sh

    # 解析 HEX 颜色为 ffmpeg 格式（0xRRGGBB）
    def _hex_to_ffmpeg_color(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        if len(h) == 6:
            return f"0x{h.upper()}"
        return "0xFF6B6B"

    _bg_color = _hex_to_ffmpeg_color(primary_color)
    _w, _h = resolution.split("x")
    _font_size_lg = int(int(_w) * 0.09)
    _font_size_sm = int(int(_w) * 0.05)
    _font_path = _get_font_path("bold")
    _font_arg = f":fontfile={_ffmpeg_escape_filter_path(_font_path)}" if _font_path else ""
    _brand_text = _ffmpeg_escape_drawtext_text(brand_name)
    _outro_text_raw = build_tail_card_display_text(product_name, cta_text)
    _outro_text = _ffmpeg_escape_drawtext_text(_outro_text_raw)

    has_audio = _has_audio_stream(video)

    try:
        with _bio_tmp.TemporaryDirectory(prefix="brand_io_") as _bio_dir:
            _bio_dir = Path(_bio_dir)

            _intro_path = None
            if intro_duration > 0:
                _intro_path = _bio_dir / "intro.mp4"
                _intro_vf = (
                    f"color=c={_bg_color}:size={resolution}:rate={fps}:d={intro_duration},"
                    f"drawtext=text='{_brand_text}'{_font_arg}:fontsize={_font_size_lg}:fontcolor=white"
                    f":x=(w-text_w)/2:y=(h-text_h)/2"
                    f":alpha='if(lt(t,0.5),t/0.5,if(gt(t,{intro_duration-0.5}),({intro_duration}-t)/0.5,1))'"
                )
                subprocess.run([
                    "ffmpeg", "-y", "-f", "lavfi", "-i", _intro_vf,
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", *_video_vbv_args(),
                    "-pix_fmt", "yuv420p", *_color_range_args(), "-r", str(fps),
                    "-c:a", "aac", "-b:a", "128k", "-t", str(intro_duration), str(_intro_path),
                ], capture_output=True, timeout=30, check=True)

            # ── 收尾片段 ──
            _outro_path = _bio_dir / "outro.mp4"
            _outro_background = None
            if outro_background_video and Path(outro_background_video).is_file():
                _outro_background = _bio_dir / "outro_background.jpg"
                _background_result = subprocess.run([
                    "ffmpeg", "-y", "-sseof", "-0.08", "-i", str(outro_background_video),
                    "-frames:v", "1", "-q:v", "2", str(_outro_background),
                ], capture_output=True, timeout=30)
                if _background_result.returncode != 0 or not _outro_background.exists():
                    if strict_material_background:
                        raise RuntimeError("无法从本地素材主片提取 CTA 尾卡背景")
                    _outro_background = None

            _drawtext = (
                f"drawtext=text='{_outro_text}'{_font_arg}:fontsize={_font_size_sm}:fontcolor=white"
                f":x=(w-text_w)/2:y=(h-text_h)/2"
                f":alpha='if(lt(t,0.3),t/0.3,if(gt(t,{outro_duration-0.3}),({outro_duration}-t)/0.3,1))'"
            )
            if _outro_background:
                _outro_video_input = ["-loop", "1", "-i", str(_outro_background)]
                _outro_video_filter = [
                    "-vf",
                    f"scale={_w}:{_h}:force_original_aspect_ratio=increase,"
                    f"crop={_w}:{_h},drawbox=x=0:y=0:w=iw:h=ih:color=black@0.38:t=fill,{_drawtext}",
                ]
            else:
                _outro_vf = f"color=c={_bg_color}:size={resolution}:rate={fps}:d={outro_duration},{_drawtext}"
                _outro_video_input = ["-f", "lavfi", "-i", _outro_vf]
                _outro_video_filter = []
            _has_outro_voice = bool(outro_audio and Path(outro_audio).exists())
            _has_outro_bgm = bool(outro_bgm and Path(outro_bgm).exists())
            _outro_audio_inputs = []
            if _has_outro_voice:
                _outro_audio_inputs += ["-i", str(outro_audio)]
            if _has_outro_bgm:
                _outro_audio_inputs += ["-stream_loop", "-1", "-i", str(outro_bgm)]
            if not _has_outro_voice and not _has_outro_bgm:
                _outro_audio_inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            _audio_filters = []
            _audio_labels = []
            _audio_index = 1
            if _has_outro_voice:
                _audio_filters.append(f"[{_audio_index}:a]apad=whole_dur={outro_duration}[voice]")
                _audio_labels.append("[voice]")
                _audio_index += 1
            if _has_outro_bgm:
                _audio_filters.append(
                    f"[{_audio_index}:a]atrim=0:{outro_duration},asetpts=PTS-STARTPTS,volume=0.18[bed]"
                )
                _audio_labels.append("[bed]")
            if len(_audio_labels) > 1:
                _audio_filters.append(
                    f"{''.join(_audio_labels)}amix=inputs={len(_audio_labels)}:duration=longest:normalize=0[aout]"
                )
                _audio_map = ["-filter_complex", ";".join(_audio_filters), "-map", "0:v", "-map", "[aout]"]
            elif _audio_filters:
                label = "voice" if _has_outro_voice else "bed"
                _audio_map = ["-filter_complex", ";".join(_audio_filters), "-map", "0:v", "-map", f"[{label}]"]
            else:
                _audio_map = []
            _outro_cmd = [
                "ffmpeg", "-y",
                *_outro_video_input,
                *_outro_audio_inputs,
                *_audio_map,
                *_outro_video_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                *_video_vbv_args(),
                "-pix_fmt", "yuv420p", *_color_range_args(), "-r", str(fps),
                "-c:a", "aac", "-b:a", "128k",
                "-t", str(outro_duration),
                str(_outro_path),
            ]
            _outro_result = subprocess.run(
                _outro_cmd, capture_output=True, text=True, timeout=30,
            )
            if _outro_result.returncode != 0:
                raise RuntimeError(
                    "CTA 尾卡渲染失败：" + (_outro_result.stderr or "unknown ffmpeg error")[-1200:]
                )

            # ── concat 拼接：显式统一音视频流，避免 data stream/采样率差异截断尾卡音频 ──
            _main_path = Path(video)
            if not has_audio:
                _main_path = _bio_dir / "main_with_silence.mp4"
                subprocess.run([
                    "ffmpeg", "-y", "-i", str(video),
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest",
                    str(_main_path),
                ], capture_output=True, timeout=120, check=True)

            _concat_segments = [
                *([_intro_path] if _intro_path else []),
                _main_path,
                _outro_path,
            ]
            _concat_cmd = ["ffmpeg", "-y"]
            for _segment_path in _concat_segments:
                _concat_cmd += ["-i", str(_segment_path)]
            _filter_parts = []
            _concat_labels = []
            _main_index = 1 if _intro_path else 0
            for _index in range(len(_concat_segments)):
                _video_filters = [f"fps={fps}", "format=yuv420p", "setsar=1"]
                _audio_filters = [
                    "aresample=44100:async=1:first_pts=0",
                    "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
                ]
                if _index == _main_index and main_duration and main_duration > 0:
                    _video_filters.insert(0, f"trim=duration={main_duration:.3f}")
                    _audio_filters.insert(0, f"atrim=duration={main_duration:.3f}")
                _filter_parts.append(
                    f"[{_index}:v:0]{','.join(_video_filters)},setpts=PTS-STARTPTS[v{_index}]"
                )
                _filter_parts.append(
                    f"[{_index}:a:0]{','.join(_audio_filters)},asetpts=PTS-STARTPTS[a{_index}]"
                )
                _concat_labels.append(f"[v{_index}][a{_index}]")
            _filter_parts.append(
                f"{''.join(_concat_labels)}concat=n={len(_concat_segments)}:v=1:a=1[vout][aout]"
            )
            _concat_cmd += [
                "-filter_complex", ";".join(_filter_parts),
                "-map", "[vout]", "-map", "[aout]",
                "-dn",
                "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                *_video_vbv_args(),
                "-pix_fmt", "yuv420p", *_color_range_args(),
                "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(output),
            ]
            subprocess.run(_concat_cmd, capture_output=True, timeout=300, check=True)

        output_duration = _get_clip_duration(output) if output.exists() else 0.0
        if output.exists() and output.stat().st_size > 1_000 and output_duration >= outro_duration + 0.4:
            print(f"✅ 品牌开场/收尾动画添加完成（开场 {intro_duration}s + 收尾 {outro_duration}s）")
            return output
        raise RuntimeError("输出文件无效")

    except Exception as _bio_err:
        if strict_material_background:
            raise
        print(f"⚠️  品牌动画添加失败（{_bio_err}），使用原视频")
        _bio_sh.copy2(video, output)
        return video


def add_sfx_to_video(
    video: Path,
    output: Path,
    sfx_list: list = None,
) -> Path:
    """
    为视频添加音效（在指定时间点叠加）

    Args:
        video: 输入视频路径
        output: 输出视频路径
        sfx_list: 音效列表，每个元素为 {"time": 秒, "type": "ding"/"impact"/"whoosh", "volume": 0-1}

    Returns:
        输出视频路径
    """
    if not sfx_list:
        # 没有音效，直接复制
        import shutil
        shutil.copy2(video, output)
        return output

    video_duration = _get_clip_duration(video)

    # 生成所有需要的音效文件
    sfx_files = []
    for sfx in sfx_list:
        sfx_type = sfx.get("type", "ding")
        volume = sfx.get("volume", 0.5)
        time = sfx.get("time", 0)

        if time > video_duration:
            continue

        if sfx_type == "ding":
            sfx_file = _generate_ding()
        elif sfx_type == "impact":
            sfx_file = _generate_impact()
        elif sfx_type == "whoosh":
            sfx_file = _generate_whoosh()
        else:
            continue

        if sfx_file and sfx_file.exists():
            sfx_files.append((time, sfx_file, volume))

    if not sfx_files:
        import shutil
        shutil.copy2(video, output)
        return output

    # 构建 FFmpeg 命令：视频 + 多个音效输入，用 adelay 定位 + amix 混合
    cmd = ["ffmpeg", "-y", "-i", str(video)]

    # 添加音效输入
    for _, sfx_file, _ in sfx_files:
        cmd.extend(["-i", str(sfx_file)])

    # 构建滤镜链
    filter_parts = []
    mix_labels = []

    # P0 #2 修复：只有视频有音轨时才将原音轨加入 mix，否则 [0:a] 不存在会崩溃
    has_audio = _has_audio_stream(video)
    if has_audio:
        mix_labels.append("[0:a]")

    # 每个音效：延迟到指定时间 + 音量调整
    for i, (time, _, volume) in enumerate(sfx_files):
        input_label = f"[{i+1}:a]"
        # adelay 延迟到指定时间点，然后音量调整
        delay_ms = int(time * 1000)
        filtered = f"[{i+1}sfx]"
        filter_parts.append(
            f"{input_label}adelay={delay_ms}|{delay_ms},volume={volume}{filtered}"
        )
        mix_labels.append(filtered)

    # 混合所有音轨
    # 优化 #11 修复：补充 normalize=0，防止 amix 默认归一化把混音整体音量缩小到 1/N
    mix_inputs = "".join(mix_labels)
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[outa]"
    )

    # 音效叠加只处理音频流，视频流直接 copy 不重编码（节省一次有损编码）
    cmd.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v",
        "-c:v", "copy",
    ])
    if has_audio or mix_labels:  # 有原音轨或有音效时才映射音频输出
        # B1 修复：补充 -ar 44100，与 export_final_video 的 aformat=sample_rates=44100 对齐，
        # 避免后续编码做额外采样率转换引入相位误差
        cmd.extend(["-map", "[outa]", "-c:a", "aac", "-b:a", "192k", "-ar", "44100"])
    cmd.append(str(output))

    run_ffmpeg(cmd, timeout=120)
    return output


def detect_scene_cuts(
    video: Path,
    threshold: float = 0.35,
    max_cuts: int = 20,
) -> List[float]:
    """
    P1-B：基于帧间差分检测视频内场景切换时间点。

    使用 ffmpeg select 滤镜（scene detection）找出帧间差分超过阈值的时刻，
    可用于精确定位音效插入点（whoosh/impact 与画面切换同步）。

    Args:
        video: 输入视频路径
        threshold: 帧间差分阈值（0-1），默认 0.35（中等切换灵敏度）
        max_cuts: 最多返回的切换点数量

    Returns:
        场景切换时间点列表（秒），已排序，去重
    """
    cuts: List[float] = []
    try:
        # ffmpeg select 滤镜：输出满足条件的帧的时间戳
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-vsync", "vfr",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # showinfo 输出在 stderr，解析 pts_time 字段
        import re as _re_sc
        for line in result.stderr.splitlines():
            m = _re_sc.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1))
                cuts.append(t)
        # 去重 + 排序 + 限制数量
        cuts = sorted(set(round(t, 3) for t in cuts))[:max_cuts]
    except Exception:
        pass
    return cuts


def generate_sfx_timings(
    num_clips: int,
    clip_duration: float,
    transition_duration: float = 0.6,
    narratives: Optional[List[str]] = None,
    segment_durations: Optional[List[float]] = None,
    transition_decisions: Optional[List[Dict[str, Any]]] = None,
    segment_timeline: Optional[List[Dict[str, Any]]] = None,
    sfx_intensity: str = "moderate",
) -> list:
    """
    根据分镜结构生成音效时间点

    策略：
    - 每个转场点加一个 whoosh/impact 音效
    - highlight/demo/reveal 叙事段开头加一个 ding 强调
    - cta 叙事段开头加一个 ding 强调

    Args:
        num_clips: 片段数量
        clip_duration: 单片段时长（秒），segment_durations 未提供时使用
        transition_duration: 转场时长（秒）
        narratives: 各段的叙事类型列表（0-based），如 ["hook","pain","highlight","result","cta"]。
            提供时按叙事字段定位强调音效，忽略硬编码段索引；None 时退化到原有位置策略。
        segment_durations: 每段独立时长列表（与片段顺序一致）。
            提供时覆盖 clip_duration，实现节奏模板驱动的非等长时间轴。
            None 时使用均匀 clip_duration（向后兼容）。
        segment_timeline: 实际渲染时间轴；提供后直接使用其 start/duration，
            不再从统一转场时长重新推导。

    Returns:
        音效列表
    """
    sfx_list = []
    intensity_scale = {
        "none": 0.0,
        "minimal": 0.45,
        "subtle": 0.65,
        "moderate": 1.0,
        "strong": 1.15,
    }.get(sfx_intensity, 1.0)
    if intensity_scale <= 0:
        return sfx_list

    # 构建段时长列表
    if segment_durations is not None:
        dur_list = list(segment_durations)
    else:
        dur_list = [float(clip_duration)] * num_clips

    if segment_timeline is not None:
        if len(segment_timeline) != num_clips:
            raise ValueError("实际时间轴段数必须与镜头数一致")
        seg_starts = [float(item["start"]) for item in segment_timeline]
        dur_list = [float(item["duration"]) for item in segment_timeline]
    else:
        # 计算每段起始时间（考虑转场重叠）
        seg_starts = []
        current = 0.0
        for i in range(num_clips):
            if i > 0:
                decision = (
                    transition_decisions[i - 1]
                    if transition_decisions and i - 1 < len(transition_decisions)
                    else None
                )
                raw_duration = decision.get("duration") if decision is not None else None
                current -= float(transition_duration if raw_duration is None else raw_duration)
            seg_starts.append(current)
            current += dur_list[i]

    # 转场音效
    for i in range(num_clips - 1):
        decision = (
            transition_decisions[i]
            if transition_decisions and i < len(transition_decisions)
            else None
        )
        raw_duration = decision.get("duration") if decision is not None else None
        selected_duration = float(transition_duration if raw_duration is None else raw_duration)
        # 转场点：第 i+1 段的开始时间 + 半个转场时长
        transition_time = seg_starts[i + 1] + selected_duration / 2
        transition_time = max(0.0, transition_time)

        if decision is not None:
            transition_type = str(decision.get("type") or "none").lower()
            if transition_type in {"none", "cut", "dissolve", "fade"}:
                continue
            sfx_type = "impact" if transition_type in {"mask", "circleopen", "circleclose"} else "whoosh"
        else:
            sfx_type = "impact" if i % 2 == 0 else "whoosh"
        volume = (0.3 if sfx_type == "impact" else 0.2) * intensity_scale

        sfx_list.append({
            "time": transition_time,
            "type": sfx_type,
            "volume": round(volume, 3),
            "locked_to_visual": True,
        })

    # 问题3修复：覆盖 ad_script.py 中全量 narrative 值
    # 高潮/转折/展示类 → 强冲击 ding(0.4)
    _HIGHLIGHT_NARRATIVES = {
        "highlight", "demo", "reveal", "showcase", "solution",
        "turning",   # pain_point_solution: 转折段
        "before",    # before_after: 对比前
        "after",     # before_after: 对比后（视觉冲击）
        "discover",  # before_after: 发现解决方案
        "process",   # before_after: 过程展示
        "change",    # story: 改变发生
        "discovery", # story: 故事发现
        "conflict",  # story: 冲突（建立张力）
        "intro",     # product_showcase: 产品登场
        "detail",    # product_showcase: 细节展示
        "effect",    # product_showcase: 效果呈现
        "popular",   # social_proof: 爆款事实
    }
    # 结果/验证类 → 轻强调 ding(0.3)
    _RESULT_NARRATIVES = {
        "result",    # pain_point_solution: 成果
        "proof",     # social_proof: 用户佐证
        "reason",    # social_proof: 理由
        "review",    # social_proof: 评价
        "setup",     # story: 铺垫
    }
    # CTA 类 → 行动召唤 ding(0.35)
    _CTA_NARRATIVES = {"cta", "call_to_action", "outro"}

    if narratives is not None:
        for pos, narrative in enumerate(narratives):
            if pos >= len(seg_starts):
                break
            seg_start = seg_starts[pos]
            if narrative in _HIGHLIGHT_NARRATIVES:
                sfx_list.append({
                    "time": max(0.0, seg_start + 0.3),
                    "type": "ding",
                    "volume": round(0.4 * intensity_scale, 3),
                    "locked_to_visual": False,
                })
            elif narrative in _RESULT_NARRATIVES:
                sfx_list.append({
                    "time": max(0.0, seg_start + 0.3),
                    "type": "ding",
                    "volume": round(0.3 * intensity_scale, 3),
                    "locked_to_visual": False,
                })
            elif narrative in _CTA_NARRATIVES:
                sfx_list.append({
                    "time": max(0.0, seg_start + 0.2),
                    "type": "ding",
                    "volume": round(0.35 * intensity_scale, 3),
                    "locked_to_visual": False,
                })
    else:
        # 退化策略：按硬编码段位置（兼容无叙事信息的调用方）
        if num_clips >= 3 and len(seg_starts) > 2:
            highlight_time = seg_starts[2] + 0.3
            sfx_list.append({
                "time": max(0.0, highlight_time),
                "type": "ding",
                "volume": round(0.4 * intensity_scale, 3),
                "locked_to_visual": False,
            })
        if num_clips >= 5 and len(seg_starts) > 4:
            cta_time = seg_starts[4] + 0.2
            sfx_list.append({
                "time": max(0.0, cta_time),
                "type": "ding",
                "volume": round(0.35 * intensity_scale, 3),
                "locked_to_visual": False,
            })

    return sfx_list


def stabilize_video(
    video: Path,
    output: Path,
    smoothing: int = 10,
    deflicker: bool = True,
) -> Path:
    """
    P1-A：视频稳定化 + 去闪烁。

    优先使用 vidstabdetect + vidstabtransform 做稳定（需 ffmpeg 编译了 --enable-libvidstab，
    brew 版本默认携带）；若不可用则降级到仅 deflicker 去闪烁。

    Args:
        video: 输入视频
        output: 输出路径
        smoothing: 稳定平滑窗口（帧数），越大越稳但边缘裁切越多，默认 10
        deflicker: 是否同时做去闪烁处理

    Returns:
        输出文件路径（若稳定化失败返回原视频路径，不抛出异常）
    """
    import tempfile as _stab_tmp

    has_audio = _has_audio_stream(video)

    # ── 先探测 vidstabdetect 是否可用 ──
    _probe = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=0.1", "-vf", "vidstabdetect", "-f", "null", "-"],
        capture_output=True, timeout=10,
    )
    _vidstab_ok = _probe.returncode == 0

    try:
        with _stab_tmp.TemporaryDirectory(prefix="stab_") as _stab_dir:
            _trf_path = Path(_stab_dir) / "transforms.trf"

            if _vidstab_ok:
                # 第一步：检测运动向量
                _detect_cmd = [
                    "ffmpeg", "-y", "-i", str(video),
                    "-vf", f"vidstabdetect=shakiness=5:accuracy=9:result={_trf_path}",
                    "-f", "null", "-",
                ]
                subprocess.run(_detect_cmd, capture_output=True, timeout=120, check=True)

                # 第二步：应用稳定变换 + 可选 deflicker
                _vf_parts = [
                    f"vidstabtransform=smoothing={smoothing}:input={_trf_path}:crop=black",
                    "unsharp=5:5:0.8:3:3:0.4",  # 锐化补偿稳定导致的轻微模糊
                ]
                if deflicker:
                    _vf_parts.append("deflicker=size=5:mode=am")
                _vf_str = ",".join(_vf_parts)

                _transform_cmd = [
                    "ffmpeg", "-y", "-i", str(video),
                    "-vf", _vf_str,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                    "-pix_fmt", "yuv420p", *_color_range_args(),
                ]
                if has_audio:
                    _transform_cmd += ["-c:a", "copy"]
                _transform_cmd.append(str(output))
                subprocess.run(_transform_cmd, capture_output=True, timeout=300, check=True)
                print(f"   ✅ 稳定化完成（vidstab smoothing={smoothing}" + ("+ deflicker" if deflicker else "") + "）")

            elif deflicker:
                # 降级：仅做去闪烁
                _deflicker_cmd = [
                    "ffmpeg", "-y", "-i", str(video),
                    "-vf", "deflicker=size=5:mode=am",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                    "-pix_fmt", "yuv420p", *_color_range_args(),
                ]
                if has_audio:
                    _deflicker_cmd += ["-c:a", "copy"]
                _deflicker_cmd.append(str(output))
                subprocess.run(_deflicker_cmd, capture_output=True, timeout=300, check=True)
                print("   ✅ 去闪烁完成（vidstab 不可用，仅 deflicker）")
            else:
                # 既无 vidstab 也不需要 deflicker，直接复制
                import shutil as _stab_shutil
                _stab_shutil.copy2(video, output)

        if not output.exists() or output.stat().st_size < 10_000:
            raise RuntimeError("输出文件无效")
        return output

    except Exception as _stab_err:
        print(f"   ⚠️  稳定化失败（{_stab_err}），使用原视频")
        import shutil as _stab_shutil2
        _stab_shutil2.copy2(video, output)
        return video


def apply_color_grading(
    video: Path,
    output: Path,
    preset: str = DEFAULT_COLOR_GRADING,
    custom_params: Optional[Dict[str, Any]] = None,
    brand_color_tint: bool = False,
) -> Path:
    """
    对视频应用统一调色

    支持的调色预设：
    - warm_cinematic：暖色电影感
    - cool_cinematic：冷色电影感
    - vintage：复古胶片
    - teal_orange：青橙色调（好莱坞）
    - moody：暗调情绪
    - bright_clean：明亮清新
    - pastel：马卡龙
    - noir：黑色电影（黑白）

    Args:
        video: 输入视频
        output: 输出路径
        preset: 调色预设名称
        custom_params: 自定义参数（覆盖预设）
        brand_color_tint: 是否叠加品牌色微调色

    Returns:
        输出文件路径
    """
    # 获取预设参数
    params = COLOR_GRADING_PRESETS.get(preset, COLOR_GRADING_PRESETS[DEFAULT_COLOR_GRADING]).copy()

    # 合并自定义参数
    if custom_params:
        params.update(custom_params)

    # 品牌色叠加（O2：0.03 → 0.06，3% 在设备屏幕上视觉感知为零，6% 有轻微可感知的品牌色调）
    # Q2 修复：noir/vintage 等低饱和度/黑白预设跳过品牌色，防止色偏破坏风格一致性
    _NO_TINT_PRESETS = {"noir", "vintage", "moody"}
    if brand_color_tint and preset not in _NO_TINT_PRESETS:
        brand_color = BRAND_CONFIG.get("primary_color", "#FF6B6B")
        params["color_overlay"] = (brand_color, 0.06)

    # 构建 FFmpeg 滤镜链
    filters = []

    # 1. 基础调色（eq 滤镜：亮度、对比度、饱和度、伽马）
    brightness = params.get("brightness", 1.0)
    contrast = params.get("contrast", 1.0)
    saturation = params.get("saturation", 1.0)
    gamma = params.get("gamma", 1.0)

    # eq 滤镜的 brightness 范围是 -1 ~ 1，我们的是 0 ~ 2，做个转换
    eq_brightness = brightness - 1.0
    eq_contrast = contrast
    eq_saturation = saturation
    eq_gamma = gamma

    eq_parts = []
    if abs(eq_brightness) > 0.001:
        eq_parts.append(f"brightness={eq_brightness:.3f}")
    if abs(eq_contrast - 1.0) > 0.001:
        eq_parts.append(f"contrast={eq_contrast:.3f}")
    if abs(eq_saturation - 1.0) > 0.001:
        eq_parts.append(f"saturation={eq_saturation:.3f}")
    if abs(eq_gamma - 1.0) > 0.001:
        eq_parts.append(f"gamma={eq_gamma:.3f}")

    if eq_parts:
        filters.append(f"eq={':'.join(eq_parts)}")

    # 2. 色温/色调调节（colorbalance 滤镜）
    temperature = params.get("temperature", 0)
    tint = params.get("tint", 0)

    # temperature 映射到红/蓝通道平衡
    # 暖色（正）：增加红，减少蓝
    # 冷色（负）：减少红，增加蓝
    temp_red = temperature * 0.15  # 阴影红
    temp_blue = -temperature * 0.15  # 阴影蓝

    # tint 映射到绿/品红通道
    # 正：品红（减绿）
    # 负：绿（减品红 = 加绿）
    tint_green = -tint * 0.1  # 阴影绿

    # 检查是否有独立的阴影/高光参数（如 teal_orange）
    shadows_red = params.get("shadows_red", temp_red)
    shadows_green = params.get("shadows_green", tint_green)
    shadows_blue = params.get("shadows_blue", temp_blue)
    highlights_red = params.get("highlights_red", temp_red * 0.5)
    highlights_green = params.get("highlights_green", tint_green * 0.5)
    highlights_blue = params.get("highlights_blue", temp_blue * 0.5)

    has_color_balance = (
        abs(shadows_red) > 0.001
        or abs(shadows_green) > 0.001
        or abs(shadows_blue) > 0.001
        or abs(highlights_red) > 0.001
        or abs(highlights_green) > 0.001
        or abs(highlights_blue) > 0.001
    )

    if has_color_balance:
        cb_parts = [
            f"rs={shadows_red:.3f}",
            f"gs={shadows_green:.3f}",
            f"bs={shadows_blue:.3f}",
            f"rh={highlights_red:.3f}",
            f"gh={highlights_green:.3f}",
            f"bh={highlights_blue:.3f}",
        ]
        filters.append(f"colorbalance={':'.join(cb_parts)}")

    # 3. 颜色叠加层（模拟品牌色滤镜）
    color_overlay = params.get("color_overlay")
    if color_overlay and isinstance(color_overlay, (tuple, list)) and len(color_overlay) == 2:
        overlay_color, opacity = color_overlay
        # HEX 转 RGB
        hex_color = overlay_color.lstrip("#")
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0

        # 使用 colorchannelmixer 做颜色叠加（混合模式：柔光简化版）
        # 或者使用 blend 滤镜：先创建纯色层再混合
        # 这里用 eq + colorbalance 的组合简化，或者用 geq
        # 最稳妥：用 colorchannelmixer 做轻微色调偏移
        mix = opacity
        cm_parts = [
            f"rr={1 - mix + mix * r:.3f}",
            f"gg={1 - mix + mix * g:.3f}",
            f"bb={1 - mix + mix * b:.3f}",
        ]
        filters.append(f"colorchannelmixer={':'.join(cm_parts)}")

    # 如果没有任何滤镜，直接复制
    if not filters:
        shutil.copy2(video, output)
        return output

    # 拼接滤镜链
    vf_str = ",".join(filters)

    # Bug #10 修复：补充 -c:v libx264，防止调色时回退到低质量默认编码器
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video),
        "-vf", vf_str,
        "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-pix_fmt", "yuv420p", *_color_range_args(),
    ]

    if _has_audio_stream(video):
        cmd.extend(["-c:a", "copy"])

    cmd.append(str(output))

    run_ffmpeg(cmd, timeout=300)
    return output


def get_color_grading_for_style(style_key: str) -> str:
    """
    根据电影风格推荐调色预设

    Args:
        style_key: 电影风格键值

    Returns:
        调色预设名称
    """
    style_to_preset = {
        "hitchcock": "moody",
        "kubrick": "teal_orange",
        "spielberg": "warm_cinematic",
        "aronofsky": "moody",
        "scorsese": "vintage",
        "nolan": "cool_cinematic",
        "anderson": "pastel",
        "wong-kar-wai": "teal_orange",
        "tarkovsky": "vintage",
    }
    return style_to_preset.get(style_key, DEFAULT_COLOR_GRADING)


def auto_trim_black_frames(
    input_video: Path,
    output: Path,
    min_black_duration: float = 0.3,
) -> Path:
    """
    自动检测并裁切首尾黑帧

    Args:
        input_video: 输入视频
        output: 输出视频
        min_black_duration: 最小黑帧时长（秒），小于这个值不裁切

    Returns:
        输出文件路径（如果没检测到黑帧，直接拷贝原文件）
    """
    try:
        # 使用 blackdetect 检测黑帧
        cmd = [
            "ffmpeg", "-i", str(input_video),
            "-vf", f"blackdetect=d={min_black_duration}:pix_th=0.10",
            "-an", "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        stderr = result.stderr

        # 解析检测结果
        import re
        pattern = r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)"
        matches = re.findall(pattern, stderr)

        if not matches:
            # 没检测到黑帧，直接拷贝
            shutil.copy2(input_video, output)
            return output

        # 获取视频总时长
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_video),
            ]
            dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            if dur_result.returncode == 0 and dur_result.stdout.strip():
                duration = float(dur_result.stdout.strip())
        except Exception:
            pass

        # 计算裁切起点和终点
        trim_start = 0.0
        trim_end = duration

        for start_str, end_str, dur_str in matches:
            start = float(start_str)
            end = float(end_str)

            # 开头黑帧（从 0 开始的）
            if start < 0.5:
                trim_start = max(trim_start, end)
            # 结尾黑帧（接近视频末尾的）
            if duration > 0 and end > duration - 0.5:
                trim_end = min(trim_end, start)

        # 如果不需要裁切
        if trim_start < 0.1 and (duration == 0 or abs(trim_end - duration) < 0.1):
            shutil.copy2(input_video, output)
            return output

        # 执行裁切
        print(f"✂️  自动裁切黑帧：开头 {trim_start:.1f}s，结尾 {duration - trim_end:.1f}s")
        trim_cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_start),
            "-to", str(trim_end),
            "-i", str(input_video),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
        run_ffmpeg(trim_cmd, timeout=120)
        return output

    except Exception as e:
        print(f"⚠️  自动裁切黑帧失败：{e}")
        shutil.copy2(input_video, output)
        return output


def detect_freeze_frames(
    video_path: Path,
    noise_tolerance: float = 0.003,
    min_freeze_duration: float = 0.4,
) -> Tuple[float, float]:
    """
    检测视频首尾的冻结帧（画面静止不动）

    使用 ffmpeg freezedetect 滤镜检测。

    Args:
        video_path: 视频路径
        noise_tolerance: 噪声容限（0.001-0.01，越小越敏感）
        min_freeze_duration: 最小冻结时长（秒）

    Returns:
        (开头冻结时长, 结尾冻结时长) 单位：秒
    """
    if not video_path.exists():
        return 0.0, 0.0

    try:
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", f"freezedetect=n={noise_tolerance}:d={min_freeze_duration}",
            "-an", "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr

        import re
        pattern = r"freeze_start:([\d.]+)\s+freeze_end:([\d.]+)\s+freeze_duration:([\d.]+)"
        matches = re.findall(pattern, stderr)

        if not matches:
            return 0.0, 0.0

        # 获取视频总时长
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
            dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            if dur_result.returncode == 0 and dur_result.stdout.strip():
                duration = float(dur_result.stdout.strip())
        except Exception:
            pass

        freeze_start = 0.0
        freeze_end = 0.0

        for start_str, end_str, dur_str in matches:
            start = float(start_str)
            end = float(end_str)

            # 开头冻结（从 0 附近开始）
            if start < 0.5:
                freeze_start = max(freeze_start, end)
            # 结尾冻结（接近视频末尾）
            if duration > 0 and end > duration - 0.5:
                freeze_end = max(freeze_end, end - start)

        return freeze_start, freeze_end

    except Exception as e:
        print(f"⚠️  冻结帧检测失败：{e}")
        return 0.0, 0.0


def auto_trim_freeze_frames(
    input_video: Path,
    output: Path,
    noise_tolerance: float = 0.003,
    min_freeze_duration: float = 0.4,
) -> Path:
    """
    自动检测并裁切首尾冻结帧

    Args:
        input_video: 输入视频
        output: 输出视频
        noise_tolerance: 噪声容限
        min_freeze_duration: 最小冻结时长

    Returns:
        输出文件路径
    """
    try:
        freeze_start, freeze_end = detect_freeze_frames(
            input_video, noise_tolerance, min_freeze_duration
        )

        if freeze_start < 0.1 and freeze_end < 0.1:
            shutil.copy2(input_video, output)
            return output

        # 获取视频总时长
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_video),
            ]
            dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            if dur_result.returncode == 0 and dur_result.stdout.strip():
                duration = float(dur_result.stdout.strip())
        except Exception:
            pass

        trim_start = freeze_start if freeze_start > 0.1 else 0.0
        trim_end = duration - freeze_end if duration > 0 and freeze_end > 0.1 else duration

        if trim_start < 0.1 and abs(trim_end - duration) < 0.1:
            shutil.copy2(input_video, output)
            return output

        print(f"❄️  自动裁切冻结帧：开头 {freeze_start:.1f}s，结尾 {freeze_end:.1f}s")
        trim_cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_start),
            "-to", str(trim_end),
            "-i", str(input_video),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
        run_ffmpeg(trim_cmd, timeout=120)
        return output

    except Exception as e:
        print(f"⚠️  自动裁切冻结帧失败：{e}")
        shutil.copy2(input_video, output)
        return output


def auto_trim_video(
    input_video: Path,
    output: Path,
) -> Path:
    """
    统一的视频首尾自动修复：黑帧 + 冻结帧联合检测与裁切

    Args:
        input_video: 输入视频
        output: 输出视频

    Returns:
        输出文件路径
    """
    # 第一步：检测黑帧
    # 第二步：检测冻结帧
    # 如果都需要裁切，一次性裁掉，减少一次重编码

    try:
        # 先检测冻结帧
        freeze_start, freeze_end = detect_freeze_frames(input_video)

        # 再检测黑帧（用 freezedetect 之后的结果？不，直接在原视频上检测）
        # 使用 blackdetect
        black_cmd = [
            "ffmpeg", "-i", str(input_video),
            "-vf", "blackdetect=d=0.3:pix_th=0.10",
            "-an", "-f", "null", "-",
        ]
        black_result = subprocess.run(black_cmd, capture_output=True, text=True, timeout=60)
        black_stderr = black_result.stderr

        import re
        black_pattern = r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)"
        black_matches = re.findall(black_pattern, black_stderr)

        # 获取总时长
        duration = 0.0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_video),
            ]
            dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            if dur_result.returncode == 0 and dur_result.stdout.strip():
                duration = float(dur_result.stdout.strip())
        except Exception:
            pass

        # 计算黑帧裁切量
        black_start = 0.0
        black_end = 0.0
        for start_str, end_str, dur_str in black_matches:
            start = float(start_str)
            end = float(end_str)
            if start < 0.5:
                black_start = max(black_start, end)
            if duration > 0 and end > duration - 0.5:
                black_end = max(black_end, end - start)

        # 总裁切量 = max(黑帧, 冻结帧)
        trim_start = max(black_start, freeze_start)
        trim_end_amount = max(black_end, freeze_end)
        trim_end = duration - trim_end_amount if duration > 0 else duration

        # 如果裁切量太小，直接拷贝
        if trim_start < 0.1 and trim_end_amount < 0.1:
            shutil.copy2(input_video, output)
            return output

        # 打印裁切信息
        parts = []
        if trim_start > 0.1:
            reason = []
            if black_start > 0.1 and abs(black_start - trim_start) < 0.01:
                reason.append("黑帧")
            if freeze_start > 0.1 and abs(freeze_start - trim_start) < 0.01:
                reason.append("冻结")
            parts.append(f"开头 {trim_start:.1f}s（{'/'.join(reason)}）")
        if trim_end_amount > 0.1:
            reason = []
            if black_end > 0.1 and abs(black_end - trim_end_amount) < 0.01:
                reason.append("黑帧")
            if freeze_end > 0.1 and abs(freeze_end - trim_end_amount) < 0.01:
                reason.append("冻结")
            parts.append(f"结尾 {trim_end_amount:.1f}s（{'/'.join(reason)}）")

        print(f"✂️  自动裁切：{'，'.join(parts)}")

        # B1 修复：先执行裁切，再对 output 判断音频流，而非对裁切前的 input_video 判断
        # -c copy 在非关键帧处裁切会产生花屏，重编码彻底消除
        trim_cmd = [
            "ffmpeg", "-y",
            "-ss", str(trim_start),
            "-to", str(trim_end),
            "-i", str(input_video),
            "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            "-pix_fmt", "yuv420p", *_color_range_args(),
            "-movflags", "+faststart",
        ]
        # 裁切前判断原始音频流是否存在（-ss 偏移不影响流的存在性判断）
        if _has_audio_stream(input_video):
            trim_cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        trim_cmd.append(str(output))
        run_ffmpeg(trim_cmd, timeout=180)
        return output

    except Exception as e:
        print(f"⚠️  自动裁切失败：{e}")
        shutil.copy2(input_video, output)
        return output


def export_final_video(
    input_video: Path,
    output: Path,
    resolution: str = OUTPUT_RESOLUTION,
    fps: int = OUTPUT_FPS,
    bitrate: str = OUTPUT_BITRATE,
    audio_lufs: float = -14.0,
    audio_peak: float = -1.0,
) -> Path:
    """
    导出最终视频（统一编码参数 + 音频标准化）

    音频处理：
    - loudnorm 响度标准化（默认 -14 LUFS，抖音/短视频平台推荐值）
    - alimiter 峰值限制（默认 -1 dBTP，防止爆音）
    - 音频重采样到 44.1kHz（抖音标准）

    Args:
        input_video: 输入视频
        output: 输出路径
        resolution: 分辨率
        fps: 帧率
        bitrate: 码率
        audio_lufs: 目标响度（LUFS），抖音推荐 -14 ~ -16
        audio_peak: 峰值上限（dBTP），建议 -1.0 ~ -2.0

    Returns:
        输出文件路径
    """
    import re as _re
    import json as _json
    import subprocess as _subprocess

    # O1 修复：改为 ABR 双约束模式（-b:v + -maxrate + -bufsize）
    # CRF 模式在转场激烈段码率突发，抖音平台二次压缩后质量不稳定
    # ABR 模式码率更稳定，平台压缩后质量损失更均匀
    _bval = _re.search(r"[\d.]+", bitrate)
    _bval_int = int(float(_bval.group())) if _bval else 8
    # maxrate = 1.5x bitrate，bufsize = 2x bitrate（标准 VBV 缓冲配置）
    maxrate = f"{int(_bval_int * 1.5)}M"
    bufsize = f"{_bval_int * 2}M"

    # Q1 修复：用 scale+pad 替代强制 -s，防止可灵返回非标尺寸时画面被拉伸变形
    # 策略：等比缩放到目标尺寸内（force_original_aspect_ratio=decrease），
    # 再用黑色填充（pad）到精确目标分辨率，保持画面比例不变形
    _res_w, _res_h = resolution.split("x")
    _scale_pad_filter = (
        f"scale={_res_w}:{_res_h}:force_original_aspect_ratio=decrease,"
        f"pad={_res_w}:{_res_h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    base_cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_video),
        "-c:v", "libx264",
        "-preset", "slow",
        "-profile:v", "high",
        "-level", "4.0",
        "-b:v", bitrate,
        "-maxrate", maxrate,   # O1：1.5x 峰值约束，防止转场段码率突发
        "-bufsize", bufsize,   # O1：2x VBV 缓冲，平滑码率波动
        "-r", str(fps),
        "-vf", _scale_pad_filter,   # Q1：scale+pad 防拉伸，替代强制 -s
        "-pix_fmt", "yuv420p", *_color_range_args(),
        "-g", str(fps * 2),    # GOP = 2s：给 P/B 帧更多码率空间，快速运动段块状失真更少
        "-movflags", "+faststart",
    ]

    # 有音轨才追加音频编码参数
    if _has_audio_stream(input_video):
        # Q2 修复：双程 loudnorm，误差从 ±2 LUFS 降到 ±0.1 LUFS
        # 第一程：分析 EBU R128 测量值
        measured_I = audio_lufs
        measured_LRA = 7.0
        measured_TP = audio_peak
        measured_thresh = audio_lufs - 10.0
        offset = 0.0
        try:
            _analyze_cmd = [
                "ffmpeg", "-y",
                "-i", str(input_video),
                "-af", (
                    "acompressor=threshold=0.126:ratio=3:attack=10:release=150:makeup=1,"
                    f"loudnorm=I={audio_lufs}:LRA=7:TP={audio_peak}:print_format=json"
                ),
                "-vn", "-f", "null", "-",
            ]
            # 动态超时：每秒视频给 5s 分析时间，最少 120s（防止长视频超时）
            _analyze_dur = _get_clip_duration(input_video)
            _analyze_timeout = max(120, int(_analyze_dur * 5))
            _r = _subprocess.run(
                _analyze_cmd, capture_output=True, text=True, timeout=_analyze_timeout
            )
            # loudnorm 把 JSON 结果打到 stderr
            _stderr = _r.stderr
            _json_start = _stderr.rfind("{")
            _json_end = _stderr.rfind("}") + 1
            if _json_start >= 0 and _json_end > _json_start:
                _ln = _json.loads(_stderr[_json_start:_json_end])
                measured_I = float(_ln.get("input_i", audio_lufs))
                measured_LRA = float(_ln.get("input_lra", 7.0))
                measured_TP = float(_ln.get("input_tp", audio_peak))
                measured_thresh = float(_ln.get("input_thresh", audio_lufs - 10.0))
                offset = float(_ln.get("target_offset", 0.0))
                print(f"  🎚  loudnorm 分析：input_i={measured_I:.1f} LUFS，将精确调整到 {audio_lufs} LUFS")
        except Exception as _e:
            print(f"  ⚠️  loudnorm 分析失败（{_e}），回退到单程模式")

        # 第二程：精确调整（使用测量值）
        # O1 修复：acompressor 参数收紧——短视频动态范围应更窄
        # ratio=3（原2）：更强的动态压缩，BGM 高潮段不会突然变响
        # attack=10ms（原20ms）：更快响应，人声进入时 BGM 立刻压低
        # release=150ms（原250ms）：人声结束后 BGM 更快恢复，节奏感更强
        audio_filter = (
            # B3 修复：acompressor 的 threshold 单位是线性值（0-1），不是 dBFS。
            # 原来的 -18dB 被 FFmpeg 解析为字符串，压缩器静默失效。
            # -18dBFS 对应线性值 = 10^(-18/20) ≈ 0.126
            f"acompressor=threshold=0.126:ratio=3:attack=10:release=150:makeup=1,"
            f"loudnorm=I={audio_lufs}:LRA=7:TP={audio_peak}"
            f":measured_I={measured_I:.2f}:measured_LRA={measured_LRA:.2f}"
            f":measured_TP={measured_TP:.2f}:measured_thresh={measured_thresh:.2f}"
            f":offset={offset:.2f}:linear=true,"
            f"alimiter=limit={audio_peak}dB:level=false:attack=5:release=50,"
            f"aformat=sample_rates=44100:sample_fmts=fltp:channel_layouts=stereo"
        )
        base_cmd.extend([
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-af", audio_filter,
        ])

    base_cmd.append(str(output))
    # O7 修复：动态超时，每秒视频给 30s 编码时间，低配 Mac 跑 pro 模式大文件不超时
    _export_dur = _get_clip_duration(input_video)
    _export_timeout = max(300, int(_export_dur * 30))
    run_ffmpeg(base_cmd, timeout=_export_timeout)
    return output
