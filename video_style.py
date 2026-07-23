"""Video style presets and CLI argument mapping."""

import argparse
import re
from typing import Optional

from ad_script import DEFAULT_SCRIPT_STYLE
from config import DEFAULT_HOOK_TYPE
from tts_client import DEFAULT_VOICEOVER_STYLE


VIDEO_STYLE_PRESETS = {
    "带货": {
        "script_style": "demonstration",
        "hook": "demonstration",
        "voiceover_style": "energetic",
        "rhythm_style": "fast",
        "tone": "direct_sales",
        "prompt_note": "表达像短视频带货口播：开头直接给购买理由，字幕突出卖点、使用场景和行动号召，口播更干脆有成交感。",
    },
    "直播带货": {
        "script_style": "demonstration",
        "hook": "demonstration",
        "voiceover_style": "energetic",
        "rhythm_style": "fast",
        "tone": "direct_sales",
        "prompt_note": "表达像短视频带货口播：开头直接给购买理由，字幕突出卖点、使用场景和行动号召，口播更干脆有成交感。",
    },
    "个人vlog": {
        "script_style": "storytelling",
        "hook": "story",
        "voiceover_style": "storytelling",
        "rhythm_style": "moderate",
        "tone": "personal_vlog",
        "prompt_note": "表达像个人 vlog 分享：字幕和口播保留第一人称体验、日常场景和真实感，少用硬广腔。",
    },
    "个人 vlog": {
        "script_style": "storytelling",
        "hook": "story",
        "voiceover_style": "storytelling",
        "rhythm_style": "moderate",
        "tone": "personal_vlog",
        "prompt_note": "表达像个人 vlog 分享：字幕和口播保留第一人称体验、日常场景和真实感，少用硬广腔。",
    },
    "vlog": {
        "script_style": "storytelling",
        "hook": "story",
        "voiceover_style": "storytelling",
        "rhythm_style": "moderate",
        "tone": "personal_vlog",
        "prompt_note": "表达像个人 vlog 分享：字幕和口播保留第一人称体验、日常场景和真实感，少用硬广腔。",
    },
    "种草": {
        "script_style": "social_proof",
        "hook": "celeb_style",
        "voiceover_style": "emotional",
        "rhythm_style": "moderate",
        "tone": "recommendation",
        "prompt_note": "表达像真实种草分享：字幕先给兴趣点和推荐理由，口播强调体验、适合谁、为什么值得试。",
    },
    "测评": {
        "script_style": "before_after",
        "hook": "pain_point",
        "voiceover_style": "professional",
        "rhythm_style": "moderate",
        "tone": "review",
        "prompt_note": "表达像真实测评：字幕和口播要有观察、对比和结论，避免夸张承诺。",
    },
    "开箱": {
        "script_style": "demonstration",
        "hook": "demonstration",
        "voiceover_style": "standard",
        "rhythm_style": "moderate",
        "tone": "unboxing",
        "prompt_note": "表达像开箱体验：字幕和口播按看到什么、怎么用、第一感受推进。",
    },
}


def _normalize_video_style(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _video_style_preset(value: str) -> dict:
    normalized = _normalize_video_style(value)
    for key, preset in VIDEO_STYLE_PRESETS.items():
        if _normalize_video_style(key) == normalized:
            return preset
    if "带货" in value or "卖货" in value or "转化" in value:
        return VIDEO_STYLE_PRESETS["带货"]
    if "vlog" in normalized or "日常" in value or "生活记录" in value:
        return VIDEO_STYLE_PRESETS["个人vlog"]
    if "种草" in value or "推荐" in value or "分享" in value:
        return VIDEO_STYLE_PRESETS["种草"]
    if "测评" in value or "评测" in value or "对比" in value:
        return VIDEO_STYLE_PRESETS["测评"]
    if "开箱" in value:
        return VIDEO_STYLE_PRESETS["开箱"]
    return {
        "script_style": DEFAULT_SCRIPT_STYLE,
        "hook": DEFAULT_HOOK_TYPE,
        "voiceover_style": DEFAULT_VOICEOVER_STYLE,
        "rhythm_style": "moderate",
        "tone": "custom_video_style",
        "prompt_note": f"视频风格参考：{value}。只影响字幕、脚本结构和口播语气，不改写产品事实。",
    }


def _recommend_video_style(product_info: dict, args: Optional[argparse.Namespace] = None) -> str:
    """Recommend the default video style from product info and existing choices."""
    if args is not None:
        current = str(getattr(args, "video_style", "") or "").strip()
        if current and current not in ("auto",):
            return current

    product_type = str(product_info.get("type", "default") or "default")
    scene_desc = str(product_info.get("scene_description", "") or "")
    audience = str(product_info.get("audience", "") or "")
    tone = str(product_info.get("tone", "") or "")
    style_hint = " ".join([product_type, scene_desc, audience, tone]).lower()

    if any(keyword in style_hint for keyword in ("vlog", "日常", "生活", "记录", "分享", "陪伴")):
        return "个人vlog"
    if any(keyword in style_hint for keyword in ("种草", "推荐", "安利", "心动")):
        return "种草"
    if any(keyword in style_hint for keyword in ("测评", "评测", "对比", "试用")):
        return "测评"
    if any(keyword in style_hint for keyword in ("开箱", "拆箱")):
        return "开箱"
    if any(keyword in style_hint for keyword in ("直播", "带货", "成交", "转化", "下单", "购买")):
        return "带货"

    try:
        from quality_gate import evolve_voiceover_style_recommendation
        voiced_style, _ = evolve_voiceover_style_recommendation(product_type)
    except Exception:
        from quality_gate import smart_pick_voiceover_style
        voiced_style = smart_pick_voiceover_style(product_type)

    if voiced_style == "storytelling":
        return "个人vlog"
    if voiced_style == "professional":
        return "测评"
    if voiced_style == "energetic":
        return "带货"
    if voiced_style == "emotional":
        return "种草"
    return "带货"


def apply_video_style_to_args(args: argparse.Namespace, product_info: Optional[dict] = None) -> dict:
    """Translate a high-level video style into existing script, hook, voice, and rhythm args."""
    explicit = set(getattr(args, "_explicit_args", set()) or set())
    video_style = str(getattr(args, "video_style", "") or "").strip()
    if not video_style or video_style == "auto":
        video_style = _recommend_video_style(product_info or {}, args)
        args.video_style = video_style

    preset = _video_style_preset(video_style)
    applied = {"video_style": video_style}

    if "script_style" not in explicit:
        args.script_style = preset["script_style"]
        applied["script_style"] = args.script_style
    if "hook" not in explicit:
        args.hook = preset["hook"]
        applied["hook"] = args.hook
    if "voiceover_style" not in explicit:
        args.voiceover_style = preset["voiceover_style"]
        applied["voiceover_style"] = args.voiceover_style
    if "rhythm_style" not in explicit:
        args.rhythm_style = preset["rhythm_style"]
        applied["rhythm_style"] = args.rhythm_style

    applied["tone"] = preset["tone"]
    applied["prompt_note"] = preset["prompt_note"]
    applied["video_style_source"] = "user" if "video_style" in explicit and video_style != "auto" else "auto"
    args.video_style_resolution = applied
    return applied
