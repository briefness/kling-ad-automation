"""Music contract and script-derived BGM signals."""

from typing import Any, Dict, List, Optional, TypedDict

from video_style import _video_style_preset


VIDEO_STYLE_MUSIC_PROFILES = {
    "direct_sales": {
        "mood": "upbeat",
        "genre": "pop",
        "energy": "high",
        "recommended_pace": "fast",
        "intro_type": "immediate",
        "keywords": ["advertising", "commercial", "promo", "upbeat", "rhythmic", "fresh"],
        "avoid": ["ambient", "sleep", "sad", "cinematic", "vocal", "singing"],
    },
    "personal_vlog": {
        "mood": "warm",
        "genre": "acoustic",
        "energy": "medium",
        "recommended_pace": "moderate",
        "intro_type": "fade_in",
        "keywords": ["vlog", "lifestyle", "warm", "acoustic", "chill", "authentic"],
        "avoid": ["corporate", "epic", "intense", "hype", "trap", "vocal"],
    },
    "recommendation": {
        "mood": "warm",
        "genre": "acoustic",
        "energy": "medium",
        "recommended_pace": "moderate",
        "intro_type": "immediate",
        "keywords": ["lifestyle", "positive", "fresh", "warm", "upbeat", "acoustic"],
        "avoid": ["corporate", "epic", "dark", "intense", "hype", "vocal"],
    },
    "review": {
        "mood": "cool",
        "genre": "lofi",
        "energy": "medium",
        "recommended_pace": "moderate",
        "intro_type": "immediate",
        "keywords": ["clean", "modern", "steady", "minimal", "lofi", "technology"],
        "avoid": ["epic", "dramatic", "comedy", "kids", "vocal", "singing"],
    },
    "unboxing": {
        "mood": "upbeat",
        "genre": "pop",
        "energy": "medium",
        "recommended_pace": "moderate",
        "intro_type": "immediate",
        "keywords": ["unboxing", "lifestyle", "clean", "fresh", "upbeat", "product"],
        "avoid": ["epic", "dark", "sad", "vocal", "singing"],
    },
    "custom_video_style": {
        "keywords": ["advertising", "lifestyle", "positive", "modern"],
        "avoid": ["vocal", "singing", "kids", "comedy"],
    },
}


class MusicContract(TypedDict):
    """Music strategy constraints determined before BGM selection."""

    bpm_min: int
    bpm_max: int
    mood: str
    genre: str
    energy: str
    recommended_pace: str
    intro_type: str
    source: str
    visual_brightness: str
    visual_contrast: str
    transition_base_duration: float
    sfx_intensity: str
    semantic_tone: str
    story_role_counts: Dict[str, int]
    video_style: str
    video_style_tone: str
    script_style: str
    voiceover_style: str
    rhythm_style: str
    music_keywords: List[str]
    avoid_keywords: List[str]


def _collect_script_music_signals(ad_script: dict) -> dict:
    segments = ad_script.get("segments") or []
    text = " ".join(
        str(part or "").lower()
        for seg in segments
        for part in (
            seg.get("subtitle"),
            seg.get("voiceover"),
            seg.get("narrative"),
            seg.get("marketing_intent"),
        )
    )

    keyword_groups = {
        "warm": ("清爽", "轻盈", "自然", "真实", "日常", "陪伴", "舒服", "无负担", "轻松", "柔和"),
        "upbeat": ("购买", "点下方", "试试", "种草", "分享", "推荐", "带货", "下单", "转化", "直接"),
        "cool": ("测评", "对比", "实测", "观察", "数据", "客观", "分析", "专业"),
        "cinematic": ("故事", "转折", "回忆", "感受", "氛围", "沉浸", "情绪"),
    }
    matched = {
        tone: [kw for kw in keywords if kw in text]
        for tone, keywords in keyword_groups.items()
    }
    tone_scores = {tone: len(values) for tone, values in matched.items()}
    tone = max(tone_scores, key=tone_scores.get) if any(tone_scores.values()) else "balanced"
    keywords = []
    avoid = []
    if tone == "warm":
        keywords = ["warm", "acoustic", "fresh", "natural", "lifestyle"]
        avoid = ["epic", "hard rock", "intense", "vocal"]
    elif tone == "upbeat":
        keywords = ["upbeat", "rhythmic", "commercial", "pop", "fresh"]
        avoid = ["ambient", "slow", "sad", "vocal"]
    elif tone == "cool":
        keywords = ["clean", "steady", "lofi", "minimal", "modern"]
        avoid = ["hype", "kids", "comedy", "singing"]
    elif tone == "cinematic":
        keywords = ["cinematic", "emotional", "ambient", "build", "orchestral"]
        avoid = ["playful", "comic", "kids", "singing"]
    else:
        keywords = ["balanced", "modern", "clean", "steady"]
        avoid = ["vocal", "singing"]

    return {
        "tone": tone,
        "keywords": keywords,
        "avoid": avoid,
        "matched": matched,
    }


