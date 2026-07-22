#!/usr/bin/env python3
"""
AI 口播配音模块

功能：
- 生成口播音频（优先火山引擎大模型 TTS，自动降级到 macOS say）
- 根据字幕生成口播文案
- 自动对齐字幕和语音时间轴
- 支持多种音色选择

依赖：
- 火山引擎 TTS（推荐）：需在 .env 中配置 VOLC_APP_ID + VOLC_ACCESS_TOKEN
- macOS say 命令（离线 fallback，免费）
- pyttsx3（非 macOS fallback，需 pip install pyttsx3）

音色优先级：火山引擎 TTS → macOS say → pyttsx3
"""

import subprocess
import re
import shutil
import hashlib
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime


# ============================================================
# 口播文案模板
# ============================================================

VOICEOVER_TEMPLATES = {
    "standard": {
        "name": "标准带货",
        "script": [
            {"segment": 0, "text": "你是不是也有这样的烦恼？"},
            {"segment": 1, "text": "直到我发现了这个宝藏好物！"},
            {"segment": 2, "text": "{selling_point}，效果真的好！"},
            {"segment": 3, "text": "用完之后整个人都不一样了！"},
            {"segment": 4, "text": "赶紧点击左下角，值得入手！"},
        ],
    },
    "emotional": {
        "name": "情感共鸣",
        "script": [
            {"segment": 0, "text": "有没有人和我一样，一直在寻找...？"},
            {"segment": 1, "text": "终于，让我遇到了它。"},
            {"segment": 2, "text": "{selling_point}，这就是我想要的。"},
            {"segment": 3, "text": "那种满足感，真的无法用语言形容。"},
            {"segment": 4, "text": "相信我，你也会喜欢它的。"},
        ],
    },
    "energetic": {
        "name": "激情喊麦",
        "script": [
            {"segment": 0, "text": "家人们！今天给你们分享一个好物！"},
            {"segment": 1, "text": "就是这个！真的很不错！"},
            {"segment": 2, "text": "{selling_point}！大家快来看！"},
            {"segment": 3, "text": "我跟你说，用过之后真的很好！"},
            {"segment": 4, "text": "费用有限，先到先得！点击左下角！"},
        ],
    },
    "professional": {
        "name": "专业测评",
        "script": [
            {"segment": 0, "text": "今天给大家测评一款最近很受关注的产品。"},
            {"segment": 1, "text": "说实话，一开始我是持怀疑态度的。"},
            {"segment": 2, "text": "但是用了之后，{selling_point}，确实有点东西。"},
            {"segment": 3, "text": "综合来看，表现超出预期。"},
            {"segment": 4, "text": "感兴趣的朋友可以了解一下。"},
        ],
    },
    "storytelling": {
        "name": "故事叙述",
        "script": [
            {"segment": 0, "text": "那段时间，我真的很困扰。"},
            {"segment": 1, "text": "偶然的机会，朋友推荐了这个给我。"},
            {"segment": 2, "text": "没想到，{selling_point}，改变了我的生活。"},
            {"segment": 3, "text": "现在的我，每天都很开心。"},
            {"segment": 4, "text": "分享给你们，希望也能帮到你。"},
        ],
    },
}

# 默认口播风格
DEFAULT_VOICEOVER_STYLE = "standard"


# ============================================================
# 音色配置
# ============================================================

VOICE_PRESETS = {
    "female_young": {
        "name": "年轻女声",
        "voice": "Tingting",
        "volc_voice_type": "zh_female_shuangkuaisisi_uranus_bigtts",
        "rate": 180,
        "pitch": 1.0,
    },
    "female_warm": {
        "name": "温暖女声",
        "voice": "Meijia",
        "volc_voice_type": "zh_female_wenroushunv_uranus_bigtts",
        "rate": 160,
        "pitch": 1.0,
    },
    "male_pro": {
        "name": "专业男声",
        "voice": "Yunyang",
        "volc_voice_type": "zh_male_ruyaqingnian_uranus_bigtts",
        "rate": 170,
        "pitch": 1.0,
    },
    "male_magnetic": {
        "name": "磁性男声",
        "voice": "Tingting",
        "volc_voice_type": "zh_male_qingcang_uranus_bigtts",
        "rate": 150,
        "pitch": 0.95,
    },
    "energetic_female": {
        "name": "活力女声",
        "voice": "Tingting",
        "volc_voice_type": "zh_female_tianmeixiaoyuan_uranus_bigtts",
        "rate": 200,
        "pitch": 1.05,
    },
}

# 默认音色
DEFAULT_VOICE = "female_young"


