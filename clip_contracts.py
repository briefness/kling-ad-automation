"""Clip and reference cache contracts."""

import hashlib
import json
from pathlib import Path
from typing import Optional

from config import KLING_IMAGE_MODEL


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
