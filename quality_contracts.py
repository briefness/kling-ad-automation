"""Small quality contracts shared by generation and post-production."""


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