def recommend_voice_for_narration(
    product_info: Dict[str, any],
    script_lines: List[Dict[str, any]],
    requested_voice: str = "auto",
    creative_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Select a stable commercial voice from product type and final spoken copy."""
    material_driven = str((creative_profile or {}).get("source") or "") in {
        "local_asset_analysis",
        "selected_local_assets",
    }
    if requested_voice in VOICE_PRESETS and not material_driven:
        return requested_voice, "用户显式指定音色"
    category = str(product_info.get("type") or product_info.get("category") or "default")
    text = "".join(str(line.get("text") or "") for line in script_lines)
    visual_energy = str((creative_profile or {}).get("energy") or "medium")
    story_role_counts = (creative_profile or {}).get("story_role_counts") or {}
    source_story_count = sum(
        int(story_role_counts.get(role) or 0)
        for role in ("ingredient", "origin", "production")
    )
    if category in {"食品", "饮品", "美食", "food", "beverage"}:
        if material_driven and source_story_count:
            return "female_young", f"素材包含原料或产地叙事，选择自然清晰而不过度煽情的年轻女声"
        if visual_energy == "high":
            return "energetic_female", f"{category}高动态素材优先清晰有推动力的活力女声"
        if visual_energy == "low":
            return "female_warm", f"{category}低动态素材优先自然连贯的温暖女声"
        return "female_young", f"{category}带货短句优先自然清晰的年轻女声"
    if material_driven and source_story_count:
        return "female_young", "素材包含原料或产地叙事，选择自然清晰的商业女声"
    if category in {"科技", "金融", "教育", "tech", "finance"}:
        return "male_pro", f"{category}信息密度较高 优先专业男声"
    if len(text) >= 90 or re.search(r"故事|回忆|陪伴|温柔", text):
        return "female_warm", "长叙事或温和语气优先温暖女声"
    if re.search(r"冲|快|立刻|现在|惊喜|挑战", text):
        return "energetic_female", "高能行动型文案优先活力女声"
    return DEFAULT_VOICE, "通用短视频口播优先自然清晰音色"

# 火山引擎 TTS V3 配置（从环境变量读取，未配置时自动降级）
# 接口文档：https://www.volcengine.com/docs/6561/1257544
_VOLC_API_URL: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_VOLC_RESOURCE_ID_DEFAULT: str = "seed-tts-2.0"  # 豆包语音合成大模型 2.0


def _load_volc_credentials() -> tuple[str, str]:
    """
    从环境变量或 .env 文件加载火山引擎 TTS V3 凭据。

    Returns:
        (api_key, resource_id) — 任一为空则表示未配置，调用方应降级
    """
    import os
    api_key = os.getenv("VOLC_API_KEY", "")
    resource_id = os.getenv("VOLC_RESOURCE_ID", _VOLC_RESOURCE_ID_DEFAULT)
    if not api_key:
        # 尝试从项目根目录 .env 文件读取
        try:
            env_path = Path(__file__).parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("VOLC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip()
                    elif line.startswith("VOLC_RESOURCE_ID="):
                        resource_id = line.split("=", 1)[1].strip()
        except Exception:
            pass
    return api_key, resource_id


def build_tts_synthesis_contract(text: str, voice: str = DEFAULT_VOICE) -> Dict[str, Any]:
    """Return the non-sensitive inputs that fully identify a TTS master request."""
    voice_config = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
    _, resource_id = _load_volc_credentials()
    payload = {
        "version": 1,
        "engine": "volcengine_tts_v3",
        "resource_id": resource_id,
        "text": re.sub(r"\s+", " ", str(text or "")).strip(),
        "voice": voice,
        "speaker": voice_config.get("volc_voice_type"),
        "rate": int(voice_config.get("rate", 180)),
        "pitch": float(voice_config.get("pitch", 1.0)),
        "audio_format": "m4a_aac_24khz_128k_mono",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _validate_audio_file(audio_path: Path, min_size: int = 1024) -> None:
    """验证音频文件存在、非空且 ffprobe 可解码。"""
    if not audio_path.exists():
        raise RuntimeError(f"音频文件不存在：{audio_path}")
    if audio_path.stat().st_size < min_size:
        raise RuntimeError(f"音频文件过小，可能生成失败：{audio_path}")
    if not shutil.which("ffprobe"):
        return
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
        raise RuntimeError(f"音频文件不可解码：{audio_path}")


def generate_voiceover_script(
    product_info: dict,
    style: str = DEFAULT_VOICEOVER_STYLE,
    clip_duration: int = 5,
    num_clips: int = 5,
) -> List[Dict[str, any]]:
    """
    生成口播文案列表

    Args:
        product_info: 产品信息字典
        style: 口播风格
        clip_duration: 单片段时长（秒）
        num_clips: 片段数量

    Returns:
        口播列表，每个元素包含 text/start/end/segment
    """
    template = VOICEOVER_TEMPLATES.get(style, VOICEOVER_TEMPLATES[DEFAULT_VOICEOVER_STYLE])
    selling_point = product_info.get("selling_point", "核心卖点")

    lines = []
    for item in template["script"]:
        seg_idx = item.get("segment", 0)
        if seg_idx >= num_clips:
            continue
        text = item["text"].format(selling_point=selling_point)
        seg_start = seg_idx * clip_duration
        start = seg_start + clip_duration * 0.1  # 每段开头留 10% 空白
        end = seg_start + clip_duration * 0.85  # 每段结尾留 15% 空白
        lines.append({
            "text": text,
            "start": start,
            "end": end,
            "segment": seg_idx,
        })

    return lines


def generate_tts_audio(
    text: str,
    output_path: Path,
    voice: str = DEFAULT_VOICE,
    rate: Optional[int] = None,
) -> Path:
    """
    生成单段 TTS 音频。

    优先级链：
    1. 火山引擎大模型 TTS V3（需在 .env 配置 VOLC_API_KEY）
    2. macOS say 命令（离线，仅 macOS）
    3. pyttsx3 跨平台引擎（需 pip install pyttsx3）

    Args:
        text: 要合成的文本
        output_path: 输出音频路径（.m4a / .wav）
        voice: 音色名称（VOICE_PRESETS 中的 key）
        rate: 语速（词/分钟），None 则使用预设值

    Returns:
        输出文件路径

    Raises:
        RuntimeError: 所有 TTS 引擎均不可用或生成失败
    """
    import sys

    voice_config = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
    actual_rate = rate if rate is not None else voice_config["rate"]
    # P1 修复：读取 pitch 字段（默认 1.0 = 正常音调）
    actual_pitch: float = voice_config.get("pitch", 1.0)

    # 优先：火山引擎大模型 TTS V3
    volc_api_key, volc_resource_id = _load_volc_credentials()
    if volc_api_key:
        try:
            return _generate_tts_volcengine(
                text=text,
                output_path=output_path,
                speaker=voice_config.get("volc_voice_type", "zh_female_shuangkuaisisi_uranus_bigtts"),
                api_key=volc_api_key,
                resource_id=volc_resource_id,
                # speech_rate: V3 范围 -50~100，映射自 wpm
                # 180 wpm → 0，220 wpm → ~22，150 wpm → ~-17
                speech_rate=int((actual_rate - 180) / 2),
                # P1 修复：pitch 映射到 pitch_rate：1.0→0, 1.1→10, 0.9→-10
                pitch_rate=int((actual_pitch - 1.0) * 100),
            )
        except Exception as e:
            print(f"  ⚠️  火山 TTS 失败（{e}）")
            raise RuntimeError(
                f"火山 TTS 失败且无高质量 fallback 可用：{e}\n"
                "口播已跳过，请在 .env 中配置 VOLC_API_KEY 后重试。"
            ) from e

    # P1 修复：无火山 Token 时直接抛出，而非静默降级到 macOS say / pyttsx3
    # macOS say 和 pyttsx3 均为机械音，在正式成片中音质极差，会严重拉低成片观感。
    # 抛出异常让调用方（run_generation_pipeline）捕获后跳过口播，不阻断主流程。
    raise RuntimeError(
        "未配置火山引擎 TTS（VOLC_API_KEY 为空），口播功能不可用。\n"
        "请在 .env 中配置 VOLC_API_KEY，或不传 --voiceover 参数。\n"
        "macOS say / pyttsx3 降级已禁用（机械音会拉低成片质量）。"
    )


def _generate_tts_volcengine(
    text: str,
    output_path: Path,
    speaker: str,
    api_key: str,
    resource_id: str = _VOLC_RESOURCE_ID_DEFAULT,
    speech_rate: int = 0,
    pitch_rate: int = 0,
) -> Path:
    """
    使用火山引擎豆包大模型 TTS V3 单向流式接口生成音频。

    接口：POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
    协议：NDJSON 行格式，每行一个 JSON，data 字段为 base64 编码的音频，code=20000000 表示结束。

    Args:
        text: 合成文本
        output_path: 输出路径（.mp3 或 .m4a）
        speaker: 豆包音色 ID（TTS 2.0 音色以 _uranus_bigtts 结尾）
        api_key: 火山引擎 API Key（X-Api-Key 头）
        resource_id: 模型版本，默认 seed-tts-2.0
        speech_rate: 语速，范围 -50~100（0 为正常速，100 为 2x，-50 为 0.5x）
        pitch_rate: 音调，范围 -50~100

    Returns:
        输出文件路径

    Raises:
        RuntimeError: API 调用失败或返回非 2xx 状态码
    """
    import uuid
    import base64
    try:
        import requests as _requests
    except ImportError:
        raise RuntimeError("火山 TTS 需要 requests 库：pip install requests")

    # 前置校验：确保文本有可读内容，避免 No readable text! 错误
    import re as _re
    _clean_text = text.strip()
    # 去掉所有常见中英文标点和空白，看是否还有文字内容
    _punct_pattern = (
        r'[。！？；，、：\u201c\u201d\u2018\u2019\uff08\uff09'
        r'\u3010\u3011\u300a\u300b\u2026\u2014'
        r' \t\n\r,.!?;:\'\"()\[\]<>]'
    )
    _only_punct = _re.sub(_punct_pattern, '', _clean_text)
    if not _only_punct:
        raise RuntimeError(f"火山 TTS V3 合成失败：文本无可读内容（仅标点或空）：{repr(text[:50])}")

    speech_rate = max(-50, min(speech_rate, 100))
    pitch_rate = max(-50, min(pitch_rate, 100))

    payload = {
        "user": {
            "uid": "kling_ad_automation",
        },
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "bit_rate": 128000,
                "speech_rate": speech_rate,
                "pitch_rate": pitch_rate,
            },
        },
    }

    mp3_path = output_path.with_suffix(".mp3")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        headers = {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        try:
            with _requests.post(
                _VOLC_API_URL,
                json=payload,
                headers=headers,
                stream=True,
                timeout=60,
            ) as resp:
                if resp.status_code != 200:
                    try:
                        err = resp.json()
                        msg = f"code={err.get('code')} message={err.get('message')}"
                    except Exception:
                        msg = resp.text[:200]
                    error = RuntimeError(f"火山 TTS V3 请求失败 HTTP {resp.status_code}: {msg}")
                    if resp.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                        raise error
                    last_error = error
                    wait = 2 ** attempt
                    print(f"  ⚠️  火山 TTS 暂时失败，{wait}s 后重试（{attempt}/3）")
                    import time
                    time.sleep(wait)
                    continue

                # 解析 NDJSON 行格式响应，拼接 base64 音频数据
                audio_bytes = bytearray()
                final_code = None
                final_msg = ""
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        data = _parse_json_line(line)
                    except Exception:
                        continue
                    code = data.get("code")
                    if code == 20000000:
                        final_code = code
                        break
                    if code is not None and code != 0:
                        final_code = code
                        final_msg = data.get("message", f"code={code}")
                        break
                    chunk_b64 = data.get("data")
                    if chunk_b64:
                        try:
                            audio_bytes.extend(base64.b64decode(chunk_b64))
                        except Exception:
                            pass

                if final_code != 20000000:
                    error = RuntimeError(
                        f"火山 TTS V3 合成失败：{final_msg or f'code={final_code}'}"
                    )
                    if attempt == 3:
                        raise error
                    last_error = error
                    wait = 2 ** attempt
                    print(f"  ⚠️  火山 TTS 合成失败，{wait}s 后重试（{attempt}/3）")
                    import time
                    time.sleep(wait)
                    continue

                if len(audio_bytes) < 100:
                    error = RuntimeError("火山 TTS V3 返回音频过小，请检查 API Key 和音色 ID")
                    if attempt == 3:
                        raise error
                    last_error = error
                    wait = 2 ** attempt
                    print(f"  ⚠️  火山 TTS 音频异常，{wait}s 后重试（{attempt}/3）")
                    import time
                    time.sleep(wait)
                    continue

                mp3_path.write_bytes(audio_bytes)
            break
        except _requests.exceptions.RequestException as e:
            last_error = e
            mp3_path.unlink(missing_ok=True)
            if attempt == 3:
                raise RuntimeError(f"火山 TTS V3 请求失败，已重试 3 次：{e}") from e
            wait = 2 ** attempt
            print(f"  ⚠️  火山 TTS 请求异常，{wait}s 后重试（{attempt}/3）")
            import time
            time.sleep(wait)
    else:
        raise RuntimeError(f"火山 TTS V3 请求失败：{last_error}")

    _validate_audio_file(mp3_path)

    if output_path.suffix != ".mp3" and shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-c:a", "aac", "-b:a", "128k",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        finally:
            if mp3_path.exists() and mp3_path != output_path:
                mp3_path.unlink()
    else:
        if mp3_path != output_path:
            shutil.move(str(mp3_path), str(output_path))

    _validate_audio_file(output_path)
    return output_path


def _parse_json_line(line: str) -> dict:
    """解析 V3 接口的 NDJSON 行，兼容可能的前缀/后缀字符。"""
    line = line.strip()
    if not line:
        return {}
    import json
    try:
        return json.loads(line)
    except Exception:
        start = line.find("{")
        end = line.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(line[start : end + 1])
            except Exception:
                pass
    return {}


def _generate_tts_macos(
    text: str,
    output_path: Path,
    voice_name: str,
    rate: int,
    pitch: float = 1.0,  # P1 修复：新增 pitch 参数
) -> Path:
    """使用 macOS say 命令生成 TTS 音频"""
    aiff_path = output_path.with_suffix(".aiff")
    # P1 修复：say -p 范围 30-65，默认约50。pitch=1.0→0偏移，每 0.1 对应 5点
    say_pitch = int(50 + (pitch - 1.0) * 50)
    say_pitch = max(30, min(65, say_pitch))
    cmd = ["say", "-v", voice_name, "-r", str(rate), "-p", str(say_pitch), "-o", str(aiff_path), text]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"macOS say 生成失败: {e.stderr.decode() if e.stderr else str(e)}")

    # 转码为 m4a（aac 编码，体积更小）
    if shutil.which("ffmpeg"):
        cmd_transcode = [
            "ffmpeg", "-y",
            "-i", str(aiff_path),
            "-c:a", "aac", "-b:a", "128k",
            str(output_path),
        ]
        try:
            subprocess.run(cmd_transcode, check=True, capture_output=True, timeout=30)
        finally:
            if aiff_path.exists():
                aiff_path.unlink()
    else:
        shutil.move(str(aiff_path), str(output_path))

    return output_path


def _generate_tts_pyttsx3(
    text: str,
    output_path: Path,
    rate: int,
    pitch: float = 1.0,  # P1 修复：新增 pitch 参数
) -> Path:
    """
    使用 pyttsx3 跨平台引擎生成 TTS 音频（Windows / Linux fallback）。

    输出为 WAV 格式（pyttsx3 原生支持），如有 ffmpeg 则转为 m4a。
    """
    try:
        import pyttsx3  # pip install pyttsx3
    except ImportError:
        raise RuntimeError(
            "非 macOS 环境需要安装 pyttsx3 才能使用 TTS 口播功能。\n"
            "安装命令：pip install pyttsx3\n"
            "或关闭口播功能：去掉 --voiceover 参数"
        )

    # pyttsx3 只能输出 WAV，先写到临时文件
    wav_path = output_path.with_suffix(".wav")

    try:
        engine = pyttsx3.init()
        # rate: pyttsx3 单位是词/分钟，与 say 一致
        engine.setProperty("rate", rate)
        # P1 修复：设置 pitch（pyttsx3 的 pitch 属性对部分引擎有效）
        try:
            engine.setProperty("pitch", pitch)
        except Exception:
            pass  # 引擎不支持 pitch 时静默跳过
        engine.save_to_file(text, str(wav_path))
        engine.runAndWait()
        engine.stop()
    except Exception as e:
        raise RuntimeError(f"pyttsx3 TTS 生成失败: {e}")

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError("pyttsx3 未生成音频文件，请检查系统 TTS 引擎是否已安装")

    # 转码为 m4a
    if shutil.which("ffmpeg") and output_path.suffix != ".wav":
        cmd_transcode = [
            "ffmpeg", "-y",
            "-i", str(wav_path),
            "-c:a", "aac", "-b:a", "128k",
            str(output_path),
        ]
        try:
            subprocess.run(cmd_transcode, check=True, capture_output=True, timeout=30)
        finally:
            if wav_path.exists() and wav_path != output_path:
                wav_path.unlink()
    else:
        shutil.move(str(wav_path), str(output_path))

    return output_path


def split_sentences(text: str, max_chars: int = 11) -> List[str]:
    """
    智能断句：将长文本按标点切成短句

    优先按句末标点（。！？；）切分，过长的句子再按逗号细分，
    确保每句字数适中，TTS 发音更自然，字幕显示更有节奏感。

    Args:
        text: 原始文本
        max_chars: 单句最大字数（超过则继续按逗号细分）

    Returns:
        短句列表
    """
    if not text or not text.strip():
        return []

    import re

    # 第一步：按句末标点切分（保留标点）
    sentences = []
    parts = re.split(r'([。！？；])', text.strip())
    current = ""
    for part in parts:
        if part in '。！？；':
            current += part
            if current.strip():
                sentences.append(current.strip())
            current = ""
        else:
            current += part
    if current.strip():
        sentences.append(current.strip())

    # 第二步：过长的句子按逗号/顿号再细分
    result = []
    for sent in sentences:
        if len(sent) <= max_chars:
            result.append(sent)
        else:
            sub_parts = re.split(r'([，、：])', sent)
            sub_sent = ""
            for p in sub_parts:
                if p in '，、：':
                    sub_sent += p
                    if len(sub_sent) >= max_chars * 0.6 and sub_sent.strip():
                        result.append(sub_sent.strip())
                        sub_sent = ""
                else:
                    sub_sent += p
            if sub_sent.strip():
                result.append(sub_sent.strip())

    # 过滤空句和纯标点句子（火山 TTS 对纯标点文本报 No readable text!）
    import re as _re
    _punct_pattern_split = (
        r'[。！？；，、：\u201c\u201d\u2018\u2019\uff08\uff09'
        r'\u3010\u3011\u300a\u300b\u2026\u2014'
        r' \t\n\r,.!?;:\'\"()\[\]<>]'
    )
    def _has_readable_content(s: str) -> bool:
        stripped = s.strip()
        if not stripped:
            return False
        # 去掉所有常见中英文标点和空白，看是否还有文字内容
        cleaned = _re.sub(_punct_pattern_split, '', stripped)
        return bool(cleaned)

    readable = [s for s in result if _has_readable_content(s)]
    from video_merger import _split_single_line_text
    return [chunk for sentence in readable for chunk in _split_single_line_text(sentence, max_chars)]


def generate_full_voiceover(
    script_lines: List[Dict[str, any]],
    output_path: Path,
    voice: str = DEFAULT_VOICE,
    total_duration: float = 25.0,
    pause_between_sentences: float = 0.15,
    max_rate_multiplier: float = 1.6,
    continuous_narration: bool = False,
    continuous_text: Optional[str] = None,
    performance_profile: Optional[Dict[str, Any]] = None,
    pre_generated_audio: Optional[Path] = None,
) -> Tuple[Path, List[Dict[str, any]]]:
    """
    生成完整的口播音频（智能断句 + 多段拼接 + 自动时间对齐）

    Args:
        script_lines: 口播文案列表（含 text/start/end）
        output_path: 输出音频路径
        voice: 音色
        total_duration: 总时长（秒），用于生成空白底噪
        pause_between_sentences: 句子间停顿时间（秒）
        max_rate_multiplier: 最大语速倍率（溢出时加速，默认 1.6 倍）

    Returns:
        (输出文件路径, 对齐后的字幕列表)
    """
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_"))
    voice_config = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
    base_rate = voice_config["rate"]
    if performance_profile is not None:
        performance_profile.update({
            "voice": voice,
            "base_rate": base_rate,
            "tempo_multiplier": 1.0,
        })

    try:
        if continuous_narration:
            return _generate_continuous_voiceover(
                script_lines=script_lines,
                output_path=output_path,
                tmp_dir=tmp_dir,
                voice=voice,
                total_duration=total_duration,
                max_rate_multiplier=max_rate_multiplier,
                continuous_text=continuous_text,
                performance_profile=performance_profile,
                pre_generated_audio=pre_generated_audio,
            )
        # ============== 第一轮：正常语速生成，检测是否溢出 ==============
        audio_segments = []
        aligned_subtitles = []
        seg_counter = 0
        overflow_ratio = 1.0  # 溢出比例（>1 表示需要加速）
        has_ffmpeg = shutil.which("ffmpeg") is not None

        for line_idx, line in enumerate(script_lines):
            text = line["text"]
            target_start = line["start"]
            seg_num = line.get("segment", line_idx)

            sentences = split_sentences(text)
            if not sentences:
                continue

            current_time = target_start

            for sent_idx, sentence in enumerate(sentences):
                seg_path = tmp_dir / f"seg_{seg_counter:03d}.m4a"
                seg_counter += 1

                generate_tts_audio(sentence, seg_path, voice=voice)
                duration = _get_audio_duration(seg_path)
                actual_end = current_time + duration

                audio_segments.append({
                    "path": seg_path,
                    "start": current_time,
                    "duration": duration,
                    "sentence": sentence,
                    "segment": seg_num,
                    "line_start": float(target_start),
                    "line_end": float(line.get("end", total_duration)),
                })

                aligned_subtitles.append({
                    "text": sentence,
                    "start": current_time,
                    "end": actual_end,
                    "segment": seg_num,
                })

                if sent_idx < len(sentences) - 1:
                    current_time = actual_end + pause_between_sentences
                else:
                    current_time = actual_end

            # P1 修复：增加单段边界检查，防止口播超出该段分配的时间窗口
            _line_end = line.get("end", total_duration * 0.95)
            if _line_end > 0 and current_time > _line_end:
                _seg_ratio = current_time / _line_end
                overflow_ratio = max(overflow_ratio, _seg_ratio)

            if current_time > total_duration * 0.95:
                needed_ratio = current_time / (total_duration * 0.95)
                overflow_ratio = max(overflow_ratio, needed_ratio)

        # ============== 如果溢出：用 ffmpeg atempo 加速已有音频（比重新生成快很多） ==============
        if overflow_ratio > 1.0 and has_ffmpeg:
            segment_groups: Dict[Any, List[Dict[str, any]]] = {}
            for segment in audio_segments:
                segment_groups.setdefault(segment.get("segment"), []).append(segment)
            rate_by_segment: Dict[Any, float] = {}
            for segment_id, group in segment_groups.items():
                line_start = float(group[0]["line_start"])
                line_end = float(group[0]["line_end"])
                available = max(0.1, min(line_end, total_duration - 0.1) - line_start)
                spoken = sum(float(item["duration"]) for item in group)
                spoken += pause_between_sentences * max(0, len(group) - 1)
                required_rate = max(1.0, spoken / available)
                if required_rate > max_rate_multiplier:
                    raise RuntimeError(
                        f"口播段 {segment_id} 在最大 {max_rate_multiplier:.1f}x 语速下仍无法装入镜头："
                        f"需要 {required_rate:.2f}x"
                    )
                rate_by_segment[segment_id] = required_rate

            max_rate = max(rate_by_segment.values(), default=1.0)
            print(f"  ⚡ 口播时长溢出，按镜头段独立加速（最高 {max_rate:.2f}x）")

            new_segments = []
            new_subtitles = []
            # P0 修复：atempo 加速后必须重新计算时间轴，基于前一段实际结束时间顺序排列，
            # 否则使用原始 seg["start"] 会导致段间出现与压缩比例成正比的累积空隙，
            # 字幕与音频严重错位。
            current_start = float(audio_segments[0]["start"]) if audio_segments else 0.0
            current_segment = audio_segments[0].get("segment") if audio_segments else None

            for i, seg in enumerate(audio_segments):
                if seg.get("segment") != current_segment:
                    current_segment = seg.get("segment")
                    current_start = float(seg.get("line_start", current_start))
                atempo = rate_by_segment.get(current_segment, 1.0)
                fast_path = tmp_dir / f"fast_{i:03d}.m4a"
                try:
                    cmd = [
                        "ffmpeg", "-y", "-i", str(seg["path"]),
                        "-filter:a", f"atempo={atempo}",
                        "-vn", "-c:a", "aac", "-b:a", "128k",
                        str(fast_path),
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=10, check=True)
                    new_duration = _get_audio_duration(fast_path)
                except Exception:
                    fast_path = seg["path"]
                    new_duration = seg["duration"]

                sub = aligned_subtitles[i]
                new_start = current_start
                new_end = new_start + new_duration
                line_end = min(float(seg.get("line_end", total_duration)), total_duration - 0.1)
                if new_end > line_end + 0.08:
                    raise RuntimeError(
                        f"口播段 {current_segment} 编码后仍越过镜头边界："
                        f"{new_end:.2f}s > {line_end:.2f}s"
                    )

                new_segments.append({
                    "path": fast_path,
                    "start": new_start,
                    "duration": new_duration,
                })
                new_subtitles.append({
                    "text": sub["text"],
                    "start": new_start,
                    "end": new_end,
                    "segment": sub.get("segment", 0),
                })

                current_start = new_end

            audio_segments = new_segments
            aligned_subtitles = new_subtitles

        if shutil.which("ffmpeg") and audio_segments:
            _mix_audio_segments(audio_segments, output_path, total_duration)
        else:
            _simple_concat(audio_segments, output_path)

        return output_path, aligned_subtitles

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _generate_continuous_voiceover(
    script_lines: List[Dict[str, any]],
    output_path: Path,
    tmp_dir: Path,
    voice: str,
    total_duration: float,
    max_rate_multiplier: float,
    continuous_text: Optional[str] = None,
    performance_profile: Optional[Dict[str, Any]] = None,
    pre_generated_audio: Optional[Path] = None,
) -> Tuple[Path, List[Dict[str, any]]]:
    """Synthesize one authored narration and keep captions locked to shot windows."""
    readable_lines = [line for line in script_lines if str(line.get("text") or "").strip()]
    if not readable_lines:
        raise RuntimeError("连续口播没有可读文案")
    full_text = re.sub(r"\s+", " ", str(continuous_text or "")).strip()
    if not full_text:
        raise RuntimeError("连续口播缺少显式完整文案，禁止把分镜短句拼接为一条伪连续口播")
    raw_path = Path(pre_generated_audio) if pre_generated_audio else tmp_dir / "continuous_raw.m4a"
    if pre_generated_audio:
        if not raw_path.is_file():
            raise RuntimeError(f"预生成连续口播不存在：{raw_path}")
    else:
        generate_tts_audio(full_text, raw_path, voice=voice)
    raw_duration = _get_audio_duration(raw_path)
    start = max(0.0, float(readable_lines[0].get("start", 0.0)))
    requested_end = float(readable_lines[-1].get("end", total_duration - 0.1))
    available = max(0.1, min(requested_end, total_duration - 0.1) - start)
    raw_ratio = raw_duration / available
    if pre_generated_audio:
        tempo_multiplier = 1.0
    elif raw_ratio > 1.0:
        tempo_multiplier = raw_ratio
    elif raw_ratio < 0.97:
        tempo_multiplier = max(0.9, raw_ratio)
    else:
        tempo_multiplier = 1.0
    if tempo_multiplier > max_rate_multiplier:
        raise RuntimeError(
            f"连续口播在最大 {max_rate_multiplier:.1f}x 语速下仍无法装入视频："
            f"需要 {tempo_multiplier:.2f}x"
        )
    if performance_profile is not None:
        performance_profile.update({
            "mode": "single_take",
            "tts_requests": 1,
            "tts_reused": bool(pre_generated_audio),
            "continuous_text": full_text,
            "tempo_multiplier": round(tempo_multiplier, 4),
        })

    audio_path = raw_path
    spoken_duration = raw_duration
    if abs(tempo_multiplier - 1.0) > 0.001:
        audio_path = tmp_dir / "continuous_timed.m4a"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(raw_path),
                "-filter:a", f"atempo={tempo_multiplier:.4f}",
                "-vn", "-c:a", "aac", "-b:a", "192k", str(audio_path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        spoken_duration = _get_audio_duration(audio_path)
        action = "加速" if tempo_multiplier > 1.0 else "自然放慢"
        print(f"  ⚡ 连续口播整体{action}到 {tempo_multiplier:.2f}x")

    spoken_audio_end = _detect_spoken_audio_end(audio_path, spoken_duration)
    speech_end = start + spoken_audio_end
    trailing_gap = max(0.0, min(requested_end, total_duration - 0.1) - speech_end)
    if performance_profile is not None:
        performance_profile.update({
            "spoken_audio_end": round(spoken_audio_end, 4),
            "speech_end": round(speech_end, 4),
            "trailing_gap": round(trailing_gap, 4),
        })
    if trailing_gap > 0.85:
        raise RuntimeError(
            f"单条连续口播尾部缺少口播：画面合同结束前仍有 {trailing_gap:.2f}s 空白"
        )

    _mix_audio_segments(
        [{"path": audio_path, "start": start, "duration": spoken_duration}],
        output_path,
        total_duration,
    )

    return output_path, split_and_align_voiceover_subtitles(
        readable_lines,
        audio_path,
        start + spoken_duration,
    )


def _detect_spoken_audio_end(audio_path: Path, duration: float) -> float:
    """Measure the last non-silent sample in an unpadded TTS performance."""
    if not audio_path.exists() or shutil.which("ffmpeg") is None:
        return duration
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(audio_path),
            "-af", "silencedetect=noise=-42dB:d=0.18", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return duration
    events = [
        (kind, float(value))
        for kind, value in re.findall(
            r"silence_(start|end):\s*([0-9.]+)",
            result.stderr or "",
        )
    ]
    if not events or events[-1][0] != "start":
        return duration
    last_silence_start = events[-1][1]
    return last_silence_start if duration - last_silence_start <= 3.0 else duration


def _detect_audio_pause_intervals(
    audio_path: Path,
    duration: float,
    minimum_duration: float = 0.12,
) -> List[Tuple[float, float]]:
    """Return closed, internal silence intervals measured from one TTS master."""
    return _detect_audio_alignment(audio_path, duration, minimum_duration)["pauses"]


def _detect_audio_alignment(
    audio_path: Path,
    duration: float,
    minimum_duration: float = 0.12,
) -> Dict[str, Any]:
    """Measure spoken bounds and internal pauses from the same TTS performance."""
    fallback = {"speech_start": 0.0, "speech_end": float(duration), "pauses": []}
    if not audio_path.exists() or shutil.which("ffmpeg") is None:
        return fallback
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(audio_path),
            "-af", f"silencedetect=noise=-42dB:d={minimum_duration}", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return fallback
    open_start: Optional[float] = None
    closed: List[Tuple[float, float]] = []
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    for kind, value in re.findall(r"silence_(start|end):\s*([0-9.]+)", stderr):
        point = float(value)
        if kind == "start":
            open_start = point
        elif open_start is not None:
            closed.append((open_start, point))
            open_start = None

    speech_start = 0.0
    speech_end = float(duration)
    if closed and closed[0][0] <= 0.05:
        speech_start = min(float(duration), closed[0][1])
    trailing = open_start
    if trailing is None and closed and closed[-1][1] >= float(duration) - 0.05:
        trailing = closed[-1][0]
    if trailing is not None and float(duration) - trailing <= 3.0:
        speech_end = max(speech_start + 0.1, min(float(duration), trailing))

    pauses = [
        (start, end)
        for start, end in closed
        if start > speech_start + 0.05
        and end < speech_end - 0.05
        and end - start >= minimum_duration
    ]
    return {
        "speech_start": speech_start,
        "speech_end": speech_end,
        "pauses": pauses,
    }


def align_voiceover_lines_to_audio_pauses(
    lines: List[Dict[str, Any]],
    audio_path: Path,
    duration: float,
    preserve_container_edges: bool = False,
) -> List[Dict[str, Any]]:
    """Snap semantic cue boundaries to pauses measured in the single TTS performance."""
    if not lines:
        return []
    container_start = float(lines[0].get("start", 0.0))
    analysis = _detect_audio_alignment(audio_path, float(duration) - container_start)
    start = (
        container_start
        if preserve_container_edges else
        container_start + float(analysis["speech_start"])
    )
    end = (
        max(container_start + 0.1, float(duration))
        if preserve_container_edges else
        max(start + 0.1, container_start + float(analysis["speech_end"]))
    )
    predicted = [float(line.get("end", end)) for line in lines[:-1]]
    pause_centers = [
        container_start + sum(interval) / 2.0
        for interval in analysis["pauses"]
    ]
    tolerance = 1.2
    boundaries: List[Tuple[float, str]] = []
    candidate_index = 0
    previous = start
    for expected in predicted:
        available = [
            (index, value)
            for index, value in enumerate(pause_centers[candidate_index:], candidate_index)
            if value > previous + 0.1
        ]
        if available:
            selected_index, selected = min(available, key=lambda item: abs(item[1] - expected))
        else:
            selected_index, selected = candidate_index, expected
        if available and abs(selected - expected) <= tolerance:
            boundary = selected
            precision = "measured_audio_pause"
            candidate_index = selected_index + 1
        else:
            boundary = expected
            precision = "speech_weight_estimate"
        boundary = max(previous + 0.1, min(boundary, end - 0.1 * (len(lines) - len(boundaries) - 1)))
        boundaries.append((boundary, precision))
        previous = boundary

    aligned: List[Dict[str, Any]] = []
    cursor = start
    for index, line in enumerate(lines):
        boundary, precision = boundaries[index] if index < len(boundaries) else (end, "measured_audio_end")
        aligned.append({
            **line,
            "start": round(cursor, 4),
            "end": round(boundary, 4),
            "alignment_precision": precision,
        })
        cursor = boundary
    return aligned


def split_and_align_voiceover_subtitles(
    lines: List[Dict[str, Any]],
    audio_path: Path,
    audio_end: float,
    max_units: int = 11,
) -> List[Dict[str, Any]]:
    """Align punctuation-delimited semantic phrases to measured speech pauses."""
    from video_merger import _subtitle_units

    semantic_phrases: List[Dict[str, Any]] = []
    for line in lines:
        text = re.sub(
            r"\s+", "", str(line.get("text") or "").replace("\\N", "").replace("\n", ""),
        )
        phrases = [
            part
            for part in re.split(r"(?<=[，。！？；、：,.!?;:])", text)
            if part
        ]
        if not phrases:
            continue
        start = float(line.get("start", 0.0))
        end = float(line.get("end", start + 0.1))
        weights = [max(1.0, _subtitle_units(phrase)) for phrase in phrases]
        total_weight = sum(weights)
        cursor = start
        for index, (phrase, weight) in enumerate(zip(phrases, weights)):
            phrase_end = end if index == len(phrases) - 1 else cursor + (end - start) * weight / total_weight
            semantic_phrases.append({
                **line,
                "text": phrase,
                "start": cursor,
                "end": phrase_end,
                "semantic_phrase": True,
            })
            cursor = phrase_end

    aligned_phrases = align_voiceover_lines_to_audio_pauses(
        semantic_phrases, audio_path, audio_end,
    )
    # A semantic phrase is the smallest unit that the TTS actually performed.
    # Display layout may shrink the font, but must not invent a pause inside it.
    return aligned_phrases


def _get_audio_duration(audio_path: Path) -> float:
    """获取音频时长（秒），带 LRU 缓存"""
    from utils_ffprobe import get_audio_duration
    dur = get_audio_duration(str(audio_path))
    return dur if dur > 0 else 2.0  # 失败时默认 2 秒（兼容旧行为）


def _mix_audio_segments(
    segments: List[Dict[str, any]],
    output_path: Path,
    total_duration: float,
):
    """
    使用 ffmpeg 将多段音频混合到时间轴上

    Args:
        segments: 音频片段列表（path/start/duration）
        output_path: 输出路径
        total_duration: 总时长
    """
    # 生成极轻微的底噪（防止完全静音的起始）
    noise_path = output_path.parent / "_base_noise.m4a"
    cmd_base = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        # c=white 是白噪声，a=0.005 是极低音量（几乎听不到，仅用于填充静音）
        "-i", f"anoisesrc=d={total_duration}:c=white:r=44100:a=0.005",
        "-c:a", "aac",
        "-b:a", "128k",
        str(noise_path),
    ]
    result = subprocess.run(cmd_base, capture_output=True, timeout=30)
    noise_ok = result.returncode == 0 and noise_path.exists()

    # 构建滤镜：在指定时间点插入各段语音
    # 使用 adelay 滤镜延迟每段音频，然后 amix 混合
    filter_parts = []
    inputs = []

    if noise_ok:
        inputs.extend(["-i", str(noise_path)])
        noise_label = "[0:a]"
        mix_offset = 1
    else:
        # 底噪生成失败就不用，直接混合各段语音
        noise_label = ""
        mix_offset = 0

    for i, seg in enumerate(segments):
        inputs.extend(["-i", str(seg["path"])])
        delay_ms = int(seg["start"] * 1000)
        filter_parts.append(f"[{i+mix_offset}:a]adelay={delay_ms}:all=1[a{i+1}]")

    # 混合所有音轨
    mix_inputs = "".join(f"[a{i+1}]" for i in range(len(segments)))
    total_inputs = len(segments) + (1 if noise_ok else 0)
    # P1 修复：duration=longest 防止第一段不是最长时后续语音被截断
    filter_parts.append(
        f"{noise_label}{mix_inputs}amix=inputs={total_inputs}:duration=longest:dropout_transition=0:normalize=0[aout]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="ignore")[:500] if result.stderr else ""
            raise RuntimeError(f"口播混音失败：{stderr}")
        _validate_audio_file(output_path)
    finally:
        if noise_path.exists():
            noise_path.unlink()


def _simple_concat(segments: List[Dict[str, any]], output_path: Path):
    """简单拼接（fallback）"""
    if not segments:
        return
    # 直接用第一段
    shutil.copy2(segments[0]["path"], output_path)


def align_subtitles_to_voiceover(
    subtitles: List[Dict[str, any]],
    voiceover_subs: List[Dict[str, any]],
) -> List[Dict[str, any]]:
    """
    将字幕替换为口播时间轴，并按 segment 继承原字幕样式信息。

    口播文案与屏显短标题通常不是同一句，文本模糊匹配会同时保留两者，
    造成同一时间两行字幕重叠。启用口播时，口播时间轴是唯一字幕来源。

    Args:
        subtitles: 原始字幕列表（含 highlight / style 等字段）
        voiceover_subs: 口播对齐后的字幕列表（含准确 start/end）

    Returns:
        对齐后的字幕列表
    """
    if not voiceover_subs:
        return subtitles
    styles_by_segment = {
        int(sub.get("segment", index)): {
            key: value
            for key, value in sub.items()
            if key not in {"text", "start", "end", "segment"}
        }
        for index, sub in enumerate(subtitles)
    }
    aligned = []
    for index, spoken in enumerate(voiceover_subs):
        segment = int(spoken.get("segment", index))
        merged = dict(styles_by_segment.get(segment, {}))
        merged.update({
            key: value
            for key, value in spoken.items()
            if key not in {"text", "start", "end", "segment"}
        })
        merged.update({
            "text": str(spoken.get("text") or ""),
            "start": float(spoken["start"]),
            "end": float(spoken["end"]),
        })
        merged["segment"] = segment
        aligned.append(merged)
    return sorted(aligned, key=lambda item: item.get("start", 0))


def adjust_voiceover_to_video_duration(
    voiceover_audio: Path,
    video_duration: float,
    output_path: Optional[Path] = None,
    max_speedup: float = 1.4,
    max_slowdown: float = 0.9,
) -> Tuple[Path, float, float]:
    """
    调整口播音频时长以匹配视频时长（音视频精确同步）。

    策略：
    1. 口播比视频短：尾部补静音，保证视频结束时口播已结束
    2. 口播比视频长，但超出比例在 max_speedup 内：用 atempo 加速口播
    3. 口播比视频长很多，加速也装不下：裁剪到视频时长，末尾加淡出
    4. 口播比视频长一点点（< 0.5s）：直接裁剪，避免微小加速影响听感

    Args:
        voiceover_audio: 输入口播音频路径
        video_duration: 目标视频时长（秒）
        output_path: 输出路径，None 则自动生成
        max_speedup: 最大加速倍率（默认 1.4x，超过则裁剪）
        max_slowdown: 最大减速倍率（默认 0.9x，短于视频则补静音）

    Returns:
        (输出音频路径, 调整后时长, 调整倍率: >1表示加速, <1表示减速/裁剪)
    """
    import shutil

    if output_path is None:
        output_path = voiceover_audio.parent / f"{voiceover_audio.stem}_adjusted{voiceover_audio.suffix}"

    if not voiceover_audio.exists():
        return voiceover_audio, 0.0, 1.0

    if shutil.which("ffmpeg") is None:
        return voiceover_audio, 0.0, 1.0

    vo_duration = _get_audio_duration(voiceover_audio)
    if vo_duration <= 0:
        return voiceover_audio, 0.0, 1.0

    diff = vo_duration - video_duration

    # 情况1：口播比视频短 → 补静音（直接返回原文件，混合时视频为主）
    if diff <= 0.5:
        return voiceover_audio, vo_duration, 1.0

    speed_ratio = vo_duration / video_duration

    # 情况2：超出在加速范围内 → 用 atempo 加速
    if speed_ratio <= max_speedup:
        try:
            cmd = [
                "ffmpeg", "-y", "-i", str(voiceover_audio),
                "-filter:a", f"atempo={speed_ratio:.3f}",
                "-vn", "-c:a", "aac", "-b:a", "128k",
                str(output_path),
            ]
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            new_dur = _get_audio_duration(output_path)
            if new_dur > 0:
                return output_path, new_dur, speed_ratio
        except Exception:
            pass

    # 情况3：加速也装不下 或 加速失败 → 裁剪到视频时长，末尾淡出
    try:
        fade_duration = min(0.3, video_duration * 0.1)
        afade_start = max(0, video_duration - fade_duration)
        cmd = [
            "ffmpeg", "-y", "-i", str(voiceover_audio),
            "-filter:a", f"atrim=0:{video_duration:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={afade_start:.3f}:d={fade_duration:.3f}",
            "-vn", "-c:a", "aac", "-b:a", "128k",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        new_dur = _get_audio_duration(output_path)
        return output_path, new_dur if new_dur > 0 else video_duration, speed_ratio
    except Exception:
        return voiceover_audio, vo_duration, 1.0


def adjust_subtitles_to_duration(
    subtitles: List[Dict[str, Any]],
    target_duration: float,
    adjust_ratio: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    根据音频调整倍率同步调整字幕时间轴。

    Args:
        subtitles: 字幕列表（含 start/end）
        target_duration: 目标总时长
        adjust_ratio: 调整倍率（>1 表示加速了，字幕时间要压缩）

    Returns:
        调整后的字幕列表
    """
    if not subtitles or abs(adjust_ratio - 1.0) < 0.01:
        return subtitles

    adjusted = []
    for sub in subtitles:
        new_sub = dict(sub)
        new_sub["start"] = sub.get("start", 0) / adjust_ratio
        new_sub["end"] = sub.get("end", 0) / adjust_ratio
        # 确保不超出目标时长
        if new_sub["end"] > target_duration - 0.1:
            new_sub["end"] = target_duration - 0.1
        if new_sub["start"] < new_sub["end"]:
            adjusted.append(new_sub)

    return adjusted


def list_available_voices() -> List[Dict[str, str]]:
    """列出系统可用的中文语音"""
    voices = []
    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if "zh_" in line:
                parts = line.split("#")
                name_part = parts[0].strip() if parts else ""
                sample = parts[1].strip() if len(parts) > 1 else ""
                voices.append({
                    "name": name_part,
                    "sample": sample,
                })
    except Exception:
        pass
    return voices


if __name__ == "__main__":
    # 测试
    print("🎤 可用的中文语音：")
    for v in list_available_voices():
        print(f"  - {v['name']}: {v['sample']}")

    print("\n🎬 口播风格：")
    for key, style in VOICEOVER_TEMPLATES.items():
        print(f"  - {key}: {style['name']}")

    print("\n🎵 音色预设：")
    for key, voice in VOICE_PRESETS.items():
        print(f"  - {key}: {voice['name']} ({voice['voice']})")
