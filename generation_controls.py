"""Output naming, template persistence, and generation cost controls."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from config import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_CINEMATIC_STYLE,
    DEFAULT_HOOK_TYPE,
    DEFAULT_IMAGE_FIDELITY,
    DEFAULT_HUMAN_FIDELITY,
    DEFAULT_MODE,
    DEFAULT_VIDEO_DURATION,
    KLING_PRICING,
    KLING_VIDEO_MODEL,
    QUALITY_GATE_CONFIG,
)
from ad_script import DEFAULT_SCRIPT_STYLE


def _safe_output_stem(value: str) -> str:
    """Generate a safe filename stem."""
    return "".join(c for c in value if c.isalnum() or c in "-_").strip() or "product"


def _arg_str(args: argparse.Namespace, name: str, default: str = "") -> str:
    value = getattr(args, name, default)
    return value if isinstance(value, str) and value else default


def build_stable_output_name(product_info: dict, args: argparse.Namespace) -> str:
    """Build a stable output name for resume/cache matching."""
    relevant = {
        "product_info": product_info,
        "style": getattr(args, "style", DEFAULT_CINEMATIC_STYLE),
        "duration": getattr(args, "duration", DEFAULT_VIDEO_DURATION),
        "mode": getattr(args, "mode", DEFAULT_MODE),
        "aspect_ratio": getattr(args, "aspect_ratio", DEFAULT_ASPECT_RATIO),
        "product_image": str(getattr(args, "product_image", "") or ""),
        "video_style": _arg_str(args, "video_style", "auto"),
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
    """Estimate generation cost for a run."""
    pricing = KLING_PRICING
    img_price = pricing["image"].get(mode, 0.05)
    vid_price = pricing["video"].get(mode, 0.30)

    best_of = max(1, int(best_of or 1))
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
    """Estimate how many segments image-first should cover."""
    if not enabled or num_clips <= 0:
        return 0
    mode = (mode or "standard").lower()
    if mode == "minimal":
        return 1
    if mode == "full":
        return num_clips
    return min(num_clips, 2)


def _get_cost_budget_limit() -> float:
    """Read the per-run cost budget."""
    try:
        return float(QUALITY_GATE_CONFIG.get("cost_control", {}).get("max_budget", 100.0))
    except Exception:
        return 100.0


def _auto_downgrade_enabled() -> bool:
    """Read whether auto downgrade is enabled."""
    try:
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
    """Apply conservative cost reductions before generation."""
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


def save_template(product_info: dict, args: argparse.Namespace, output_path: Path):
    """Save a template to JSON."""
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
            "video_style": getattr(args, "video_style", "auto"),
            "video_style_source": getattr(args, "video_style_source", "auto"),
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
    """Load a template from JSON."""
    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    product_info = template.get("product_info", {})
    args_dict = template.get("args", {})

    args_dict.setdefault("dual_output", False)
    args_dict.setdefault("image_fidelity", DEFAULT_IMAGE_FIDELITY)
    args_dict.setdefault("human_fidelity", DEFAULT_HUMAN_FIDELITY)
    args_dict.setdefault("seed", None)
    args_dict.setdefault("product_image", None)
    args_dict.setdefault("allow_no_product_image", False)
    args_dict.setdefault("video_style", "auto")
    args_dict.setdefault("video_style_source", "auto")
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