def build_music_contract(
    product_info: dict,
    cinematic_style: str = "none",
    asset_creative_profile: Optional[Dict[str, Any]] = None,
    script_style: str = "",
    voiceover_style: str = "",
    rhythm_style: str = "",
    script_music_profile: Optional[Dict[str, Any]] = None,
) -> MusicContract:
    """Build a BGM search and post-production contract from product, video, and script signals."""
    ptype = product_info.get("type", "default")
    audience = product_info.get("audience", "18-35")
    video_style = str(product_info.get("video_style") or "").strip()
    video_preset = _video_style_preset(video_style) if video_style else {}
    video_style_tone = str(video_preset.get("tone") or "custom_video_style")
    video_music = VIDEO_STYLE_MUSIC_PROFILES.get(
        video_style_tone,
        VIDEO_STYLE_MUSIC_PROFILES["custom_video_style"],
    )
    script_music_profile = script_music_profile or {}
    script_music_keywords = list(script_music_profile.get("keywords") or [])
    script_music_avoid = list(script_music_profile.get("avoid") or [])

    product_music_map = {
        "美妆": {"mood": "upbeat", "genre": "pop", "energy": "high"},
        "科技": {"mood": "cool", "genre": "electronic", "energy": "medium"},
        "食品": {"mood": "warm", "genre": "acoustic", "energy": "medium"},
        "家居": {"mood": "calm", "genre": "lofi", "energy": "low"},
        "服装": {"mood": "upbeat", "genre": "pop", "energy": "high"},
        "医疗": {"mood": "calm", "genre": "acoustic", "energy": "low"},
        "教育": {"mood": "warm", "genre": "acoustic", "energy": "medium"},
        "房产": {"mood": "grand", "genre": "orchestral", "energy": "medium"},
    }
    base = product_music_map.get(ptype, {"mood": "upbeat", "genre": "pop", "energy": "medium"})

    style_mood_override = {
        "hitchcock": {"mood": "suspenseful", "genre": "orchestral", "intro_type": "buildup"},
        "kubrick": {"mood": "cold", "genre": "orchestral", "intro_type": "fade_in"},
        "spielberg": {"mood": "emotional", "genre": "orchestral", "intro_type": "buildup"},
        "miyazaki": {"mood": "dreamy", "genre": "lofi", "intro_type": "fade_in"},
        "wongkarwai": {"mood": "moody", "genre": "jazz", "intro_type": "fade_in"},
        "zhangyimou": {"mood": "grand", "genre": "orchestral", "intro_type": "buildup"},
    }
    style_override = style_mood_override.get(cinematic_style, {})

    mood = str(style_override.get("mood") or video_music.get("mood") or base["mood"])
    genre = str(style_override.get("genre") or video_music.get("genre") or base["genre"])
    energy = str(
        (asset_creative_profile or {}).get("energy")
        or video_music.get("energy")
        or base["energy"]
    )
    intro_type = str(
        style_override.get("intro_type")
        or (asset_creative_profile or {}).get("intro_type")
        or video_music.get("intro_type")
        or "immediate"
    )

    audience_bpm = {
        "18-25": (120, 140),
        "25-35": (100, 120),
        "35-45": (90, 110),
        "45+": (80, 100),
    }
    bpm_min, bpm_max = audience_bpm.get(audience, (100, 120))

    energy_pace = {"high": "fast", "medium": "moderate", "low": "cinematic"}
    recommended_pace = str(
        (asset_creative_profile or {}).get("recommended_pace")
        or video_music.get("recommended_pace")
        or energy_pace.get(energy, "moderate")
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
        video_style=video_style,
        video_style_tone=video_style_tone,
        script_style=str(script_style or ""),
        voiceover_style=str(voiceover_style or ""),
        rhythm_style=str(rhythm_style or ""),
        music_keywords=list(dict.fromkeys(list(video_music.get("keywords") or []) + script_music_keywords)),
        avoid_keywords=list(dict.fromkeys(list(video_music.get("avoid") or []) + script_music_avoid)),
    )
