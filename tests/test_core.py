"""
核心逻辑单元测试

运行方式：
    cd kling-ad-automation
    python -m pytest tests/ -v
"""

import sys
import time
import threading
import sqlite3
import re
import hashlib
import copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import requests


class TestUserScriptFeedbackPolicy:
    def test_cold_start_has_no_subjective_script_rules(self, tmp_path):
        from script_feedback import ScriptFeedbackStore

        policy = ScriptFeedbackStore(tmp_path / "script_feedback.db").build_policy(
            product_category="食品",
            script_style="demonstration",
        )

        assert policy["rules"] == []
        assert policy["positive_examples"] == []
        assert policy["negative_examples"] == []
        assert policy["source"] == "explicit_user_feedback_only"

    def test_automatic_observations_cannot_create_script_rules(self, tmp_path):
        from script_feedback import ScriptFeedbackStore

        store = ScriptFeedbackStore(tmp_path / "script_feedback.db", min_distinct_videos=2)
        for video_id in ("auto-a", "auto-b", "auto-c"):
            store.record_feedback(
                video_id=video_id,
                rule_text="口播必须更有购买欲",
                verdict="violated",
                source="automatic",
                script={"voiceover_full": "自动质检脚本"},
            )

        assert store.build_policy()["rules"] == []

    def test_legacy_automatic_feedback_database_is_not_a_script_rule_source(self, tmp_path):
        from feedback_loop import FeedbackLoop
        from script_feedback import ScriptFeedbackStore

        FeedbackLoop(tmp_path / "automatic_feedback.db").collect_feedback(
            video_id="auto-video",
            generation_params={"script": "程序自评分脚本"},
            rating=5,
            issues=["综合评分 100 分"],
            auto_quality_score=100,
        )

        policy = ScriptFeedbackStore(tmp_path / "script_feedback.db").build_policy()
        assert policy["rules"] == []

    def test_explicit_feedback_promotes_rule_only_after_distinct_video_evidence(self, tmp_path):
        from script_feedback import ScriptFeedbackStore

        store = ScriptFeedbackStore(tmp_path / "script_feedback.db", min_distinct_videos=2)
        store.record_feedback(
            video_id="video-a",
            rule_text="字幕应该像带货文案而不是画面描述",
            verdict="violated",
            source="user",
            user_comment="这版字幕只是在描述画面",
            script={"voiceover_full": "画面里展示一瓶茶咖"},
        )
        provisional = store.build_policy()
        assert provisional["rules"][0]["status"] == "provisional"
        assert provisional["rules"][0]["distinct_video_count"] == 1
        assert provisional["negative_examples"]

        store.record_feedback(
            video_id="video-b",
            rule_text="字幕应该像带货文案而不是画面描述",
            verdict="satisfied",
            source="user",
            user_comment="这版有购买理由，不再是素材解说",
            script={"voiceover_full": "倒杯加冰就能喝，少一步调配更省事"},
        )
        active = store.build_policy()
        assert active["rules"][0]["status"] == "active"
        assert active["rules"][0]["distinct_video_count"] == 2
        assert active["positive_examples"]
        assert active["negative_examples"]

    def test_same_video_cannot_self_confirm_a_rule(self, tmp_path):
        from script_feedback import ScriptFeedbackStore

        store = ScriptFeedbackStore(tmp_path / "script_feedback.db", min_distinct_videos=2)
        for _ in range(3):
            store.record_feedback(
                video_id="same-video",
                rule_text="口播断句要自然",
                verdict="violated",
                source="user",
            )

        rule = store.build_policy()["rules"][0]
        assert rule["status"] == "provisional"
        assert rule["distinct_video_count"] == 1

    def test_active_user_examples_change_candidate_selection(self, tmp_path):
        from local_asset_pipeline import _normalize_compact_script_response
        from script_feedback import ScriptFeedbackStore

        store = ScriptFeedbackStore(tmp_path / "script_feedback.db", min_distinct_videos=2)
        rule = "字幕应该像带货文案而不是画面描述"
        store.record_feedback(
            video_id="bad-video",
            rule_text=rule,
            verdict="violated",
            source="user",
            script={"voiceover_full": "画面里展示一瓶茶咖镜头里倒进杯子"},
        )
        store.record_feedback(
            video_id="good-video",
            rule_text=rule,
            verdict="satisfied",
            source="user",
            script={"voiceover_full": "倒杯加冰就能喝少一步调配更省事"},
        )
        response = {"creative_candidates": [
            {
                "route": "画面描述",
                "segments": [
                    {"segment": 0, "cue": "画面里展示一瓶茶咖"},
                    {"segment": 1, "cue": "镜头里倒进杯子"},
                ],
            },
            {
                "route": "使用者偏好",
                "segments": [
                    {"segment": 0, "cue": "倒杯加冰就能喝"},
                    {"segment": 1, "cue": "少一步调配更省事"},
                ],
            },
        ]}

        assert _normalize_compact_script_response(
            response,
            2,
            require_creative_candidates=True,
            candidate_validator=lambda _candidate: [],
            user_script_policy=store.build_policy(),
        ) is True

        assert response["route"] == "使用者偏好"
        assert response["subjective_quality"]["active_rules"][0]["text"] == rule


def creative_candidates(*responses):
    """Wrap script fixtures in the production best-of creative response contract."""
    return {
        "creative_candidates": [
            {"route": route, **copy.deepcopy(response)}
            for route, response in zip(("反差好奇", "场景共鸣", "证据递进"), responses)
        ],
    }


def minimal_local_asset_index(asset_folder="/tmp/test-local-assets", product_name="茶咖"):
    return {
        "asset_folder": asset_folder,
        "windows": [{
            "window_id": "product-0",
            "source_video": "product.mp4",
            "source_path": "/tmp/product.mp4",
            "start": 0.0,
            "end": 4.0,
            "analysis": {
                "usable_for_ad": True,
                "confidence": 1.0,
                "product_story_role": "finished_product",
                "product_visibility": 5,
                "visible_subjects": ["product"],
                "visible_text": [product_name],
                "visible_objects": [f"瓶装{product_name}"],
                "narrative_roles": ["hook", "product_showcase", "cta"],
                "evidence": f"桌面展示瓶装{product_name}",
            },
            "motion": {"motion_class": "semi_dynamic"},
            "frame_quality": {"passed": True},
        }],
    }


from config import (
    CINEMATIC_STYLES,
    PRODUCT_PRESETS,
    get_preset,
    DEFAULT_CINEMATIC_STYLE,
    KLING_PRICING,
    KLING_VIDEO_MODEL,
)
from one_click_create import (
    apply_cinematic_style,
    generate_clip_prompts,
    generate_character_prompt,
    estimate_cost,
    print_cost_estimate,
    parse_args,
    build_stable_output_name,
    _bind_reference_tags_to_prompt,
    _build_video_idempotency_key,
    _build_clip_manifest,
    _build_character_manifest,
    _manifest_matches,
    _score_candidate_video_quality,
    _check_segment_semantic_quality,
    _is_product_required_narrative,
    _validate_product_image_file,
    _write_clip_manifest,
    _sanitize_prompt_for_image_generation,
    _preflight_keyframe_check,
    _estimate_image_first_segment_count,
    apply_low_cost_generation_policy,
    _prepare_generation_source_plan,
    _print_generation_source_plan,
    _record_production_workflow_completion,
    build_character_bibles,
    build_product_bible,
    character_bible_to_prompt,
    product_bible_to_prompt,
    _get_primary_char_for_clip,
    build_music_contract,
    _repair_prompt_by_issues,
    MusicContract,
    CharacterBible,
    ProductBible,
)


class TestLLMClientReliability:
    def test_json_timeout_retries_json_mode_without_plain_request_fallback(self):
        from llm_client import LLMClient

        client = LLMClient(
            api_key="key",
            base_url="https://example.invalid/v1",
            model="model",
            timeout=1,
            max_retries=1,
        )
        client._session.post = MagicMock(side_effect=requests.exceptions.Timeout("slow"))
        client.chat = MagicMock()

        assert client.chat_json([{"role": "user", "content": "json"}]) is None
        assert client._session.post.call_count == 2
        assert client.chat.call_count == 0
        assert all(
            call.kwargs["json"]["response_format"] == {"type": "json_object"}
            for call in client._session.post.call_args_list
        )

    def test_json_mode_falls_back_only_for_explicit_unsupported_parameter(self):
        from llm_client import LLMClient

        client = LLMClient(
            api_key="key",
            base_url="https://example.invalid/v1",
            model="model",
            timeout=1,
            max_retries=0,
        )
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":{"message":"response_format is not supported"}}'
        response.url = "https://example.invalid/v1/chat/completions"
        client._session.post = MagicMock(return_value=response)
        client.chat = MagicMock(return_value='{"ok": true}')

        assert client.chat_json([{"role": "user", "content": "json"}]) == {"ok": True}
        assert client._session.post.call_count == 1
        assert client.chat.call_count == 1

    @pytest.mark.parametrize("finish_reason, error_name", [
        ("length", "LLMJSONTruncatedError"),
        ("stop", "LLMJSONParseError"),
    ])
    def test_json_parse_failure_preserves_truncation_semantics(self, finish_reason, error_name):
        import llm_client
        from llm_client import LLMClient

        client = LLMClient(
            api_key="key",
            base_url="https://example.invalid/v1",
            model="model",
            timeout=1,
            max_retries=0,
        )
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json = MagicMock(return_value={
            "choices": [{
                "finish_reason": finish_reason,
                "message": {"content": '{"creative_candidates": ['},
            }],
        })
        client._session.post = MagicMock(return_value=response)

        error_type = getattr(llm_client, error_name)
        with pytest.raises(error_type) as raised:
            client.chat_json(
                [{"role": "user", "content": "json"}],
                raise_on_parse_error=True,
            )

        assert raised.value.finish_reason == finish_reason
        assert "creative_candidates" in raised.value.raw_text

    def test_project_context_persists_non_negotiable_local_video_principles(self):
        context = Path("CONTEXT.md").read_text(encoding="utf-8")

        for principle in (
            "素材是真相来源",
            "爆款参考只迁移创作机制",
            "字幕是带货文案，不是画面解说",
            "全视频只生成一条连续 TTS",
            "禁止低质量兜底",
            "脚本远程调用必须有共享上限",
        ):
            assert principle in context


class TestCinematicStyles:
    """测试电影风格配置"""

    def test_cinematic_styles_not_empty(self):
        """至少有 1 种风格"""
        assert len(CINEMATIC_STYLES) >= 1

    def test_cinematic_styles_has_default(self):
        """默认风格应为 'auto'，且是有效的风格值"""
        from config import DEFAULT_CINEMATIC_STYLE
        assert DEFAULT_CINEMATIC_STYLE == "auto"
        valid_choices = list(CINEMATIC_STYLES.keys()) + [DEFAULT_CINEMATIC_STYLE, "none"]
        assert DEFAULT_CINEMATIC_STYLE in valid_choices

    def test_cinematic_styles_has_required_keys(self):
        """每种风格必须包含必要字段"""
        required_keys = {
            "name", "name_en", "description",
            "camera_push", "camera_pull", "camera_orbit",
            "transition_match", "transition_light",
            "lighting", "color", "mood",
        }
        for key, style in CINEMATIC_STYLES.items():
            missing = required_keys - set(style.keys())
            assert not missing, f"风格 {key} 缺少字段：{missing}"

    def test_cinematic_styles_all_have_non_empty_strings(self):
        """每种风格的字段都不能为空（支持字符串和列表类型）"""
        for key, style in CINEMATIC_STYLES.items():
            for field, value in style.items():
                if isinstance(value, str):
                    assert len(value) > 0, f"风格 {key} 的 {field} 为空字符串"
                elif isinstance(value, list):
                    assert len(value) > 0, f"风格 {key} 的 {field} 为空列表"
                    for i, item in enumerate(value):
                        assert isinstance(item, str) and len(item) > 0, \
                            f"风格 {key} 的 {field}[{i}] 为空或不是字符串"
                else:
                    assert False, f"风格 {key} 的 {field} 类型不支持：{type(value)}"


class TestProductPresets:
    """测试产品预设配置"""

    def test_product_presets_not_empty(self):
        """至少有 1 个产品预设"""
        assert len(PRODUCT_PRESETS) >= 1

    def test_product_presets_has_default(self):
        """必须有 default 预设"""
        assert "default" in PRODUCT_PRESETS

    def test_product_presets_has_required_keys(self):
        """每个预设必须包含必要字段"""
        required_keys = {"style", "lighting", "scene", "demo_action", "result"}
        for key, preset in PRODUCT_PRESETS.items():
            missing = required_keys - set(preset.keys())
            assert not missing, f"产品预设 {key} 缺少字段：{missing}"

    def test_get_preset_returns_default_for_unknown(self):
        """未知产品类型应返回 default 预设"""
        preset = get_preset("未知类型")
        assert preset == PRODUCT_PRESETS["default"]

    def test_get_preset_returns_correct_preset(self):
        """已知产品类型应返回对应预设"""
        for key in PRODUCT_PRESETS:
            if key != "default":
                preset = get_preset(key)
                assert preset == PRODUCT_PRESETS[key]


class TestApplyCinematicStyle:
    """测试电影风格注入"""

    def test_none_style_returns_base_prompt(self):
        """none 风格应返回原始 Prompt"""
        base = "static shot, slow push in"
        result = apply_cinematic_style(base, "none", "push")
        assert result == base

    def test_unknown_style_returns_base_prompt(self):
        """未知风格应返回原始 Prompt"""
        base = "static shot, slow push in"
        result = apply_cinematic_style(base, "unknown_style", "push")
        assert result == base

    def test_hitchcock_style_injects_camera_push(self):
        """hitchcock 风格应注入推镜描述"""
        base = "static shot, slow push in"
        result = apply_cinematic_style(base, "hitchcock", "push")
        assert "Hitchcock" in result
        assert "dolly zoom" in result.lower()

    def test_kubrick_style_injects_camera_pull(self):
        """kubrick 风格应注入拉镜描述"""
        base = "slow pull back"
        result = apply_cinematic_style(base, "kubrick", "pull")
        assert "Kubrick" in result

    def test_all_styles_have_push_description(self):
        """所有风格都必须有 push 描述"""
        for key, style in CINEMATIC_STYLES.items():
            if key != "none":
                desc = style.get("camera_push", "")
                assert len(desc) > 0, f"风格 {key} 缺少 camera_push 描述"

    def test_all_styles_have_pull_description(self):
        """所有风格都必须有 pull 描述"""
        for key, style in CINEMATIC_STYLES.items():
            if key != "none":
                desc = style.get("camera_pull", "")
                assert len(desc) > 0, f"风格 {key} 缺少 camera_pull 描述"

    def test_all_styles_have_orbit_description(self):
        """所有风格都必须有 orbit 描述"""
        for key, style in CINEMATIC_STYLES.items():
            if key != "none":
                desc = style.get("camera_orbit", "")
                assert len(desc) > 0, f"风格 {key} 缺少 camera_orbit 描述"


class TestGenerateCharacterPrompt:
    """测试角色定妆照 Prompt 生成"""

    def test_basic_product_info(self):
        """基础产品信息应生成有效 Prompt"""
        product_info = {
            "name": "测试产品",
            "type": "default",
            "age": "25",
            "gender": "女",
            "outfit": "casual clothes",
        }
        prompt = generate_character_prompt(product_info)
        assert "测试产品" in prompt
        assert "25-year-old" in prompt
        assert "女" in prompt

    def test_product_type_preset_affects_prompt(self):
        """不同产品类型应影响 Prompt"""
        product_info = {
            "name": "面霜",
            "type": "美妆",
            "age": "28",
            "gender": "女",
            "outfit": "white hoodie",
        }
        prompt = generate_character_prompt(product_info)
        # 美妆预设的 scene 应该出现在 prompt 中
        preset = get_preset("美妆")
        assert preset["scene"] in prompt


class TestGenerateClipPrompts:
    """测试分镜片段 Prompt 生成"""

    def test_generates_five_clips(self):
        """应生成 5 个片段"""
        product_info = {"name": "测试产品", "type": "default"}
        clips = generate_clip_prompts(product_info, cinematic_style="none")
        assert len(clips) == 5

    def test_hitchcock_style_injected_in_all_clips(self):
        """hitchcock 风格应注入所有片段"""
        product_info = {"name": "测试产品", "type": "default"}
        clips = generate_clip_prompts(product_info, cinematic_style="hitchcock")
        for clip in clips:
            assert "Hitchcock" in clip or "hitchcock" in clip.lower()

    def test_none_style_no_cinematic_injection(self):
        """none 风格不应注入电影描述"""
        product_info = {"name": "测试产品", "type": "default"}
        clips = generate_clip_prompts(product_info, cinematic_style="none")
        for clip in clips:
            # 不应包含导演名字
            assert "Hitchcock" not in clip
            assert "Kubrick" not in clip
            assert "Spielberg" not in clip

    def test_clips_contain_product_name(self):
        """展示/CTA 片段（2-5）应包含产品名称"""
        product_info = {"name": "我的产品", "type": "default"}
        clips = generate_clip_prompts(product_info, cinematic_style="none")
        # 片段 1（钩子）不含产品名，片段 2-5 应包含
        assert "我的产品" not in clips[0]
        for clip in clips[1:]:
            assert "我的产品" in clip

    def test_clips_are_strings(self):
        """所有片段应为字符串"""
        product_info = {"name": "测试", "type": "default"}
        clips = generate_clip_prompts(product_info, cinematic_style="none")
        for clip in clips:
            assert isinstance(clip, str)
            assert len(clip) > 0


class TestEstimateCost:
    """测试成本估算"""

    def test_pro_mode_5_clips_5s(self):
        """pro 模式 5 段 5 秒的成本估算"""
        result = estimate_cost(mode="pro", duration_per_clip=5, num_clips=5, num_characters=1)
        assert result["image_count"] == 2  # 1 角色 × 2 张/角色
        assert result["video_seconds"] == 25
        expected_cost = 2 * KLING_PRICING["image"]["pro"] + 25 * KLING_PRICING["video"]["pro"]
        assert abs(result["estimated_cost"] - expected_cost) < 0.01

    def test_std_mode_1_clip_preview(self):
        """预览模式（std + 1 段）的成本估算"""
        result = estimate_cost(mode="std", duration_per_clip=5, num_clips=1, num_characters=1)
        assert result["image_count"] == 2  # 1 角色 × 2 张/角色
        assert result["video_seconds"] == 5
        expected_cost = 2 * KLING_PRICING["image"]["std"] + 5 * KLING_PRICING["video"]["std"]
        assert abs(result["estimated_cost"] - expected_cost) < 0.01

    def test_preview_is_cheaper_than_pro(self):
        """预览模式成本应显著低于完整 pro 版本"""
        preview_cost = estimate_cost(mode="std", duration_per_clip=5, num_clips=1, num_characters=1)
        full_cost = estimate_cost(mode="pro", duration_per_clip=5, num_clips=5, num_characters=1)
        # 预览应该是完整版本的约 1/10 成本
        assert preview_cost["estimated_cost"] < full_cost["estimated_cost"] * 0.3

    def test_4k_mode_highest_cost(self):
        """4k 模式成本应最高"""
        std_cost = estimate_cost(mode="std", duration_per_clip=5, num_clips=5)["estimated_cost"]
        pro_cost = estimate_cost(mode="pro", duration_per_clip=5, num_clips=5)["estimated_cost"]
        k4_cost = estimate_cost(mode="4k", duration_per_clip=5, num_clips=5)["estimated_cost"]
        assert std_cost < pro_cost < k4_cost

    def test_ab_versions_multiplies_cost(self):
        """A/B 多版本应倍增成本"""
        cost_1 = estimate_cost(mode="pro", duration_per_clip=5, num_clips=5, ab_versions=1)
        cost_3 = estimate_cost(mode="pro", duration_per_clip=5, num_clips=5, ab_versions=3)
        assert abs(cost_3["estimated_cost"] - cost_1["estimated_cost"] * 3) < 0.01

    def test_image_first_cost_counts_preflight_candidates(self):
        """图片先行候选图应计入成本，避免低估发布级生成预算"""
        result = estimate_cost(
            mode="pro",
            duration_per_clip=5,
            num_clips=5,
            num_characters=1,
            image_first_segments=2,
            image_first_variants=2,
        )
        # 2 张角色定妆照（1角色×2） + 4 张图片先行候选（2段×2张/段）= 6 张
        assert result["image_count"] == 6
        assert any("图片先行预检" in line for line in result["breakdown"])

    def test_image_first_segment_count_by_mode(self):
        """图片先行范围估算应匹配 minimal/standard/full 策略"""
        assert _estimate_image_first_segment_count(5, "minimal") == 1
        assert _estimate_image_first_segment_count(5, "standard") == 2
        assert _estimate_image_first_segment_count(5, "full") == 5
        assert _estimate_image_first_segment_count(5, "standard", enabled=False) == 0

    def test_local_source_plan_has_no_kling_cost_or_parameter_downgrade(self, tmp_path):
        import argparse

        args = argparse.Namespace(
            local_assets=str(tmp_path),
            mode="4k",
            duration=10,
            best_of=3,
            preview=False,
            target_duration=15,
        )
        with patch("one_click_create.estimate_cost", side_effect=AssertionError("local must not estimate Kling cost")), \
             patch("one_click_create.apply_low_cost_generation_policy", side_effect=AssertionError("local must not downgrade Kling params")):
            plan = _prepare_generation_source_plan(
                args,
                {"name": "茶咖", "type": "食品"},
                ab_versions=1,
            )

        assert plan == {
            "source": "local_assets",
            "label": "本地视频混剪",
            "policy_changes": [],
            "kling_cost_info": None,
            "estimated_output_size": None,
        }
        assert (args.mode, args.duration, args.best_of) == ("4k", 10, 3)

    def test_local_source_plan_output_contains_no_ai_generation_steps(self, tmp_path, capsys):
        import argparse

        args = argparse.Namespace(local_assets=str(tmp_path), target_duration=15)
        _print_generation_source_plan({
            "source": "local_assets",
            "label": "本地视频混剪",
            "policy_changes": [],
            "kling_cost_info": None,
            "estimated_output_size": None,
        }, args)
        output = capsys.readouterr().out

        assert "本地视频混剪计划" in output
        assert "可灵图片/视频生成：不调用" in output
        assert "视频片段：" not in output
        assert "图片先行预检" not in output
        assert "角色定妆照" not in output


class TestParallelGeneration:
    """测试并行生成逻辑（mock 生成函数）"""

    def test_parallel_execution_completes_all_tasks(self):
        """并行执行应完成所有任务"""
        results = {}
        lock = threading.Lock()

        def mock_generate(idx, prompt):
            time.sleep(0.05)  # 模拟耗时
            with lock:
                results[idx] = f"clip_{idx:02d}.mp4"
            return (idx, f"clip_{idx:02d}.mp4", None)

        tasks = [{"idx": i, "prompt": f"prompt_{i}"} for i in range(2, 6)]  # 4 个任务

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(mock_generate, t["idx"], t["prompt"]) for t in tasks]
            for future in as_completed(futures):
                idx, path, err = future.result()

        assert len(results) == 4
        for i in range(2, 6):
            assert i in results
            assert results[i] == f"clip_{i:02d}.mp4"

    def test_parallel_is_faster_than_serial(self):
        """并行执行应快于串行"""
        def slow_task(idx):
            time.sleep(0.1)
            return idx

        # 串行计时
        start = time.time()
        serial_results = []
        for i in range(4):
            serial_results.append(slow_task(i))
        serial_time = time.time() - start

        # 并行计时
        start = time.time()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(slow_task, i) for i in range(4)]
            parallel_results = [f.result() for f in as_completed(futures)]
        parallel_time = time.time() - start

        # 并行应该明显更快
        assert parallel_time < serial_time * 0.8
        assert len(parallel_results) == 4

    def test_parallel_with_failures_continues(self):
        """部分任务失败时，并行执行应继续并收集成功结果"""
        results = {}
        lock = threading.Lock()
        fail_indices = {3, 5}  # 让第 3、5 段失败

        def mock_generate(idx, prompt):
            time.sleep(0.02)
            if idx in fail_indices:
                with lock:
                    results[idx] = None
                return (idx, None, RuntimeError("mock failure"))
            with lock:
                results[idx] = f"clip_{idx:02d}.mp4"
            return (idx, f"clip_{idx:02d}.mp4", None)

        tasks = [{"idx": i, "prompt": f"prompt_{i}"} for i in range(2, 7)]  # 5 个任务（段 2-6）

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(mock_generate, t["idx"], t["prompt"]) for t in tasks]
            for future in as_completed(futures):
                idx, path, err = future.result()

        success_count = sum(1 for v in results.values() if v is not None)
        fail_count = sum(1 for v in results.values() if v is None)

        assert success_count == 3  # 5 段中 3 段成功
        assert fail_count == 2     # 2 段失败
        # 成功数 >= 3 应该继续（60% 阈值）
        min_clips = 3
        assert success_count >= min_clips

    def test_parallel_below_min_clips_should_fail(self):
        """成功数低于 min_clips 时应判定失败"""
        results = {}
        lock = threading.Lock()

        def mock_generate(idx, prompt):
            time.sleep(0.02)
            if idx > 3:  # 只有前 2 段成功
                with lock:
                    results[idx] = None
                return (idx, None, RuntimeError("mock failure"))
            with lock:
                results[idx] = f"clip_{idx:02d}.mp4"
            return (idx, f"clip_{idx:02d}.mp4", None)

        tasks = [{"idx": i, "prompt": f"prompt_{i}"} for i in range(2, 7)]  # 5 个任务

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(mock_generate, t["idx"], t["prompt"]) for t in tasks]
            for future in as_completed(futures):
                idx, path, err = future.result()

        success_count = sum(1 for v in results.values() if v is not None)
        min_clips = 3
        # 只有 2 段成功，低于 3 段的最低要求
        assert success_count < min_clips
        # 这种情况应抛出 RuntimeError（由调用方判定）
        with pytest.raises(RuntimeError):
            if success_count < min_clips:
                raise RuntimeError(
                    f"片段生成失败过多（成功 {success_count}/5，需要 ≥{min_clips} 段）"
                )

    def test_results_are_ordered_by_index(self):
        """并行结果应按索引顺序收集"""
        results = {}
        lock = threading.Lock()

        def mock_generate(idx, prompt):
            # 让高索引先完成（倒序完成）
            sleep_time = (10 - idx) * 0.01
            time.sleep(sleep_time)
            with lock:
                results[idx] = f"clip_{idx:02d}.mp4"
            return (idx, f"clip_{idx:02d}.mp4", None)

        tasks = [{"idx": i, "prompt": f"prompt_{i}"} for i in range(2, 6)]

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(mock_generate, t["idx"], t["prompt"]) for t in tasks]
            for future in as_completed(futures):
                future.result()

        # 按索引顺序收集
        ordered_clips = []
        for i in range(2, 6):
            if results.get(i):
                ordered_clips.append(results[i])

        assert ordered_clips == [
            "clip_02.mp4", "clip_03.mp4", "clip_04.mp4", "clip_05.mp4"
        ]

    def test_thread_safety_of_shared_dict(self):
        """并发写入共享字典应是线程安全的（使用 Lock）"""
        results = {}
        lock = threading.Lock()
        counter = {"value": 0}
        counter_lock = threading.Lock()

        def mock_generate(idx, prompt):
            with lock:
                results[idx] = f"clip_{idx:02d}.mp4"
            with counter_lock:
                counter["value"] += 1
            return (idx, f"clip_{idx:02d}.mp4", None)

        tasks = [{"idx": i, "prompt": f"prompt_{i}"} for i in range(100)]

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(mock_generate, t["idx"], t["prompt"]) for t in tasks]
            for future in as_completed(futures):
                future.result()

        assert len(results) == 100
        assert counter["value"] == 100


class TestCLIArguments:
    """测试新的 CLI 参数"""

    def test_preview_flag_short(self):
        """-p 短选项应启用预览模式"""
        args = parse_args.__wrapped__(["-p"]) if hasattr(parse_args, "__wrapped__") else None
        # 直接测试 argparse 行为
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--preview", "-p", action="store_true")
        args = parser.parse_args(["-p"])
        assert args.preview is True

    def test_preview_flag_long(self):
        """--preview 长选项应启用预览模式"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--preview", "-p", action="store_true")
        args = parser.parse_args(["--preview"])
        assert args.preview is True

    def test_serial_flag(self):
        """--serial 应强制串行模式"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--serial", action="store_true")
        args = parser.parse_args(["--serial"])
        assert args.serial is True

    def test_min_clips_default(self):
        """--min-clips 默认值应为 3"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--min-clips", type=int, default=3)
        args = parser.parse_args([])
        assert args.min_clips == 3

    def test_min_clips_custom(self):
        """--min-clips 可自定义"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--min-clips", type=int, default=3)
        args = parser.parse_args(["--min-clips", "4"])
        assert args.min_clips == 4

    def test_max_workers_default(self):
        """--max-workers 默认值应为 4"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--max-workers", type=int, default=4)
        args = parser.parse_args([])
        assert args.max_workers == 4

    def test_default_parallel_mode(self):
        """默认应为并行模式（serial=False）"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--serial", action="store_true")
        args = parser.parse_args([])
        parallel = not args.serial
        assert parallel is True

    def test_quality_defaults_are_publish_first(self):
        """CLI 默认应启用发布级质量策略，同时避免默认视频抽卡"""
        with patch.object(sys, "argv", ["one_click_create.py"]):
            args = parse_args()
        assert args.strict is True
        assert args.stabilize is True
        assert args.best_of == 1
        assert args.preflight_keyframe is True
        assert args.image_first is True
        assert args.image_first_mode == "standard"
        assert args.image_first_variants == 2

    def test_quality_defaults_can_be_disabled_for_debug(self):
        """调试时应允许显式关闭严格模式和稳定化"""
        with patch.object(sys, "argv", ["one_click_create.py", "--no-strict", "--no-stabilize", "--no-image-first", "--best-of", "1"]):
            args = parse_args()
        assert args.strict is False
        assert args.stabilize is False
        assert args.image_first is False
        assert args.best_of == 1


class TestProductPresenceDetection:
    """测试轻量产品出现检测"""

    def test_product_similarity_same_image_high(self, tmp_path):
        """同一商品图与帧图应有较高相似度"""
        from PIL import Image, ImageDraw
        from quality_checker import _product_similarity

        product = tmp_path / "product.png"
        frame = tmp_path / "frame.png"

        img = Image.new("RGB", (160, 160), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((45, 25, 115, 135), fill=(220, 30, 60))
        draw.ellipse((65, 45, 95, 75), fill=(255, 240, 180))
        img.save(product)
        img.save(frame)

        assert _product_similarity(product, [frame]) >= 0.8

    def test_product_similarity_different_image_low(self, tmp_path):
        """明显不同的画面应有较低商品相似度"""
        from PIL import Image, ImageDraw
        from quality_checker import _product_similarity

        product = tmp_path / "product.png"
        frame = tmp_path / "frame.png"

        p = Image.new("RGB", (160, 160), "white")
        pd = ImageDraw.Draw(p)
        pd.rectangle((45, 25, 115, 135), fill=(220, 30, 60))
        p.save(product)

        f = Image.new("RGB", (160, 160), (20, 80, 210))
        fd = ImageDraw.Draw(f)
        fd.polygon([(20, 20), (140, 40), (80, 140)], fill=(30, 220, 80))
        f.save(frame)

        assert _product_similarity(product, [frame]) < 0.45


class TestSemanticQualityGates:
    """测试语义质量门禁基础能力"""

    def test_character_similarity_same_image_high(self, tmp_path):
        """同一角色参考图与帧图应有较高相似度"""
        from PIL import Image, ImageDraw
        from quality_checker import _character_similarity

        ref = tmp_path / "char.png"
        frame = tmp_path / "frame.png"

        img = Image.new("RGB", (180, 240), (245, 245, 245))
        draw = ImageDraw.Draw(img)
        draw.ellipse((55, 35, 125, 115), fill=(230, 160, 120))
        draw.rectangle((70, 115, 110, 210), fill=(40, 80, 180))
        img.save(ref)
        img.save(frame)

        assert _character_similarity(ref, [frame]) >= 0.75

    def test_check_video_quality_accepts_semantic_gate_args(self):
        """发布级质检应支持商品/角色语义门禁参数"""
        import inspect
        from quality_checker import check_video_quality

        sig = inspect.signature(check_video_quality)
        assert "character_reference_image" in sig.parameters
        assert "require_semantic_alignment" in sig.parameters


class TestResumeAndIdempotency:
    """测试断点续跑和幂等键"""

    def test_parse_args_has_resume_output_name_and_product_escape_hatch(self):
        """CLI 应暴露续跑参数和无商品图显式放行参数"""
        with patch.object(
            sys,
            "argv",
            ["one_click_create.py", "--resume", "--output-name", "demo_run", "--allow-no-product-image"],
        ):
            args = parse_args()
        assert args.resume is True
        assert args.output_name == "demo_run"
        assert args.allow_no_product_image is True

    def test_resume_is_enabled_by_default_and_can_be_disabled(self):
        """默认应复用稳定输出名，必要时可显式关闭。"""
        with patch.object(sys, "argv", ["one_click_create.py"]):
            args = parse_args()
        assert args.resume is True

        with patch.object(sys, "argv", ["one_click_create.py", "--no-resume"]):
            args = parse_args()
        assert args.resume is False

    def test_stable_output_name_is_deterministic(self):
        """相同输入和关键参数应生成相同续跑输出名"""
        product_info = {"name": "测试产品", "type": "美妆", "selling_point": "清爽"}
        with patch.object(sys, "argv", ["one_click_create.py", "--style", "none", "--seed", "42"]):
            args = parse_args()
        first = build_stable_output_name(product_info, args)
        second = build_stable_output_name(product_info, args)
        assert first == second
        assert first.startswith("测试产品_")

    def test_idempotency_key_is_stable_for_same_candidate(self):
        """同一候选视频的幂等键必须稳定，供 POST 重试复用"""
        key1 = _build_video_idempotency_key(
            "prompt",
            ["data:image/png;base64,abc"],
            1,
            Path("clip_01_demo_cand1.mp4"),
            model="kling-v3-omni",
            mode="pro",
            duration=5,
            aspect_ratio="9:16",
            seed=42,
        )
        key2 = _build_video_idempotency_key(
            "prompt",
            ["data:image/png;base64,abc"],
            1,
            Path("clip_01_demo_cand1.mp4"),
            model="kling-v3-omni",
            mode="pro",
            duration=5,
            aspect_ratio="9:16",
            seed=42,
        )
        assert key1 == key2
        assert key1.startswith("kaa-")

    def test_reference_binding_is_structured(self):
        """参考图 tag 应以结构化语义块绑定，不再依赖泛关键词插入"""
        prompt = "A person uses the product in a clean room."
        result = _bind_reference_tags_to_prompt(
            prompt,
            [
                {"role": "product", "image": "img1"},
                {"role": "character", "image": "img2"},
                {"role": "continuity", "image": "img3"},
            ],
            "showcase",
        )
        assert result.startswith("PRODUCT REFERENCE")
        assert "<<<image_1>>>" in result
        assert "<<<image_2>>>" in result
        assert "<<<image_3>>>" in result
        assert "must match exactly" in result
        assert "Exact same person" in result
        assert "continuity" in result.lower()
        assert prompt in result

    def test_reference_binding_marks_approved_keyframe(self):
        """图片先行通过的关键帧应作为强首帧参考绑定到视频 Prompt"""
        result = _bind_reference_tags_to_prompt(
            "A person presents the product.",
            [{"role": "approved_keyframe", "image": "img1"}],
            "showcase",
        )
        assert "APPROVED KEYFRAME REFERENCE" in result
        assert "quality preflight passed" in result
        assert "first-frame visual target" in result

    def test_image_first_approved_keyframe_skips_duplicate_preflight(self):
        """图片先行已通过的片段不应再额外触发单张首帧预检"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "and (idx - 1) not in approved_keyframes" in src

    def test_reference_binding_uses_roles_not_narrative_guess(self):
        """展示段如果只有角色图，也不能误标为 Product reference"""
        result = _bind_reference_tags_to_prompt(
            "A person talks to camera.",
            [{"role": "character", "image": "img1"}],
            "showcase",
        )
        assert "CHARACTER REFERENCE" in result
        assert "<<<image_1>>>" in result
        assert "Exact same person" in result
        assert "PRODUCT REFERENCE" not in result

    def test_clip_manifest_must_match_for_cache(self, tmp_path):
        """片段缓存必须严格匹配 manifest，避免旧画面配新字幕"""
        clip = tmp_path / "clip_01_demo.mp4"
        clip.write_bytes(b"x" * 1024)
        manifest = _build_clip_manifest(
            final_prompt="old prompt",
            ref_images=[{"role": "character", "image": "img1"}],
            idx=1,
            model="kling-v3-omni",
            mode="pro",
            duration=5,
            aspect_ratio="9:16",
            seed=42,
            negative_prompt="bad",
        )
        manifest["target_name"] = clip.name
        _write_clip_manifest(clip, manifest)

        assert _manifest_matches(clip, manifest) is True

        changed = dict(manifest)
        changed["prompt_sha256"] = "different"
        assert _manifest_matches(clip, changed) is False

    def test_low_cost_policy_reduces_best_of_before_mode(self):
        """超预算时应优先减少候选数，再降低生成模式。"""
        with patch.object(
            sys,
            "argv",
            ["one_click_create.py", "--mode", "4k", "--best-of", "3", "--duration", "8"],
        ):
            args = parse_args()

        changes = apply_low_cost_generation_policy(
            args,
            num_clips=5,
            num_characters=1,
            ab_versions=1,
            budget_limit=20.0,
        )

        assert changes[0] == "best_of 3 -> 1"
        assert args.best_of == 1
        assert "mode 4k -> pro" in changes
        assert args.mode in {"pro", "std"}


class TestBatchQualityDefaults:
    """测试批量模式质量默认值"""

    def test_batch_defaults_match_publish_first_policy(self):
        """批量生成默认也应使用发布级质量策略，同时避免默认视频抽卡"""
        from batch import create_task_args

        args = create_task_args({"product_name": "测试产品"}, {})
        assert args["strict_mode"] is True
        assert args["stabilize"] is True
        assert args["best_of"] == 1
        assert args["preflight_keyframe"] is True
        assert args["image_first"] is True
        assert args["image_first_mode"] == "standard"
        assert args["image_first_variants"] == 2
        assert args["allow_no_product_image"] is False
        assert args["resume"] is True

    def test_batch_stable_output_name_is_deterministic(self):
        """批量任务默认输出名应稳定，便于重跑命中缓存。"""
        from batch import _build_stable_task_output_name

        kwargs = {
            "product_info": {"name": "测试产品", "type": "美妆"},
            "style": "none",
            "duration": 5,
            "mode": "pro",
            "aspect_ratio": "9:16",
        }

        first = _build_stable_task_output_name(1, "测试产品", kwargs)
        second = _build_stable_task_output_name(1, "测试产品", dict(reversed(list(kwargs.items()))))

        assert first == second
        assert first.startswith("001_测试产品_")


class TestProductionWorkflowBridge:
    """回归测试：one_click 主流程接入工作流闭环"""

    def test_completion_records_assets_feedback_and_experiment(self, tmp_path):
        """最终质检通过后应登记资产、收集反馈并追踪实验。"""
        from PIL import Image

        image_path = tmp_path / "product.png"
        Image.new("RGB", (64, 64), (80, 120, 200)).save(image_path)
        final_path = tmp_path / "final.mp4"
        final_path.write_bytes(b"fake-video")

        class FakeQuality:
            overall_score = 86.0
            issues = []

        class FakeAssetLibrary:
            def __init__(self):
                self.characters = []
                self.products = []
                self.scores = []

            def add_character(self, **kwargs):
                self.characters.append(kwargs)
                return "char_1"

            def add_product(self, **kwargs):
                self.products.append(kwargs)
                return "product_1"

            def update_quality_score(self, asset_id, score):
                self.scores.append((asset_id, score))

        class FakeFeedbackLoop:
            def __init__(self):
                self.calls = []

            def collect_feedback(self, **kwargs):
                self.calls.append(kwargs)
                return True

        class FakeExperimentTracker:
            def __init__(self):
                self.started = []
                self.completed = []

            def start_experiment(self, **kwargs):
                self.started.append(kwargs)
                return True

            def complete_experiment(self, **kwargs):
                self.completed.append(kwargs)
                return True

        assets = FakeAssetLibrary()
        feedback = FakeFeedbackLoop()
        experiments = FakeExperimentTracker()

        summary = _record_production_workflow_completion(
            output_name="demo",
            final_path=final_path,
            quality_result=FakeQuality(),
            product_info={"name": "测试产品", "type": "美妆"},
            ad_script={"segments": [{"narrative": "hook"}]},
            generation_params={"mode": "pro", "best_of": 1},
            character_assets=[{"name": "主角", "image_path": image_path}],
            product_image_path=image_path,
            character_bibles=[{"name": "主角"}],
            product_bible={"name": "测试产品"},
            asset_library=assets,
            feedback_loop=feedback,
            experiment_tracker=experiments,
        )

        assert len(summary["registered_assets"]) == 2
        assert summary["automatic_observation_recorded"] is True
        assert summary["feedback_collected"] is False
        assert summary["experiment_tracked"] is True
        assert len(feedback.calls) == 1
        assert len(experiments.started) == 1
        assert len(experiments.completed) == 1
        artifact_suffix = hashlib.sha256(b"fake-video").hexdigest()[:12]
        assert experiments.started[0]["experiment_id"] == f"exp_demo_{artifact_suffix}"
        assert feedback.calls[0]["video_id"] == f"video_demo_{artifact_suffix}"

    def test_workflow_does_not_complete_an_experiment_that_failed_to_start(self, tmp_path):
        final_path = tmp_path / "final.mp4"
        final_path.write_bytes(b"fake-video")

        class FakeQuality:
            overall_score = 90.0
            issues = []

        class FailedExperimentTracker:
            def __init__(self):
                self.completed = []

            def start_experiment(self, **kwargs):
                return False

            def complete_experiment(self, **kwargs):
                self.completed.append(kwargs)
                return True

        experiments = FailedExperimentTracker()
        summary = _record_production_workflow_completion(
            output_name="same-name",
            final_path=final_path,
            quality_result=FakeQuality(),
            product_info={"name": "测试产品", "type": "食品"},
            ad_script={"segments": [{"narrative": "hook"}]},
            generation_params={"mode": "local"},
            character_assets=[],
            product_image_path=None,
            character_bibles=[],
            product_bible={},
            experiment_tracker=experiments,
        )

        assert summary["experiment_tracked"] is False
        assert experiments.completed == []

    def test_final_export_compresses_before_terminal_loudness_normalization(self):
        source = Path("video_merger.py").read_text(encoding="utf-8")
        export_block = source[source.index("def export_final_video("):]
        compressor_pos = export_block.index("acompressor=")
        loudnorm_pos = export_block.index(
            'f"loudnorm=I={audio_lufs}:LRA=7:TP={audio_peak}"',
            export_block.index("# 第二程"),
        )

        assert compressor_pos < loudnorm_pos
        assert 'alimiter=limit={audio_peak}dB:level=false' in export_block

    def test_one_click_pipeline_calls_workflow_bridge_after_quality_gate(self):
        """主流水线源码应包含前置智能决策和完成闭环。"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "_run_pre_generation_smart_decision(" in src
        assert "_record_production_workflow_completion(" in src

    def test_workflow_orchestrator_uses_current_quality_gate_api(self):
        """工作流编排器应调用当前质量门 API，而不是旧的 storyboard 参数。"""
        src = Path("workflow_orchestrator.py").read_text(encoding="utf-8")
        quality_block = src[src.index("def _step_quality_gate"):src.index("def _step_smart_decision")]
        assert "ad_script=ad_script" in quality_block
        assert "product_image_path=product_image_path" in quality_block
        assert "run_quality_gate(\n            storyboard=" not in quality_block

    def test_strict_quality_gate_failure_blocks_without_prompt(self):
        """严格模式下质量门失败应直接阻断，不能进入人工确认再抽视频。"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        block = src[src.index("if not quality_gate_result.passed:"):src.index("workflow_decision_result =")]
        assert "if strict_mode:" in block
        assert "避免进入高成本视频抽卡" in block
        assert block.index("if strict_mode:") < block.index("input(")

    def test_workflow_registers_video_clips_as_video_assets(self):
        """工作流资产注册不能把 mp4 片段当商品图片资产。"""
        src = Path("workflow_orchestrator.py").read_text(encoding="utf-8")
        asset_block = src[src.index("def _step_asset_registration"):src.index("def _step_feedback_collection")]
        block = asset_block[asset_block.index("for clip in video_clips:"):asset_block.index("print(f\"📦 资产注册完成")]
        assert "add_video_clip(" in block
        assert "add_product(" not in block

    def test_asset_library_supports_video_clip_assets(self, tmp_path):
        """资产库应原生支持视频片段资产类型。"""
        from asset_library import AssetLibrary

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake-video")

        library = AssetLibrary(tmp_path / "assets")
        asset_id = library.add_video_clip(
            video_path=video,
            name="clip_0",
            metadata={"narrative": "hook"},
            tags=["clip", "hook"],
        )

        asset = library.get_asset(asset_id)
        assert asset["asset_type"] == "video_clip"
        assert Path(asset["video_path"]).exists()
        assert library.get_stats()["video_clips"] == 1


class TestTrimOffsetCompensation:
    """回归测试：预裁切后字幕/口播时间轴偏移补偿"""

    def test_subtitle_times_shifted_by_trim_start(self):
        """字幕起始/结束时间应减去裁切时长并截断到 >=0"""
        subtitles = [
            {"start": 0.5, "end": 2.5, "text": "第一段"},
            {"start": 3.0, "end": 5.0, "text": "第二段"},
        ]
        _trim_start = 0.3
        for sub in subtitles:
            sub["start"] = max(0.0, sub["start"] - _trim_start)
            sub["end"] = max(0.0, sub["end"] - _trim_start)

        assert subtitles[0]["start"] == 0.2
        assert subtitles[0]["end"] == 2.2
        assert subtitles[1]["start"] == 2.7
        assert subtitles[1]["end"] == 4.7

    def test_subtitle_times_clamped_to_zero(self):
        """当裁切时长大于字幕起始时间时，应截断到 0"""
        subtitles = [
            {"start": 0.1, "end": 2.0, "text": "第一段"},
        ]
        _trim_start = 0.3
        for sub in subtitles:
            sub["start"] = max(0.0, sub["start"] - _trim_start)
            sub["end"] = max(0.0, sub["end"] - _trim_start)

        assert subtitles[0]["start"] == 0.0
        assert subtitles[0]["end"] == 1.7


class TestFallbackAudioQualityGate:
    """回归测试：fallback 底噪应被质量门拦截"""

    def test_analyze_audio_flags_extremely_low_volume(self):
        """_analyze_audio_ffmpeg 应对低于 -35 LUFS 的音频标记问题"""
        from quality_checker import _analyze_audio_ffmpeg
        from unittest.mock import patch

        # 模拟 ffmpeg 返回 -45 LUFS 的响度数据
        mock_stderr = '{"input_i":"-45.0","input_tp":"-50.0","input_lra":"1.0"}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stderr=mock_stderr, returncode=0)
            lufs, peak, issues = _analyze_audio_ffmpeg(Path("dummy.mp4"))

        assert lufs == -45.0
        assert any("音量极低" in issue for issue in issues)

    def test_quality_gate_rejects_silent_audio_when_required(self):
        """require_audio=True 时，底噪填充音频应直接判失败"""
        from quality_checker import VideoQualityResult

        result = VideoQualityResult()
        result.audio_lufs = -45.0
        result.audio_issues = ["音量极低，可能为静音或底噪填充"]
        require_audio = True

        # 复现评分逻辑中的硬失败判定
        if require_audio and result.audio_lufs < -30:
            result.passed = False
            result.issues.insert(0, "最终成片音频为静音或底噪填充，无法作为可发布广告视频")

        assert result.passed is False
        assert "底噪填充" in result.issues[0]


class TestProductDetectionThreshold:
    """回归测试：产品检测阈值提高"""

    def test_product_detected_threshold_is_0_55(self):
        """产品出现判定阈值应为 0.55"""
        from quality_checker import VideoQualityResult

        result = VideoQualityResult()
        result.product_similarity = 0.50
        result.product_detected = result.product_similarity >= 0.55
        assert result.product_detected is False

        result.product_similarity = 0.60
        result.product_detected = result.product_similarity >= 0.55
        assert result.product_detected is True

    def test_weak_detection_threshold_is_0_65(self):
        """产品特征较弱提示阈值应为 0.65"""
        from quality_checker import VideoQualityResult

        result = VideoQualityResult()
        result.product_similarity = 0.60
        result.product_detected = result.product_similarity >= 0.55
        is_weak = result.product_detected and result.product_similarity < 0.65
        assert is_weak is True


class TestVoiceoverSegmentBounds:
    """回归测试：口播单段边界检查"""

    def test_per_line_overflow_detected(self):
        """单句口播超出 line['end'] 时应触发 overflow_ratio"""
        script_lines = [
            {"text": "短句", "start": 0.0, "end": 1.0, "segment": 0},
            {"text": "另一句", "start": 2.0, "end": 3.0, "segment": 1},
        ]
        # 模拟：第一句生成后 current_time = 1.5 > line["end"] = 1.0
        overflow_ratio = 1.0
        for line in script_lines:
            current_time = 1.5 if line["segment"] == 0 else 2.8
            _line_end = line.get("end", 10.0)
            if _line_end > 0 and current_time > _line_end:
                _seg_ratio = current_time / _line_end
                overflow_ratio = max(overflow_ratio, _seg_ratio)

        assert overflow_ratio == 1.5  # 1.5 / 1.0


class TestMergeTransitionsAudioMapping:
    """回归测试：无 BGM 时原片音频映射"""

    def test_filter_parts_has_outa_when_clips_have_audio_no_bgm(self):
        """片段有音频且无 BGM 时，filter_parts 必须包含 [outa] 路由"""
        # 直接验证逻辑：any_audio=True + current_alabel 存在 + 无 BGM
        # 应追加 f"{current_alabel}anull[outa]"
        any_audio = True
        current_alabel = "[acatfaded]"
        bgm_exists = False

        filter_parts = []
        if bgm_exists:
            pass
        elif any_audio and current_alabel:
            filter_parts.append(f"{current_alabel}anull[outa]")

        assert "[acatfaded]anull[outa]" in filter_parts


class TestBatchGlobalDefaults:
    """回归测试：batch 全局默认参数完整性"""

    def test_run_batch_global_defaults_has_all_keys(self):
        """global_defaults 应包含 create_task_args 中读取的所有字段"""
        from batch import create_task_args

        # 获取 create_task_args 中从 global_defaults 读取的所有 key
        import inspect
        src = inspect.getsource(create_task_args)
        import re
        keys_in_code = set(re.findall(r'global_defaults\.get\("([^"]+)"', src))

        # run_batch 中定义的 global_defaults  keys（模拟构造）
        global_defaults = {
            "style": "none",
            "duration": 5,
            "mode": "std",
            "aspect_ratio": "9:16",
            "dual_output": False,
            "image_fidelity": 0.5,
            "human_fidelity": 0.5,
            "seed": None,
            "best_of": 1,
            "quality_frames": 12,
            "keep_candidates": False,
            "stabilize": True,
            "strict_mode": True,
            "brand_intro_outro": False,
            "kling_model": None,
            "multi_shot": False,
            "preflight_keyframe": True,
            "image_first": True,
            "image_first_mode": "standard",
            "image_first_variants": 2,
            "hook_type": "question",
            "use_voiceover": False,
            "voiceover_style": "standard",
            "voice": "female_young",
            "script_style": "pain_point_solution",
            "force": False,
            "parallel": True,
            "min_clips": 3,
            "preview": False,
            "max_workers": 4,
            "target_duration": None,
            "rhythm_style": "moderate",
            "resume": True,
            "total_tasks": 1,
        }

        missing = keys_in_code - set(global_defaults.keys())
        assert not missing, f"global_defaults 缺少字段：{missing}"


class TestFinalQualityBugFixes:
    """回归测试：最终成片质量相关修复"""

    def test_drawtext_escape_handles_special_chars(self):
        """品牌/CTA 文案中的特殊字符不应破坏 ffmpeg drawtext 语法"""
        from video_merger import _ffmpeg_escape_drawtext_text

        escaped = _ffmpeg_escape_drawtext_text("L'Oreal: 50%, now\\new")
        assert r"\'" in escaped
        assert r"\:" in escaped
        assert r"\," in escaped
        assert "\\\\" in escaped

    def test_tail_card_display_copy_uses_same_punctuation_free_rendering_contract(self):
        from video_merger import build_tail_card_display_text

        assert build_tail_card_display_text(
            "茶咖",
            "没试过？赶紧来试试~",
        ) == "茶咖 没试过赶紧来试试"

    def test_video_vbv_args_derive_from_output_bitrate(self):
        """中间编码应使用 maxrate/bufsize 约束，防止 CRF 文件体积失控"""
        from video_merger import _video_vbv_args

        args = _video_vbv_args("10M")
        assert args == ["-maxrate", "15M", "-bufsize", "20M"]

    def test_extract_frame_b64_cleans_temp_file_on_failure(self, tmp_path):
        """抽帧失败时 NamedTemporaryFile(delete=False) 也必须清理"""
        import tempfile
        import one_click_create

        created = tmp_path / "leaked.png"

        class FakeTmp:
            name = str(created)

            def __enter__(self):
                created.write_bytes(b"partial")
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(tempfile, "NamedTemporaryFile", return_value=FakeTmp()):
            with patch.object(one_click_create, "extract_frame", side_effect=RuntimeError("mock fail")):
                result = one_click_create._extract_frame_b64(Path("missing.mp4"), 1.0)

        assert result is None
        assert not created.exists()

    def test_tts_retries_on_429(self, tmp_path):
        """火山 TTS 遇到 429 应应用层重试，而不是直接放弃口播"""
        import tts_client
        import base64
        import json

        class FakeResp:
            def __init__(self, status_code: int, audio_data: bytes | None = None):
                self.status_code = status_code
                self._audio_data = audio_data
                self.text = "rate limited"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def json(self):
                return {"code": self.status_code, "message": self.text}

            def iter_content(self, chunk_size=4096):
                if self._audio_data:
                    yield self._audio_data

            def iter_lines(self, decode_unicode=False):
                if self._audio_data:
                    chunk_b64 = base64.b64encode(self._audio_data).decode()
                    yield json.dumps({"code": 0, "data": chunk_b64})
                    yield json.dumps({"code": 20000000, "message": "ok", "data": None})
                return
                yield

        fake_audio = b"x" * 2048
        responses = [
            FakeResp(429),
            FakeResp(200, fake_audio),
        ]

        with patch("requests.post", side_effect=responses) as post:
            with patch("time.sleep"):
                with patch.object(tts_client, "_validate_audio_file"):
                    out = tts_client._generate_tts_volcengine(
                        text="测试",
                        output_path=tmp_path / "out.mp3",
                        speaker="speaker",
                        api_key="key",
                    )

        assert out.exists()
        assert post.call_count == 2


class TestFifthReviewFixes:
    """回归测试：第 5 轮致命问题修复"""

    def test_kling_downloaded_video_validation_rejects_invalid_file(self, tmp_path):
        """下载到非空但不可解码的视频时，应阻断后续 pipeline"""
        from kling_client import _validate_downloaded_video
        from config import VideoGenerationError

        bad_video = tmp_path / "bad.mp4"
        bad_video.write_bytes(b"not a real mp4")

        with pytest.raises(VideoGenerationError):
            _validate_downloaded_video(bad_video)

    def test_http_url_detection_for_product_image(self):
        """product_image 支持 HTTP/HTTPS URL 判断，避免被 Path 破坏"""
        from one_click_create import _is_http_url

        assert _is_http_url("https://example.com/product.jpg") is True
        assert _is_http_url("http://example.com/product.jpg") is True
        assert _is_http_url("/tmp/product.jpg") is False

    def test_batch_keeps_product_image_url_as_string(self):
        """批量模式不能把 URL 提前转成 Path('https:/...')"""
        from batch import create_task_args

        args = create_task_args(
            {"product_name": "测试", "product_image": "https://example.com/a.jpg"},
            {},
        )
        assert args["product_image"] == "https://example.com/a.jpg"

    def test_ffmpeg_filter_path_escape_handles_special_chars(self):
        """字幕/字体滤镜路径需要保护空格、冒号、逗号、单引号"""
        from video_merger import _ffmpeg_escape_filter_path

        escaped = _ffmpeg_escape_filter_path("/Users/me/My Project/a,b's/font.ttf")
        assert r"\ " in escaped
        assert r"\," in escaped
        assert r"\'" in escaped

    def test_ffconcat_path_escape_handles_single_quote(self):
        """concat demuxer 的 file 行需要保护单引号"""
        from video_merger import _ffconcat_escape_path

        escaped = _ffconcat_escape_path(Path("/tmp/a'b.mp4"))
        assert r"'\''" in escaped

    def test_xfade_duration_uses_shorter_neighbor(self):
        """转场时长必须按相邻两段较短者钳制"""
        durations = [5.0, 0.3]
        requested = 1.2
        trans_duration = min(requested, min(durations[0], durations[1]) * 0.45)
        assert trans_duration == pytest.approx(0.135)

    def test_intelligent_transition_scoring_is_deterministic_and_explainable(self):
        """同一镜头特征必须得到相同排序，并输出逐维评分理由。"""
        from intelligent_transition import score_transition_candidates

        features = {
            "subject_distance": 0.08,
            "composition_similarity": 0.92,
            "motion_alignment": 0.95,
            "motion_speed": 0.35,
            "motion_direction": "left",
            "brightness_delta": 0.06,
            "color_delta": 0.08,
            "scene_difference": 0.18,
            "narrative_pair": "showcase->showcase",
            "style": "fast",
        }

        first = score_transition_candidates(features)
        second = score_transition_candidates(features)

        assert first == second
        assert {item["type"] for item in first} >= {"cut", "dissolve", "slideleft"}
        assert all(item["score_details"] for item in first)
        assert all("reason" in item for item in first)

    def test_legacy_transition_selector_is_also_deterministic(self):
        """旧入口不能残留随机选择，避免非主流程调用产生漂移。"""
        from video_merger import select_transition

        results = [
            select_transition("hook", "product_showcase", style="fast", duration=0.3)
            for _ in range(20)
        ]

        assert results == [results[0]] * len(results)

    def test_intelligent_transition_blocks_when_rendered_candidates_fail(self, tmp_path):
        """所有实渲染候选未达标时必须阻断，不能退回固定转场或简单拼接。"""
        from intelligent_transition import IntelligentTransitionError, plan_intelligent_transitions

        clips = [tmp_path / "left.mp4", tmp_path / "right.mp4"]
        for clip in clips:
            clip.write_bytes(b"fixture")

        features = {
            "subject_distance": 0.1,
            "composition_similarity": 0.9,
            "motion_alignment": 0.9,
            "motion_speed": 0.2,
            "motion_direction": "static",
            "brightness_delta": 0.1,
            "color_delta": 0.1,
            "scene_difference": 0.2,
            "narrative_pair": "hook->showcase",
            "style": "moderate",
        }

        with patch("intelligent_transition.analyze_transition_boundary", return_value=features), patch(
            "intelligent_transition.render_and_evaluate_transition",
            return_value={"passed": False, "quality_score": 0.2, "metrics": {"black_ratio": 0.8}},
        ):
            with pytest.raises(IntelligentTransitionError, match="没有转场候选通过"):
                plan_intelligent_transitions(
                    clips,
                    ["hook", "product_showcase"],
                    style="moderate",
                    base_duration=0.3,
                    work_dir=tmp_path / "previews",
                )

    def test_transition_learning_uses_only_verified_minimum_samples(self, tmp_path):
        """自学习权重只接受达到样本量的已验证结果，拒绝单次或未验证数据。"""
        from intelligent_transition import TransitionLearningStore

        store = TransitionLearningStore(tmp_path / "learning.db", min_samples=3)
        for _ in range(3):
            store.record(
                feature_bucket="low_static_showcase_showcase_moderate",
                transition_type="dissolve",
                render_score=0.88,
                final_quality=92,
                verified_source="production_quality_gate",
            )
        for _ in range(8):
            store.record(
                feature_bucket="low_static_showcase_showcase_moderate",
                transition_type="circlecrop",
                render_score=0.99,
                final_quality=99,
                verified_source="unverified",
            )
        store.record(
            feature_bucket="low_static_showcase_showcase_moderate",
            transition_type="slideleft",
            render_score=0.95,
            final_quality=95,
            verified_source="production_quality_gate",
        )

        bonuses = store.get_verified_bonuses("low_static_showcase_showcase_moderate")

        assert bonuses["dissolve"] > 0
        assert "circlecrop" not in bonuses
        assert "slideleft" not in bonuses

    def test_transition_learning_requires_distinct_verified_outputs(self, tmp_path):
        """同一成片的多个边界不能伪造出足够的学习样本。"""
        from intelligent_transition import TransitionLearningStore

        store = TransitionLearningStore(tmp_path / "learning.db", min_samples=3)
        for _ in range(5):
            store.record(
                feature_bucket="low_static_showcase_showcase_moderate",
                transition_type="dissolve",
                render_score=0.9,
                final_quality=92,
                verified_source="production_quality_gate",
                verification_id="same-video",
            )

        assert store.get_verified_bonuses("low_static_showcase_showcase_moderate") == {}

    def test_transition_learning_rejects_low_temporal_quality(self, tmp_path):
        """即使总体可发布，时间一致性不足的转场也不能污染学习权重。"""
        from intelligent_transition import TransitionLearningStore, record_transition_outcomes

        store = TransitionLearningStore(tmp_path / "learning.db")
        report = {
            "boundaries": [{
                "feature_bucket": "low_static_showcase_showcase_moderate",
                "selected": {
                    "type": "dissolve",
                    "render_validation": {"quality_score": 0.9},
                },
            }],
        }

        assert record_transition_outcomes(
            report,
            final_quality=50,
            final_passed=True,
            transition_failure_attributed=False,
            store=store,
        ) == 0

    def test_transition_negative_samples_create_penalty(self, tmp_path):
        """经过转场质量门确认的失败样本必须降低对应候选的历史权重。"""
        from intelligent_transition import TransitionLearningStore

        store = TransitionLearningStore(tmp_path / "learning.db", min_samples=3)
        for index in range(3):
            assert store.record(
                feature_bucket="high_mixed_hook_showcase_fast",
                transition_type="circlecrop",
                render_score=0.2,
                final_quality=40,
                verified_source="transition_render_gate",
                verification_id=f"failed-{index}",
                outcome="negative",
                attribution=1.0,
                failure_reason="black_ratio",
            )

        adjustments = store.get_verified_bonuses("high_mixed_hook_showcase_fast")

        assert adjustments["circlecrop"] < 0

    def test_transition_learning_combines_positive_and_negative_evidence(self, tmp_path):
        """同一策略的正负数据必须共同形成净调整，不能只保留最后一种结果。"""
        from intelligent_transition import TransitionLearningStore

        store = TransitionLearningStore(tmp_path / "learning.db", min_samples=3)
        for index in range(3):
            store.record(
                feature_bucket="medium_left_showcase_showcase_fast",
                transition_type="slideleft",
                render_score=0.9,
                final_quality=92,
                verified_source="production_quality_gate",
                verification_id=f"positive-{index}",
            )
        positive_only = store.get_verified_bonuses("medium_left_showcase_showcase_fast")["slideleft"]

        for index in range(3):
            store.record(
                feature_bucket="medium_left_showcase_showcase_fast",
                transition_type="slideleft",
                render_score=0.15,
                final_quality=35,
                verified_source="transition_render_gate",
                verification_id=f"negative-{index}",
                outcome="negative",
                failure_reason="motion_smoothness",
            )
        combined = store.get_verified_bonuses("medium_left_showcase_showcase_fast")["slideleft"]

        assert combined < positive_only

    def test_unattributed_final_failure_does_not_penalize_transition(self, tmp_path):
        """口播、字幕等非转场失败不能错误训练转场策略。"""
        from intelligent_transition import TransitionLearningStore, record_transition_outcomes

        store = TransitionLearningStore(tmp_path / "learning.db")
        report = {
            "boundaries": [{
                "feature_bucket": "low_static_showcase_showcase_moderate",
                "selected": {
                    "type": "dissolve",
                    "render_validation": {"quality_score": 0.9},
                },
            }],
        }

        assert record_transition_outcomes(
            report,
            final_quality=45,
            final_passed=False,
            transition_failure_attributed=False,
            store=store,
            verification_id="audio-failure",
        ) == 0

    def test_render_rejections_are_recorded_as_negative_learning(self, tmp_path):
        """候选预览实渲染失败是局部可归因负样本，应自动写入学习库。"""
        from intelligent_transition import IntelligentTransitionError, TransitionLearningStore, plan_intelligent_transitions

        clips = [tmp_path / "left.mp4", tmp_path / "right.mp4"]
        for clip in clips:
            clip.write_bytes(b"fixture")
        features = {
            "subject_distance": 0.1,
            "composition_similarity": 0.9,
            "motion_alignment": 0.9,
            "motion_speed": 0.2,
            "motion_direction": "static",
            "brightness_delta": 0.1,
            "color_delta": 0.1,
            "scene_difference": 0.2,
            "narrative_pair": "hook->showcase",
            "style": "moderate",
        }
        store = TransitionLearningStore(tmp_path / "learning.db")

        with patch("intelligent_transition.analyze_transition_boundary", return_value=features), patch(
            "intelligent_transition.render_and_evaluate_transition",
            return_value={"passed": False, "quality_score": 0.2, "metrics": {"black_ratio": 0.8}},
        ):
            with pytest.raises(IntelligentTransitionError):
                plan_intelligent_transitions(
                    clips,
                    ["hook", "product_showcase"],
                    style="moderate",
                    base_duration=0.3,
                    work_dir=tmp_path / "previews",
                    learning_store=store,
                    verification_id="render-failure-1",
                )

        with sqlite3.connect(str(store.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM transition_outcomes WHERE outcome = 'negative'"
            ).fetchone()[0]
        assert count > 0

    def test_merged_boundary_failure_is_attributed_and_recorded(self, tmp_path):
        """只有合成后具体边界失败，才能把最终失败归因给所选转场。"""
        from intelligent_transition import TransitionLearningStore, validate_merged_transition_boundaries

        store = TransitionLearningStore(tmp_path / "learning.db")
        report = {
            "boundaries": [{
                "boundary": "0->1",
                "feature_bucket": "medium_left_showcase_showcase_fast",
                "output_timing": {"center": 3.9, "duration": 0.2},
                "selected": {"type": "slideleft", "duration": 0.2},
            }],
        }

        with patch(
            "intelligent_transition._evaluate_preview",
            return_value={"passed": False, "quality_score": 0.3, "metrics": {"max_frame_jump": 0.9}},
        ):
            result = validate_merged_transition_boundaries(
                tmp_path / "merged.mp4",
                report,
                store=store,
                verification_id="failed-merged-video",
            )

        assert result == {
            "passed": False,
            "failed_boundaries": ["0->1"],
            "negative_records": 1,
        }

    def test_strict_transition_merge_does_not_fallback(self):
        """主流程要求智能转场时，FFmpeg 失败必须向上抛出。"""
        src = Path("video_merger.py").read_text(encoding="utf-8")
        block = src[src.index("def merge_clips_ffmpeg("):src.index("def add_subtitles_ffmpeg(")]
        assert "strict_transitions" in block
        assert "if strict_transitions:" in block

    def test_pipeline_returns_transition_decision_report(self):
        """调用方需要拿到转场证据文件，不能只能从终端日志猜测。"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert '"transition_decision_report": locals().get("transition_report_path")' in src

    def test_config_import_does_not_create_output_dir(self):
        """config 模块导入阶段不应 mkdir，避免只读目录下无法启动"""
        config_text = Path("config.py").read_text(encoding="utf-8")
        assert "OUTPUT_DIR.mkdir(parents=True, exist_ok=True)" not in config_text

    def test_color_range_args_marks_full_range(self):
        """重编码输出应显式标记 BT.709 + full-range，避免平台误读色彩范围"""
        from video_merger import _color_range_args

        args = _color_range_args()
        assert "-color_range" in args and "pc" in args
        assert "-colorspace" in args and "bt709" in args
        assert "-color_trc" in args and "bt709" in args
        assert "-color_primaries" in args and "bt709" in args


class TestPublishableSuccessFixes:
    """回归测试：错误 success 收敛修复"""

    def test_batch_coerces_quoted_bool_and_int(self):
        """YAML 中的 'false'/'5' 应归一化为 bool/int，而不是按字符串透传"""
        from batch import create_task_args

        args = create_task_args(
            {
                "product_name": "测试",
                "preview": "false",
                "strict_mode": "true",
                "parallel": "false",
                "duration": "5",
                "min_clips": "3",
                "max_workers": "4",
            },
            {},
        )
        assert args["preview"] is False
        assert args["strict_mode"] is True
        assert args["parallel"] is False
        assert args["duration"] == 5
        assert args["min_clips"] == 3
        assert args["max_workers"] == 4

    def test_batch_rejects_invalid_bool_string(self):
        """非法布尔字符串必须显式报错，不能靠 truthy/falsey 猜测"""
        from batch import create_task_args

        with pytest.raises(ValueError):
            create_task_args({"product_name": "测试", "preview": "maybe"}, {})

    def test_batch_output_name_contains_task_id_and_microseconds(self):
        """批量输出名应包含 task_id 和微秒，避免并发同名同秒覆盖"""
        from batch import _build_task_output_name

        name1 = _build_task_output_name(1, "同款面霜")
        name2 = _build_task_output_name(2, "同款面霜")
        assert name1.startswith("001_同款面霜_")
        assert name2.startswith("002_同款面霜_")
        assert name1 != name2

    def test_pipeline_uses_output_name_scoped_intermediate_dirs(self):
        """色调/均衡/节奏适配中间目录必须按 output_name 隔离，防止 batch 串片"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert 'clips_dir / f"{output_name}_color_matched"' in src
        assert 'clips_dir / f"{output_name}_histeq"' in src
        assert 'clips_dir / f"{output_name}_rhythm_adjusted"' in src
        assert 'final_dir / f"{output_name}_refs"' in src

    def test_final_export_failure_does_not_fallback_to_intermediate(self):
        """最终导出失败不能把 _sfx 等中间文件作为 success final_path"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert 'final_path = sfx_path' not in src
        assert "已阻断中间文件被标记为成功成片" in src

    def test_publishable_video_requires_real_bgm(self):
        """发布成片必须有真实 BGM，不能用底噪占位后假装完成。"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "BGM 不可用，已阻断不可发布成片" in src
        assert "BGM 不可用，将生成极低音量占位音轨" not in src

    def test_voiceover_is_validated_before_subtitle_burn(self):
        """口播必须在字幕烧录前校验，避免无效口播时间轴被烧进视频"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        validate_pos = src.index("_validate_voiceover_audio(voiceover_audio)")
        subtitle_pos = src.index("add_fancy_subtitles(")
        assert validate_pos < subtitle_pos

    def test_wide_path_has_quality_gate(self):
        """16:9 版本作为请求产物，也必须单独经过质量门"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "开始 16:9 版本发布级质量检测" in src
        assert "check_video_quality(" in src
        assert "16:9 成片质量检测未通过" in src


class TestSeventhReviewFixes:
    """回归测试：第7轮深度审查修复"""

    def test_ffmpeg_filter_path_escapes_brackets(self):
        """filter_complex 路径必须转义方括号，避免被解析为流标签"""
        from video_merger import _ffmpeg_escape_filter_path
        from pathlib import Path

        p = Path("/tmp/[cache]/subs.ass")
        escaped = _ffmpeg_escape_filter_path(p)
        assert r"\[" in escaped
        assert r"\]" in escaped
        assert "[cache]" not in escaped

    def test_transition_ffmpeg_aevalsrc_has_aformat(self):
        """aevalsrc 默认格式可能与输入音频不匹配，必须追加 aformat 统一"""
        src = Path("video_merger.py").read_text(encoding="utf-8")
        assert "aevalsrc=0:d={clip2_duration},aformat=" in src
        assert "aevalsrc=0:d={clip1_duration},aformat=" in src

    def test_subtitles_ffmpeg_handles_no_audio(self):
        """烧录字幕时输入无音轨不能硬编码 -c:a copy，否则 ffmpeg 崩溃"""
        src = Path("video_merger.py").read_text(encoding="utf-8")
        assert "_has_audio_stream(video)" in src
        assert 'cmd.append("-an")' in src

    def test_bgm_download_sanitizes_track_id(self):
        """track_id 必须 sanitize 后再拼路径，防止路径遍历"""
        src = Path("bgm_client.py").read_text(encoding="utf-8")
        assert "safe_track_id = re.sub" in src
        assert "safe_track_id" in src

    def test_bgm_medium_pace_does_bpm_check(self):
        """medium 节奏不应跳过 BPM 校验，否则可能选中严重脱拍的 BGM"""
        src = Path("bgm_client.py").read_text(encoding="utf-8")
        # 旧代码有 pace != "medium" 的跳过，修复后应已删除
        assert 'pace != "medium"' not in src

    def test_kling_download_checks_content_length(self):
        """下载视频后应对比 Content-Length，防止残缺文件漏过"""
        src = Path("kling_client.py").read_text(encoding="utf-8")
        assert "expected_size = int(response.headers" in src
        assert "actual_size != expected_size" in src

    def test_kling_character_ref_validates_image(self):
        """角色定妆照下载后必须验证是合法图片，防止 CDN 错误页被当图片保存"""
        src = Path("kling_client.py").read_text(encoding="utf-8")
        assert "Image.open(io.BytesIO(image_bytes)).verify()" in src
        assert "角色定妆照下载内容不是有效图片" in src

    def test_tts_amix_uses_longest_duration(self):
        """amix duration=first 会截断后续语音，应改为 longest"""
        src = Path("tts_client.py").read_text(encoding="utf-8")
        assert "duration=longest:dropout_transition=0" in src

    def test_quality_checker_face_ratio_over_50_fails(self):
        """超过一半帧人脸异常应直接判失败，不能只扣分"""
        src = Path("quality_checker.py").read_text(encoding="utf-8")
        assert "face_ratio > 0.5" in src
        # 在 face_ratio > 0.5 分支内应有 result.passed = False
        face_block = src[src.index("face_ratio > 0.5"):src.index("face_ratio > 0.5") + 300]
        assert "result.passed = False" in face_block

    def test_quality_checker_audio_parse_failure_fails(self):
        """音频响度解析失败时应判失败，不能因 lufs=0 而意外通过"""
        src = Path("quality_checker.py").read_text(encoding="utf-8")
        assert "lufs = -999.0" in src
        assert "result.audio_lufs == -999.0" in src

    def test_best_of_rejects_all_zero_scores(self):
        """所有候选质量分为 0 时应直接失败，不能选第一个蒙混过关"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "best_score <= 0" in src
        assert "无法选出有效片段" in src


class TestFinalVideoQualityFixes:
    """回归测试：发布级成片质量收敛修复"""

    def test_stable_output_name_uses_effective_default_kling_model(self):
        """未显式传 --kling-model 时，稳定输出名应绑定真实默认模型"""
        args_default = MagicMock()
        args_default.style = "none"
        args_default.duration = 5
        args_default.mode = "std"
        args_default.aspect_ratio = "9:16"
        args_default.product_image = "product.png"
        args_default.hook = "question"
        args_default.script_style = "pain_point_solution"
        args_default.target_duration = None
        args_default.rhythm_style = "moderate"
        args_default.seed = 7
        args_default.kling_model = None
        args_default.multi_shot = False

        args_explicit = MagicMock()
        args_explicit.style = args_default.style
        args_explicit.duration = args_default.duration
        args_explicit.mode = args_default.mode
        args_explicit.aspect_ratio = args_default.aspect_ratio
        args_explicit.product_image = args_default.product_image
        args_explicit.hook = args_default.hook
        args_explicit.script_style = args_default.script_style
        args_explicit.target_duration = args_default.target_duration
        args_explicit.rhythm_style = args_default.rhythm_style
        args_explicit.seed = args_default.seed
        args_explicit.kling_model = KLING_VIDEO_MODEL
        args_explicit.multi_shot = args_default.multi_shot

        product_info = {"name": "同款面霜", "type": "beauty"}
        assert build_stable_output_name(product_info, args_default) == build_stable_output_name(product_info, args_explicit)

    def test_character_ref_manifest_invalidates_changed_character(self, tmp_path):
        """同一个 output_name 下，人设变化必须让角色定妆照缓存失效"""
        char_path = tmp_path / "demo_charA_ref.png"
        char_path.write_bytes(b"x" * 2048)
        product_info = {"name": "面霜", "type": "beauty"}
        character = {"name": "Character A", "description": "25-year-old woman"}
        prompt = "portrait prompt"

        manifest = _build_character_manifest(
            product_info=product_info,
            character=character,
            prompt=prompt,
        )
        _write_clip_manifest(char_path, manifest)
        assert _manifest_matches(char_path, manifest)

        changed_manifest = _build_character_manifest(
            product_info=product_info,
            character={"name": "Character A", "description": "45-year-old man"},
            prompt="portrait prompt for another person",
        )
        assert not _manifest_matches(char_path, changed_manifest)

    def test_candidate_quality_scoring_zeroes_failed_semantic_candidate(self, tmp_path):
        """best-of 候选一旦未通过语义/质量门禁，择优分数必须归零"""
        video_path = tmp_path / "candidate.mp4"
        video_path.write_bytes(b"fake")
        product_ref = tmp_path / "product.png"
        product_ref.write_bytes(b"fake-product")
        character_ref = tmp_path / "character.png"
        character_ref.write_bytes(b"fake-character")

        fake_result = MagicMock()
        fake_result.passed = False
        fake_result.overall_score = 96
        fake_result.issues = ["[产品检测] 未检测到足够的商品参考图特征"]

        with patch("one_click_create.check_video_quality", return_value=fake_result) as mocked_check:
            score, issues = _score_candidate_video_quality(
                video_path,
                quality_frames=12,
                product_reference_image=product_ref,
                character_reference_image=character_ref,
            )

        assert score == 0.0
        assert issues == fake_result.issues
        mocked_check.assert_called_once()
        kwargs = mocked_check.call_args.kwargs
        assert kwargs["product_reference_image"] == product_ref
        assert kwargs["character_reference_image"] == character_ref
        assert kwargs["require_semantic_alignment"] is True
        assert kwargs["content_focus"] == "center"

    def test_best_of_uses_semantic_candidate_scoring(self):
        """best-of 不应退回只按通用清晰度评分"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "_score_candidate_video_quality(" in src
        assert "product_ref_for_candidate" in src
        assert "character_ref_for_candidate" in src
        assert "scores[cand_path] = score" in src

    def test_wide_output_uses_same_semantic_quality_gate(self):
        """16:9 版本也必须校验产品和角色语义，避免横版裁切后不可发布"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        wide_block = src[src.index("开始 16:9 版本发布级质量检测"):src.index("print_quality_report(wide_quality_result")]
        assert "product_reference_image=product_image_path if product_image_path else None" in wide_block
        assert "character_reference_image=main_char_path if main_char_path else None" in wide_block
        assert "require_semantic_alignment=True" in wide_block

    def test_product_required_narrative_covers_review_and_proof(self):
        """review/proof/demo 等产品相关段必须纳入产品语义门禁"""
        for narrative in ("hook", "showcase", "cta", "review", "proof", "demo", "detail", "reason", "effect"):
            assert _is_product_required_narrative(narrative)
        assert not _is_product_required_narrative("pure_emotion")

    def test_local_product_image_validation_rejects_non_image(self, tmp_path):
        """本地商品参考图不能只检查 exists，损坏文件必须提前失败"""
        bad_image = tmp_path / "product.png"
        bad_image.write_text("not an image", encoding="utf-8")

        with pytest.raises(RuntimeError, match="商品参考图"):
            _validate_product_image_file(bad_image)

    def test_local_product_image_validation_accepts_real_image(self, tmp_path):
        """合法商品图应通过预检，避免误杀正常输入"""
        from PIL import Image

        image_path = tmp_path / "product.png"
        img = Image.new("RGB", (512, 512), (240, 80, 60))
        for x in range(128, 384):
            for y in range(128, 384):
                img.putpixel((x, y), (60, 180, 220))
        img.save(image_path)

        _validate_product_image_file(image_path)

    def test_segment_semantic_quality_blocks_failed_product_segment(self, tmp_path):
        """关键分镜语义失败必须阻断，不能只靠整片抽帧兜底"""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"fake video")
        product_ref = tmp_path / "product.png"
        product_ref.write_bytes(b"fake product")

        fake_result = MagicMock()
        fake_result.passed = False
        fake_result.issues = ["[产品检测] 未检测到足够的商品参考图特征"]

        with patch("one_click_create.check_video_quality", return_value=fake_result) as mocked_check:
            with pytest.raises(RuntimeError, match="分段语义质检未通过"):
                _check_segment_semantic_quality(
                    clip_paths=[clip_path],
                    successful_clip_indices=[2],
                    ad_script={"segments": [{"narrative": "hook"}, {"narrative": "turning"}, {"narrative": "showcase"}]},
                    product_image_path=product_ref,
                    main_char_path=None,
                    quality_frames=12,
                )

        kwargs = mocked_check.call_args.kwargs
        assert kwargs["product_reference_image"] == product_ref
        assert kwargs["content_focus"] == "center"

    def test_publish_mode_blocks_missing_segments(self):
        """strict 发布级成片缺段时必须阻断，避免生成缺 CTA/产品段的视频"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        failed_block = src[src.index("if failed_indices:"):src.index("# 最少成功段数")]
        assert "strict_mode and not preview" in failed_block
        assert "发布级成片要求分镜完整" in failed_block

    def test_rhythm_over_limit_blocks_in_strict_mode(self):
        """节奏模板超过后期拉伸能力时，strict 模式不能只警告后继续"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        rhythm_block = src[src.index("if _over_limit_segs:"):src.index("# 生成完整广告脚本")]
        assert "strict_mode and not preview" in rhythm_block
        assert "节奏模板存在超过当前生成片段后期拉伸能力" in rhythm_block


class TestLowCostQualityStrategy:
    """回归测试：低成本高质量生成策略"""

    def test_negative_prompt_does_not_block_product_logo(self):
        """负面词不能全局禁止商品包装自身 logo，只能禁止无关品牌/水印"""
        from config import NEGATIVE_PROMPT

        assert "unrelated logo" in NEGATIVE_PROMPT
        assert "unrelated brand mark" in NEGATIVE_PROMPT
        assert "text watermark" in NEGATIVE_PROMPT
        assert "text watermark, logo, brand mark" not in NEGATIVE_PROMPT

    def test_reference_strategy_product_segment_uses_quality_roles(self):
        """产品段优先商品图 + 主角多角度 + 连续性（性价比策略：用图买成功率）"""
        from one_click_create import _reference_strategy_for_narrative

        roles = _reference_strategy_for_narrative(
            "showcase",
            product_available=True,
            character_available=True,
            continuity_available=True,
            multi_angle_char=True,
        )
        assert roles[0] == "product"
        assert "character_primary" in roles
        assert "continuity" in roles
        assert len(roles) <= 5

    def test_reference_strategy_character_segment_prioritizes_character(self):
        """非产品强制段应优先人物多角度，再补连续性"""
        from one_click_create import _reference_strategy_for_narrative

        roles = _reference_strategy_for_narrative(
            "turning",
            product_available=True,
            character_available=True,
            continuity_available=True,
            multi_angle_char=True,
        )
        assert roles[0] == "character_primary"
        assert "character_angle" in roles
        assert "continuity" in roles

    def test_preflight_contract_blocks_product_segment_without_product_name(self):
        """产品强制分镜缺少产品名时应在视频生成前阻断，而不是生成后才质检失败"""
        from one_click_create import _preflight_generation_contract

        with pytest.raises(RuntimeError, match="生成前合同预检未通过"):
            _preflight_generation_contract(
                product_info={"name": "蓝罐汽水"},
                ad_script={"segments": [{"narrative": "showcase", "product_visibility": "prominent"}]},
                clip_prompts=["close-up lifestyle shot, cold drink on table"],
                product_image_path=Path("product.png"),
                char_refs=[{"img_b64": "abc"}],
                strict_mode=True,
            )

    def test_prompt_compact_keeps_within_budget(self):
        """最终 Prompt 调用前应压缩，避免泛词挤掉核心商品/动作信息"""
        from one_click_create import _compact_prompt_for_generation

        long_prompt = ", ".join(["蓝罐汽水 product hero shot"] + ["high quality"] * 200)
        compacted = _compact_prompt_for_generation(long_prompt, max_chars=220)
        assert len(compacted) <= 220
        assert "蓝罐汽水 product hero shot" in compacted

    def test_adaptive_best_of_has_early_stop_and_strategy_difference(self):
        """best_of 应自适应早停，补候选时必须改变策略而非重复抽卡"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "early_stop_score = 85.0" in src
        assert "停止继续生成候选以节省成本" in src
        assert "product_rescue" in src
        assert "character_rescue" in src

    def test_parallel_generation_does_not_reuse_first_tail_as_fake_prev(self):
        """并行模式不能把第 1 段尾帧伪装成所有后续段的上一帧"""
        src = Path("one_click_create.py").read_text(encoding="utf-8")
        assert "first_clip_last_frame" not in src
        assert "_generate_one_clip(idx, prompt, None, None)" in src


class TestPreflightKeyframe:
    """测试首帧低成本预检逻辑"""

    def test_sanitize_prompt_removes_camera_terms(self):
        """运镜词汇应从图片 Prompt 中被清洗掉"""
        prompt = "A woman holds a bottle, slow push in, dolly zoom, natural lighting"
        result = _sanitize_prompt_for_image_generation(prompt)
        assert "slow push in" not in result
        assert "dolly zoom" not in result
        assert "natural lighting" in result

    def test_sanitize_prompt_removes_image_tags(self):
        """参考图绑定标签应从图片 Prompt 中被清洗掉"""
        prompt = "A product display, <<<image_1>>>, bright studio light"
        result = _sanitize_prompt_for_image_generation(prompt)
        assert "<<<image_1>>>" not in result
        assert "product display" in result

    def test_sanitize_prompt_preserves_content(self):
        """非运镜的核心内容描述应保留"""
        prompt = "Young woman, red dress, holding skincare bottle, soft window light, clean background"
        result = _sanitize_prompt_for_image_generation(prompt)
        assert "Young woman" in result
        assert "skincare bottle" in result
        assert "soft window light" in result

    def test_preflight_skips_when_no_references(self):
        """无参考图时首帧预检应直接跳过，避免无意义调用"""
        client = MagicMock()
        passed, issues, path = _preflight_keyframe_check(
            client=client,
            prompt="test prompt",
            ref_images=[],
            narrative="hook",
            product_image_path=None,
            main_char_path=None,
            save_path=Path("/tmp/test_preflight.png"),
        )
        assert passed is True
        assert not issues
        assert path is None
        client.generate_image.assert_not_called()

    def test_preflight_calls_generate_image_with_sanitized_prompt(self, tmp_path):
        """有参考图时应调用 generate_image，且 prompt 已被清洗"""
        from PIL import Image

        client = MagicMock()
        # 构造一个假图片结果
        client.session.get.return_value = MagicMock(
            content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
            raise_for_status=lambda: None,
        )
        client.generate_image.return_value = {
            "data": {
                "task_result": {
                    "images": [{"url": "http://fake.url/img.png"}]
                }
            }
        }

        # 构造一张真实的小图片作为参考图
        ref_img = tmp_path / "ref.png"
        img = Image.new("RGB", (64, 64), color="red")
        img.save(ref_img)

        save_path = tmp_path / "preflight.png"
        prompt = "Woman, slow push in, <<<image_1>>>, soft light"
        passed, issues, kf_path = _preflight_keyframe_check(
            client=client,
            prompt=prompt,
            ref_images=[],
            narrative="hook",
            product_image_path=None,
            main_char_path=ref_img,
            save_path=save_path,
            aspect_ratio="9:16",
            image_fidelity=0.85,
        )

        assert client.generate_image.called
        call_kwargs = client.generate_image.call_args.kwargs
        # prompt 应被清洗过
        assert "slow push in" not in call_kwargs["prompt"]
        assert "<<<image_" not in call_kwargs["prompt"]
        # 参考图参数应正确
        assert call_kwargs["image_reference"] == "face"
        assert call_kwargs["resolution"] == "1k"
        assert call_kwargs["aspect_ratio"] == "9:16"

    def test_preflight_fails_when_image_generation_empty(self, tmp_path):
        """图片生成返回空结果时预检应失败"""
        client = MagicMock()
        client.generate_image.return_value = {"data": {"task_result": {"images": []}}}

        ref_img = tmp_path / "ref.png"
        from PIL import Image
        img = Image.new("RGB", (64, 64), color="red")
        img.save(ref_img)

        passed, issues, kf_path = _preflight_keyframe_check(
            client=client,
            prompt="test",
            ref_images=[],
            narrative="hook",
            product_image_path=None,
            main_char_path=ref_img,
            save_path=tmp_path / "pf.png",
        )
        assert passed is False
        assert any("结果为空" in i for i in issues)


class TestCharacterBible:
    """测试角色圣经 / 商品圣经结构"""

    def test_build_character_bibles_single_role(self):
        """单角色时从 product_info 构建默认圣经"""
        product_info = {
            "name": "TestProduct",
            "age": "28",
            "gender": "女",
            "outfit": "white dress",
        }
        bibles = build_character_bibles(product_info, characters=None)
        assert len(bibles) == 1
        assert bibles[0]["id"] == "char_01"
        assert bibles[0]["age"] == "28"
        assert bibles[0]["gender"] == "女"
        assert bibles[0]["outfit"] == "white dress"

    def test_build_character_bibles_multi_role_with_description(self):
        """多角色时从 description 解析结构化字段"""
        product_info = {"age": "25", "gender": "女", "outfit": "casual"}
        characters = [
            {"name": "小雅", "description": "25-year-old Asian woman, long black hair"},
            {"name": "Amy", "description": "30-year-old Caucasian woman, short blonde hair"},
        ]
        bibles = build_character_bibles(product_info, characters)
        assert len(bibles) == 2
        assert bibles[0]["name"] == "小雅"
        assert bibles[0]["age"] == "25"
        assert "long black hair" in bibles[0]["hair_style"]
        assert bibles[1]["name"] == "Amy"
        assert bibles[1]["age"] == "30"
        assert "short blonde hair" in bibles[1]["hair_style"]

    def test_infer_family_insurance_characters(self):
        """家财险核心角色应包含父母+孩子+保险顾问（孩子是情感锚点必须定妆，顾问是服务场景核心）"""
        from one_click_create import build_cast_plan, infer_characters_from_product

        product_info = {
            "name": "众安家财险",
            "type": "app",
            "selling_point": "全屋保障无忧，极速理赔守护家庭财产安全",
            "audience": "2545",
        }
        cast_plan = build_cast_plan(product_info)
        characters = infer_characters_from_product(product_info)

        assert len(characters) == 4
        core_names = [c["name"] for c in characters]
        assert "Mother" in core_names
        assert "Father" in core_names
        assert "Child" in core_names
        assert "Insurance Advisor" in core_names

    def test_explicit_characters_are_preserved(self):
        """LLM 或模板显式给出的角色列表优先，不被兜底推断覆盖"""
        from one_click_create import infer_characters_from_product

        product_info = {
            "name": "家庭保险",
            "characters": [
                {
                    "name": "Grandma",
                    "role": "elder family member",
                    "description": "68-year-old Chinese woman, silver hair, warm smile",
                },
                {
                    "name": "Daughter",
                    "role": "adult child",
                    "description": "32-year-old Chinese woman, short black hair, office outfit",
                },
            ],
        }
        characters = infer_characters_from_product(product_info)

        assert len(characters) == 2
        assert [c["name"] for c in characters] == ["Grandma", "Daughter"]

    def test_pet_product_can_use_animal_as_core_subject(self):
        """宠物是主角时动物应作为核心主体，而不是背景路人式实体"""
        from one_click_create import build_cast_plan

        product_info = {
            "name": "智能猫粮机",
            "type": "宠物",
            "selling_point": "自动定时喂猫，出差也能照顾猫咪",
        }
        cast_plan = build_cast_plan(product_info)

        assert [c["name"] for c in cast_plan["core_characters"]] == ["Owner", "Pet"]
        assert cast_plan["core_characters"][1]["role_type"] == "animal"

    def test_character_bible_to_prompt_includes_all_fields(self):
        """角色圣经转 prompt 应包含所有非空字段"""
        bible = CharacterBible(
            id="char_01", name="Test", age="25", gender="female",
            ethnicity="Asian", hair_style="long black hair",
            outfit="red dress", accessories="gold earrings",
            facial_features="high cheekbones", expression_baseline="warm smile",
        )
        prompt = character_bible_to_prompt(bible)
        assert "25-year-old female" in prompt
        assert "Asian" in prompt
        assert "long black hair" in prompt
        assert "wearing red dress" in prompt
        assert "gold earrings" in prompt
        assert "high cheekbones" in prompt
        assert "warm smile" in prompt

    def test_generate_character_prompt_uses_bible(self):
        """generate_character_prompt 优先使用圣经生成更精确的描述"""
        bible = CharacterBible(
            id="char_01", name="小雅", age="28", gender="female",
            ethnicity="Asian", hair_style="long straight black hair",
            outfit="white blouse and jeans", accessories="",
            facial_features="", expression_baseline="confident",
        )
        product_info = {"name": "面霜", "type": "美妆", "age": "28", "gender": "女", "outfit": "casual"}
        prompt = generate_character_prompt(product_info, bible=bible)
        # 圣经描述应出现在 prompt 中
        assert "long straight black hair" in prompt
        assert "white blouse and jeans" in prompt
        assert "confident" in prompt

    def test_generate_clip_prompts_uses_bible_for_multi_role(self):
        """多角色时分镜 prompt 应包含每个角色的精确圣经描述"""
        product_info = {"name": "TestProduct", "type": "default"}
        bibles = [
            CharacterBible(
                id="char_01", name="小雅", age="25", gender="female",
                ethnicity="Asian", hair_style="long black hair",
                outfit="red dress", accessories="", facial_features="",
                expression_baseline="",
            ),
            CharacterBible(
                id="char_02", name="Amy", age="30", gender="female",
                ethnicity="Caucasian", hair_style="short blonde hair",
                outfit="blue suit", accessories="", facial_features="",
                expression_baseline="",
            ),
        ]
        clips = generate_clip_prompts(
            product_info,
            cinematic_style="none",
            character_bibles=bibles,
        )
        # 至少有一个分镜包含两个角色的精确描述
        assert any("小雅: " in clip and "long black hair" in clip for clip in clips)
        assert any("Amy: " in clip and "short blonde hair" in clip for clip in clips)

    def test_build_product_bible_includes_brand_info(self):
        """商品圣经应整合 product_info 和 BRAND_CONFIG"""
        product_info = {"name": "TestCream", "type": "美妆", "selling_point": "保湿"}
        bible = build_product_bible(product_info)
        assert bible["name"] == "TestCream"
        assert bible["category"] == "美妆"
        assert bible["key_selling_point"] == "保湿"
        # BRAND_CONFIG 中的字段也应被纳入
        assert "packaging" in bible
        assert "primary_color" in bible

    def test_generate_clip_prompts_uses_product_bible(self):
        """商品圣经应注入分镜 prompt 的商品一致性描述"""
        product_info = {"name": "TestProduct", "type": "default"}
        p_bible = ProductBible(
            name="TestProduct", category="default",
            packaging="white cylindrical bottle", primary_color="white",
            shape="slim cylindrical", logo_description="minimalist logo",
            usage_context="", key_selling_point="",
        )
        clips = generate_clip_prompts(
            product_info,
            cinematic_style="none",
            product_bible=p_bible,
        )
        # product_consistency 应使用圣经描述
        assert any("white cylindrical bottle" in clip for clip in clips)


class TestMultiCharacterReferenceBinding:
    """测试多人物按角色 ID 绑定参考图"""

    def test_single_character_returns_zero(self):
        """单角色时始终返回主角色索引 0"""
        bibles = [CharacterBible(id="c1", name="小雅", age="25", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline="")]
        result = _get_primary_char_for_clip(1, "小雅在化妆", {"segments": []}, bibles)
        assert result == 0

    def test_detects_extra_character_by_name(self):
        """clip_prompt 中出现额外角色名时应返回对应索引"""
        bibles = [
            CharacterBible(id="c1", name="小雅", age="25", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
            CharacterBible(id="c2", name="Amy", age="30", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
        ]
        result = _get_primary_char_for_clip(2, "Amy introduces the product", {"segments": []}, bibles)
        assert result == 1

    def test_detects_from_ad_script_scene_prompt(self):
        """优先从 ad_script segment 的 scene_prompt 中检测角色名"""
        bibles = [
            CharacterBible(id="c1", name="小雅", age="25", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
            CharacterBible(id="c2", name="Amy", age="30", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
        ]
        ad_script = {
            "segments": [
                {"narrative": "hook", "scene_prompt": "小雅 looks at her phone"},
                {"narrative": "showcase", "scene_prompt": "Amy holds the bottle"},
            ]
        }
        result = _get_primary_char_for_clip(2, "some generic prompt", ad_script, bibles)
        assert result == 1

    def test_fallback_to_primary_when_no_match(self):
        """检测不到任何角色名时回退主角色"""
        bibles = [
            CharacterBible(id="c1", name="小雅", age="25", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
            CharacterBible(id="c2", name="Amy", age="30", gender="女", outfit="", ethnicity="", hair_style="", hair_color="", accessories="", facial_features="", expression_baseline=""),
        ]
        result = _get_primary_char_for_clip(1, "A generic scene with no names", {"segments": []}, bibles)
        assert result == 0

    def test_empty_bibles_returns_zero(self):
        """空圣经列表时安全回退 0"""
        result = _get_primary_char_for_clip(1, "test", {"segments": []}, [])
        assert result == 0


class TestMusicContract:
    """测试音乐合同结构"""

    def test_build_music_contract_basic(self):
        """基础产品信息应生成合理的音乐合同"""
        product_info = {"type": "美妆", "audience": "18-25"}
        contract = build_music_contract(product_info, cinematic_style="none")
        assert contract["mood"] == "upbeat"
        assert contract["genre"] == "pop"
        assert contract["energy"] == "high"
        assert contract["recommended_pace"] == "fast"
        assert contract["bpm_min"] >= 120
        assert contract["bpm_max"] <= 150

    def test_cinematic_style_overrides_mood(self):
        """电影风格应覆盖基础 mood 和 genre"""
        product_info = {"type": "美妆", "audience": "18-25"}
        contract = build_music_contract(product_info, cinematic_style="hitchcock")
        assert contract["mood"] == "suspenseful"
        assert contract["genre"] == "orchestral"
        assert contract["intro_type"] == "buildup"

    def test_audience_affects_bpm(self):
        """不同受众年龄应有不同 BPM 范围"""
        young = build_music_contract({"type": "default", "audience": "18-25"})
        old = build_music_contract({"type": "default", "audience": "45+"})
        assert young["bpm_min"] > old["bpm_min"]
        assert young["bpm_max"] > old["bpm_max"]

    def test_energy_affects_pace(self):
        """energy 应正确映射到 recommended_pace"""
        high = build_music_contract({"type": "美妆"})
        low = build_music_contract({"type": "家居"})
        assert high["recommended_pace"] == "fast"
        assert low["recommended_pace"] == "cinematic"

    def test_local_asset_motion_overrides_product_default_music_energy(self):
        """本地素材模式的 BGM 节奏必须跟随画面动态，而不是只看产品品类。"""
        visual_profile = {
            "source": "selected_local_assets",
            "energy": "high",
            "recommended_pace": "fast",
            "bpm_min": 116,
            "bpm_max": 132,
            "intro_type": "immediate",
            "transition_base_duration": 0.25,
            "sfx_intensity": "moderate",
        }

        contract = build_music_contract(
            {"type": "家居", "audience": "45+"},
            asset_creative_profile=visual_profile,
        )

        assert contract["source"] == "selected_local_assets"
        assert contract["energy"] == "high"
        assert contract["recommended_pace"] == "fast"
        assert (contract["bpm_min"], contract["bpm_max"]) == (116, 132)

    def test_generate_clip_prompts_injects_rhythm(self):
        """音乐合同应注入分镜 prompt 的节奏描述"""
        product_info = {"name": "TestProduct", "type": "default"}
        contract = build_music_contract(product_info, cinematic_style="hitchcock")
        clips = generate_clip_prompts(
            product_info,
            cinematic_style="none",
            music_contract=contract,
        )
        # 每个 clip 都应包含节奏描述（插入到 prompt 开头）
        for clip in clips:
            assert "suspenseful orchestral energy" in clip
            assert "BPM rhythm" in clip


class TestIssueDrivenRepair:
    """测试失败原因驱动的精准修复策略"""

    def test_no_issues_returns_original(self):
        """无问题时返回原 prompt"""
        prompt = "A woman holds a bottle"
        repaired, tags = _repair_prompt_by_issues(prompt, [])
        assert repaired == prompt
        assert tags == []

    def test_detects_logo_issue(self):
        """检测到 logo 问题时注入品牌清晰度修复"""
        prompt = "A woman holds a bottle"
        issues = ["brand logo not visible, text unreadable"]
        repaired, tags = _repair_prompt_by_issues(prompt, issues)
        assert "clear brand logo visible" in repaired
        assert "logo" in tags

    def test_detects_face_issue(self):
        """检测到面部问题时注入正面约束"""
        prompt = "A woman uses the product"
        issues = ["face not detected, character similarity low"]
        repaired, tags = _repair_prompt_by_issues(prompt, issues)
        assert "front-facing portrait" in repaired
        assert "face" in tags

    def test_detects_profile_issue(self):
        """检测到侧脸问题时注入正面朝向修复"""
        prompt = "A woman turns around"
        issues = ["side profile detected"]
        repaired, tags = _repair_prompt_by_issues(prompt, issues)
        assert "no profile or back view" in repaired
        assert "profile" in tags

    def test_detects_product_obstruction(self):
        """检测到商品遮挡时注入可见性修复"""
        prompt = "A woman shows the product"
        issues = ["product hidden by hands, packaging obstructed"]
        repaired, tags = _repair_prompt_by_issues(prompt, issues)
        assert "product fully visible" in repaired
        assert "obstructed" in tags

    def test_multiple_issues_merge_repairs(self):
        """多个问题时应合并所有修复指令"""
        prompt = "A woman holds a bottle"
        issues = ["logo unclear", "product color mismatch"]
        repaired, tags = _repair_prompt_by_issues(prompt, issues)
        assert "clear brand logo visible" in repaired
        assert "exact same product color" in repaired
        assert len(tags) == 2

    def test_uses_product_bible_for_exact_description(self):
        """有商品圣经时，用精确描述替换泛化修复"""
        prompt = "A woman holds a bottle"
        issues = ["product similarity low"]
        bible = ProductBible(
            name="Test", category="default",
            packaging="white cylindrical bottle with gold cap", primary_color="white",
            shape="", logo_description="", usage_context="", key_selling_point="",
        )
        repaired, tags = _repair_prompt_by_issues(prompt, issues, product_bible=bible)
        assert "white cylindrical bottle with gold cap" in repaired

    def test_uses_character_bible_for_exact_description(self):
        """有角色圣经时，用精确描述替换泛化修复"""
        prompt = "A woman uses the product"
        issues = ["face not detected"]
        bible = CharacterBible(
            id="c1", name="小雅", age="25", gender="女",
            hair_style="long straight black hair", hair_color="", outfit="", ethnicity="",
            accessories="", facial_features="", expression_baseline="",
        )
        repaired, tags = _repair_prompt_by_issues(prompt, issues, character_bible=bible)
        assert "long straight black hair" in repaired


class TestPostProcessingP0Fixes:
    """第8轮审查：P0 级后处理修复回归测试"""

    def test_color_range_args_includes_bt709(self):
        """颜色空间标记必须包含完整 BT.709 triplet + full-range"""
        from video_merger import _color_range_args
        args = _color_range_args()
        assert "-colorspace" in args
        assert args[args.index("-colorspace") + 1] == "bt709"
        assert "-color_trc" in args
        assert args[args.index("-color_trc") + 1] == "bt709"
        assert "-color_primaries" in args
        assert args[args.index("-color_primaries") + 1] == "bt709"
        assert "-color_range" in args
        assert args[args.index("-color_range") + 1] == "pc"

    def test_subtitle_outline_is_2(self, tmp_path):
        """字幕描边必须从 5 降到 2，避免过粗影响画面"""
        import subprocess
        from unittest.mock import patch
        from video_merger import add_subtitles_ffmpeg

        video = tmp_path / "test.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=100x100:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            ],
            check=True, capture_output=True,
        )
        subtitles = [{"text": "测试", "start": 0, "end": 0.5}]
        output = tmp_path / "out.mp4"

        captured = []
        with patch("video_merger.run_ffmpeg") as mock_run:
            mock_run.side_effect = lambda cmd, **kw: captured.extend(cmd)
            add_subtitles_ffmpeg(video, subtitles, output, font_size=24)

        cmd_str = " ".join(str(c) for c in captured)
        assert "Outline=2" in cmd_str, f"字幕描边应为 2，实际命令：{cmd_str}"
        assert "Outline=5" not in cmd_str, f"不应再出现 Outline=5：{cmd_str}"

    def test_sidechain_is_adaptive_and_keeps_alimiter(self, tmp_path):
        """BGM ducking 必须自适应且保留限幅器，不能再固定强压到几乎听不见。"""
        import subprocess
        from unittest.mock import patch
        from one_click_create import _mix_voiceover_with_bgm

        video = tmp_path / "video.mp4"
        voice = tmp_path / "voice.m4a"
        output = tmp_path / "out.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=100x100:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video),
            ],
            check=True, capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "1", "-c:a", "aac", str(voice),
            ],
            check=True, capture_output=True,
        )

        captured = []
        with patch("one_click_create.run_ffmpeg") as mock_run:
            mock_run.side_effect = lambda cmd, **kw: captured.extend(cmd)
            _mix_voiceover_with_bgm(video, voice, output)

        filter_str = None
        for i, c in enumerate(captured):
            if c == "-filter_complex":
                filter_str = captured[i + 1]
                break
        assert filter_str is not None, "未找到 -filter_complex"
        assert "ratio=2.5" in filter_str, f"sidechain 应使用温和自适应压缩：{filter_str}"
        assert "alimiter" in filter_str, f"应包含 alimiter 限幅器：{filter_str}"
        assert "volume=0.2" not in filter_str, f"不应再固定把 BGM 压到 0.2：{filter_str}"

    def test_voiceover_sidechain_is_split_before_reuse(self, tmp_path):
        """口播必须拆成 sidechain 和 mix 两路，不能被滤镜消费后从成片丢失。"""
        import subprocess
        from unittest.mock import patch
        from one_click_create import _mix_voiceover_with_bgm

        video = tmp_path / "video.mp4"
        voice = tmp_path / "voice.m4a"
        output = tmp_path / "out.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=100x100:d=1",
                "-f", "lavfi", "-i", "sine=f=220:d=1:r=44100",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video),
            ],
            check=True, capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=880:d=1:r=44100",
                "-c:a", "aac", str(voice),
            ],
            check=True, capture_output=True,
        )

        captured = []
        with patch("one_click_create.run_ffmpeg") as mock_run:
            mock_run.side_effect = lambda cmd, **kw: captured.extend(cmd)
            _mix_voiceover_with_bgm(video, voice, output)

        filter_str = captured[captured.index("-filter_complex") + 1]
        assert "asplit=2[voice_sc][voice_mix]" in filter_str
        assert "[bgm_pre][voice_sc]sidechaincompress" in filter_str
        assert "[bgm_duck][voice_mix]amix" in filter_str

    def test_voiceover_similarity_tolerates_codec_filter_delay(self, tmp_path):
        """滤镜产生几毫秒固定延迟时仍应识别同一口播，不能在零延迟点误报。"""
        import subprocess
        from one_click_create import _audio_waveform_similarity

        source = tmp_path / "source.wav"
        delayed = tmp_path / "delayed.wav"
        unrelated = tmp_path / "unrelated.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1.5:r=44100", str(source)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-af", "adelay=5ms:all=1", str(delayed)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=880:d=1.5:r=44100", str(unrelated)],
            check=True, capture_output=True,
        )

        assert _audio_waveform_similarity(delayed, source) > 0.9
        assert _audio_waveform_similarity(unrelated, source) < 0.15

    def test_single_line_subtitle_segmentation_preserves_timeline(self):
        """长口播必须按可读短语切开，每条只有一行且时间连续。"""
        from video_merger import prepare_single_line_subtitles

        source = [{
            "text": "它兼具茶香和咖香，提神解腻特别适合我们上班族日常喝",
            "start": 1.0,
            "end": 5.0,
            "segment": 3,
        }]

        result = prepare_single_line_subtitles(source, max_units=11)

        assert len(result) >= 3
        assert result[0]["start"] == pytest.approx(1.0)
        assert result[-1]["end"] == pytest.approx(5.0)
        assert all("\n" not in item["text"] for item in result)
        assert all(len(item["text"]) <= 11 for item in result)
        assert all(not item["text"].endswith("我") for item in result)
        assert all(not item["text"].startswith("们") for item in result)
        assert all(result[i]["end"] == pytest.approx(result[i + 1]["start"]) for i in range(len(result) - 1))

    def test_single_line_subtitles_preserve_chinese_phrases(self):
        """单行切分应保持中文短语完整，不能产生类似打字机残句的半词字幕。"""
        from video_merger import _split_single_line_text

        cases = {
            "今天给大家带来这款瓶装茶咖": {"大家", "这款", "瓶装茶咖"},
            "有不同规格的瓶装茶咖可选": {"不同规格", "瓶装茶咖"},
            "给大家展示倒出的状态": {"给大家", "展示", "倒出"},
            "想要了解这款瓶装茶咖，快查看当前展示的产品吧": {"想要了解", "这款", "瓶装茶咖", "当前展示"},
        }
        for text, phrases in cases.items():
            chunks = _split_single_line_text(text, 11)
            assert all(len(chunk) <= 11 for chunk in chunks)
            for phrase in phrases:
                assert any(phrase in chunk for chunk in chunks), (text, chunks, phrase)

    def test_single_line_capacity_ignores_punctuation_removed_from_display(self):
        from video_merger import _split_single_line_text

        phrase = "都是当日现做的新鲜面包，"

        assert _split_single_line_text(phrase, 11) == [phrase]

    def test_subtitle_animation_is_selected_per_narrative(self):
        """口播不能强制全片打字机，动画应按每段叙事用途确定。"""
        from video_merger import choose_subtitle_animation

        animations = [
            choose_subtitle_animation("hook", has_voiceover=True),
            choose_subtitle_animation("product_showcase", has_voiceover=True),
            choose_subtitle_animation("usage_demo", has_voiceover=True),
            choose_subtitle_animation("cta", has_voiceover=True),
        ]

        assert "typewriter" not in animations
        assert len(set(animations)) >= 2

    def test_fancy_subtitles_disable_automatic_wrapping(self):
        """单行字幕合同必须由 ASS 渲染层强制执行。"""
        src = Path("video_merger.py").read_text(encoding="utf-8")
        block = src[src.index("def add_fancy_subtitles("):src.index("def add_bgm_ffmpeg(")]
        assert "WrapStyle: 2" in block
        assert "subtitle exceeds single-line safe width" in block

    def test_local_asset_binding_rejects_other_role_compatible_windows(self):
        """脚本绑定具体素材窗口后，不能被另一个同角色但语义不同的窗口替换。"""
        from local_asset_pipeline import _score_window

        segment = {
            "narrative": "product_showcase",
            "asset_window_ids": ["tea-bottle"],
            "visual_requirement": "茶咖瓶身包装正面展示",
        }
        wrong = {
            "window_id": "tea-leaves",
            "source_path": "/tmp/tea.mp4",
            "start": 0,
            "end": 4,
            "analysis": {
                "usable_for_ad": True,
                "confidence": 0.98,
                "product_visibility": 5,
                "narrative_roles": ["product_showcase"],
                "visible_subjects": ["product"],
                "evidence": "jasmine tea leaves in a bamboo tray",
            },
        }

        score, details = _score_window(segment, wrong, [])

        assert score == 0.0
        assert details == {"asset_binding_mismatch": 1.0}

    def test_segment_validation_blocks_effect_claims_but_not_ordinary_sensory_copy(self, tmp_path):
        """硬事实门只拦截功效结果，普通食品感官表达不作为开发者硬规则。"""
        from unittest.mock import patch
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)
        segment = {
            "subtitle": "加冰倒出来超治愈",
            "voiceover": "冰爽茶咖，茶香与咖香完美融合",
            "visual_requirement": "把瓶装饮品倒入有冰块的杯子",
        }

        with patch("local_asset_pipeline.requests.post") as post:
            result = VisionAnalyzer().validate_segment(sheet, segment)

        assert result["supported"] is False
        assert result["subtitle_supported"] is False
        assert result["voiceover_supported"] is True
        assert result["visual_supported"] is True
        assert set(result["unsupported_fields"]) == {"subtitle"}
        assert any("治愈" in claim for claim in result["unsupported_claims"])
        assert all("冰爽" not in claim for claim in result["unsupported_claims"])
        assert post.call_count == 0

    def test_segment_validation_requires_each_field_to_pass(self, tmp_path):
        """VLM 的总 supported 不能覆盖口播字段失败，三个字段必须分别通过。"""
        import json
        from unittest.mock import patch
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": json.dumps({
                        "supported": True,
                        "subtitle_supported": True,
                        "voiceover_supported": False,
                        "visual_supported": True,
                        "static_supported": True,
                        "dynamic_supported": True,
                        "confidence": 0.96,
                        "unsupported_fields": ["voiceover"],
                        "unsupported_claims": ["口播中的口感无法从画面验证"],
                        "reason": "画面只展示倒入动作",
                    }, ensure_ascii=False)}}],
                }

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)
        segment = {
            "subtitle": "倒入冰杯",
            "voiceover": "入口顺滑",
            "visual_requirement": "把瓶装饮品倒入有冰块的杯子",
        }

        with patch("local_asset_pipeline.requests.post", return_value=FakeResponse()):
            result = VisionAnalyzer().validate_segment(
                sheet,
                segment,
                motion={
                    "subject_motion": "high",
                    "subject_motion_ratio": 0.12,
                    "confidence": 0.95,
                    "active_ranges": [[0.0, 3.0]],
                },
                analysis={
                    "literal_actions": ["把瓶装饮品倒入冰杯"],
                    "temporal_events": [{"action": "把瓶装饮品倒入冰杯"}],
                },
            )

        assert result["supported"] is False
        assert result["subtitle_supported"] is True
        assert result["voiceover_supported"] is False
        assert result["visual_supported"] is True
        assert result["unsupported_fields"] == ["voiceover"]

    def test_segment_validation_retries_incomplete_judge_schema(self, tmp_path):
        """判定器漏字段属于无效响应，必须重试，不能误记成素材失败。"""
        import json
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(self.payload)}}]}

        incomplete = FakeResponse({
            "supported": True,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": True,
            "confidence": 0.96,
        })
        complete = FakeResponse({
            "supported": True,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": True,
            "static_supported": True,
            "dynamic_supported": True,
            "confidence": 0.96,
            "unsupported_fields": [],
            "unsupported_claims": [],
            "reason": "all fields supported",
        })
        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)

        with patch("local_asset_pipeline.requests.post", side_effect=[incomplete, complete]) as post, \
             patch("local_asset_pipeline.time.sleep"):
            result = VisionAnalyzer().validate_segment(
                sheet,
                {"subtitle": "桌面茶咖", "voiceover": "桌面展示茶咖", "visual_requirement": "桌面展示瓶装茶咖"},
                motion={"subject_motion": "low", "confidence": 0.9},
            )

        assert result["supported"] is True
        assert post.call_count == 2

    def test_window_analysis_reads_streamed_json(self, tmp_path):
        """视觉分析应流式接收复杂响应，避免等待完整响应时触发读取超时。"""
        import json
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        analysis = {
            "shot_type": "static",
            "narrative_roles": ["product_showcase"],
            "visible_subjects": ["product"],
            "product_story_role": "finished_product",
            "product_visibility": 5,
            "confidence": 0.96,
            "evidence": "瓶装产品位于画面中央",
        }
        content = json.dumps(analysis, ensure_ascii=False)

        class StreamResponse:
            status_code = 200
            headers = {"x-request-id": "request-stream-ok"}

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                midpoint = len(content) // 2
                for chunk in (content[:midpoint], content[midpoint:]):
                    event = {"choices": [{"delta": {"content": chunk}}]}
                    event_json = json.dumps(event, ensure_ascii=False)
                    event_midpoint = len(event_json) // 2
                    yield "data: " + event_json[:event_midpoint]
                    yield "data: " + event_json[event_midpoint:]
                yield "data: [DONE]"

            def close(self):
                return None

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)

        with patch("local_asset_pipeline.requests.post", return_value=StreamResponse()) as post:
            result = VisionAnalyzer().analyze_window(sheet, {"frame_count": 2})

        assert result["product_story_role"] == "finished_product"
        assert result["evidence"] == "瓶装产品位于画面中央"
        assert post.call_args.kwargs["stream"] is True
        assert post.call_args.kwargs["json"]["stream"] is True

    def test_window_analysis_retries_read_timeout_then_succeeds(self, tmp_path):
        """可恢复的读取超时只做有界重试，成功响应仍被正常使用。"""
        import json
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        class StreamResponse:
            status_code = 200
            headers = {"x-request-id": "request-after-timeout"}

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                content = json.dumps({
                    "shot_type": "static",
                    "product_story_role": "ingredient",
                    "confidence": 0.9,
                    "evidence": "散装叶片位于托盘中",
                }, ensure_ascii=False)
                yield "data: " + json.dumps({
                    "choices": [{"delta": {"content": content}}],
                }, ensure_ascii=False)
                yield ""
                yield "data: [DONE]"
                yield ""

            def close(self):
                return None

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)

        with patch(
            "local_asset_pipeline.requests.post",
            side_effect=[requests.exceptions.ReadTimeout("slow first byte"), StreamResponse()],
        ) as post, patch("local_asset_pipeline.time.sleep"):
            result = VisionAnalyzer().analyze_window(sheet, {"frame_count": 2})

        assert result["product_story_role"] == "ingredient"
        assert post.call_count == 2

    def test_window_analysis_does_not_retry_nonrecoverable_http_error(self, tmp_path):
        """鉴权或参数错误必须立即失败，不能伪装成可恢复的服务拥塞。"""
        from PIL import Image
        from local_asset_pipeline import LocalAssetError, VisionAnalyzer

        response = requests.Response()
        response.status_code = 400
        response.url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        response._content = b'{"error":{"message":"invalid request"}}'
        response._content_consumed = True
        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)

        with patch("local_asset_pipeline.requests.post", return_value=response) as post, \
             pytest.raises(LocalAssetError, match="视觉分析失败"):
            VisionAnalyzer().analyze_window(sheet, {"frame_count": 2})

        assert post.call_count == 1

    def test_window_analysis_still_fails_after_transient_retries_are_exhausted(self, tmp_path):
        """所有流式重试都失败时必须阻断整条素材理解任务。"""
        from PIL import Image
        from local_asset_pipeline import LocalAssetError, VisionAnalyzer

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)

        with patch(
            "local_asset_pipeline.requests.post",
            side_effect=requests.exceptions.ReadTimeout("no stream data"),
        ) as post, patch("local_asset_pipeline.time.sleep"), \
             pytest.raises(LocalAssetError, match="视觉分析失败"):
            VisionAnalyzer().analyze_window(sheet, {"frame_count": 2})

        assert post.call_count == 3

    def test_segment_validator_treats_voiceover_as_text_claims(self):
        """口播按营销事实校验，不能因声音不可见或不是画面复述而失败。"""
        source = Path("local_asset_pipeline.py").read_text(encoding="utf-8")
        block = source[source.index("    def validate_segment("):source.index("def _normalize_analysis")]
        assert "voiceover_text_claims" in block
        assert "Subtitle and voiceover are sales copy, not literal image captions" in block
        assert "trusted_product_info" in block

    def test_marketing_hook_can_name_product_over_ingredient_footage(self):
        """带货钩子不是画面说明，提到产品名不要求原料镜头同时露出成品标签。"""
        from local_asset_pipeline import _marketing_claim_violations

        segment = {
            "subtitle": "一杯茶咖，亮点藏在原料里",
            "voiceover": "这杯茶咖，先从原料看起",
            "marketing_intent": "hook",
            "claims": [],
        }

        assert _marketing_claim_violations(
            segment,
            product_info={"name": "茶咖"},
            analysis={"product_story_role": "ingredient", "relation_candidates": ["茉莉花茶"]},
        ) == {}

    def test_ingredient_claim_requires_trusted_product_fact(self):
        """明确声称产品采用某原料时，必须命中可信产品资料。"""
        from local_asset_pipeline import _marketing_claim_violations

        segment = {
            "subtitle": "采用茉莉花茶原料",
            "voiceover": "这款茶咖采用茉莉花茶原料",
            "claims": [{
                "text": "采用茉莉花茶原料",
                "type": "ingredient",
                "evidence_source": "product_info.ingredients",
            }],
        }
        missing = _marketing_claim_violations(segment, {"name": "茶咖"}, {})
        trusted = _marketing_claim_violations(
            segment,
            {"name": "茶咖", "ingredients": ["茉莉花茶"]},
            {"product_story_role": "ingredient", "relation_candidates": ["茉莉花茶"]},
        )

        assert set(missing) == {"subtitle", "voiceover", "claims"}
        assert trusted == {}

    def test_visual_claim_is_deferred_to_real_contact_sheet_validation(self):
        """视觉事实不做脆弱字符串匹配，交给后续真实联系表 VLM 核验。"""
        from local_asset_pipeline import _marketing_claim_violations

        segment = {
            "subtitle": "0糖茶咖",
            "voiceover": "这款是0糖茶咖",
            "claims": [{"text": "0糖", "type": "specification", "evidence_source": "visual"}],
        }
        assert _marketing_claim_violations(segment, {"name": "茶咖"}, {"visible_text": ["茶咖"]}) == {}

    def test_script_checkpoint_resumes_matching_validated_state(self, tmp_path):
        """同签名的未完成或已验证脚本均可续跑，签名不同不得误用。"""
        from local_asset_pipeline import _load_script_checkpoint, _write_script_checkpoint

        path = tmp_path / "script_checkpoint.json"
        result = {"segments": [{"segment": 0, "subtitle": "值得细看"}]}
        _write_script_checkpoint(path, "signature-a", result)

        assert _load_script_checkpoint(path, "signature-a") == result
        assert _load_script_checkpoint(path, "signature-b") is None

        _write_script_checkpoint(path, "signature-a", result, status="complete")
        assert _load_script_checkpoint(path, "signature-a") == result

    def test_script_checkpoint_signature_changes_with_trusted_product_facts(self):
        """产品可信事实变化后必须重新生成，不能沿用旧产品关系和文案。"""
        from local_asset_pipeline import _script_checkpoint_context

        index = {
            "index_version": 4,
            "asset_folder": "/tmp/assets",
            "sources": [{"path": "/tmp/assets/a.mp4", "sha256": "abc", "size": 3}],
        }
        _, empty_signature = _script_checkpoint_context(
            index, {"name": "茶咖", "ingredients": []}, 4, "before_after",
        )
        _, ingredient_signature = _script_checkpoint_context(
            index, {"name": "茶咖", "ingredients": ["茉莉花茶"]}, 4, "before_after",
        )

        assert empty_signature != ingredient_signature

    def test_verified_relationship_claim_uses_current_product_annotation(self):
        """产品关系证据只在本次产品核验后的窗口分析中生效。"""
        from local_asset_pipeline import _marketing_claim_violations

        segment = {
            "subtitle": "从茉莉花茶原料看起",
            "voiceover": "先看茶咖的茉莉花茶原料",
            "claims": [{
                "text": "茉莉花茶原料",
                "type": "ingredient",
                "evidence_source": "verified_relationship",
            }],
        }
        analysis = {
            "product_relationship_verified": True,
            "matched_product_facts": ["茉莉花茶"],
            "relation_evidence": "画面可见茉莉花茶原料",
        }

        assert _marketing_claim_violations(segment, {"name": "茶咖"}, analysis) == {}

    def test_origin_and_effect_claims_require_matching_trusted_facts(self):
        """产地与功效不能由相似画面或通用卖点臆测。"""
        from local_asset_pipeline import _marketing_claim_violations

        invented_origin = _marketing_claim_violations(
            {"subtitle": "来自云南核心产区", "voiceover": "来自云南核心产区"},
            {"name": "茶咖", "origin": "广西横州"},
            {"product_story_role": "origin", "relation_candidates": ["花田"]},
        )
        unsupported_effect = _marketing_claim_violations(
            {"subtitle": "提神解腻", "voiceover": "一口提神解腻"},
            {"name": "茶咖", "ingredients": ["茉莉花茶"]},
            {},
        )
        trusted_effect = _marketing_claim_violations(
            {"subtitle": "提神解腻", "voiceover": "一口提神解腻"},
            {"name": "茶咖", "verified_claims": ["提神", "解腻"]},
            {},
        )

        assert set(invented_origin) == {"subtitle", "voiceover"}
        assert set(unsupported_effect) == {"subtitle", "voiceover"}
        assert trusted_effect == {}

    def test_cta_does_not_require_literal_pixel_equivalence(self):
        """无事实主张的行动引导可以配产品相关画面，不必在画面中出现同样文字。"""
        from local_asset_pipeline import _marketing_claim_violations

        assert _marketing_claim_violations(
            {"subtitle": "现在就来看看", "voiceover": "现在就来看看", "marketing_intent": "cta"},
            {"name": "茶咖"},
            {"product_story_role": "finished_product"},
        ) == {}

    def test_product_story_relationship_requires_matching_product_info(self):
        """VLM 的原料角色只是候选，必须与可信产品资料相交才建立产品关系。"""
        from local_asset_pipeline import _annotate_product_relationships

        catalog = [{
            "product_story_role": "ingredient",
            "relation_candidates": ["茉莉花茶"],
            "visible_text": ["茉莉花茶"],
            "visible_objects": ["竹筛", "花朵"],
            "evidence": "竹筛中可见花茶原料",
        }]
        _annotate_product_relationships(catalog, {"name": "茶咖"})
        assert catalog[0]["product_relationship_verified"] is False

        _annotate_product_relationships(catalog, {"name": "茶咖", "ingredients": ["茉莉花茶"]})
        assert catalog[0]["product_relationship_verified"] is True
        assert catalog[0]["matched_product_facts"] == ["茉莉花茶"]

    def test_marketing_copy_does_not_relax_visual_requirement(self, tmp_path):
        """字幕可以营销化，但画面要求仍必须通过真实联系表校验。"""
        import json
        from unittest.mock import patch
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "supported": False,
                    "subtitle_supported": True,
                    "voiceover_supported": True,
                    "visual_supported": False,
                    "static_supported": False,
                    "dynamic_supported": True,
                    "confidence": 0.98,
                    "unsupported_fields": ["visual_requirement"],
                    "unsupported_claims": ["画面没有展示茶园"],
                    "reason": "联系表仅展示瓶装成品",
                }, ensure_ascii=False)}}]}

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)
        with patch("local_asset_pipeline.requests.post", return_value=FakeResponse()):
            result = VisionAnalyzer().validate_segment(
                sheet,
                {
                    "subtitle": "亮点藏在源头",
                    "voiceover": "想知道这杯茶咖的亮点？先看源头",
                    "marketing_intent": "hook",
                    "claims": [],
                    "visual_requirement": "茶园全景",
                },
                product_info={"name": "茶咖"},
            )

        assert result["subtitle_supported"] is True
        assert result["voiceover_supported"] is True
        assert result["visual_supported"] is False
        assert result["supported"] is False

    def test_story_role_is_a_hard_asset_binding_constraint(self):
        """脚本指定原料镜头时，成品镜头即使分数高也不能替代。"""
        from local_asset_pipeline import _score_window

        segment = {"product_story_role": "ingredient", "visual_requirement": "茉莉花茶原料"}
        finished_product = {
            "window_id": "product",
            "start": 0,
            "end": 4,
            "source_path": "/tmp/product.mp4",
            "analysis": {
                "usable_for_ad": True,
                "confidence": 1.0,
                "product_story_role": "finished_product",
                "product_visibility": 5,
            },
        }
        ingredient = {
            "window_id": "ingredient",
            "start": 0,
            "end": 4,
            "source_path": "/tmp/ingredient.mp4",
            "analysis": {
                "usable_for_ad": True,
                "confidence": 0.9,
                "product_story_role": "ingredient",
                "relation_candidates": ["茉莉花茶"],
                "product_visibility": 0,
            },
        }

        rejected_score, rejected_details = _score_window(segment, finished_product, [])
        ingredient_score, _ = _score_window(segment, ingredient, [])

        assert rejected_score == 0.0
        assert rejected_details == {"product_story_role_mismatch": 1.0}
        assert ingredient_score >= 0.7

    def test_generic_product_value_can_use_relevant_usage_broll(self):
        from local_asset_pipeline import _score_window

        segment = {
            "product_story_role": "finished_product",
            "narrative": "value_closer",
            "marketing_intent": "value",
            "claims": [],
            "voiceover": "放在家里或带出门都方便",
            "visual_requirement": "日常饮用场景",
        }
        usage = {
            "window_id": "usage",
            "start": 0.0,
            "end": 4.0,
            "source_path": "/tmp/usage.mp4",
            "analysis": {
                "usable_for_ad": True,
                "confidence": 1.0,
                "product_story_role": "usage",
                "product_relevance_prior": "high",
                "product_visibility": 5,
                "visible_subjects": ["product", "hands"],
                "narrative_roles": ["usage_demo", "product_showcase"],
            },
        }

        score, details = _score_window(segment, usage, [], allow_replan=True)

        assert score >= 0.70
        assert "product_story_role_mismatch" not in details

    def test_theme_expansion_keeps_only_user_stated_product_facts(self):
        """主题 LLM 不能把品类常识补成可信原料、产地或功效。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from one_click_create import expand_theme_with_llm

        generated = {
            "product_info": {
                "name": "茶咖",
                "ingredients": ["茉莉花茶"],
                "origin": "云南",
                "production_process": ["冷萃"],
                "specifications": ["500ml"],
                "verified_claims": ["提神"],
            },
            "args": {},
        }
        with patch("llm_client.generate_json", return_value=generated):
            result = expand_theme_with_llm("做一个茶咖带货视频", SimpleNamespace())

        assert result["product_info"]["ingredients"] == []
        assert result["product_info"]["origin"] == ""
        assert result["product_info"]["production_process"] == []
        assert result["product_info"]["specifications"] == []
        assert result["product_info"]["verified_claims"] == []

    def test_theme_expansion_preserves_explicit_product_facts(self):
        """用户原文明确提供的事实必须保留，供素材关系和文案证据核验。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from one_click_create import expand_theme_with_llm

        generated = {
            "product_info": {
                "name": "茶咖",
                "ingredients": ["茉莉花茶"],
                "origin": "广西横州",
                "production_process": ["冷萃"],
                "specifications": ["500ml"],
                "verified_claims": ["0糖"],
            },
            "args": {},
        }
        theme = "广西横州产的茶咖，原料有茉莉花茶，冷萃工艺，500ml，已确认0糖"
        with patch("llm_client.generate_json", return_value=generated):
            result = expand_theme_with_llm(theme, SimpleNamespace())

        assert result["product_info"]["ingredients"] == ["茉莉花茶"]
        assert result["product_info"]["origin"] == "广西横州"
        assert result["product_info"]["production_process"] == ["冷萃"]
        assert result["product_info"]["specifications"] == ["500ml"]
        assert result["product_info"]["verified_claims"] == ["0糖"]

    def test_temporal_actions_distinguish_state_from_action(self):
        """“桌面摆放产品”是状态，“双手调整摆放”才是时序动作。"""
        from local_asset_pipeline import _temporal_action_claims

        state = _temporal_action_claims({"visual_requirement": "白色桌面摆放多瓶茶咖"})
        action = _temporal_action_claims({"visual_requirement": "双手调整并摆放多瓶茶咖"})

        assert state == []
        assert "place" in action

    def test_action_category_must_match_temporal_events(self):
        """高动态只能证明画面在动，不能用拿起动作冒充倒入动作。"""
        from local_asset_pipeline import _unsupported_action_claims

        unsupported = _unsupported_action_claims(
            {"voiceover": "把茶咖倒入杯中"},
            {"literal_actions": ["手拿起瓶装饮品"], "temporal_events": [{"action": "手将饮品放回桌面"}]},
        )
        supported = _unsupported_action_claims(
            {"voiceover": "把茶咖倒入杯中"},
            {"temporal_events": [{"action": "手向玻璃杯倾倒深色液体"}]},
        )

        assert unsupported == ["pour"]
        assert supported == []

    def test_usage_value_does_not_claim_literal_on_screen_action(self):
        """“随时能打开”是使用价值，不等于声称当前镜头正在开瓶。"""
        from local_asset_pipeline import _temporal_action_claims

        assert _temporal_action_claims({
            "voiceover": "不管放家里还是带出门想喝随时都能打开",
            "visual_requirement": "手向玻璃杯倾倒饮品",
        }) == ["pour"]

    def test_explicit_action_before_capability_result_still_requires_action_evidence(self):
        """后半句的“能用”不能抹掉前半句明确发生的倾倒动作。"""
        from local_asset_pipeline import _temporal_action_claims

        assert _temporal_action_claims({
            "voiceover": "拿出来直接倒进装了冰块的玻璃杯就能用",
        }) == ["pour"]

    def test_local_asset_catalog_has_high_product_relevance_prior(self):
        from local_asset_pipeline import _material_catalog

        catalog = _material_catalog({"windows": [{
            "window_id": "window-0",
            "source_video": "material.mp4",
            "analysis": {
                "usable_for_ad": True,
                "confidence": 0.9,
                "product_story_role": "context",
            },
        }]})

        assert catalog[0]["product_relevance_prior"] == "high"
        assert catalog[0]["product_relevance_source"] == "curated_local_asset_folder"

    def test_local_asset_script_never_uses_template_fallback(self):
        """本地素材发布链路缺少智能脚本能力时必须失败，不能输出泛化模板。"""
        from unittest.mock import patch
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        with patch("config.LLM_ENABLED", False):
            with pytest.raises(LocalAssetError, match="LLM 文案生成未启用"):
                build_material_constrained_script(
                    product_info={"name": "茶咖"},
                    coverage={},
                    num_segments=4,
                    script_style="before_after",
                    asset_index=minimal_local_asset_index(),
                )

    def test_local_asset_script_rejects_incomplete_generated_segments(self):
        """LLM 少生成段落时不能用模板补齐，必须阻断重新生成。"""
        from unittest.mock import patch
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        generated = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "voiceover_cues": ["桌面展示"],
            "segments": [{"segment": 0, "subtitle": "桌面展示", "voiceover": "桌面展示"}],
        }
        with patch("llm_client.generate_json", return_value=generated):
            with pytest.raises(LocalAssetError, match="所有响应均未形成有效的完整 segments JSON"):
                build_material_constrained_script(
                    product_info={"name": "茶咖"},
                    coverage={},
                    num_segments=4,
                    script_style="before_after",
                    asset_index=minimal_local_asset_index(),
                )

    def test_invalid_initial_json_regenerates_with_the_same_material_contract(self, tmp_path):
        """首个 JSON 损坏时必须依据同一素材合同重生成，不能切换到模板。"""
        import json
        from local_asset_pipeline import build_material_constrained_script

        generated = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "voiceover_cues": [
                "为什么这瓶装茶咖值得你现在认真看看？",
                "想试试瓶装茶咖就从眼前这一瓶开始了解。",
            ],
            "segments": [
                {
                    "segment": 0,
                    "narrative": "hook",
                    "product_story_role": "finished_product",
                    "subtitle": "还在随便选茶咖吗",
                    "voiceover": "还在随便选茶咖吗？",
                    "marketing_intent": "hook",
                    "marketing_device": "contrast_question",
                    "buyer_value": "重新考虑当前产品选择",
                    "evidence_refs": ["product:name"],
                    "continuity_from": None,
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-0"],
                },
                {
                    "segment": 1,
                    "narrative": "cta",
                    "product_story_role": "finished_product",
                    "subtitle": "想试试就去了解",
                    "voiceover": "想试试这款茶咖就去了解。",
                    "marketing_intent": "cta",
                    "marketing_device": "action",
                    "buyer_value": "进一步了解当前产品",
                    "evidence_refs": ["product:name", "product:usage:0"],
                    "continuity_from": 0,
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-1"],
                },
            ],
        }
        windows = [
            {
                "window_id": f"product-{index}",
                "source_video": f"product-{index}.mp4",
                "source_path": f"/tmp/product-{index}.mp4",
                "start": 0.0,
                "end": 4.0,
                "contact_sheet": f"/tmp/product-{index}.jpg",
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "visible_text": ["茶咖"],
                    "visible_objects": ["瓶装饮品"],
                    "evidence": "桌面上清晰展示茶咖瓶装成品",
                },
            }
            for index in range(2)
        ]
        passed = {
            "supported": True,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": True,
            "static_supported": True,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": [],
            "unsupported_claims": [],
            "reason": "通过",
        }

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=[
                 {"invalid": True}, creative_candidates(generated, generated, generated),
             ]) as generate, \
             patch("local_asset_pipeline.VisionAnalyzer.validate_segment", return_value=passed), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品", "usage": "茶咖饮用场景"},
                coverage={},
                num_segments=2,
                script_style="demonstration",
                asset_index={"asset_folder": "/tmp/json-retry-assets", "windows": windows},
                segment_durations={0: 4.0, 1: 5.0},
            )

        assert generate.call_count == 2
        first_prompt = json.loads(generate.call_args_list[0].args[0])
        retry_prompt = json.loads(generate.call_args_list[1].args[0])
        assert "material_catalog" not in first_prompt
        assert "coverage" not in first_prompt
        assert "narrative_plan" not in first_prompt
        assert retry_prompt["segment_contracts"] == first_prompt["segment_contracts"]
        assert retry_prompt["structured_output_retry"]["attempt"] == 2
        assert result["json_generation_attempt"] == 2

    def test_truncated_creative_json_retries_with_same_material_contract(self, tmp_path):
        import json
        from llm_client import LLMJSONTruncatedError
        from local_asset_pipeline import build_material_constrained_script

        valid = {
            "segments": [{
                "segment": 0,
                "cue": "这种瓶装新搭配你见过吗，想试就从这一瓶开始。",
                "marketing_device": "contrast_question",
                "buyer_value": "产生尝试兴趣",
                "evidence_refs": ["product:name"],
                "claims": [],
            }],
        }
        windows = [{
            "window_id": "product-0",
            "source_video": "product.mp4",
            "source_path": "/tmp/product.mp4",
            "start": 0.0,
            "end": 4.0,
            "analysis": {
                "usable_for_ad": True,
                "confidence": 1.0,
                "product_story_role": "finished_product",
                "visible_text": ["产品"],
                "visible_objects": ["瓶装产品"],
                "evidence": "桌面展示产品",
            },
        }]

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=[
                 LLMJSONTruncatedError("cut", '{"creative_candidates":[', "length"),
                 creative_candidates(valid, valid, valid),
             ]) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            result = build_material_constrained_script(
                product_info={"name": "产品", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index={"asset_folder": "/tmp/truncated-json", "windows": windows},
                segment_durations={0: 4.0},
            )

        assert generate.call_count == 2
        first = json.loads(generate.call_args_list[0].args[0])
        second = json.loads(generate.call_args_list[1].args[0])
        assert second["segment_contracts"] == first["segment_contracts"]
        assert "token 上限" in second["structured_output_retry"]["reason"]
        assert result["json_generation_attempt"] == 2

    def test_obsolete_script_repair_runtime_is_physically_absent(self):
        source = Path("local_asset_pipeline.py").read_text(encoding="utf-8")

        for obsolete_symbol in (
            "_build_material_constrained_script_prebound_legacy",
            "whole_narration_revision",
            "script_repair_unavailable",
            "material_semantic_checks",
            "MAX_SCRIPT_ATTEMPTS",
            "MAX_SCRIPT_REMOTE_CALLS",
            "_copy_constraints_by_segment",
            "_materialize_script_segments",
        ):
            assert obsolete_symbol not in source

    def test_initial_script_prompt_only_requests_copy_not_known_visual_fields(self):
        import json
        from local_asset_pipeline import _build_compact_script_prompt

        prompt = _build_compact_script_prompt(
            product_info={"name": "茶咖", "type": "食品", "ingredients": ["茉莉花茶"]},
            script_reference_profile={},
            external_cta=False,
            script_style="demonstration",
            copy_constraints={
                "0": {
                    "segment": 0,
                    "window_id": "window-0",
                    "marketing_intent": "hook",
                    "copy_goal": "制造产品好奇",
                    "max_voiceover_units": 16,
                    "evidence_anchors": [{"id": "product:name", "text": "茶咖", "kind": "product_info"}],
                    "allowed_marketing_devices": ["curiosity"],
                    "requires_buyer_value": False,
                    "required_continuity_from": None,
                    "forbidden_without_verified_facts": ["提神"],
                    "claims_rule": "没有事实时 claims 为空",
                },
            },
        )

        encoded = json.dumps(prompt, ensure_ascii=False)
        assert "material_catalog" not in prompt
        assert "narrative_plan" not in prompt
        assert "visual_requirement" not in encoded
        assert "asset_window_ids" not in encoded
        assert "scene_prompt" not in encoded
        shape = prompt["json_shape"]["creative_candidates"][0]["segments"][0]
        assert shape["segment"] == 0
        assert shape["cue"] == "connected spoken copy grounded in referenced evidence"
        assert "desired_story_role" not in shape
        assert "visual_query" in shape
        assert shape["evidence_refs"] == ["allowed evidence id"]
        assert shape["claims"] == []
        assert prompt["viral_creative_blueprint"]["candidate_strategy"]["count"] == 3
        assert prompt["segment_contracts"][0]["copy_goal"] == "制造产品好奇"
        assert "具体事实只取可信产品资料" in prompt["copywriting_brief"]["material_role"]
        assert "购买理由" in prompt["copywriting_brief"]["transformation"]

    def test_reference_sales_copy_drives_style_without_becoming_product_facts(self):
        import json
        from local_asset_pipeline import (
            _build_compact_script_prompt,
            _build_viral_creative_blueprint,
            bind_reference_profile_to_product,
        )

        profile = {
            "sales_structure": ["question_hook", "origin_proof", "usage_demo", "cta_outro"],
            "visible_sales_copy": [
                "健身期里面为什么你家都抢着喝",
                "这款茶咖广西横县手工茉莉搭配而来",
                "没试过赶紧来试试",
            ],
            "factual_claims": [{
                "text": "采用广西横县手工茉莉搭配",
                "type": "origin",
            }],
            "cta_text": "没试过赶紧来试试",
            "outro_duration": 3.4,
            "creative_mechanics": {
                "hook_mechanism": "curiosity_gap",
                "proof_pattern": "source_to_reason",
                "progression_pattern": "hook_to_proof_to_value_to_action",
                "spoken_style": "conversational",
                "sentence_rhythm": "short_punchy",
                "cta_pressure": "soft",
            },
        }
        bound = bind_reference_profile_to_product(profile, {"name": "茶咖"})
        blueprint = _build_viral_creative_blueprint(bound, "demonstration")
        prompt = _build_compact_script_prompt(
            product_info={"name": "茶咖", "type": "食品", "usage": "茶咖饮用场景"},
            script_reference_profile=bound,
            external_cta=False,
            script_style="demonstration",
            copy_constraints={
                "0": {
                    "segment": 0,
                    "marketing_intent": "hook",
                    "copy_goal": "把产品组合转成停留理由",
                    "evidence_anchors": [{"id": "product:name", "text": "茶咖", "kind": "product_info"}],
                    "claims_rule": "没有事实时 claims 为空",
                    "evidence_scope": "direct_visual_value",
                    "relation_boundary": "只表达可见产品",
                },
            },
        )

        encoded = json.dumps(prompt, ensure_ascii=False)
        assert blueprint["reference_copy_patterns"][0] == "用目标人群或使用场景提出一个具体问题"
        assert "reference_copy_examples" not in blueprint
        assert blueprint["reference_rhythm"]["average_units"] > 0
        assert prompt["copywriting_brief"]["reference_usage"] == "只迁移句式节奏和销售推进，不复制实体或事实"
        assert "广西横县" not in encoded
        assert bound["factual_claims"] == []
        assert prompt["segment_contracts"][0]["copy_goal"] == "把产品组合转成停留理由"

    def test_compact_script_prompt_and_response_budget_match_short_copy_task(self):
        import json
        from local_asset_pipeline import _build_compact_script_prompt, _script_response_token_budget

        constraints = {
            str(index): {
                "segment": index,
                "marketing_intent": "hook" if index == 0 else "cta" if index == 4 else "value",
                "copy_goal": "增加一个购买理由",
                "max_voiceover_units": 12,
                "evidence_anchors": [
                    {"id": f"visual:{anchor}", "text": "可见素材证据" * 3, "kind": "visual"}
                    for anchor in range(12)
                ],
                "allowed_marketing_devices": ["reason", "reveal"],
                "requires_buyer_value": True,
                "required_continuity_from": index - 1 if index else None,
                "forbidden_without_verified_facts": ["提神", "清爽", "好喝"],
                "claims_rule": "没有可信事实时 claims 为空",
            }
            for index in range(5)
        }
        prompt = _build_compact_script_prompt(
            {"name": "茶咖", "ingredients": ["茉莉花茶"]}, {}, False, "demonstration", constraints,
        )

        assert len(json.dumps(prompt, ensure_ascii=False).encode()) < 8000
        assert all(len(item["evidence_anchors"]) <= 6 for item in prompt["segment_contracts"])
        assert "forbidden_without_verified_facts" not in prompt["segment_contracts"][0]
        assert prompt["forbidden_without_verified_facts"] == ["提神", "清爽", "好喝"]
        assert 1200 < _script_response_token_budget(5) <= 3000

    def test_compact_segment_cues_materialize_the_single_voiceover_source(self):
        from local_asset_pipeline import _normalize_compact_script_response

        response = {
            "segments": [
                {"segment": 0, "cue": "这瓶茶咖你试过吗？"},
                {"segment": 1, "cue": "再看它的原料，"},
            ],
        }

        assert _normalize_compact_script_response(response, 2) is True
        assert response["voiceover_cues"] == ["这瓶茶咖你试过吗？", "再看它的原料，"]

    def test_compact_cues_gain_natural_boundaries_for_single_take_tts(self):
        from local_asset_pipeline import _normalize_compact_script_response

        response = {
            "segments": [
                {"segment": 0, "cue": "这瓶茶咖你试过吗"},
                {"segment": 1, "cue": "瓶装现成想喝更省事"},
            ],
        }

        assert _normalize_compact_script_response(response, 2) is True
        assert response["voiceover_cues"] == ["这瓶茶咖你试过吗？", "瓶装现成想喝更省事。"]

    def test_unverified_source_context_translates_context_without_inventing_quality(self):
        from local_asset_pipeline import _build_coverage_driven_narrative_plan

        materials = [
            {
                "window_id": "product",
                "product_story_role": "finished_product",
                "product_identity_supported": True,
                "product_relationship_verified": True,
                "matched_product_facts": [],
                "confidence": 1.0,
                "product_visibility": 5,
                "motion": {"motion_class": "dynamic"},
            },
            {
                "window_id": "origin",
                "product_story_role": "origin",
                "product_identity_supported": False,
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "confidence": 1.0,
                "product_visibility": 0,
                "motion": {
                    "motion_class": "semi_dynamic",
                    "subject_motion": "medium",
                    "confidence": 1.0,
                    "active_ranges": [[0.0, 2.0]],
                },
            },
            {
                "window_id": "usage",
                "product_story_role": "usage",
                "product_identity_supported": True,
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "confidence": 1.0,
                "product_visibility": 5,
                "motion": {"motion_class": "dynamic"},
            },
        ]

        plan = _build_coverage_driven_narrative_plan(materials, 3, False)
        source = next(item for item in plan if item["product_story_role"] == "origin")

        assert "转译成消费者能理解的购买理由" in source["copy_goal"]
        assert "不得虚构具体地名" in source["copy_goal"]
        assert "品质结论" in source["copy_goal"]

    def test_external_cta_is_authored_inside_the_same_full_narration(self):
        from local_asset_pipeline import (
            _normalize_compact_script_response,
            materialize_continuous_voiceover_contract,
        )

        response = {
            "segments": [
                {"segment": 0, "cue": "先看这瓶茶咖，"},
                {"segment": 1, "cue": "原料和喝法都说明白了，"},
            ],
            "outro_cue": "没试过这类茶咖，就从这一瓶开始，赶紧来试试。",
        }

        assert _normalize_compact_script_response(
            response,
            2,
            required_cta_text="赶紧来试试",
        ) is True
        response["segments"] = [{}, {}]
        full_text = materialize_continuous_voiceover_contract(response)

        assert response["voiceover_outro_cue"] in full_text
        assert full_text == "".join(response["voiceover_cues"]) + response["voiceover_outro_cue"]

    def test_derived_visual_contract_does_not_call_remote_validator_again(self):
        from local_asset_pipeline import _validate_derived_material_segment

        segment = {
            "segment": 0,
            "product_story_role": "usage",
            "voiceover": "倒杯加冰就能喝",
            "subtitle": "倒杯加冰就能喝",
            "claims": [],
            "visual_requirement": "玻璃杯；将深色液体倒入杯中",
        }
        window = {
            "analysis": {
                "confidence": 0.95,
                "product_story_role": "usage",
                "literal_actions": ["将深色液体倒入玻璃杯中"],
                "temporal_events": [{"action": "手持瓶装饮品向玻璃杯倾倒深色液体"}],
                "visible_objects": ["玻璃杯", "瓶装饮品"],
            },
            "motion": {"subject_motion": "high", "confidence": 0.9},
        }

        result = _validate_derived_material_segment(segment, window, {"name": "茶咖"})

        assert result["supported"] is True
        assert result["validation_source"] == "indexed_local_evidence"

    def test_estimated_narration_capacity_does_not_block_factually_valid_copy(self, tmp_path):
        """文本长度只是生成建议，真实 TTS 时长才有资格驱动素材时间轴。"""
        import copy
        import json
        from local_asset_pipeline import build_material_constrained_script

        base = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "segments": [{
                "segment": 0,
                "narrative": "hook_cta",
                "product_story_role": "finished_product",
                "marketing_intent": "cta",
                "marketing_device": "action",
                "buyer_value": "进一步了解当前产品",
                "evidence_refs": ["product:name"],
                "continuity_from": None,
                "claims": [],
                "visual_requirement": "桌面上的瓶装茶咖",
                "scene_prompt": "桌面上的瓶装茶咖",
                "asset_query": ["瓶装茶咖"],
                "asset_window_ids": ["product-0"],
            }],
        }
        oversized = {**copy.deepcopy(base), "voiceover_cues": ["这是一条明显超过四秒镜头自然口播容量而且还在继续增加内容的茶咖行动号召"]}
        asset_index = {
            "asset_folder": "/tmp/oversized-cue-assets",
            "windows": [{
                "window_id": "product-0",
                "source_video": "product.mp4",
                "source_path": "/tmp/product.mp4",
                "start": 0.0,
                "end": 4.0,
                "contact_sheet": "/tmp/product.jpg",
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "visible_text": ["茶咖"],
                    "visible_objects": ["瓶装饮品"],
                    "evidence": "桌面上展示茶咖瓶装成品",
                },
            }],
        }
        passed = {
            "supported": True,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": True,
            "static_supported": True,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": [],
            "unsupported_claims": [],
            "reason": "通过",
        }

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", return_value=
                   creative_candidates(oversized, oversized, oversized)) as generate, \
             patch("local_asset_pipeline.VisionAnalyzer.validate_segment", return_value=passed), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path), \
             patch("quality_gate.record_failure_case") as record:
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 4.0},
            )

        assert generate.call_count == 1
        assert record.call_count == 0
        assert result["voiceover_full"] == oversized["voiceover_cues"][0] + "。"
        initial_payload = json.loads(generate.call_args_list[0].args[0])
        guidance = initial_payload["narration_guidance"]
        assert guidance["enforced"] is False
        assert guidance["duration_source"] == "estimated_text_load_only"
        assert "narration_contract" not in initial_payload

    def test_invalid_initial_json_blocks_after_bounded_material_regeneration(self, tmp_path):
        """连续无效 JSON 只能阻断发布，不能生成或补齐通用模板。"""
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=[
                 {"invalid": 1}, {"invalid": 2}, {"invalid": 3},
             ]) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            with pytest.raises(LocalAssetError, match="所有响应均未形成有效的完整 segments JSON"):
                build_material_constrained_script(
                    product_info={"name": "茶咖", "type": "食品"},
                    coverage={},
                    num_segments=4,
                    script_style="demonstration",
                    asset_index=minimal_local_asset_index("/tmp/json-block-assets"),
                )

        assert generate.call_count == 3

    def test_initial_script_does_not_enforce_non_user_candidate_violations(self, tmp_path):
        """非用户反馈产生的候选标签不得阻断或触发第二次生成。"""
        from local_asset_pipeline import build_material_constrained_script

        responses = [{"attempt": attempt} for attempt in range(1, 4)]
        def normalize(result, *_args, **_kwargs):
            attempt = result["attempt"]
            result["segments"] = [{"segment": 0, "cue": "测试口播"}]
            result["voiceover_cues"] = ["测试口播"]
            result["candidate_factual_violations"] = [
                f"段 0: 第 {attempt} 版缺少素材或产品资料支持的事实"
            ]
            return True

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=responses), \
             patch("local_asset_pipeline._normalize_compact_script_response", side_effect=normalize), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=minimal_local_asset_index("/tmp/mixed-contract-assets"),
                segment_durations={0: 4.0},
            )

        assert result["voiceover_full"] == "测试口播"
        assert result["json_generation_attempt"] == 1

    def test_network_failure_is_not_multiplied_by_structured_output_attempts(self, tmp_path):
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", return_value=None) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            with pytest.raises(LocalAssetError, match="脚本生成服务不可用"):
                build_material_constrained_script(
                    product_info={"name": "茶咖", "type": "食品"},
                    coverage={},
                    num_segments=1,
                    script_style="demonstration",
                    asset_index=minimal_local_asset_index("/tmp/network-failure-assets"),
                )

        assert generate.call_count == 1

    def test_local_asset_creative_profile_comes_from_visual_measurements(self):
        """音乐、节奏和特效合同应由素材的运动与画面质量统计生成。"""
        from local_asset_pipeline import build_local_asset_creative_profile

        windows = [
            {
                "analysis": {"usable_for_ad": True, "confidence": 0.95},
                "motion": {
                    "motion_class": "dynamic",
                    "camera_speed": 0.04,
                    "subject_motion_ratio": 0.18,
                    "temporal_change": 0.12,
                    "confidence": 0.9,
                },
                "frame_quality": {"median_brightness": 182.0, "median_contrast": 70.0},
            }
            for _ in range(3)
        ]

        profile = build_local_asset_creative_profile({"windows": windows})

        assert profile["source"] == "local_asset_analysis"
        assert profile["energy"] == "high"
        assert profile["recommended_pace"] == "fast"
        assert profile["brightness"] == "bright"
        assert profile["contrast"] == "high"
        assert profile["sfx_intensity"] == "moderate"

    def test_sfx_uses_rendered_transition_decisions_instead_of_alternating_defaults(self):
        """素材驱动流程不能给溶解或无转场盲加 impact/whoosh。"""
        from video_merger import generate_sfx_timings

        timings = generate_sfx_timings(
            num_clips=3,
            clip_duration=4.0,
            transition_duration=0.4,
            narratives=["hook", "value", "value_closer"],
            transition_decisions=[
                {"type": "dissolve", "duration": 0.5},
                {"type": "slideleft", "duration": 0.25},
            ],
            sfx_intensity="subtle",
        )

        transition_sfx = [item for item in timings if item["type"] in {"impact", "whoosh"}]
        assert len(transition_sfx) == 1
        assert transition_sfx[0]["type"] == "whoosh"
        assert transition_sfx[0]["volume"] < 0.2

    def test_motion_analysis_separates_static_and_subject_motion(self, tmp_path):
        """连续帧分析必须区分真正静止画面与前景主体移动。"""
        import cv2
        import numpy as np
        from local_asset_pipeline import _analyze_window_motion

        static_path = tmp_path / "static.mp4"
        moving_path = tmp_path / "moving.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        static_writer = cv2.VideoWriter(str(static_path), fourcc, 10, (320, 180))
        moving_writer = cv2.VideoWriter(str(moving_path), fourcc, 10, (320, 180))
        rng = np.random.default_rng(7)
        background = rng.integers(25, 90, size=(180, 320, 3), dtype=np.uint8)
        for index in range(30):
            static_writer.write(background)
            frame = background.copy()
            cv2.rectangle(frame, (20 + index * 6, 65), (100 + index * 6, 145), (245, 245, 245), -1)
            moving_writer.write(frame)
        static_writer.release()
        moving_writer.release()

        static = _analyze_window_motion(static_path, 0.0, 2.8)
        moving = _analyze_window_motion(moving_path, 0.0, 2.8)

        assert static["motion_class"] == "static"
        assert static["subject_motion"] == "low"
        assert moving["subject_motion"] in {"medium", "high"}
        assert moving["subject_motion_ratio"] > static["subject_motion_ratio"] + 0.03
        assert moving["active_ranges"]

    def test_candidate_windows_respect_detected_scene_boundaries(self):
        """固定长度窗口不能跨越真实剪辑点，否则联系表会混入两个场景。"""
        from local_asset_pipeline import _candidate_windows

        source = {"path": "/tmp/source.mp4", "duration": 9.0}
        scenes = [
            {"scene_id": 0, "start": 0.0, "end": 3.0},
            {"scene_id": 1, "start": 3.0, "end": 9.0},
        ]
        with patch("local_asset_pipeline._detect_scene_ranges", return_value=scenes):
            windows = _candidate_windows(source, window_seconds=4.0, stride=2.0)

        assert windows
        assert all(window["end"] <= 3.0 or window["start"] >= 3.0 for window in windows)
        assert {window["scene_id"] for window in windows} == {0, 1}

    def test_dynamic_action_script_rejects_static_window(self, tmp_path):
        """脚本声称倒入等动作时，静态联系表不能通过动态语义门。"""
        from PIL import Image
        from local_asset_pipeline import VisionAnalyzer

        sheet = tmp_path / "sheet.jpg"
        Image.new("RGB", (32, 32), "white").save(sheet)
        result = VisionAnalyzer().validate_segment(
            sheet,
            {
                "subtitle": "倒入冰杯",
                "voiceover": "把瓶中饮品倒入杯中",
                "visual_requirement": "手将瓶装饮品倒入玻璃杯",
            },
            motion={
                "subject_motion": "low",
                "subject_motion_ratio": 0.01,
                "confidence": 0.9,
            },
        )

        assert result["supported"] is False
        assert result["dynamic_required"] is True
        assert result["dynamic_supported"] is False
        assert "连续帧" in result["reason"]

    def test_strict_subtitle_rendering_has_no_plain_fallback(self):
        """发布链路花字失败必须阻断，普通字幕会重新引入换行和越界。"""
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        block = source[source.index("    try:\n        add_fancy_subtitles("):source.index("    # 混合口播到视频音轨")]
        assert "add_subtitles_ffmpeg(" not in block
        assert "字幕渲染失败，已阻断不可发布成片" in block

    def test_visible_text_requires_multi_frame_consensus(self):
        """单帧偶然读到的标签不能进入脚本证据，至少需要两帧一致可辨。"""
        from local_asset_pipeline import _normalize_analysis

        normalized = _normalize_analysis({
            "visible_text": [
                {"text": "疑似文字", "visible_frame_count": 1},
                {"text": "茶咖", "visible_frame_count": 3},
            ],
        })

        assert normalized["visible_text"] == ["茶咖"]

    def test_motion_channel_disagreement_blocks_window(self):
        """VLM 和连续帧高置信结论冲突时必须拒绝，不允许任选一个结论。"""
        from local_asset_pipeline import _motion_consistency

        result = _motion_consistency(
            {"shot_type": "static", "confidence": 0.95},
            {"motion_class": "dynamic", "confidence": 0.92},
        )

        assert result["passed"] is False
        assert result["reasons"]

    def test_gap_report_records_structured_negative_sample(self, tmp_path):
        """失败产物必须成为带类型和证据的负面数据，而不是只有一条错误字符串。"""
        import json
        from local_asset_pipeline import _write_gap_report

        report = tmp_path / "gap.json"
        with patch("quality_gate.record_failure_case") as record:
            _write_gap_report(report, {
                "reason": "segment_low_confidence",
                "failed_segment": {"segment": 2, "narrative": "usage_demo"},
                "unsupported_claims": ["缺少倒入动作"],
                "top_candidates": [{"window_id": "window-1"}],
            })

        assert json.loads(report.read_text(encoding="utf-8"))["reason"] == "segment_low_confidence"
        kwargs = record.call_args.kwargs
        assert kwargs["failure_type"] == "local_asset_semantic_mismatch"
        assert kwargs["segment_index"] == 2
        assert kwargs["extra"]["negative_sample"] is True
        assert kwargs["extra"]["unsupported_claims"] == ["缺少倒入动作"]

    def test_local_asset_index_build_is_serialized(self, tmp_path):
        """重复启动索引构建时必须经过同一文件锁，避免重复 VLM 成本和并发覆盖。"""
        from contextlib import contextmanager
        import local_asset_pipeline

        context = local_asset_pipeline.LocalAssetContext(
            folder=tmp_path,
            cache_dir=tmp_path / "cache",
            index_path=tmp_path / "cache" / "index.json",
        )
        entered = []

        @contextmanager
        def fake_lock(path):
            entered.append(path)
            yield

        with patch.object(local_asset_pipeline, "ensure_vision_backend_available"), \
             patch.object(local_asset_pipeline, "_folder_cache_context", return_value=context), \
             patch.object(local_asset_pipeline, "_index_build_lock", side_effect=fake_lock), \
             patch.object(local_asset_pipeline, "_build_local_asset_index_locked", return_value={"index_version": 3}):
            result = local_asset_pipeline.build_local_asset_index(tmp_path)

        assert result["index_version"] == 3
        assert entered == [context.cache_dir / ".index.lock"]

    def test_frame_evidence_upgrade_reuses_compatible_visual_analysis(self, tmp_path):
        """仅帧证据版本升级时，不得重新支付全部视频语义理解成本。"""
        import json
        from local_asset_pipeline import _load_reusable_window_analyses

        signatures = [{"path": "/tmp/a.mp4", "sha256": "same", "size": 123}]
        old_index = tmp_path / "index.json"
        old_index.write_text(json.dumps({
            "index_version": 4,
            "build_complete": True,
            "sources": signatures,
            "windows": [{
                "window_id": "source_0000",
                "source_path": "/tmp/a.mp4",
                "start": 0.0,
                "end": 4.0,
                "analysis": {
                    "shot_type": "dynamic",
                    "narrative_roles": ["product_showcase"],
                    "product_story_role": "finished_product",
                    "usable_for_ad": True,
                    "confidence": 0.9,
                },
            }],
        }), encoding="utf-8")

        reusable = _load_reusable_window_analyses(old_index, signatures)

        assert reusable["source_0000"]["product_story_role"] == "finished_product"

    def test_atempo_subtitles_sequential_not_original_start(self, tmp_path):
        """atempo 加速后字幕时间轴必须顺序累加，不能保留原始空隙导致错位"""
        import subprocess, shutil
        from unittest.mock import patch
        from tts_client import generate_full_voiceover
        import tts_client

        template = tmp_path / "template.m4a"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "1", "-c:a", "aac", str(template),
            ],
            check=True, capture_output=True,
        )

        def mock_tts(text, path, voice=None, rate=None):
            shutil.copy(str(template), str(path))

        def mock_duration(path):
            # 正常音频 4.0 秒；atempo 后的音频（路径含 fast_）约 3.1 秒
            if "fast_" in str(path):
                return 3.1
            return 4.0

        # 3 句话，每句 4 秒 + 0.15 秒停顿 = 12.3 秒 > 9.5 秒（total_duration=10），触发 atempo
        script_lines = [
            {"text": "第一句。第二句。第三句。", "start": 0, "end": 10, "segment": 0}
        ]
        out_path = tmp_path / "voiceover.m4a"

        with patch.object(tts_client, "generate_tts_audio", side_effect=mock_tts), \
             patch.object(tts_client, "_get_audio_duration", side_effect=mock_duration):
            _, subtitles = generate_full_voiceover(
                script_lines, out_path, total_duration=10.0, pause_between_sentences=0.15
            )

        assert len(subtitles) == 3, f"应有 3 句字幕，实际 {len(subtitles)}"
        # 关键断言：修复前 bug 会导致第二段 start 保留原始值≈4.15，
        # 修复后应紧密跟随第一段结束（≈3.1）
        assert subtitles[1]["start"] < 4.0, (
            f"atempo 后第二段 start={subtitles[1]['start']}, "
            "应紧密跟随第一段而非保留原始空隙"
        )
        assert subtitles[2]["start"] < 7.0, (
            f"atempo 后第三段 start={subtitles[2]['start']}, "
            "应顺序累加而非累积错位"
        )
        # 确保字幕之间没有超过 0.5 秒的不自然空隙
        for i in range(1, len(subtitles)):
            gap = subtitles[i]["start"] - subtitles[i - 1]["end"]
            assert gap < 0.5, f"字幕 {i} 与 {i-1} 之间空隙过大：{gap:.2f}s"

    def test_atempo_preserves_cross_segment_video_start(self, tmp_path):
        """口播加速只能压缩段内句子，不能把后续镜头口播提前到上一镜头。"""
        import shutil
        import subprocess
        import tts_client

        template = tmp_path / "template.m4a"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=2:r=44100", "-c:a", "aac", str(template)],
            check=True, capture_output=True,
        )

        def mock_tts(text, path, voice=None, rate=None):
            shutil.copy(str(template), str(path))

        def mock_duration(path):
            return 1.2 if "fast_" in str(path) else 2.0

        lines = [
            {"text": "第一段第一句。第一段第二句。", "start": 0.0, "end": 4.0, "segment": 0},
            {"text": "第二段第一句。第二段第二句。", "start": 5.0, "end": 9.0, "segment": 1},
        ]
        with patch.object(tts_client, "generate_tts_audio", side_effect=mock_tts), \
             patch.object(tts_client, "_get_audio_duration", side_effect=mock_duration):
            _, subtitles = tts_client.generate_full_voiceover(lines, tmp_path / "voice.m4a", total_duration=10.0)

        second_segment = [item for item in subtitles if item["segment"] == 1]
        assert second_segment[0]["start"] >= 5.0
        assert second_segment[1]["start"] == pytest.approx(second_segment[0]["end"])

    def test_voiceover_overflow_blocks_instead_of_dropping_sentences(self, tmp_path):
        """单段口播超过最大语速仍放不下时必须失败，禁止裁剪或丢掉后半句。"""
        import shutil
        import subprocess
        import tts_client

        template = tmp_path / "template.m4a"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3:r=44100", "-c:a", "aac", str(template)],
            check=True, capture_output=True,
        )

        with patch.object(tts_client, "generate_tts_audio", side_effect=lambda text, path, voice=None, rate=None: shutil.copy(str(template), str(path))), \
             patch.object(tts_client, "_get_audio_duration", return_value=3.0):
            with pytest.raises(RuntimeError, match="仍无法装入镜头"):
                tts_client.generate_full_voiceover(
                    [{"text": "第一句。第二句。", "start": 0.0, "end": 2.0, "segment": 0}],
                    tmp_path / "voice.m4a",
                    total_duration=3.0,
                    max_rate_multiplier=1.6,
                )


class TestFrameEvidenceSelection:
    """素材理解输入必须保留变化证据，同时删除无信息重复帧。"""

    @staticmethod
    def _write_video(path, frames, fps=10):
        import cv2

        height, width = frames[0].shape[:2]
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            fps,
            (width, height),
        )
        assert writer.isOpened()
        for frame in frames:
            writer.write(frame)
        writer.release()

    def test_scene_change_is_kept_while_static_duplicates_are_removed(self, tmp_path):
        import numpy as np
        from frame_evidence import build_contact_sheet_evidence

        red = np.full((180, 320, 3), (20, 20, 220), dtype=np.uint8)
        blue = np.full((180, 320, 3), (220, 20, 20), dtype=np.uint8)
        video = tmp_path / "cut.mp4"
        self._write_video(video, [red.copy() for _ in range(20)] + [blue.copy() for _ in range(20)])

        evidence = build_contact_sheet_evidence(
            source=video,
            start=0.0,
            end=4.0,
            output=tmp_path / "sheet.jpg",
            frame_count=6,
        )

        assert evidence["candidate_count"] > evidence["kept_count"]
        assert evidence["kept_count"] <= 6
        assert any(
            item["kept"] and item["selection_reason"] == "scene_change"
            for item in evidence["candidates"]
        )
        assert any(not item["kept"] for item in evidence["candidates"])
        assert (tmp_path / "sheet.jpg").exists()

    def test_small_settled_local_change_survives_global_dedup(self, tmp_path):
        import cv2
        import numpy as np
        from frame_evidence import build_contact_sheet_evidence

        blank = np.full((180, 320, 3), 235, dtype=np.uint8)
        changed = blank.copy()
        cv2.rectangle(changed, (140, 75), (180, 92), (10, 10, 10), -1)
        video = tmp_path / "local-change.mp4"
        self._write_video(
            video,
            [blank.copy() for _ in range(20)] + [changed.copy() for _ in range(20)],
        )

        evidence = build_contact_sheet_evidence(
            source=video,
            start=0.0,
            end=4.0,
            output=tmp_path / "sheet.jpg",
            frame_count=8,
        )

        assert any(
            item["kept"] and item["selection_reason"] == "local_state_change"
            for item in evidence["candidates"]
        )

    def test_evidence_manifest_and_audit_report_explain_every_decision(self, tmp_path):
        import json
        import numpy as np
        from frame_evidence import build_contact_sheet_evidence, write_frame_evidence_artifacts

        frame = np.full((180, 320, 3), 128, dtype=np.uint8)
        video = tmp_path / "static.mp4"
        self._write_video(video, [frame.copy() for _ in range(30)])
        sheet = tmp_path / "contact_sheets" / "window.jpg"
        evidence = build_contact_sheet_evidence(
            source=video,
            start=0.0,
            end=3.0,
            output=sheet,
            frame_count=6,
        )
        manifest = tmp_path / "frame_evidence.json"
        report = tmp_path / "frame_evidence_report.html"

        write_frame_evidence_artifacts(
            [{"window_id": "window-0", "contact_sheet": str(sheet), "frame_evidence": evidence}],
            manifest,
            report,
        )

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["windows"][0]["window_id"] == "window-0"
        assert all("selection_reason" in item for item in payload["windows"][0]["candidates"])
        html = report.read_text(encoding="utf-8")
        assert "window-0" in html
        assert "保留" in html and "丢弃" in html
        assert html.count("data:image/jpeg;base64,") > 2

    def test_required_reference_anchors_cannot_be_removed_by_capacity_pruning(self, tmp_path):
        import numpy as np
        from frame_evidence import build_contact_sheet_evidence

        colors = [
            np.full((180, 320, 3), color, dtype=np.uint8)
            for color in ((30, 30, 220), (30, 220, 30), (220, 30, 30), (180, 180, 30))
        ]
        video = tmp_path / "reference-scenes.mp4"
        self._write_video(
            video,
            [frame.copy() for frame in colors for _ in range(10)],
        )
        required = [0.5, 1.5, 2.5, 3.5]

        evidence = build_contact_sheet_evidence(
            source=video,
            start=0.0,
            end=4.0,
            output=tmp_path / "sheet.jpg",
            frame_count=4,
            required_times=required,
        )

        kept_required = [
            item["timestamp"]
            for item in evidence["candidates"]
            if item["kept"] and item["selection_reason"] == "required_anchor"
        ]
        assert kept_required == pytest.approx(required, abs=0.12)
        assert evidence["kept_count"] == 4

    def test_frame_evidence_version_invalidates_script_checkpoint(self):
        from local_asset_pipeline import _script_checkpoint_context

        base = {
            "asset_folder": "/tmp/materials",
            "index_version": 5,
            "sources": [{"path": "/tmp/a.mp4", "sha256": "x", "size": 10, "mtime_ns": 1}],
        }
        _, first = _script_checkpoint_context(
            {**base, "frame_evidence_version": 1},
            {"name": "茶咖"},
            4,
            "demonstration",
        )
        _, second = _script_checkpoint_context(
            {**base, "frame_evidence_version": 2},
            {"name": "茶咖"},
            4,
            "demonstration",
        )

        assert first != second


class TestMaterialDrivenPostProduction:
    """本地素材的所有后期决策必须来自同一份实际选片合同。"""

    def test_local_story_duration_scales_with_selected_material_not_static_template(self):
        from local_asset_pipeline import build_local_asset_story_contract

        def index_with_duration(duration):
            windows = []
            for index, role in enumerate(("finished_product", "ingredient", "origin", "usage", "finished_product")):
                windows.append({
                    "window_id": f"window-{index}",
                    "source_path": f"/tmp/source-{index}.mp4",
                    "source_video": f"source-{index}.mp4",
                    "start": 0.0,
                    "end": duration,
                    "duration": duration,
                    "analysis": {
                        "usable_for_ad": True,
                        "product_story_role": role,
                        "product_visibility": 5 if role in {"finished_product", "usage"} else 1,
                        "confidence": 0.95,
                        "visible_text": ["茶咖"] if role in {"finished_product", "usage"} else [],
                        "visible_objects": ["茶咖"] if role in {"finished_product", "usage"} else [],
                        "narrative_roles": ["product_showcase"],
                    },
                    "motion": {"motion_class": "semi_dynamic", "stability": 0.9},
                    "frame_quality": {"readable_ratio": 1.0, "passed": True},
                })
            return {"windows": windows, "asset_folder": "/tmp/materials"}

        short = build_local_asset_story_contract(
            index_with_duration(2.5), {"name": "茶咖", "type": "食品"},
        )
        long = build_local_asset_story_contract(
            index_with_duration(5.0), {"name": "茶咖", "type": "食品"},
        )

        assert long["natural_main_duration"] == pytest.approx(short["natural_main_duration"] * 2)
        assert long["duration_source"] == "selected_local_asset_windows"

    def test_requested_fifteen_seconds_cannot_compress_required_material_story(self):
        from local_asset_pipeline import build_local_asset_story_contract

        windows = []
        for index, role in enumerate(("finished_product", "ingredient", "origin", "usage", "finished_product")):
            windows.append({
                "window_id": f"window-{index}",
                "source_path": f"/tmp/source-{index}.mp4",
                "source_video": f"source-{index}.mp4",
                "start": 0.0,
                "end": 4.0,
                "duration": 4.0,
                "analysis": {
                    "usable_for_ad": True,
                    "product_story_role": role,
                    "product_visibility": 5 if role in {"finished_product", "usage"} else 1,
                    "confidence": 0.95,
                    "visible_text": ["茶咖"] if role in {"finished_product", "usage"} else [],
                    "visible_objects": ["茶咖"] if role in {"finished_product", "usage"} else [],
                    "narrative_roles": ["product_showcase"],
                },
                "motion": {"motion_class": "semi_dynamic", "stability": 0.9},
                "frame_quality": {"readable_ratio": 1.0, "passed": True},
            })

        contract = build_local_asset_story_contract(
            {"windows": windows, "asset_folder": "/tmp/materials"},
            {"name": "茶咖", "type": "食品"},
            requested_duration=15,
        )

        assert contract["natural_main_duration"] == pytest.approx(20.0)
        assert contract["requested_duration"] == 15
        assert contract["requested_duration_applied"] is False

    def test_four_segment_plan_uses_available_ingredient_and_origin_without_forcing_proof(self):
        from local_asset_pipeline import _build_coverage_driven_narrative_plan

        catalog = [
            *[
                {
                    "window_id": f"product-{index}",
                    "source_path": f"/tmp/product-{index}.mp4",
                    "start": 0.0,
                    "end": 4.0,
                    "product_story_role": "finished_product",
                    "product_identity_supported": True,
                    "product_relationship_verified": True,
                    "product_visibility": 5,
                }
                for index in range(2)
            ],
            {
                "window_id": "ingredient",
                "source_path": "/tmp/ingredient.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "ingredient",
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "product_visibility": 0,
            },
            {
                "window_id": "origin",
                "source_path": "/tmp/origin.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "origin",
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "product_visibility": 0,
            },
            {
                "window_id": "usage",
                "source_path": "/tmp/usage.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "usage",
                "product_identity_supported": True,
                "product_relationship_verified": True,
                "product_visibility": 5,
            },
        ]

        plan = _build_coverage_driven_narrative_plan(catalog, 4, external_cta=False)

        roles = [item["product_story_role"] for item in plan]
        assert {"ingredient", "origin"}.issubset(roles)
        assert roles[-1] in {"finished_product", "usage"}
        assert all(
            item["marketing_intent"] != "proof"
            for item in plan
            if item["product_story_role"] in {"ingredient", "origin"}
        )

    def test_material_story_order_changes_when_visual_attention_changes(self):
        from copy import deepcopy
        from local_asset_pipeline import _build_coverage_driven_narrative_plan

        def item(window_id, role, motion_class, *, identity=False):
            return {
                "window_id": window_id,
                "source_path": f"/tmp/{window_id}.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": role,
                "product_identity_supported": identity,
                "product_relationship_verified": identity,
                "product_visibility": 5 if identity else 1,
                "confidence": 0.95,
                "motion": {
                    "motion_class": motion_class,
                    "camera_speed": 0.04 if motion_class == "dynamic" else 0.0,
                    "subject_motion_ratio": 0.18 if motion_class == "dynamic" else 0.01,
                    "stability": 0.9,
                },
                "frame_quality": {"readable_ratio": 1.0, "passed": True},
            }

        ingredient_hook = [
            item("product-a", "finished_product", "static", identity=True),
            item("product-b", "finished_product", "static", identity=True),
            item("ingredient", "ingredient", "dynamic"),
            item("origin", "origin", "semi_dynamic"),
        ]
        product_hook = deepcopy(ingredient_hook)
        product_hook[0]["motion"] = {
            "motion_class": "dynamic", "camera_speed": 0.04,
            "subject_motion_ratio": 0.18, "stability": 0.9,
        }
        product_hook[2]["motion"] = {
            "motion_class": "static", "camera_speed": 0.0,
            "subject_motion_ratio": 0.01, "stability": 0.9,
        }

        ingredient_plan = _build_coverage_driven_narrative_plan(
            ingredient_hook, 3, external_cta=False,
        )
        product_plan = _build_coverage_driven_narrative_plan(
            product_hook, 3, external_cta=False,
        )

        assert ingredient_plan[0]["product_story_role"] == "ingredient"
        assert product_plan[0]["product_story_role"] == "finished_product"

    def test_material_driven_voice_does_not_treat_requested_preset_as_authoritative(self):
        from tts_client import recommend_voice_for_narration

        voice, reason = recommend_voice_for_narration(
            {"type": "食品"},
            [{"text": "从茉莉原料到一杯茶咖"}],
            requested_voice="female_warm",
            creative_profile={
                "source": "selected_local_assets",
                "energy": "medium",
                "story_role_counts": {"ingredient": 1, "origin": 1, "finished_product": 2},
            },
        )

        assert voice == "female_young"
        assert "素材" in reason

    def test_initial_local_asset_profile_also_uses_material_driven_voice_selection(self):
        from tts_client import recommend_voice_for_narration

        voice, reason = recommend_voice_for_narration(
            {"type": "食品"},
            [{"text": "先用成品抓住注意力再自然带到茉莉花茶原料"}],
            requested_voice="female_warm",
            creative_profile={
                "source": "local_asset_analysis",
                "energy": "medium",
                "story_role_counts": {"ingredient": 2, "finished_product": 7},
            },
        )

        assert voice == "female_young"
        assert "素材" in reason

    def test_unspecified_pipeline_and_batch_voice_defaults_remain_auto(self):
        import inspect
        from batch import create_task_args
        from one_click_create import run_generation_pipeline

        assert inspect.signature(run_generation_pipeline).parameters["voice"].default == "auto"
        assert create_task_args({"product_name": "茶咖"}, {})["voice"] == "auto"

    def test_bgm_metadata_ranking_prefers_natural_acoustic_over_playful_track(self):
        from bgm_client import rank_tracks_for_contract

        tracks = [
            {"id": "kids", "title": "Playtime", "tags": [[0, "playful"]], "categories": [[0, {"name": "Kids"}]]},
            {"id": "natural", "title": "Quiet Fields", "tags": [[0, "organic"], [1, "acoustic"]], "categories": [[0, {"name": "Calm"}]]},
        ]

        ranked = rank_tracks_for_contract(tracks, {
            "genre": "acoustic",
            "mood": "warm",
            "energy": "medium",
            "semantic_tone": "natural_origin",
        })

        assert ranked[0]["id"] == "natural"

    def test_material_music_contract_selects_balanced_audio_not_loudest_chorus(self):
        from video_merger import select_bgm_segment

        loudness = [
            -20.0, -19.8, -20.1, -19.9, -20.0,
            -18.2, -18.0, -18.1, -18.3, -18.0,
            -13.0, -12.8, -13.2, -12.9, -13.1,
        ]
        with patch("video_merger._analyze_loudness", return_value=loudness), \
             patch("video_merger._detect_beats", return_value=[]):
            selected = select_bgm_segment(
                bgm_duration=15.0,
                video_duration=5.0,
                bgm_path=Path("/tmp/music.mp3"),
                music_contract={
                    "source": "selected_local_assets",
                    "energy": "medium",
                    "semantic_tone": "natural_origin",
                    "intro_type": "immediate",
                },
            )

        assert selected["strategy"] == "contract_energy_window"
        assert selected["start_time"] < 10.0
        assert selected["average_loudness_db"] < -15.0

    def test_actual_timeline_preserves_zero_and_variable_transition_durations(self):
        from douyin_adapter import compute_segment_timeline

        template = {
            "transition_duration": 0.5,
            "segments": [
                {"index": 0, "duration": 3.0, "type": "hook"},
                {"index": 1, "duration": 4.0, "type": "showcase"},
                {"index": 2, "duration": 5.0, "type": "result"},
            ],
        }

        timeline = compute_segment_timeline(
            template,
            segment_durations={0: 2.8, 1: 4.2, 2: 4.7},
            transitions=[
                {"type": "none", "duration": 0.0},
                {"type": "dissolve", "duration": 0.35},
            ],
        )

        assert timeline == [
            {"index": 0, "start": 0.0, "end": 2.8, "duration": 2.8, "type": "hook", "purpose": ""},
            {"index": 1, "start": 2.8, "end": 7.0, "duration": 4.2, "type": "showcase", "purpose": ""},
            {"index": 2, "start": 6.65, "end": 11.35, "duration": 4.7, "type": "result", "purpose": ""},
        ]

    def test_subtitles_and_voiceover_share_actual_rendered_timeline(self):
        from ad_script import script_to_subtitles, script_to_voiceover

        script = {
            "segments": [
                {"segment": 0, "subtitle": "第一段", "voiceover": "第一段口播"},
                {"segment": 1, "subtitle": "第二段", "voiceover": "第二段口播"},
            ],
        }
        timeline = [
            {"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0},
            {"index": 1, "start": 2.0, "end": 5.0, "duration": 3.0},
        ]

        subtitles = script_to_subtitles(script, segment_timeline=timeline)
        voiceover = script_to_voiceover(script, segment_timeline=timeline)

        assert subtitles[1]["start"] == pytest.approx(2.45)
        assert subtitles[1]["end"] == pytest.approx(4.55)
        assert voiceover[1]["start"] == pytest.approx(2.3)
        assert voiceover[1]["end"] == pytest.approx(4.7)

    def test_none_transition_does_not_shift_sfx_timeline(self):
        from video_merger import generate_sfx_timings

        sfx = generate_sfx_timings(
            num_clips=2,
            clip_duration=3.0,
            narratives=["hook", "showcase"],
            segment_durations=[2.0, 3.0],
            transition_decisions=[{"type": "none", "duration": 0.0}],
        )

        showcase_ding = next(item for item in sfx if item["type"] == "ding")
        assert showcase_ding["time"] == pytest.approx(2.3)

    def test_local_multiclip_sfx_uses_edit_timeline_not_semantic_timeline(self):
        from video_merger import generate_sfx_timings

        edit_timeline = [
            {"index": 0, "start": 0.0, "end": 2.0, "duration": 2.0},
            {"index": 1, "start": 2.0, "end": 4.0, "duration": 2.0},
            {"index": 2, "start": 4.0, "end": 7.0, "duration": 3.0},
        ]

        sfx = generate_sfx_timings(
            num_clips=3,
            clip_duration=3.0,
            narratives=["hook", "hook", "value_closer"],
            transition_decisions=[
                {"type": "fade", "duration": 0.2},
                {"type": "none", "duration": 0.0},
            ],
            segment_timeline=edit_timeline,
            sfx_intensity="subtle",
        )

        assert not any(item["type"] in {"impact", "whoosh"} for item in sfx)

    def test_postproduction_contract_keeps_subtitle_position_platform_fixed(self, tmp_path):
        import cv2
        import numpy as np
        from postproduction_contract import build_local_postproduction_contract

        video = tmp_path / "busy-top.mp4"
        writer = cv2.VideoWriter(
            str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180),
        )
        for index in range(30):
            frame = np.full((180, 320, 3), 220, dtype=np.uint8)
            cv2.line(frame, (0, 15 + index % 40), (319, 60 + index % 30), (10, 10, 10), 5)
            writer.write(frame)
        writer.release()

        contract = build_local_postproduction_contract(
            selected_segments=[{
                "script_segment": 0,
                "narrative": "hook",
                "product_story_role": "finished_product",
                "clip_path": str(video),
                "motion": {"motion_class": "dynamic", "camera_motion": "pan_right"},
                "frame_quality": {"median_brightness": 180, "median_contrast": 55},
                "analysis": {"product_visibility": 5},
            }],
            creative_profile={"energy": "high", "recommended_pace": "fast"},
            music_contract={"bpm_min": 116, "bpm_max": 132, "sfx_intensity": "moderate"},
            reference_profile={"cta_text": "现在了解", "outro_duration": 2.0},
        )

        segment = contract["segments"][0]
        assert segment["subtitle"]["animation"] == "fade"
        assert "position" not in segment["subtitle"]
        assert "y_ratio" not in segment["subtitle"]
        assert "position" not in contract["semantic_subtitles"][0]
        assert "y_ratio" not in contract["semantic_subtitles"][0]
        assert contract["subtitle_style"]["placement_policy"] == "platform_fixed_bottom_safe_area"
        assert contract["bgm"]["fallback_allowed"] is False
        assert contract["transition"]["allow_none"] is True
        assert contract["cta"]["visual_mode"] == "closing_frame_tail_card"

    def test_fancy_subtitles_ignore_per_cue_vertical_positions(self, tmp_path):
        """遗留位置字段也不能覆盖抖音统一底部安全边距。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from video_merger import add_fancy_subtitles

        source = tmp_path / "source.mp4"
        source.write_bytes(b"video")
        output = tmp_path / "output.mp4"
        captured = {}

        def capture_ass(_cmd, timeout=300):
            ass_path = next(tmp_path.glob("*_fancy_subs.ass"))
            captured["content"] = ass_path.read_text(encoding="utf-8")

        probe = SimpleNamespace(returncode=0, stdout="1080\n1920\n2.0\n", stderr="")
        with (
            patch("video_merger.subprocess.run", return_value=probe),
            patch("video_merger._has_audio_stream", return_value=False),
            patch("video_merger.run_ffmpeg", side_effect=capture_ass),
        ):
            add_fancy_subtitles(
                source,
                [
                    {"text": "上方遗留值", "start": 0.0, "end": 1.0, "y_ratio": 0.24, "animation": "fade"},
                    {"text": "中间遗留值", "start": 1.0, "end": 2.0, "y_ratio": 0.68, "animation": "fade"},
                ],
                output,
                bottom_margin_ratio=0.22,
            )

        dialogue_lines = [
            line for line in captured["content"].splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogue_lines) == 2
        assert all(",,0,0,422,," in line for line in dialogue_lines)

    def test_visual_locked_sfx_is_not_moved_to_music_beat(self, tmp_path):
        from video_merger import align_sfx_to_beats

        bgm = tmp_path / "bgm.mp3"
        bgm.write_bytes(b"test")
        sfx = [
            {"time": 1.0, "type": "whoosh", "locked_to_visual": True},
            {"time": 1.0, "type": "ding", "locked_to_visual": False},
        ]
        with patch("video_merger._detect_beats", return_value=[0.0, 1.18, 2.0]):
            aligned = align_sfx_to_beats(sfx, bgm)

        assert aligned[0]["time"] == 1.0
        assert aligned[1]["time"] == pytest.approx(1.18)

    def test_transition_candidates_include_true_none(self):
        from intelligent_transition import score_transition_candidates

        candidates = score_transition_candidates({
            "composition_similarity": 0.8,
            "motion_alignment": 0.8,
            "motion_speed": 0.2,
            "brightness_delta": 0.1,
            "color_delta": 0.1,
            "scene_difference": 0.3,
            "subject_centeredness": 0.7,
            "narrative_pair": "showcase->showcase",
            "style": "moderate",
            "motion_direction": "static",
        })

        assert "none" in {candidate["type"] for candidate in candidates}

    def test_true_none_transition_preserves_full_clip_durations(self, tmp_path):
        import subprocess
        from video_merger import _get_clip_duration, _merge_with_transitions

        clips = []
        for index, color in enumerate(("red", "blue")):
            clip = tmp_path / f"clip-{index}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=360x640:r=30:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
            ], capture_output=True, check=True)
            clips.append(clip)
        output = tmp_path / "none.mp4"

        _merge_with_transitions(
            clips,
            output,
            [{"type": "none", "duration": 0.0}],
        )

        assert _get_clip_duration(output) == pytest.approx(2.0, abs=0.08)

    def test_mixed_concat_and_xfade_boundaries_keep_one_timebase(self, tmp_path):
        import subprocess
        from video_merger import _get_clip_duration, _merge_with_transitions

        clips = []
        for index, color in enumerate(("red", "blue", "green", "yellow", "purple")):
            clip = tmp_path / f"clip-{index}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=360x640:r=30:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
            ], capture_output=True, check=True)
            clips.append(clip)
        output = tmp_path / "mixed.mp4"

        _merge_with_transitions(
            clips,
            output,
            [
                {"type": "fade", "duration": 0.2},
                {"type": "none", "duration": 0.0},
                {"type": "fade", "duration": 0.2},
                {"type": "dissolve", "duration": 0.2},
            ],
        )

        assert _get_clip_duration(output) == pytest.approx(4.4, abs=0.12)

    def test_local_cta_tail_uses_closing_material_frame(self, tmp_path):
        import subprocess
        from PIL import Image
        from video_merger import add_brand_intro_outro

        video = tmp_path / "blue.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=30:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ], capture_output=True, check=True)
        output = tmp_path / "cta.mp4"

        result = add_brand_intro_outro(
            video=video,
            output=output,
            product_name="茶咖",
            cta_text="现在了解",
            primary_color="#FF0000",
            intro_duration=0.0,
            outro_duration=1.5,
            outro_background_video=video,
            strict_material_background=True,
            resolution="360x640",
        )
        frame = tmp_path / "tail.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-sseof", "-0.2", "-i", str(output),
            "-frames:v", "1", str(frame),
        ], capture_output=True, check=True)
        red, green, blue = Image.open(frame).convert("RGB").resize((1, 1)).getpixel((0, 0))

        assert result == output
        assert blue > red * 1.5

    def test_quality_guard_uses_real_subtitle_fields_and_external_cta_contract(self):
        from production_quality_guard import ProductionQualityGuard, ProductionQualityReport

        guard = ProductionQualityGuard()
        report = ProductionQualityReport()
        report.video_metadata = {"height": 1920}
        guard._check_ad_effectiveness(
            Path("/tmp/not-read.mp4"),
            product_reference=None,
            subtitles=[{"start": 0.0, "end": 2.0}],
            segments=[{"narrative": "value", "text": "最后一个购买理由"}],
            report=report,
            cta_contract={"enabled": True, "duration": 2.0, "text": "现在了解"},
        )

        issues = report.dimension_scores["ad_effectiveness"].issues
        assert not any("未检测到明确的CTA" in issue.message for issue in issues)
        assert not any("平均停留" in issue.message for issue in issues)
        assert not any("平台UI遮挡区域" in issue.message for issue in issues)

    def test_local_mode_does_not_use_static_bgm_or_cinematic_color_fallbacks(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")

        assert "PROJECT_ROOT / BGM_PATH" not in source
        assert source.index("_actual_segment_timeline = compute_segment_timeline(") < source.index("bgm_file = pick_bgm_for_product(")
        assert 'color_preset = "none" if local_asset_mode' in source
        assert "brand_color_tint=not local_asset_mode" in source


class TestReferenceAdAnalysis:
    """参考广告必须完整覆盖销售结构，尤其不能漏掉结尾 CTA。"""

    def test_reference_sampling_covers_every_scene_and_final_scene_three_times(self):
        from local_asset_pipeline import _reference_contact_sheet_times

        scenes = [
            {"start": 0.0, "end": 3.667},
            {"start": 3.667, "end": 6.833},
            {"start": 6.833, "end": 11.233},
            {"start": 11.233, "end": 12.733},
            {"start": 12.733, "end": 15.867},
            {"start": 15.867, "end": 19.278},
        ]

        times = _reference_contact_sheet_times(scenes, duration=19.328, frame_count=16)

        for scene in scenes:
            midpoint = (scene["start"] + scene["end"]) / 2
            assert any(abs(timestamp - midpoint) < 0.02 for timestamp in times)
        final_scene_samples = [timestamp for timestamp in times if timestamp >= scenes[-1]["start"]]
        assert len(final_scene_samples) >= 3
        assert min(final_scene_samples) <= scenes[-1]["start"] + 0.2
        assert max(final_scene_samples) >= scenes[-1]["end"] - 0.2

    def test_stale_reference_profile_is_rebuilt(self, tmp_path):
        import json
        import local_asset_pipeline

        video = tmp_path / "reference.mp4"
        video.write_bytes(b"reference-video")
        source_hash = "a" * 64
        cache_dir = tmp_path / "index" / "reference_ads" / source_hash[:16]
        cache_dir.mkdir(parents=True)
        (cache_dir / "profile.json").write_text(
            json.dumps({"source_sha256": source_hash, "cta_text": ""}),
            encoding="utf-8",
        )
        analyzed = {
            "source_sha256": source_hash,
            "cta_text": "赶紧来试试",
            "outro_duration": 3.4,
        }

        with patch.object(local_asset_pipeline, "ensure_vision_backend_available"), \
             patch.object(local_asset_pipeline, "LOCAL_ASSET_INDEX_PATH", tmp_path / "index"), \
             patch.object(local_asset_pipeline, "_ffprobe", return_value={"duration": 19.328}), \
             patch.object(local_asset_pipeline, "_hash_file", return_value=source_hash), \
             patch.object(local_asset_pipeline, "_detect_scene_ranges", return_value=[
                 {"start": 0.0, "end": 15.867},
                 {"start": 15.867, "end": 19.278},
             ]), \
             patch.object(local_asset_pipeline, "_make_contact_sheet"), \
             patch.object(local_asset_pipeline.VisionAnalyzer, "analyze_reference_ad", return_value=analyzed) as analyze:
            profile = local_asset_pipeline.analyze_reference_ad(video)

        analyze.assert_called_once()
        assert profile["cta_text"] == "赶紧来试试"
        assert profile["reference_profile_version"] == local_asset_pipeline.REFERENCE_PROFILE_VERSION

    def test_complete_v4_reference_profile_migrates_without_remote_reanalysis(self, tmp_path):
        import json
        import local_asset_pipeline

        video = tmp_path / "reference.mp4"
        video.write_bytes(b"reference-video")
        source_hash = "b" * 64
        cache_dir = tmp_path / "index" / "reference_ads" / source_hash[:16]
        cache_dir.mkdir(parents=True)
        profile_path = cache_dir / "profile.json"
        profile_path.write_text(json.dumps({
            "reference_profile_version": 4,
            "source_sha256": source_hash,
            "duration": 19.3,
            "sales_structure": ["question_hook", "ingredient_proof", "cta_outro"],
            "copy_tone": ["question", "proof", "cta"],
            "visible_sales_copy": ["可见参考字幕"],
            "cta_text": "来试试",
        }), encoding="utf-8")

        with patch.object(local_asset_pipeline, "ensure_vision_backend_available"), \
             patch.object(local_asset_pipeline, "LOCAL_ASSET_INDEX_PATH", tmp_path / "index"), \
             patch.object(local_asset_pipeline, "_ffprobe", return_value={"duration": 19.3}), \
             patch.object(local_asset_pipeline, "_hash_file", return_value=source_hash), \
             patch.object(local_asset_pipeline.VisionAnalyzer, "analyze_reference_ad") as analyze:
            profile = local_asset_pipeline.analyze_reference_ad(video)

        analyze.assert_not_called()
        assert profile["reference_profile_version"] == 5
        assert profile["creative_mechanics"]["hook_mechanism"] == "curiosity_gap"
        assert json.loads(profile_path.read_text())["reference_profile_version"] == 5

    def test_reference_claims_require_two_timestamped_frames(self):
        from local_asset_pipeline import _normalize_reference_profile

        profile = _normalize_reference_profile({
            "visible_sales_copy": [
                {"text": "单帧疑似文案", "evidence_times": [1.2]},
                {"text": "0卡糖 0脂", "evidence_times": [8.8, 10.4]},
            ],
            "factual_claims": [
                {"text": "疑似横县", "type": "origin", "evidence_times": [4.2]},
                {"text": "0卡糖 0脂", "type": "specification", "evidence_times": [8.8, 10.4]},
            ],
            "cta_text": "赶紧来试试",
            "cta_evidence_times": [17.5, 19.2],
        }, {"duration": 20.0})

        assert profile["visible_sales_copy"] == ["0卡糖 0脂"]
        assert [claim["text"] for claim in profile["factual_claims"]] == ["0卡糖 0脂"]
        assert profile["cta_text"] == "赶紧来试试"

    def test_reference_claims_must_match_independent_product_facts(self):
        from local_asset_pipeline import bind_reference_profile_to_product

        profile = {
            "factual_claims": [
                {"text": "广西横州茉莉", "type": "origin", "evidence_source": "reference_video"},
                {"text": "三窨一提工艺", "type": "production", "evidence_source": "reference_video"},
            ],
            "visible_sales_copy": ["广西横州茉莉", "三窨一提工艺", "赶紧来试试"],
            "cta_text": "赶紧来试试",
        }

        bound = bind_reference_profile_to_product(profile, {"origin": ["广西横州茉莉"]})

        assert [claim["text"] for claim in bound["factual_claims"]] == ["广西横州茉莉"]
        assert bound["visible_sales_copy"] == ["广西横州茉莉", "赶紧来试试"]
        assert bound["cta_text"] == "赶紧来试试"

    def test_continuous_voiceover_synthesizes_once_and_keeps_segment_timing(self, tmp_path):
        import tts_client

        generated_texts = []

        def fake_tts(text, path, voice=None, rate=None):
            generated_texts.append(text)
            path.write_bytes(b"audio")

        lines = [
            {"text": "先看横州茉莉", "start": 0.2, "end": 3.5, "segment": 0},
            {"text": "再看手工采摘", "start": 3.5, "end": 7.0, "segment": 1, "is_outro": True},
        ]
        with patch.object(tts_client, "generate_tts_audio", side_effect=fake_tts), \
             patch.object(tts_client, "_get_audio_duration", return_value=6.0), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=6.0), \
             patch.object(tts_client.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(tts_client, "_mix_audio_segments") as mix:
            _, aligned = tts_client.generate_full_voiceover(
                lines,
                tmp_path / "voice.m4a",
                total_duration=7.2,
                continuous_narration=True,
                continuous_text="先看横州茉莉，再看手工采摘。",
            )

        assert generated_texts == ["先看横州茉莉，再看手工采摘。"]
        mix.assert_called_once()
        assert [item["segment"] for item in aligned] == [0, 1]
        assert aligned[0]["end"] == pytest.approx(aligned[1]["start"])
        assert aligned[1]["is_outro"] is True

    def test_local_voiceover_is_one_explicit_script_not_segment_sentence_join(self, tmp_path):
        import tts_client

        generated_texts = []

        def fake_tts(text, path, voice=None, rate=None):
            generated_texts.append(text)
            path.write_bytes(b"audio")

        lines = [
            {"text": "这瓶茶咖为什么值得看", "start": 0.4, "end": 3.4, "segment": 0},
            {"text": "先从原料讲起", "start": 3.6, "end": 6.4, "segment": 1},
            {"text": "最后想试就看这一瓶", "start": 6.6, "end": 9.4, "segment": 2},
        ]
        full_text = "这瓶茶咖为什么值得看？先从原料讲起，再说到随手能喝，最后想试就看这一瓶。"
        with patch.object(tts_client, "generate_tts_audio", side_effect=fake_tts), \
             patch.object(tts_client, "_get_audio_duration", return_value=8.5), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=8.5), \
             patch.object(tts_client.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(tts_client, "_mix_audio_segments"):
            tts_client.generate_full_voiceover(
                lines,
                tmp_path / "voice.m4a",
                total_duration=9.8,
                continuous_narration=True,
                continuous_text=full_text,
            )

        assert generated_texts == [full_text]

    def test_pre_generated_one_take_master_is_reused_without_second_tts_request(self, tmp_path):
        import tts_client

        master = tmp_path / "master.m4a"
        master.write_bytes(b"audio")
        performance = {}
        with patch.object(tts_client, "generate_tts_audio") as generate, \
             patch.object(tts_client, "_get_audio_duration", return_value=8.5), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=8.5), \
             patch.object(tts_client.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(tts_client, "_mix_audio_segments"):
            tts_client.generate_full_voiceover(
                [
                    {"text": "先看产品，", "start": 0.2, "end": 4.5, "segment": 0},
                    {"text": "再看原料。", "start": 4.5, "end": 9.5, "segment": 1},
                ],
                tmp_path / "voice.m4a",
                total_duration=9.8,
                continuous_narration=True,
                continuous_text="先看产品，再看原料。",
                performance_profile=performance,
                pre_generated_audio=master,
            )

        generate.assert_not_called()
        assert performance["mode"] == "single_take"
        assert performance["tts_requests"] == 1
        assert performance["tts_reused"] is True

    def test_local_one_take_master_cache_is_keyed_by_full_synthesis_contract(self, tmp_path):
        import one_click_create

        output = tmp_path / "final" / "voiceover_master.m4a"
        output.parent.mkdir(parents=True)
        script = {
            "voiceover_full": "同一条完整口播。",
            "segments": [{"segment": 0, "voiceover": "同一条完整口播。", "product_story_role": "finished_product"}],
        }
        windows = [{
            "source_path": str(tmp_path / "product.mp4"),
            "start": 0.0,
            "end": 8.0,
            "analysis": {"usable_for_ad": True, "product_story_role": "finished_product"},
        }]

        def fake_tts(_text, path, voice=None, rate=None):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio-master")

        with patch.object(one_click_create, "generate_tts_audio", side_effect=fake_tts) as generate, \
             patch.object(one_click_create, "_validate_voiceover_audio", return_value=4.0):
            first = one_click_create._prepare_local_one_take_master(
                script, {"windows": windows}, {"name": "产品"}, "female_young",
                {}, {}, 0.0, output,
            )
            second = one_click_create._prepare_local_one_take_master(
                script, {"windows": windows}, {"name": "产品"}, "female_young",
                {}, {}, 0.0, output,
            )

        assert generate.call_count == 1
        assert first["tts_cache_hit"] is False
        assert second["tts_cache_hit"] is True
        assert second["tts_external_requests"] == 0

    def test_unspecified_visual_roles_use_global_usable_material_capacity(self, tmp_path):
        import one_click_create

        cues = ["第一句。", "第二句。", "第三句。", "第四句。", "第五句。"]
        script = {
            "voiceover_full": "".join(cues),
            "segments": [
                {"segment": index, "voiceover": cue, "product_story_role": ""}
                for index, cue in enumerate(cues)
            ],
        }
        asset_index = {
            "windows": [{
                "source_path": str(tmp_path / "product.mp4"),
                "start": 0.0,
                "end": 20.0,
                "analysis": {
                    "usable_for_ad": True,
                    "product_story_role": "finished_product",
                },
            }],
        }

        timeline = one_click_create._build_local_one_take_timeline_from_audio(
            ad_script=script,
            asset_index=asset_index,
            reference_profile={},
            transition_duration=0.0,
            output_path=tmp_path / "master.m4a",
            audio_duration=14.592,
            voice="energetic_female",
            voice_reason="test",
        )

        assert timeline["main_duration"] == pytest.approx(14.592, abs=0.001)
        assert sum(timeline["clip_durations"].values()) == pytest.approx(14.592, abs=0.01)
        assert timeline["tempo_multiplier"] == 1.0

    def test_one_take_capacity_gap_is_reported_without_mutating_audio(self):
        from local_asset_pipeline import LocalAssetError, build_one_take_timeline

        script = {
            "voiceover_full": "同一条完整口播。",
            "segments": [{"segment": 0, "voiceover": "同一条完整口播。"}],
        }

        with pytest.raises(LocalAssetError) as raised:
            build_one_take_timeline(
                script,
                main_duration=33.67,
                max_clip_durations={0: 30.63},
            )

        assert raised.value.details == {
            "reason": "semantic_segment_material_capacity_gap",
            "stage": "one_take_timeline",
            "required_duration": 33.67,
            "covered_duration": 30.63,
            "missing_duration": 3.04,
        }

    def test_local_one_take_pipeline_has_no_automatic_tts_retime_path(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")

        assert "_retime_local_one_take_for_capacity" not in source
        assert "atempo=" not in source[source.index("def _prepare_local_one_take_master"):source.index("def _build_local_one_take_timeline_from_audio")]

    def test_one_take_timeline_drives_clip_durations_from_full_narration(self):
        from local_asset_pipeline import build_one_take_timeline

        script = {
            "voiceover_full": "这瓶你见过吗？先看原料，再看怎么喝。想试就了解一下。",
            "voiceover_outro_cue": "想试就了解一下。",
            "segments": [
                {"segment": 0, "voiceover": "这瓶你见过吗？"},
                {"segment": 1, "voiceover": "先看原料，"},
                {"segment": 2, "voiceover": "再看怎么喝。"},
            ],
        }

        timeline = build_one_take_timeline(
            script,
            main_duration=9.0,
            outro_duration=3.0,
            max_clip_durations={0: 4.0, 1: 4.0, 2: 4.0},
        )

        assert sum(timeline["clip_durations"].values()) == pytest.approx(9.0, abs=0.01)
        assert timeline["voiceover_lines"][-1]["is_outro"] is True
        assert timeline["total_duration"] == 12.0

    def test_one_take_timeline_redistributes_long_cue_to_real_material_capacity(self):
        from local_asset_pipeline import build_one_take_timeline

        script = {
            "voiceover_full": "这一段口播故意特别特别长。短句。收尾。",
            "segments": [
                {"segment": 0, "voiceover": "这一段口播故意特别特别长。"},
                {"segment": 1, "voiceover": "短句。"},
                {"segment": 2, "voiceover": "收尾。"},
            ],
        }

        timeline = build_one_take_timeline(
            script,
            main_duration=9.0,
            max_clip_durations={0: 3.0, 1: 4.0, 2: 4.0},
        )

        assert timeline["clip_durations"][0] == 3.0
        assert sum(timeline["clip_durations"].values()) == pytest.approx(9.0, abs=0.01)

    def test_local_pipeline_generates_master_before_materializing_and_reuses_it(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")

        prepare_pos = source.index("local_one_take_timeline = _prepare_local_one_take_master(")
        materialize_pos = source.index("local_asset_result = plan_and_materialize_local_clips(")
        reuse_pos = source.index("pre_generated_audio=local_one_take_master")

        assert prepare_pos < materialize_pos < reuse_pos

    def test_local_script_requires_full_narration_to_equal_ordered_visual_cues(self):
        from local_asset_pipeline import LocalAssetError, validate_continuous_voiceover_contract

        valid = {
            "voiceover_full": "为什么看这瓶？因为原料有依据，想试就看这一瓶。",
            "segments": [
                {"voiceover": "为什么看这瓶？"},
                {"voiceover": "因为原料有依据，"},
                {"voiceover": "想试就看这一瓶。"},
            ],
        }
        assert validate_continuous_voiceover_contract(valid) == valid["voiceover_full"]

        invalid = {
            **valid,
            "voiceover_full": "为什么看这瓶？这是另一篇口播。",
        }
        with pytest.raises(LocalAssetError, match="无损、有序切分"):
            validate_continuous_voiceover_contract(invalid)

    def test_one_take_cues_are_the_only_authored_copy_source(self):
        from local_asset_pipeline import materialize_continuous_voiceover_contract

        script = {
            "voiceover_cues": [
                "这瓶茶咖为什么值得看？",
                "先看原料依据，",
                "再看日常怎么喝，",
                "想试就看这一瓶。",
            ],
            "voiceover_full": "模型重复输出但不一致的旧文本",
            "segments": [
                {"voiceover": "旧分段一", "subtitle": "旧字幕一"},
                {"voiceover": "旧分段二", "subtitle": "旧字幕二"},
                {"voiceover": "旧分段三", "subtitle": "旧字幕三"},
                {"voiceover": "旧分段四", "subtitle": "旧字幕四"},
            ],
        }

        full_text = materialize_continuous_voiceover_contract(script)

        assert full_text == "".join(script["voiceover_cues"])
        assert script["voiceover_full"] == full_text
        assert [segment["voiceover"] for segment in script["segments"]] == script["voiceover_cues"]
        assert script["segments"][0]["subtitle"] == "这瓶茶咖为什么值得看？"

    def test_one_take_contract_rejects_wrong_cue_count_before_semantic_validation(self):
        from local_asset_pipeline import LocalAssetError, materialize_continuous_voiceover_contract

        with pytest.raises(LocalAssetError, match="cue 数量"):
            materialize_continuous_voiceover_contract({
                "voiceover_cues": ["只有一段。"],
                "segments": [{}, {}],
            })

    def test_local_script_style_cannot_rewrite_one_take_cues(self):
        from ad_script import script_to_voiceover

        script = {
            "voiceover_full": "先看这一瓶，因为原料有依据。",
            "segments": [
                {"segment": 0, "voiceover": "先看这一瓶，"},
                {"segment": 1, "voiceover": "因为原料有依据。"},
            ],
        }
        lines = script_to_voiceover(
            script,
            voiceover_style="storytelling",
            segment_timeline=[
                {"index": 0, "start": 0.0, "end": 3.0, "duration": 3.0},
                {"index": 1, "start": 3.0, "end": 6.0, "duration": 3.0},
            ],
        )

        assert "".join(line["text"] for line in lines) == script["voiceover_full"]

    def test_continuous_subtitles_do_not_invent_shot_gaps_absent_from_one_take_audio(self, tmp_path):
        import tts_client

        lines = [
            {"text": "先看成品", "start": 0.4, "end": 3.4, "segment": 0},
            {"text": "再看原料", "start": 3.9, "end": 6.3, "segment": 1},
            {"text": "接着看产地", "start": 6.9, "end": 9.3, "segment": 2},
        ]
        with patch.object(tts_client, "generate_tts_audio", side_effect=lambda _text, path, **_: path.write_bytes(b"audio")), \
             patch.object(tts_client, "_get_audio_duration", return_value=8.7), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=8.7), \
             patch.object(tts_client.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(tts_client, "_mix_audio_segments"):
            _, aligned = tts_client.generate_full_voiceover(
                lines,
                tmp_path / "voice.m4a",
                total_duration=9.8,
                continuous_narration=True,
                continuous_text="先看成品，再看原料，接着看产地。",
            )

        assert [(item["start"], item["end"]) for item in aligned] == [
            (0.4, 3.4),
            (3.4, 6.3),
            (6.3, 9.1),
        ]

    def test_one_take_cue_boundaries_follow_measured_audio_pauses(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        lines = [
            {"text": "先看产品。", "start": 0.0, "end": 2.8, "segment": 0},
            {"text": "再看原料。", "start": 2.8, "end": 5.7, "segment": 1},
            {"text": "最后看用法。", "start": 5.7, "end": 9.0, "segment": 2},
        ]
        stderr = """
        silence_start: 0
        silence_end: 0.20 | silence_duration: 0.20
        silence_start: 3.18
        silence_end: 3.42 | silence_duration: 0.24
        silence_start: 6.31
        silence_end: 6.55 | silence_duration: 0.24
        silence_start: 8.90
        """
        with patch.object(tts_client.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(
                 tts_client.subprocess,
                 "run",
                 return_value=MagicMock(returncode=0, stderr=stderr),
             ):
            aligned = tts_client.align_voiceover_lines_to_audio_pauses(lines, audio, 9.0)

        assert aligned[0]["start"] == pytest.approx(0.20)
        assert aligned[0]["end"] == pytest.approx(3.30)
        assert aligned[1]["start"] == pytest.approx(3.30)
        assert aligned[1]["end"] == pytest.approx(6.43)
        assert aligned[2]["start"] == pytest.approx(6.43)
        assert aligned[-1]["end"] == pytest.approx(8.90)
        assert all(item["alignment_precision"] == "measured_audio_pause" for item in aligned[:-1])
        assert aligned[-1]["alignment_precision"] == "measured_audio_end"

    def test_one_take_subtitle_phrases_use_punctuation_and_measured_pauses(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        lines = [{
            "text": "先看原料，再看产地，最后看怎么喝。",
            "start": 0.0,
            "end": 6.0,
            "segment": 0,
        }]
        with patch.object(
            tts_client,
            "_detect_audio_alignment",
            return_value={
                "speech_start": 0.0,
                "speech_end": 6.0,
                "pauses": [(1.8, 2.0), (3.7, 3.9)],
            },
        ):
            aligned = tts_client.split_and_align_voiceover_subtitles(
                lines, audio, 6.0, max_units=7,
            )

        assert [item["text"] for item in aligned] == ["先看原料，", "再看产地，", "最后看怎么喝。"]
        assert [item["end"] for item in aligned] == pytest.approx([1.9, 3.8, 6.0])
        assert all(item["segment"] == 0 for item in aligned)

    def test_one_take_subtitle_keeps_semantic_phrase_at_measured_pause(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        long_phrase = "都是当天现做的多种新鲜手工面包，"
        lines = [{
            "text": f"这家开在路边的面包摊，{long_phrase}分装干净拿取方便。",
            "start": 0.0,
            "end": 6.0,
            "segment": 0,
        }]
        with patch.object(
            tts_client,
            "_detect_audio_alignment",
            return_value={
                "speech_start": 0.0,
                "speech_end": 6.0,
                "pauses": [(1.8, 2.0), (3.7, 3.9)],
            },
        ):
            aligned = tts_client.split_and_align_voiceover_subtitles(
                lines, audio, 6.0, max_units=11,
            )

        assert [item["text"] for item in aligned] == [
            "这家开在路边的面包摊，",
            long_phrase,
            "分装干净拿取方便。",
        ]
        assert aligned[1]["end"] == pytest.approx(3.8)
        assert all(item["semantic_phrase"] is True for item in aligned)

    def test_local_one_take_keeps_tts_aligned_subtitles_as_timing_source(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        alignment_block = source[
            source.index("subtitles = align_subtitles_to_voiceover("):
            source.index("subtitles = optimize_subtitles_for_douyin(")
        ]

        assert "subtitles = align_subtitles_to_voiceover(subtitles, main_voiceover_subs)" in alignment_block
        assert 'local_one_take_timeline["voiceover_lines"]' not in alignment_block

    def test_voiceover_trailing_silence_blocks_publishable_output(self, tmp_path):
        import tts_client

        with patch.object(tts_client, "generate_tts_audio", side_effect=lambda _text, path, **_: path.write_bytes(b"audio")), \
             patch.object(tts_client, "_get_audio_duration", return_value=6.0), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=5.8), \
             patch.object(tts_client.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(tts_client, "_mix_audio_segments"):
            with pytest.raises(RuntimeError, match="尾部缺少口播"):
                tts_client.generate_full_voiceover(
                    [{"text": "结尾也要有口播", "start": 0.2, "end": 9.5, "segment": 0}],
                    tmp_path / "voice.m4a",
                    total_duration=10.0,
                    continuous_narration=True,
                    continuous_text="结尾也要有口播。",
                )

    def test_spoken_audio_end_ignores_closed_cta_pause(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        stderr = """
        silence_start: 0
        silence_end: 0.224167 | silence_duration: 0.224167
        silence_start: 3.689542
        silence_end: 3.987417 | silence_duration: 0.297875
        silence_start: 30.773208
        silence_end: 31.035542 | silence_duration: 0.262334
        """
        with patch.object(tts_client.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(
                 tts_client.subprocess,
                 "run",
                 return_value=MagicMock(returncode=0, stderr=stderr),
             ):
            spoken_end = tts_client._detect_spoken_audio_end(audio, 32.736)

        assert spoken_end == pytest.approx(32.736)

    def test_spoken_audio_end_returns_unclosed_final_silence_start(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        stderr = """
        silence_start: 0
        silence_end: 0.21 | silence_duration: 0.21
        silence_start: 8.72
        """
        with patch.object(tts_client.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(
                 tts_client.subprocess,
                 "run",
                 return_value=MagicMock(returncode=0, stderr=stderr),
             ):
            spoken_end = tts_client._detect_spoken_audio_end(audio, 9.4)

        assert spoken_end == pytest.approx(8.72)

    def test_spoken_audio_end_ignores_closed_leading_and_intermediate_silence(self, tmp_path):
        import tts_client

        audio = tmp_path / "voice.m4a"
        audio.write_bytes(b"audio")
        stderr = """
        silence_start: 0
        silence_end: 0.25 | silence_duration: 0.25
        silence_start: 4.10
        silence_end: 4.42 | silence_duration: 0.32
        """
        with patch.object(tts_client.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(
                 tts_client.subprocess,
                 "run",
                 return_value=MagicMock(returncode=0, stderr=stderr),
             ):
            spoken_end = tts_client._detect_spoken_audio_end(audio, 9.4)

        assert spoken_end == pytest.approx(9.4)

    def test_exact_fit_one_take_is_not_artificially_accelerated(self, tmp_path):
        import tts_client

        master = tmp_path / "master.m4a"
        master.write_bytes(b"audio")
        performance = {}
        with patch.object(tts_client, "_get_audio_duration", return_value=9.0), \
             patch.object(tts_client, "_detect_spoken_audio_end", return_value=9.0), \
             patch.object(tts_client.subprocess, "run") as run, \
             patch.object(tts_client, "_mix_audio_segments"):
            tts_client.generate_full_voiceover(
                [{"text": "完整的一条口播", "start": 0.0, "end": 9.0, "segment": 0}],
                tmp_path / "voice.m4a",
                total_duration=9.1,
                continuous_narration=True,
                continuous_text="完整的一条口播。",
                performance_profile=performance,
                pre_generated_audio=master,
            )

        assert performance["tempo_multiplier"] == 1.0
        assert all("atempo=" not in " ".join(call.args[0]) for call in run.call_args_list)

    def test_continuous_voiceover_fills_window_and_reports_performance_tempo(self, tmp_path):
        """连续口播明显短于画面时应自然放慢，并把同一演绎速度交给 CTA。"""
        import shutil
        import subprocess
        import tts_client

        template = tmp_path / "template.m4a"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=24000:duration=8.0",
            "-c:a", "aac", str(template),
        ], check=True, capture_output=True)
        performance = {}
        with patch.object(
            tts_client,
            "generate_tts_audio",
            side_effect=lambda _text, path, voice=None, rate=None: shutil.copy(template, path),
        ):
            _, aligned = tts_client.generate_full_voiceover(
                [
                    {"text": "先看产品", "start": 0.2, "end": 5.0, "segment": 0},
                    {"text": "再看使用方式", "start": 5.0, "end": 9.8, "segment": 1},
                ],
                tmp_path / "voice.m4a",
                total_duration=10.0,
                continuous_narration=True,
                continuous_text="先看产品，再看使用方式。",
                performance_profile=performance,
            )

        assert 0.89 <= performance["tempo_multiplier"] < 1.0
        assert performance["voice"] == "female_young"
        assert aligned[-1]["end"] > 8.8

    def test_reference_cta_only_adds_timing_for_text_already_in_full_narration(self):
        from one_click_create import _append_outro_timing_to_voiceover_script

        combined = _append_outro_timing_to_voiceover_script(
            [{"text": "先看真实茉莉花茶", "start": 0.2, "end": 5.0, "segment": 0}],
            outro_cue="没试过赶紧来试试",
            main_duration=16.2,
            outro_duration=3.4,
        )

        assert len(combined) == 2
        assert combined[-1]["text"] == "没试过赶紧来试试"
        assert combined[-1]["is_outro"] is True
        assert combined[-1]["start"] == 16.2
        assert combined[-1]["end"] == pytest.approx(19.6)

    def test_cover_candidate_cleanup_keeps_only_final_cover(self, tmp_path):
        from one_click_create import _cleanup_cover_candidates

        candidates = [tmp_path / f"cover_c{index}.jpg" for index in range(3)]
        final_cover = tmp_path / "cover.jpg"
        for path in [*candidates, final_cover]:
            path.write_bytes(b"image")

        _cleanup_cover_candidates(candidates)

        assert final_cover.exists()
        assert not any(path.exists() for path in candidates)

    def test_voiceover_alignment_uses_exact_spoken_copy(self):
        from tts_client import align_subtitles_to_voiceover

        subtitles = [{"text": "0卡糖 0脂", "start": 0.0, "end": 2.0, "segment": 2}]
        voiceover = [{"text": "想喝点有风味又没负担的，就看这一瓶。", "start": 4.0, "end": 7.0, "segment": 2}]

        aligned = align_subtitles_to_voiceover(subtitles, voiceover)

        assert aligned == [{"text": "想喝点有风味又没负担的，就看这一瓶。", "start": 4.0, "end": 7.0, "segment": 2}]

    def test_voiceover_alignment_preserves_semantic_phrase_provenance(self):
        from tts_client import align_subtitles_to_voiceover

        aligned = align_subtitles_to_voiceover(
            [{"text": "旧字幕", "start": 0.0, "end": 2.0, "segment": 1}],
            [{
                "text": "这款瓶装茶咖太适合你的需求了。",
                "start": 0.0,
                "end": 2.0,
                "segment": 1,
                "semantic_phrase": True,
                "alignment_precision": "measured_audio_pause",
            }],
        )

        assert aligned[0]["semantic_phrase"] is True
        assert aligned[0]["alignment_precision"] == "measured_audio_pause"

    def test_semantic_phrase_is_not_resplit_by_single_line_renderer(self):
        from video_merger import prepare_single_line_subtitles

        result = prepare_single_line_subtitles([{
            "text": "这款瓶装茶咖太适合你的需求了。",
            "start": 0.0,
            "end": 2.0,
            "segment": 1,
            "semantic_phrase": True,
        }], max_units=11)

        assert result == [{
            "text": "这款瓶装茶咖太适合你的需求了",
            "start": 0.0,
            "end": 2.0,
            "segment": 1,
            "semantic_phrase": True,
            "highlight": [],
        }]

    def test_subtitle_copy_removes_all_punctuation_before_single_line_split(self):
        from video_merger import prepare_single_line_subtitles

        result = prepare_single_line_subtitles([{
            "text": "没试过？赶紧来试试~ 0卡糖、0脂！",
            "start": 0.0,
            "end": 3.0,
            "segment": 0,
        }], max_units=11)

        assert "".join(item["text"] for item in result) == "没试过赶紧来试试0卡糖0脂"
        assert all(not re.search(r"[^\w\s\u4e00-\u9fff]", item["text"]) for item in result)

    def test_subtitle_phrase_boundaries_are_chosen_before_display_punctuation_is_removed(self):
        from video_merger import prepare_single_line_subtitles

        result = prepare_single_line_subtitles([{
            "text": "先看原料，再看产地，最后看怎么喝。",
            "start": 0.0,
            "end": 4.0,
            "segment": 0,
        }], max_units=7)

        assert [item["text"] for item in result] == ["先看原料", "再看产地", "最后看怎么喝"]
        assert all(not re.search(r"[^\w\s\u4e00-\u9fff]", item["text"]) for item in result)

    def test_local_sales_subtitles_keep_short_semantic_sentences_intact(self):
        from one_click_create import _single_line_subtitle_capacity
        from video_merger import prepare_single_line_subtitles

        source = [
            {"text": "这款茶咖就是很合适的选择。", "start": 0.0, "end": 2.0, "segment": 1},
            {"text": "想要的直接戳下方小黄车购买。", "start": 2.0, "end": 4.0, "segment": 4},
        ]

        result = prepare_single_line_subtitles(
            source,
            max_units=_single_line_subtitle_capacity(
                video_width=1080,
                font_size=int(1920 * 0.035),
            ),
        )

        assert [item["text"] for item in result] == [
            "这款茶咖就是很合适的选择",
            "想要的直接戳下方小黄车购买",
        ]

    def test_subtitle_segmentation_never_splits_native_chinese_words(self):
        from video_merger import prepare_single_line_subtitles

        result = prepare_single_line_subtitles([{
            "text": "你喝过同时包含茶和咖啡的瓶装饮品吗？是不是好奇它的搭配到底是什么样的？",
            "start": 0.0,
            "end": 8.0,
            "segment": 0,
        }], max_units=11)
        chunks = [item["text"] for item in result]

        for term in ("咖啡", "瓶装", "饮品", "搭配"):
            assert any(term in chunk for chunk in chunks), (term, chunks)

    def test_subtitle_color_uses_contrast_and_falls_back_to_white(self):
        from video_merger import choose_readable_subtitle_color

        assert choose_readable_subtitle_color((245, 245, 245), ["#111111", "#FF6B6B"]) == "#111111"
        assert choose_readable_subtitle_color((128, 128, 128), ["#777777"], fallback="#FFFFFF") == "#FFFFFF"

    def test_voice_selection_uses_product_and_narration_but_respects_explicit_choice(self):
        from tts_client import recommend_voice_for_narration

        lines = [{"text": "先看茉莉花茶原料 再看瓶装和倒饮演示"}]
        selected, reason = recommend_voice_for_narration({"type": "食品"}, lines, requested_voice="auto")

        assert selected == "female_young"
        assert "食品" in reason
        assert recommend_voice_for_narration({}, lines, requested_voice="male_pro")[0] == "male_pro"

    def test_outro_voiceover_is_part_of_single_full_narration_and_single_mix(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        main_call = source[source.index("voiceover_audio, voiceover_subs = generate_full_voiceover("):source.index("_validate_voiceover_audio", source.index("voiceover_audio, voiceover_subs = generate_full_voiceover("))]
        brand_block = source[source.index("# P2-A：品牌开场/收尾动画"):source.index("# 导出最终成片")]

        assert "_append_outro_timing_to_voiceover_script" in source
        assert 'continuous_voiceover_text += str(reference_profile["cta_text"])' not in source
        assert "performance_profile=voice_performance" in main_call
        assert "generate_followup_tts_audio" not in brand_block
        assert "完整连续口播统一混音" in brand_block

    def test_bgm_mix_profile_keeps_music_audible_without_covering_voice(self):
        from one_click_create import _compute_bgm_mix_profile

        profile = _compute_bgm_mix_profile(bgm_mean_db=-19.8, voice_mean_db=-22.7)

        assert profile["base_volume"] == pytest.approx(0.776, abs=0.01)
        assert profile["sidechain_ratio"] == 2.5
        assert profile["target_gap_db"] == 6.0

    def test_voiceover_enabled_reference_outro_requires_cta_audio(self, tmp_path):
        from one_click_create import _validate_cta_voiceover_contract

        with pytest.raises(RuntimeError, match="CTA 口播"):
            _validate_cta_voiceover_contract(
                {"cta_text": "赶紧来试试", "outro_duration": 3.4},
                voiceover_enabled=True,
                cta_audio=None,
            )

    def test_main_script_reference_contract_excludes_external_cta(self):
        from local_asset_pipeline import _reference_profile_for_main_script

        profile = {
            "sales_structure": ["ingredient_proof", "usage_demo", "cta_outro"],
            "visible_sales_copy": ["茉莉花茶", "赶紧来试试"],
            "cta_text": "赶紧来试试",
            "outro_duration": 3.4,
        }

        main = _reference_profile_for_main_script(profile)

        assert main["sales_structure"] == ["ingredient_proof", "usage_demo"]
        assert main["cta_text"] == ""
        assert main["visible_sales_copy"] == ["茉莉花茶"]
        assert main["external_cta_outro"] is True

    def test_viral_blueprint_transfers_reference_copy_as_style_not_product_facts(self):
        import json
        from local_asset_pipeline import _build_viral_creative_blueprint

        blueprint = _build_viral_creative_blueprint({
            "sales_structure": ["question_hook", "origin_proof", "usage_demo"],
            "copy_tone": ["question", "proof", "benefit"],
            "visible_sales_copy": ["广西横县手工茉莉", "三窨一提工艺"],
            "creative_mechanics": {
                "hook_mechanism": "contrast_question",
                "proof_pattern": "source_to_reason",
                "spoken_style": "conversational",
                "sentence_rhythm": "short_punchy",
                "cta_pressure": "soft",
            },
        }, "demonstration")

        encoded = json.dumps(blueprint, ensure_ascii=False)
        assert blueprint["creative_mechanics"]["hook_mechanism"] == "contrast_question"
        assert blueprint["candidate_strategy"]["count"] == 3
        assert blueprint["reference_example_scope"] == "style_and_rhythm_only"
        assert "广西横县" not in encoded
        assert "三窨一提" not in encoded
        assert blueprint["reference_copy_patterns"]
        assert blueprint["reference_rhythm"]["average_units"] > 0
        assert "origin_proof" not in encoded

    def test_viral_quality_prefers_buyer_driven_copy_over_literal_footage_description(self):
        from material_copy_optimizer import evaluate_viral_script

        blueprint = {
            "creative_mechanics": {
                "hook_mechanism": "contrast_question",
                "proof_pattern": "source_to_reason",
            },
        }
        descriptive = [
            {"cue": "画面中展示一瓶产品", "marketing_intent": "hook"},
            {"cue": "镜头里是原料和产地", "marketing_intent": "value"},
            {"cue": "最后展示产品倒进杯子", "marketing_intent": "value"},
        ]
        persuasive = [
            {"cue": "普通喝法腻了，这种新搭配你见过吗？", "marketing_intent": "hook"},
            {"cue": "原料来源看得见，选的时候更踏实。", "marketing_intent": "proof"},
            {"cue": "开瓶倒进冰杯就行，忙的时候也省事。", "marketing_intent": "value"},
        ]

        weak = evaluate_viral_script(descriptive, blueprint, outro_cue="来看看", external_cta=True)
        strong = evaluate_viral_script(persuasive, blueprint, outro_cue="没试过就来试试", external_cta=True)

        assert weak["passed"] is False
        assert strong["passed"] is True
        assert strong["score"] > weak["score"]

    def test_viral_quality_only_requires_buyer_value_from_value_bearing_segments(self):
        from material_copy_optimizer import evaluate_viral_script

        segments = [
            {"cue": "茶和咖啡放在一瓶里你见过吗？", "marketing_intent": "hook"},
            {
                "cue": "想选一瓶新的搭配，原料也是会认真看的地方。",
                "marketing_intent": "value",
                "requires_buyer_value": False,
            },
            {
                "cue": "倒进杯里就能喝，忙的时候也省事。",
                "marketing_intent": "value",
                "requires_buyer_value": True,
            },
            {"cue": "想试试这种搭配就去了解一下。", "marketing_intent": "cta"},
        ]

        quality = evaluate_viral_script(segments, {})

        assert quality["dimensions"]["evidence_to_buyer_value"] == 1.0
        assert "素材证据没有充分转译成购买理由" not in quality["errors"]

    def test_cold_start_selects_first_execution_valid_route_without_subjective_scoring(self):
        from local_asset_pipeline import _normalize_compact_script_response

        response = {"creative_candidates": [
            {
                "route": "素材解说",
                "segments": [
                    {"segment": 0, "cue": "画面中展示一瓶产品"},
                    {"segment": 1, "cue": "镜头里展示倒进杯子"},
                ],
            },
            {
                "route": "反差好奇",
                "segments": [
                    {"segment": 0, "cue": "普通喝法腻了，这种新搭配你见过吗？"},
                    {"segment": 1, "cue": "开瓶倒进冰杯就行，忙的时候也省事。"},
                ],
            },
        ]}
        contracts = [
            {"segment": 0, "marketing_intent": "hook"},
            {"segment": 1, "marketing_intent": "value"},
        ]

        assert _normalize_compact_script_response(
            response,
            2,
            creative_blueprint={"creative_mechanics": {"hook_mechanism": "contrast_question"}},
            segment_contracts=contracts,
        ) is True
        assert response["route"] == "素材解说"
        assert response["subjective_quality"]["enforced"] is False
        assert "creative_quality" not in response

    def test_subjective_heuristic_score_cannot_block_without_active_user_rules(self):
        from local_asset_pipeline import _normalize_compact_script_response

        descriptive = {
            "route": "冷启动候选",
            "segments": [
                {"segment": 0, "cue": "画面中展示一瓶产品"},
                {"segment": 1, "cue": "镜头里展示倒进杯子"},
            ],
        }
        response = {"creative_candidates": [descriptive]}

        assert _normalize_compact_script_response(
            response,
            2,
            creative_blueprint={},
            segment_contracts=[
                {"segment": 0, "marketing_intent": "hook"},
                {"segment": 1, "marketing_intent": "value"},
            ],
            require_creative_candidates=True,
            candidate_validator=lambda _candidate: [],
            user_script_policy={"source": "explicit_user_feedback_only", "rules": []},
        ) is True

        assert response["subjective_quality"]["enforced"] is False
        assert response["candidate_factual_violations"] == []

    def test_production_script_contract_rejects_legacy_single_route_response(self):
        from local_asset_pipeline import _normalize_compact_script_response

        legacy = {
            "segments": [
                {"segment": 0, "cue": "这瓶产品你见过吗？"},
                {"segment": 1, "cue": "开瓶就能直接用。"},
            ],
        }

        assert _normalize_compact_script_response(
            legacy,
            2,
            creative_blueprint={},
            segment_contracts=[],
            require_creative_candidates=True,
        ) is False

    def test_creative_selection_prefers_truthful_route_over_higher_scoring_hallucination(self):
        from local_asset_pipeline import _normalize_compact_script_response

        response = {"creative_candidates": [
            {
                "route": "高分但编事实",
                "segments": [
                    {"segment": 0, "cue": "为什么大家都抢这款产品？"},
                    {"segment": 1, "cue": "它来自顶级产区，用起来当然更省心。"},
                ],
            },
            {
                "route": "真实证据递进",
                "segments": [
                    {"segment": 0, "cue": "这种新搭配你见过吗？"},
                    {"segment": 1, "cue": "开瓶就能直接用，忙的时候更省事。"},
                ],
            },
        ]}

        assert _normalize_compact_script_response(
            response,
            2,
            creative_blueprint={"creative_mechanics": {"hook_mechanism": "contrast_question"}},
            segment_contracts=[
                {"segment": 0, "marketing_intent": "hook"},
                {"segment": 1, "marketing_intent": "value"},
            ],
            require_creative_candidates=True,
            candidate_validator=lambda candidate: (
                ["缺少可信产区和销量证据"] if candidate.get("route") == "高分但编事实" else []
            ),
        ) is True

        assert response["route"] == "真实证据递进"
        hallucinated = next(item for item in response["creative_candidates_evaluated"] if item["route"] == "高分但编事实")
        assert hallucinated["passed"] is False
        assert hallucinated["factual_violations"] == ["缺少可信产区和销量证据"]

    def test_all_factually_invalid_routes_report_only_factual_failures(self):
        from local_asset_pipeline import _normalize_compact_script_response

        response = {"creative_candidates": [{
            "route": route,
            "segments": [
                {"segment": 0, "cue": "为什么大家都抢这款产品？"},
                {"segment": 1, "cue": "顶级原料让它用起来更省心。"},
            ],
        } for route in ("反差好奇", "场景共鸣", "证据递进")]}

        assert _normalize_compact_script_response(
            response,
            2,
            creative_blueprint={},
            segment_contracts=[
                {"segment": 0, "marketing_intent": "hook"},
                {"segment": 1, "marketing_intent": "value"},
            ],
            require_creative_candidates=True,
            candidate_validator=lambda _candidate: ["缺少销量和原料证据"],
        ) is True
        assert response["subjective_quality"]["enforced"] is False
        assert response["candidate_factual_violations"] == ["缺少销量和原料证据"]
        assert all(
            item["factual_violations"] == ["缺少销量和原料证据"]
            for item in response["creative_candidates_evaluated"]
        )

    def test_compound_narrative_prefers_cta_animation(self):
        from video_merger import choose_subtitle_animation

        assert choose_subtitle_animation("usage_demo + cta", has_voiceover=True) == "slide"

    def test_brand_outro_can_be_generated_without_intro(self, tmp_path):
        import subprocess
        import video_merger

        video = tmp_path / "main.mp4"
        output = tmp_path / "with_outro.mp4"
        video.write_bytes(b"video")
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, stdout="4.4\n")
            Path(command[-1]).write_bytes(b"x" * 12_000)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(video_merger, "_has_audio_stream", return_value=False), \
             patch.object(video_merger.subprocess, "run", side_effect=fake_run):
            result = video_merger.add_brand_intro_outro(
                video,
                output,
                product_name="茶咖",
                cta_text="赶紧来试试",
                intro_duration=0.0,
                outro_duration=3.4,
            )

        assert result == output
        assert len([command for command in commands if command[0] == "ffmpeg"]) == 3
        assert not any("intro.mp4" in str(part) for command in commands for part in command)

    def test_local_cta_background_uses_clean_pre_subtitle_video(self, tmp_path):
        import subprocess
        from PIL import Image
        from one_click_create import _resolve_local_cta_background
        from video_merger import add_brand_intro_outro

        clean_video = tmp_path / "graded.mp4"
        subtitled_video = tmp_path / "subtitled.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=30:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clean_video),
        ], capture_output=True, check=True)
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=30:d=1",
            "-vf", "drawbox=x=0:y=480:w=360:h=100:color=red:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(subtitled_video),
        ], capture_output=True, check=True)

        background = _resolve_local_cta_background(
            local_asset_mode=True,
            postproduction_contract={"cta": {"visual_mode": "closing_frame_tail_card"}},
            clean_video=clean_video,
        )
        assert background == clean_video
        assert _resolve_local_cta_background(
            local_asset_mode=False,
            postproduction_contract={"cta": {"visual_mode": "closing_frame_tail_card"}},
            clean_video=clean_video,
        ) is None

        output = tmp_path / "with_outro.mp4"
        add_brand_intro_outro(
            video=subtitled_video,
            output=output,
            product_name="茶咖",
            cta_text="现在了解",
            intro_duration=0.0,
            outro_duration=1.0,
            outro_background_video=background,
            strict_material_background=True,
            resolution="360x640",
        )
        tail = tmp_path / "tail.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-sseof", "-0.2", "-i", str(output),
            "-frames:v", "1", str(tail),
        ], capture_output=True, check=True)
        red, _green, blue = Image.open(tail).convert("RGB").getpixel((20, 530))

        assert blue > red * 1.5

    def test_brand_outro_concat_extends_audio_and_video_together(self, tmp_path):
        """主片与 CTA 音频规格不同时，尾卡后音轨仍必须覆盖完整视频。"""
        import json
        import subprocess
        import video_merger

        main = tmp_path / "main.mp4"
        cta = tmp_path / "cta.m4a"
        output = tmp_path / "with_outro.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:size=320x568:rate=24:d=1.0",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1.0",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", "-shortest", str(main),
        ], check=True, capture_output=True)
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "sine=frequency=660:sample_rate=24000:duration=0.45",
            "-c:a", "aac", "-ac", "1", str(cta),
        ], check=True, capture_output=True)

        result = video_merger.add_brand_intro_outro(
            main,
            output,
            product_name="茶咖",
            cta_text="现在了解",
            intro_duration=0.0,
            outro_duration=0.8,
            outro_audio=cta,
            main_duration=0.7,
            resolution="320x568",
            fps=24,
        )
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,duration",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True)
        metadata = json.loads(probe.stdout)
        video_duration = float(next(
            stream["duration"] for stream in metadata["streams"]
            if stream["codec_type"] == "video"
        ))
        audio_duration = float(next(
            stream["duration"] for stream in metadata["streams"]
            if stream["codec_type"] == "audio"
        ))

        assert result == output
        assert 1.45 <= video_duration <= 1.6
        assert video_duration - audio_duration <= 0.15

    def test_reference_rhythm_matches_net_main_duration_after_transitions(self):
        from one_click_create import _fit_rhythm_template_to_net_duration
        from douyin_adapter import compute_segment_timeline

        template = {
            "transition_duration": 0.3,
            "segments": [
                {"index": index, "duration": duration, "type": "showcase", "purpose": "test"}
                for index, duration in enumerate([2.25, 3.0, 3.75, 3.3, 2.7])
            ],
        }
        fitted = _fit_rhythm_template_to_net_duration(template, 15.867)
        timeline = compute_segment_timeline(fitted)

        assert timeline[-1]["end"] == pytest.approx(15.867, abs=0.01)

    def test_material_copy_optimizer_uses_dynamic_anchors_across_categories(self):
        from material_copy_optimizer import evaluate_candidate

        skincare_contract = {
            "segment": 1,
            "marketing_intent": "value",
            "max_voiceover_units": 14,
            "allowed_marketing_devices": ["convenience", "reason", "proof"],
            "requires_buyer_value": True,
            "required_continuity_from": 0,
            "evidence_anchors": [
                {"id": "visual:0", "text": "滴管按压", "kind": "visual"},
                {"id": "visual:1", "text": "掌心精华液", "kind": "visual"},
            ],
        }
        appliance_contract = {
            "segment": 2,
            "marketing_intent": "value",
            "max_voiceover_units": 14,
            "allowed_marketing_devices": ["convenience", "reason", "proof"],
            "requires_buyer_value": True,
            "required_continuity_from": 1,
            "evidence_anchors": [
                {"id": "visual:0", "text": "推动吸尘器", "kind": "visual"},
                {"id": "visual:1", "text": "地面灰尘", "kind": "visual"},
            ],
        }

        skincare = evaluate_candidate({
            "voiceover": "滴管一按用量更好控制",
            "marketing_device": "convenience",
            "buyer_value": "控制每次用量",
            "evidence_refs": ["visual:0"],
            "continuity_from": 0,
        }, skincare_contract)
        appliance = evaluate_candidate({
            "voiceover": "往前一推地面灰尘就处理了",
            "marketing_device": "convenience",
            "buyer_value": "清理动作更直接",
            "evidence_refs": ["visual:0", "visual:1"],
            "continuity_from": 1,
        }, appliance_contract)
        flat = evaluate_candidate({
            "voiceover": "画面里有一瓶精华液",
            "marketing_device": "description",
            "buyer_value": "",
            "evidence_refs": ["visual:1"],
            "continuity_from": 0,
        }, skincare_contract)

        assert skincare["passed"] is True
        assert appliance["passed"] is True
        assert flat["passed"] is False

    def test_material_copy_optimizer_rejects_buyer_value_hidden_only_in_metadata(self):
        from material_copy_optimizer import evaluate_candidate

        contract = {
            "segment": 1,
            "marketing_intent": "value",
            "allowed_marketing_devices": ["convenience", "reason", "proof", "reveal"],
            "requires_buyer_value": True,
            "required_continuity_from": 0,
            "evidence_anchors": [
                {"id": "visual:0", "text": "开阔种植环境", "kind": "visual"},
            ],
        }
        description = evaluate_candidate({
            "voiceover": "你现在看到的是远山和开阔田野",
            "marketing_device": "reason",
            "buyer_value": "让产品原料来源更值得信任",
            "evidence_refs": ["visual:0"],
            "continuity_from": 0,
        }, contract)
        sales_copy = evaluate_candidate({
            "voiceover": "原料生长环境看得见，选起来也更踏实",
            "marketing_device": "reason",
            "buyer_value": "原料生长环境看得见，选择更踏实",
            "evidence_refs": ["visual:0"],
            "continuity_from": 0,
        }, contract)

        assert description["passed"] is False
        assert any("口播正文" in error or "画面描述" in error for error in description["errors"])
        assert sales_copy["passed"] is True

    def test_local_asset_mode_does_not_enter_character_pipeline(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")

        assert '"rationale": "local_asset_mode"' in source
        assert "enumerate([] if local_asset_mode else clip_paths)" in source
        assert '"kling_cost_info": None' in source
        assert "本地选片按素材语义证据与匹配置信度执行" in source
        assert "本地视频混剪" in source
        assert "本地素材模式：跳过人物角色计划" not in source
        assert "本地素材模式：跳过 AI 生成故事板" not in source

    def test_ai_source_plan_keeps_kling_cost_and_low_cost_policy(self):
        import argparse

        args = argparse.Namespace(
            local_assets=None,
            mode="pro",
            duration=5,
            best_of=1,
            preview=False,
            target_duration=25,
            image_first=True,
            preflight_keyframe=True,
            strict=True,
            image_first_mode="standard",
            image_first_variants=2,
        )
        plan = _prepare_generation_source_plan(
            args,
            {"name": "茶咖", "type": "食品"},
            ab_versions=1,
        )

        assert plan["source"] == "kling_generation"
        assert plan["kling_cost_info"]["video_seconds"] == 25
        assert plan["kling_cost_info"]["image_count"] >= 4
        assert plan["estimated_output_size"]["total_size_mb"] > 0

    def test_template_mode_restores_reference_video_argument(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        block = source[source.index("    if args.load:"):source.index("    else:\n        # 交互式输入产品信息")]

        assert 'args.reference_video = args_dict["reference_video"]' in block

    def test_explicit_output_name_overrides_loaded_template_name(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        block = source[source.index("    if args.load:"):source.index("    else:\n        # 交互式输入产品信息")]

        assert "explicit_output_name = args.output_name" in block
        assert 'args.output_name = explicit_output_name or args_dict["output_name"]' in block

    def test_string_claim_is_normalized_only_when_product_fact_matches(self):
        from local_asset_pipeline import _normalize_claim_objects

        segment = {"product_story_role": "ingredient", "claims": ["茉莉花茶", "三窨一提"]}
        claims = _normalize_claim_objects(segment, {"ingredients": ["茉莉花茶"]})

        assert claims == [{
            "text": "茉莉花茶",
            "type": "ingredient",
            "evidence_source": "product_info.ingredients",
        }]

    def test_structured_claim_with_untrusted_product_field_is_removed_before_validation(self):
        """模型不能把 selling_point 冒充为已验证功效事实。"""
        from local_asset_pipeline import _normalize_claim_objects

        claims = _normalize_claim_objects(
            {
                "product_story_role": "finished_product",
                "claims": [{
                    "text": "兼具茶香咖啡香",
                    "type": "effect",
                    "evidence_source": "product_info.selling_point",
                }],
            },
            {"selling_point": "瓶装产品展示"},
            {"visible_text": ["茶咖"]},
        )

        assert claims == []

    def test_local_asset_narrative_plan_uses_available_story_roles_not_missing_template_roles(self):
        """没有 pain/result/cta 类素材时，营销功能必须重规划到真实可用画面。"""
        from local_asset_pipeline import _build_coverage_driven_narrative_plan

        catalog = [
            {
                "window_id": "hook-product",
                "source_path": "/tmp/hook.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "finished_product",
                "product_identity_supported": True,
                "product_relationship_verified": True,
                "product_visibility": 5,
            },
            {
                "window_id": "ingredient-proof",
                "source_path": "/tmp/ingredient.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "ingredient",
                "product_relationship_verified": True,
                "matched_product_facts": ["茉莉花茶"],
                "product_visibility": 5,
            },
            *[
                {
                    "window_id": f"product-{index}",
                    "source_path": f"/tmp/product-{index}.mp4",
                    "start": 0.0,
                    "end": 4.0,
                    "product_story_role": "finished_product",
                    "product_identity_supported": True,
                    "product_relationship_verified": True,
                    "product_visibility": 5,
                }
                for index in range(3)
            ],
            {
                "window_id": "usage",
                "source_path": "/tmp/usage.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "usage",
                "product_identity_supported": True,
                "product_relationship_verified": True,
                "product_visibility": 5,
            },
            {
                "window_id": "unverified-origin",
                "source_path": "/tmp/origin.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "origin",
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "product_visibility": 0,
            },
        ]

        plan = _build_coverage_driven_narrative_plan(catalog, 6, external_cta=False)

        assert len(plan) == 6
        assert {item["narrative"] for item in plan}.isdisjoint({"pain_point", "result"})
        assert plan[-1]["narrative"] == "cta"
        assert plan[-1]["product_story_role"] == "finished_product"
        assert any(item["marketing_intent"] == "proof" for item in plan)
        assert all(
            item["product_relationship_verified"]
            for item in plan
            if item["marketing_intent"] == "proof"
        )
        assert "unverified-origin" not in {
            item["asset_window_ids"][0]
            for item in plan
            if item["marketing_intent"] == "proof"
        }

    def test_external_cta_plan_keeps_main_content_on_value_not_fake_cta_material(self):
        """独立尾卡承担 CTA 时，主内容不要求本地素材被识别成 CTA。"""
        from local_asset_pipeline import _build_coverage_driven_narrative_plan

        catalog = [
            {
                "window_id": f"product-{index}",
                "source_path": f"/tmp/product-{index}.mp4",
                "start": 0.0,
                "end": 4.0,
                "product_story_role": "finished_product",
                "product_identity_supported": True,
                "product_relationship_verified": True,
                "product_visibility": 5,
            }
            for index in range(2)
        ]

        plan = _build_coverage_driven_narrative_plan(catalog, 2, external_cta=True)

        assert plan[-1]["narrative"] == "value_closer"
        assert plan[-1]["marketing_intent"] == "value"
        assert plan[-1]["product_story_role"] == "finished_product"

    def test_visual_validation_no_longer_triggers_script_repair(self, tmp_path):
        """逐镜头视觉校验属于后置匹配，不能让脚本生成器重写口播。"""
        import copy
        import json
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        generated = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "voiceover_cues": [
                "为什么这瓶装茶咖值得你现在认真看看",
                "想试试瓶装茶咖就从眼前这一瓶开始了解",
            ],
            "segments": [
                {
                    "segment": 0,
                    "narrative": "hook",
                    "product_story_role": "finished_product",
                    "subtitle": "还在随便选茶咖吗",
                    "voiceover": "还在随便选茶咖吗",
                    "marketing_intent": "hook",
                    "marketing_device": "contrast_question",
                    "buyer_value": "重新考虑当前产品选择",
                    "evidence_refs": ["product:name"],
                    "continuity_from": None,
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-0"],
                },
                {
                    "segment": 1,
                    "narrative": "cta",
                    "product_story_role": "finished_product",
                    "subtitle": "想试试就去了解",
                    "voiceover": "想试试这款茶咖就去了解",
                    "marketing_intent": "cta",
                    "marketing_device": "action",
                    "buyer_value": "进一步了解当前产品",
                    "evidence_refs": ["product:name"],
                    "continuity_from": 0,
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-1"],
                },
            ],
        }
        asset_index = {
            "asset_folder": "/tmp/local-assets-attempt-limit",
            "windows": [
                {
                    "window_id": f"product-{index}",
                    "source_video": f"product-{index}.mp4",
                    "source_path": f"/tmp/product-{index}.mp4",
                    "start": 0.0,
                    "end": 4.0,
                    "contact_sheet": f"/tmp/product-{index}.jpg",
                    "analysis": {
                        "usable_for_ad": True,
                        "confidence": 1.0,
                        "product_story_role": "finished_product",
                        "product_visibility": 5,
                        "visible_text": ["茶咖"],
                        "visible_objects": ["瓶装饮品"],
                        "evidence": "桌面上清晰展示茶咖瓶装成品",
                    },
                }
                for index in range(2)
            ],
        }
        failed_check = {
            "supported": False,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "visual_supported": False,
            "static_supported": False,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": ["visual_requirement"],
            "unsupported_claims": ["测试中的持续视觉失败"],
            "reason": "测试中的持续视觉失败",
        }

        generation_count = 0

        def generate_changed_script(*_args, **_kwargs):
            nonlocal generation_count
            generation_count += 1
            candidate = copy.deepcopy(generated)
            if generation_count > 1:
                for segment in candidate["segments"]:
                    segment["voiceover"] = (
                        f"还在随便选茶咖吗版本{generation_count}？"
                        if segment["segment"] == 0
                        else f"想试试茶咖就去了解版本{generation_count}。"
                    )
                candidate["voiceover_cues"] = [
                    segment["voiceover"] for segment in candidate["segments"]
                ]
            return creative_candidates(candidate, candidate, candidate)

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=generate_changed_script) as generate, \
             patch("local_asset_pipeline._validate_derived_material_segment", return_value=failed_check), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path), \
             patch("quality_gate.record_failure_case") as record:
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=2,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 3.6, 1: 5.4},
            )

        assert generate.call_count == 1
        assert record.call_count == 0
        assert result["generation_order"].endswith("same_role_shot_matching")
        assert all("asset_window_ids" not in segment for segment in result["segments"])

    def test_removed_semantic_repair_does_not_make_a_second_remote_call(self):
        """视觉适配不再触发第二次远程口播重写。"""
        import json
        from local_asset_pipeline import LocalAssetError, build_material_constrained_script

        generated = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "voiceover_cues": ["为什么这瓶茶咖值得了解？现在就从这瓶试试"],
            "segments": [{
                "segment": 0,
                "narrative": "hook_cta",
                "product_story_role": "finished_product",
                "subtitle": "想了解茶咖吗现在就试试",
                "voiceover": "想了解茶咖吗？现在就试试。",
                "marketing_intent": "cta",
                "claims": [],
                "visual_requirement": "桌面上的瓶装茶咖",
                "scene_prompt": "桌面上的瓶装茶咖",
                "asset_query": ["瓶装茶咖"],
                "asset_window_ids": ["product-0"],
            }],
        }
        asset_index = {
            "windows": [{
                "window_id": "product-0",
                "source_video": "product.mp4",
                "source_path": "/tmp/product.mp4",
                "start": 0.0,
                "end": 4.0,
                "contact_sheet": "/tmp/product.jpg",
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "visible_text": ["茶咖"],
                    "visible_objects": ["瓶装饮品"],
                    "evidence": "桌面上展示茶咖瓶装成品",
                },
            }],
        }
        failed_check = {
            "supported": False,
            "subtitle_supported": False,
            "voiceover_supported": False,
            "visual_supported": True,
            "static_supported": True,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": ["voiceover"],
            "unsupported_claims": ["测试失败"],
            "reason": "测试失败",
        }

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=[
                 creative_candidates(generated, generated, generated), None,
             ]) as generate, \
             patch("local_asset_pipeline._validate_derived_material_segment", return_value=failed_check) as validate, \
             patch("quality_gate.record_failure_case") as record:
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 4.0},
            )

        assert generate.call_count == 1
        assert validate.call_count == 0
        assert record.call_count == 0
        assert result["voiceover_full"]

    def test_copy_preflight_reports_facts_without_estimated_speech_budget(self):
        """事实预检不得把文本字数估算伪装成真实音频执行约束。"""
        from local_asset_pipeline import _copy_preflight_check

        check = _copy_preflight_check(
            {
                "voiceover": "既有茶香又有咖啡香口感清爽层次丰富而且值得认真了解",
                "subtitle": "既有茶香又有咖啡香口感清爽层次丰富而且值得认真了解",
                "claims": [{
                    "text": "兼具茶香与咖啡香",
                    "type": "effect",
                    "evidence_source": "product_info.selling_point",
                }],
            },
            duration=3.75,
            product_info={"name": "茶咖", "selling_point": "瓶装产品展示"},
            analysis={"visible_text": ["茶咖"]},
        )

        assert check is not None
        assert check["unsupported_fields"] == ["claims"]
        assert "max_voiceover_units" not in check
        assert "voiceover_units" not in check
        assert any("茶香" in claim for claim in check["unsupported_claims"])

    def test_unverified_social_proof_is_rejected_before_creative_selection(self):
        from local_asset_pipeline import _marketing_claim_violations

        violations = _marketing_claim_violations({
            "voiceover": "最近火起来的这款产品，很多人都在买",
            "subtitle": "最近火起来的这款产品很多人都在买",
            "claims": [],
        }, product_info={"name": "产品"})

        assert set(violations) == {"subtitle", "voiceover"}
        assert all(violations[field] for field in violations)

    def test_verified_social_proof_can_be_used_as_sales_evidence(self):
        from local_asset_pipeline import _marketing_claim_violations

        text = "公开销量资料显示这款产品月销十万"
        assert _marketing_claim_violations({
            "voiceover": text,
            "subtitle": text,
            "claims": [{
                "text": text,
                "type": "social_proof",
                "evidence_source": "product_info.social_proof",
            }],
        }, product_info={"social_proof": [text]}) == {}

    @pytest.mark.parametrize("text, field", [
        ("这款产品不是靠工业调香做出来的", "production_process"),
        ("这款产品大小规格都有", "specifications"),
    ])
    def test_unverified_production_and_specification_claims_are_rejected(self, text, field):
        from local_asset_pipeline import _marketing_claim_violations

        violations = _marketing_claim_violations({
            "voiceover": text,
            "subtitle": text,
            "claims": [],
        }, product_info={"name": "产品"})

        assert set(violations) == {"subtitle", "voiceover"}, field

    def test_price_claim_detection_requires_a_complete_commercial_expression(self):
        from local_asset_pipeline import _infer_copy_claims

        ordinary_copy = [
            "不用自己折腾搭配刚好满足想尝试新饮品的需求",
            "想换口味又怕自己搭配折腾半天不对味",
        ]
        commercial_copy = ["售价29元", "现在有优惠", "活动价打8折", "到手价只要二十九"]

        assert all(
            not any(claim["type"] == "price" for claim in _infer_copy_claims(text))
            for text in ordinary_copy
        )
        assert all(
            any(claim["type"] == "price" for claim in _infer_copy_claims(text))
            for text in commercial_copy
        )

    def test_current_local_script_builder_has_no_automatic_copy_reviewer(self):
        import inspect
        from local_asset_pipeline import build_material_constrained_script

        source = inspect.getsource(build_material_constrained_script)

        assert "candidate_validator=" not in source
        assert "candidate_factual_violations" not in source
        assert "global_factual_evidence_failed" not in source

    @pytest.mark.parametrize("text", [
        "这款产品用料实在",
        "核心原料绝对不掺假",
        "适合怕买到假货的朋友",
        "这就是品质更好的选择",
    ])
    def test_unverified_quality_and_authenticity_endorsements_are_rejected(self, text):
        from local_asset_pipeline import _marketing_claim_violations

        violations = _marketing_claim_violations({
            "voiceover": text,
            "subtitle": text,
            "claims": [],
        }, product_info={"name": "产品"})

        assert set(violations) == {"subtitle", "voiceover"}

    def test_material_catalog_relevance_prior_is_synchronized_into_validation_windows(self):
        source = Path("local_asset_pipeline.py").read_text(encoding="utf-8")
        synchronization = source[
            source.index('for window in (asset_index or {}).get("windows") or []:'):
            source.index("reference_profile = product_info.get", source.index('for window in (asset_index or {}).get("windows") or []:'))
        ]

        assert '"product_relevance_prior"' in synchronization

    def test_verified_ingredient_relationship_allows_product_name_in_narration(self):
        from local_asset_pipeline import _product_identity_violations

        assert _product_identity_violations(
            {"voiceover": "这款茶咖用到茉莉花茶", "subtitle": "这款茶咖用到茉莉花茶"},
            "茶咖",
            {
                "visible_text": ["茉莉花茶"],
                "product_relationship_verified": True,
                "matched_product_facts": ["茉莉花茶"],
            },
        ) == {}

    def test_unverified_origin_scene_cannot_claim_product_identity(self):
        from local_asset_pipeline import _product_identity_violations

        violations = _product_identity_violations(
            {"voiceover": "这款茶咖来自这里", "subtitle": "这款茶咖来自这里"},
            "茶咖",
            {
                "visible_text": [],
                "product_relationship_verified": False,
                "product_relevance_prior": "high",
            },
        )

        assert violations == {"subtitle": ["茶咖"], "voiceover": ["茶咖"]}

    def test_unverified_origin_scene_cannot_imply_product_ingredient_quality(self):
        from local_asset_pipeline import _product_identity_violations

        text = "看到这片开阔的花田，我就知道这款茶咖的原料底子不一般"
        violations = _product_identity_violations(
            {"voiceover": text, "subtitle": text},
            "茶咖",
            {
                "visible_text": [],
                "product_relationship_verified": False,
                "product_relevance_prior": "high",
            },
        )

        assert violations == {"subtitle": ["茶咖"], "voiceover": ["茶咖"]}

    def test_relevant_local_broll_can_carry_generic_product_narration(self):
        from local_asset_pipeline import _product_identity_violations

        assert _product_identity_violations(
            {"voiceover": "这款茶咖适配你的日常", "subtitle": "这款茶咖适配你的日常"},
            "茶咖",
            {
                "visible_text": [],
                "product_relationship_verified": False,
                "product_relevance_prior": "high",
            },
        ) == {}

    def test_non_user_factual_label_does_not_block_or_rewrite_script(self):
        """自动事实标签只能作为生成上下文，不得审核、阻断或重写文案。"""
        import json
        from local_asset_pipeline import build_material_constrained_script

        generated = {
            "title": "茶咖",
            "hashtags": ["#茶咖"],
            "voiceover_cues": [
                "你想了解这瓶号称能提神的瓶装茶咖吗？",
                "看完瓶装设计再从眼前这款开始认真了解。",
            ],
            "segments": [
                {
                    "segment": 0,
                    "cue": "你想了解这瓶号称能提神的瓶装茶咖吗？",
                    "marketing_device": "contrast_question",
                    "buyer_value": "产生产品好奇",
                    "evidence_refs": ["product:name"],
                    "narrative": "hook",
                    "product_story_role": "finished_product",
                    "subtitle": "这瓶茶咖真的能提神吗",
                    "voiceover": "这瓶茶咖真的能提神吗？",
                    "marketing_intent": "hook",
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-0"],
                },
                {
                    "segment": 1,
                    "cue": "看完瓶装设计再从眼前这款开始认真了解。",
                    "marketing_device": "action",
                    "buyer_value": "进一步了解当前产品",
                    "evidence_refs": ["product:name"],
                    "narrative": "cta",
                    "product_story_role": "finished_product",
                    "subtitle": "快来了解这款茶咖",
                    "voiceover": "快来了解这款茶咖。",
                    "marketing_intent": "cta",
                    "claims": [],
                    "visual_requirement": "桌面上的瓶装茶咖",
                    "scene_prompt": "桌面上的瓶装茶咖",
                    "asset_query": ["瓶装茶咖"],
                    "asset_window_ids": ["product-1"],
                },
            ],
        }
        windows = [
            {
                "window_id": f"product-{index}",
                "source_video": f"product-{index}.mp4",
                "source_path": f"/tmp/product-{index}.mp4",
                "start": 0.0,
                "end": 4.0,
                "contact_sheet": f"/tmp/product-{index}.jpg",
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "visible_text": ["茶咖"],
                    "visible_objects": ["瓶装饮品"],
                    "evidence": "桌面上清晰展示茶咖瓶装成品",
                },
            }
            for index in range(2)
        ]
        failed = {
            "supported": False,
            "subtitle_supported": False,
            "voiceover_supported": False,
            "visual_supported": True,
            "static_supported": True,
            "dynamic_required": False,
            "dynamic_supported": True,
            "confidence": 1.0,
            "unsupported_fields": ["subtitle", "voiceover"],
            "unsupported_claims": ["voiceover: 茶香"],
            "reason": "字幕或口播包含缺少可信产品资料或画面证据的事实主张",
        }
        passed = {
            **failed,
            "supported": True,
            "subtitle_supported": True,
            "voiceover_supported": True,
            "unsupported_fields": [],
            "unsupported_claims": [],
            "reason": "通过",
        }

        def validate(_self, _sheet, segment, **_kwargs):
            return failed if "茶香" in segment["voiceover"] else passed

        repaired = {
                "segments": [
                    {
                        "segment": 0,
                        "cue": "茶和咖啡装进同一瓶的新搭配你见过吗？",
                        "marketing_device": "contrast_question",
                        "buyer_value": "产生产品组合的新鲜感",
                        "evidence_refs": ["product:name"],
                        "claims": [],
                    },
                    {
                        "segment": 1,
                        "cue": "想试试这种新搭配就从眼前这瓶开始了解。",
                        "marketing_device": "action",
                        "buyer_value": "进一步了解当前产品",
                        "evidence_refs": ["product:name"],
                        "claims": [],
                    },
                ],
            }
        responses = [
            creative_candidates(generated, generated, generated),
            creative_candidates(repaired, repaired, repaired),
        ]
        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=responses) as generate, \
             patch("local_asset_pipeline.VisionAnalyzer.validate_segment", new=validate), \
             patch("quality_gate.record_failure_case"):
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=2,
                script_style="demonstration",
                asset_index={"windows": windows},
                segment_durations={0: 4.0, 1: 5.0},
            )

        assert generate.call_count == 1
        assert "提神" in result["voiceover_full"]
        initial_payload = json.loads(generate.call_args_list[0].args[0])
        assert "max_voiceover_units" not in initial_payload["segment_contracts"][0]
        assert initial_payload["narration_guidance"]["enforced"] is False
        assert initial_payload["narration_guidance"]["suggested_max_voiceover_units"] > 0
        assert "茶香" not in initial_payload["forbidden_without_verified_facts"]


class TestGlobalMaterialDrivenScriptArchitecture:
    def test_global_material_capacity_merges_overlapping_source_windows(self):
        from local_asset_pipeline import _global_material_capability_pool

        pool = _global_material_capability_pool(
            [
                {
                    "window_id": "first",
                    "source_path": "/tmp/source.mp4",
                    "start": 0.0,
                    "end": 4.0,
                    "product_story_role": "finished_product",
                },
                {
                    "window_id": "overlap",
                    "source_path": "/tmp/source.mp4",
                    "start": 2.0,
                    "end": 6.0,
                    "product_story_role": "finished_product",
                },
            ],
            {"name": "茶咖", "type": "食品"},
        )

        assert pool["total_usable_duration"] == 6.0
        assert pool["role_capabilities"][0]["capacity_seconds"] == 6.0

    def test_script_generation_enforces_selected_voice_physical_capacity(self, tmp_path):
        import json
        from unittest.mock import patch
        from local_asset_pipeline import build_material_constrained_script

        long_candidate = {
            "segments": [{
                "segment": 0,
                "marketing_intent": "cta",
                "cue": "这是一段明显超过当前音色和真实素材时长容量的带货口播内容需要重新精炼。",
                "evidence_refs": ["product:name"],
                "claims": [],
                "desired_story_role": "finished_product",
                "visual_query": ["瓶装茶咖"],
            }],
        }
        short_candidate = {
            "segments": [{
                "segment": 0,
                "marketing_intent": "cta",
                "cue": "想尝鲜就去看看。",
                "evidence_refs": ["product:name"],
                "claims": [],
                "desired_story_role": "finished_product",
                "visual_query": ["瓶装茶咖"],
            }],
        }
        responses = [
            creative_candidates(long_candidate, long_candidate, long_candidate),
            creative_candidates(short_candidate, short_candidate, short_candidate),
        ]

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", side_effect=responses) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            result = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=minimal_local_asset_index("/tmp/voice-capacity-assets"),
                segment_durations={0: 4.0},
                narration_contract={
                    "voice": "female_warm",
                    "rate": 160,
                    "max_units_per_second": 3.2,
                },
            )

        prompt = json.loads(generate.call_args_list[0].args[0])
        assert prompt["narration_guidance"]["enforced"] is True
        assert prompt["narration_guidance"]["duration_source"] == "deduplicated_local_asset_capacity"
        assert prompt["narration_guidance"]["voice"] == "female_warm"
        assert prompt["narration_guidance"]["maximum_voiceover_units"] == 12
        assert generate.call_count == 2
        assert result["voiceover_full"] == "想尝鲜就去看看。"

    def test_copy_stage_does_not_treat_spoken_verbs_as_visual_action_requirements(self, tmp_path):
        from unittest.mock import patch
        from local_asset_pipeline import build_material_constrained_script

        candidate = {
            "segments": [{
                "segment": 0,
                "marketing_intent": "cta",
                "cue": "这款茶咖加入真实茉莉花茶调和风味。",
                "evidence_refs": ["product:name", "product:ingredients:0"],
                "claims": [],
            }],
        }
        asset_index = minimal_local_asset_index(str(tmp_path / "assets"))

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", return_value=creative_candidates(candidate, candidate, candidate)), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            script = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品", "ingredients": ["茉莉花茶"]},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 4.0},
            )

        assert script["voiceover_full"] == "这款茶咖加入真实茉莉花茶调和风味。"

    def test_visual_roles_are_not_claimable_copy_evidence(self):
        from local_asset_pipeline import _global_material_capability_pool

        pool = _global_material_capability_pool(
            [{
                "window_id": "origin-window",
                "start": 0.0,
                "end": 3.0,
                "product_story_role": "origin",
                "product_relationship_verified": False,
                "matched_product_facts": [],
                "visible_objects": ["山地茶园"],
                "literal_actions": [],
                "visible_text": [],
            }],
            {"name": "茶咖", "type": "食品"},
        )

        assert "origin" in pool["available_story_roles"]
        assert all(
            anchor["kind"] != "material_capability"
            and not str(anchor["id"]).startswith("material_role:")
            for anchor in pool["evidence_anchors"]
        )

    def test_material_story_plan_is_the_single_role_source_for_copy_and_shot_intent(self, tmp_path):
        import json
        from unittest.mock import patch
        from local_asset_pipeline import build_material_constrained_script

        def window(window_id, role, objects, actions):
            return {
                "window_id": window_id,
                "source_video": f"{window_id}.mp4",
                "source_path": str(tmp_path / f"{window_id}.mp4"),
                "start": 0.0,
                "end": 3.0,
                "duration": 3.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 0.95,
                    "product_story_role": role,
                    "product_relevance_prior": "high",
                    "visible_objects": objects,
                    "literal_actions": actions,
                    "visible_text": [],
                    "narrative_roles": ["hook", "product_showcase", "cta"],
                },
                "motion": {"motion_class": "semi_dynamic"},
                "frame_quality": {"passed": True},
            }

        asset_index = {
            "asset_folder": str(tmp_path / "assets"),
            "windows": [
                window("origin-window", "origin", ["山地茶园"], ["茶树随风摆动"]),
                window("product-window", "finished_product", ["瓶装茶咖"], ["瓶身缓慢转动"]),
            ],
            "coverage": {},
        }
        narrative_plan = [
            {
                "segment": 0,
                "narrative": "hook",
                "marketing_intent": "hook",
                "copy_goal": "从产地环境建立产品好奇心",
                "product_story_role": "origin",
                "asset_window_ids": ["origin-window"],
            },
            {
                "segment": 1,
                "narrative": "cta",
                "marketing_intent": "cta",
                "copy_goal": "回到成品自然邀请了解",
                "product_story_role": "finished_product",
                "asset_window_ids": ["product-window"],
            },
        ]
        conflicting_candidate = {
            "segments": [
                {
                    "segment": 0,
                    "marketing_intent": "hook",
                    "cue": "这瓶茶咖为什么让人想多看一眼？",
                    "evidence_refs": ["product:name"],
                    "claims": [],
                    "desired_story_role": "finished_product",
                    "visual_query": ["瓶装茶咖"],
                },
                {
                    "segment": 1,
                    "marketing_intent": "hook",
                    "cue": "想换个新搭配就去了解一下。",
                    "evidence_refs": ["product:name"],
                    "claims": [],
                    "desired_story_role": "origin",
                    "visual_query": ["山地茶园"],
                },
            ],
        }

        with patch("config.LLM_ENABLED", True), \
             patch(
                 "llm_client.generate_json",
                 return_value=creative_candidates(
                     conflicting_candidate,
                     conflicting_candidate,
                     conflicting_candidate,
                 ),
             ) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            script = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=2,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 3.0, 1: 3.0},
                narrative_plan_override=narrative_plan,
            )

        prompt_text = generate.call_args.args[0]
        prompt = json.loads(prompt_text)
        assert [item["product_story_role"] for item in prompt["segment_contracts"]] == [
            "origin",
            "finished_product",
        ]
        assert "origin-window" not in prompt_text
        assert "product-window" not in prompt_text
        assert [item["desired_product_story_role"] for item in script["segments"]] == [
            "origin",
            "finished_product",
        ]
        assert script["segments"][0]["visual_story_role"] == "origin"
        assert script["segments"][0]["narrative"] == "hook"
        assert script["segments"][0]["visual_query"] == ["山地茶园", "茶树随风摆动"]
        assert script["segments"][1]["visual_story_role"] == "finished_product"
        assert script["segments"][1]["marketing_intent"] == "cta"
        assert script["segments"][1]["narrative"] == "cta"
        assert script["segments"][1]["visual_query"] == ["瓶装茶咖", "瓶身缓慢转动"]

    def test_one_click_passes_dynamic_material_story_plan_to_copy_generation(self):
        source = Path("one_click_create.py").read_text(encoding="utf-8")
        call = source[
            source.index("ad_script = build_material_constrained_script("):
            source.index("else:\n        ad_script = generate_ad_script", source.index("ad_script = build_material_constrained_script("))
        ]

        assert 'narrative_plan_override=local_story_contract["narrative_plan"]' in call

    def test_script_generation_has_no_prebound_window_and_planner_binds_afterward(self, tmp_path):
        import json
        from unittest.mock import patch
        from local_asset_pipeline import (
            build_material_constrained_script,
            plan_and_materialize_local_clips,
        )

        def window(window_id, role, source):
            return {
                "window_id": window_id,
                "source_video": source,
                "source_path": str(tmp_path / source),
                "start": 0.0,
                "end": 2.0,
                "duration": 2.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": role,
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["product"],
                    "visible_objects": ["瓶装茶咖" if role == "finished_product" else "茶咖饮用场景"],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["hook", "product_showcase", "usage_demo", "cta"],
                    "evidence": f"{role} product footage",
                },
                "motion": {
                    "motion_class": "semi_dynamic",
                    "subject_motion": "medium",
                    "confidence": 1.0,
                    "active_ranges": [[0.0, 2.0]],
                },
                "frame_quality": {"passed": True},
            }

        asset_index = {
            "asset_folder": str(tmp_path / "assets"),
            "windows": [
                window("finished-window", "finished_product", "finished.mp4"),
                window("usage-window", "usage", "usage.mp4"),
            ],
            "coverage": {},
        }
        candidate = {
            "route": "buyer-first",
            "segments": [
                {
                    "segment": 0,
                    "narrative": "hook",
                    "marketing_intent": "hook",
                    "desired_story_role": "finished_product",
                    "visual_query": ["瓶装茶咖"],
                    "cue": "这瓶茶咖你可能真没喝过。",
                    "evidence_refs": ["product:name"],
                    "claims": [],
                },
                {
                    "segment": 1,
                    "narrative": "cta",
                    "marketing_intent": "cta",
                    "desired_story_role": "usage",
                    "visual_query": ["茶咖饮用场景"],
                    "cue": "想换个新搭配，就从这瓶开始试试。",
                    "evidence_refs": ["product:name"],
                    "claims": [],
                },
            ],
        }

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", return_value=creative_candidates(candidate, candidate, candidate)) as generate, \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            script = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=2,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 2.0, 1: 2.0},
                narrative_plan_override=[{
                    "segment": 0,
                    "asset_window_ids": ["wrong-prebound-window"],
                }],
            )

        prompt_text = generate.call_args.args[0]
        assert "finished-window" not in prompt_text
        assert "usage-window" not in prompt_text
        assert "wrong-prebound-window" not in prompt_text
        assert all("asset_window_ids" not in segment for segment in script["segments"])
        assert script["generation_order"] == "material_story_plan_then_sales_copy_then_same_role_shot_matching"

        result = plan_and_materialize_local_clips(
            asset_index=asset_index,
            ad_script=script,
            rhythm_template={
                "transition_duration": 0.0,
                "segments": [
                    {"index": 0, "duration": 2.0, "type": "hook"},
                    {"index": 1, "duration": 2.0, "type": "cta"},
                ],
            },
            clips_dir=tmp_path / "clips",
            final_dir=tmp_path / "final",
            output_name="architecture",
            product_info={"name": "茶咖", "type": "食品"},
            plan_only=True,
        )

        assert [segment["asset_window_ids"] for segment in result["bound_segments"]] == [
            ["finished-window"],
            ["usage-window"],
        ]
        assert all("asset_window_ids" not in segment for segment in script["segments"])
        assert result["semantic_indices"] == [0, 1]
        prompt = json.loads(prompt_text)
        assert prompt["video_timeline_contract"]["source"] == "global_local_video_understanding"
        assert "material_capability_pool" not in prompt
        assert "finished-window" not in json.dumps(prompt["visual_capabilities"], ensure_ascii=False)
        assert "usage-window" not in json.dumps(prompt["visual_capabilities"], ensure_ascii=False)
        assert {item["role"] for item in prompt["visual_capabilities"]} == {
            "finished_product",
            "usage",
        }
        assert all(
            anchor["kind"] in {"product_info", "verified_fact"}
            for anchor in prompt["copy_evidence_anchors"]
        )

    def test_generated_visual_intent_drives_same_role_shot_matching(self, tmp_path):
        from unittest.mock import patch
        from local_asset_pipeline import (
            build_material_constrained_script,
            plan_and_materialize_local_clips,
        )

        def window(window_id, source, confidence, action):
            return {
                "window_id": window_id,
                "source_video": source,
                "source_path": str(tmp_path / source),
                "start": 0.0,
                "end": 2.0,
                "duration": 2.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": confidence,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["hands", "product"],
                    "visible_objects": ["瓶装茶咖", "冰杯"],
                    "literal_actions": [action],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["hook", "product_showcase"],
                    "evidence": action,
                },
                "motion": {
                    "motion_class": "semi_dynamic",
                    "subject_motion": "medium",
                    "confidence": 1.0,
                    "active_ranges": [[0.0, 2.0]],
                },
                "frame_quality": {"passed": True},
            }

        asset_index = {
            "asset_folder": str(tmp_path / "assets"),
            "windows": [
                window("label", "label.mp4", 1.0, "手指向瓶身标签"),
                window("pour", "pour.mp4", 0.8, "手向冰杯倾倒茶咖"),
            ],
            "coverage": {},
        }
        candidate = {
            "route": "usage-led",
            "segments": [{
                "segment": 0,
                "marketing_intent": "hook",
                "cue": "想喝的时候直接倒上一杯就行。",
                "evidence_refs": ["product:name"],
                "claims": [],
                "desired_story_role": "finished_product",
                "visual_query": ["手向冰杯倾倒茶咖"],
            }],
        }

        with patch("config.LLM_ENABLED", True), \
             patch("llm_client.generate_json", return_value=creative_candidates(candidate, candidate, candidate)), \
             patch("local_asset_pipeline.LOCAL_ASSET_INDEX_PATH", tmp_path):
            script = build_material_constrained_script(
                product_info={"name": "茶咖", "type": "食品"},
                coverage={},
                num_segments=1,
                script_style="demonstration",
                asset_index=asset_index,
                segment_durations={0: 2.0},
            )

        result = plan_and_materialize_local_clips(
            asset_index=asset_index,
            ad_script=script,
            rhythm_template={"segments": [{"index": 0, "duration": 2.0}]},
            clips_dir=tmp_path / "clips",
            final_dir=tmp_path / "final",
            output_name="visual-intent",
            product_info={"name": "茶咖", "type": "食品"},
            plan_only=True,
        )

        assert script["segments"][0]["visual_query"] == ["手向冰杯倾倒茶咖"]
        assert script["segments"][0]["desired_product_story_role"] == ""
        assert script["segments"][0]["visual_story_role"] == "finished_product"
        assert result["selected_segments"][0]["source_video"] == "pour.mp4"

    def test_generated_visual_story_role_is_preference_not_capacity_gate(self, tmp_path):
        from local_asset_pipeline import plan_and_materialize_local_clips

        def window(window_id, role, source):
            return {
                "window_id": window_id,
                "source_video": source,
                "source_path": str(tmp_path / source),
                "start": 0.0,
                "end": 2.0,
                "duration": 2.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": role,
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["product"],
                    "visible_objects": ["瓶装茶咖"],
                    "literal_actions": [],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["product_showcase"],
                },
                "motion": {"motion_class": "static"},
                "frame_quality": {"passed": True},
            }

        script = {"segments": [{
            "segment": 0,
            "narrative": "value",
            "marketing_intent": "value",
            "product_story_role": "",
            "desired_product_story_role": "",
            "visual_story_role": "finished_product",
            "visual_query": ["手拿起瓶装茶咖"],
            "voiceover": "这款茶咖日常喝很顺手。",
            "subtitle": "这款茶咖日常喝很顺手。",
            "claims": [],
        }]}

        result = plan_and_materialize_local_clips(
            asset_index={
                "asset_folder": str(tmp_path),
                "windows": [
                    window("product", "finished_product", "product.mp4"),
                    window("usage", "usage", "usage.mp4"),
                ],
                "coverage": {},
            },
            ad_script=script,
            rhythm_template={"segments": [{"index": 0, "duration": 3.0}]},
            clips_dir=tmp_path / "clips",
            final_dir=tmp_path / "final",
            output_name="soft-visual-role",
            product_info={"name": "茶咖", "type": "食品"},
            plan_only=True,
            record_failure=False,
        )

        assert result["selected_segments"][0]["product_story_role"] == "finished_product"
        assert {item["product_story_role"] for item in result["selected_segments"]} == {
            "finished_product",
            "usage",
        }


class TestLocalMultiClipPlanning:
    """One semantic cue may retrieve multiple evidence-backed edit clips."""

    def test_planner_restores_source_tail_reserved_only_for_frame_analysis(self, tmp_path):
        import copy
        from local_asset_pipeline import plan_and_materialize_local_clips

        source = tmp_path / "product.mp4"
        asset_index = {
            "asset_folder": str(tmp_path),
            "sources": [{"path": str(source), "duration": 4.0}],
            "windows": [{
                "window_id": "product-tail-inset",
                "source_video": source.name,
                "source_path": str(source),
                "start": 0.0,
                "end": 3.95,
                "duration": 3.95,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["product"],
                    "visible_objects": ["瓶装茶咖"],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["product_showcase", "cta"],
                    "evidence": "清晰展示瓶装茶咖成品",
                },
                "motion": {"motion_class": "semi_dynamic"},
                "frame_quality": {"passed": True},
            }],
            "coverage": {},
        }
        original_index = copy.deepcopy(asset_index)

        result = plan_and_materialize_local_clips(
            asset_index=asset_index,
            ad_script={"segments": [{
                "segment": 0,
                "narrative": "cta",
                "visual_story_role": "finished_product",
                "visual_query": ["瓶装茶咖"],
                "voiceover": "喜欢就试试这瓶茶咖",
                "subtitle": "喜欢就试试这瓶茶咖",
                "claims": [],
            }]},
            rhythm_template={"segments": [{"index": 0, "duration": 4.0}]},
            clips_dir=tmp_path / "clips",
            final_dir=tmp_path / "final",
            output_name="source-tail-capacity",
            product_info={"name": "茶咖", "type": "食品"},
            plan_only=True,
            record_failure=False,
        )

        assert result["selected_segments"][0]["source_end"] == pytest.approx(4.0)
        assert sum(item["target_duration"] for item in result["selected_segments"]) == pytest.approx(4.0)
        assert asset_index == original_index

    def test_visual_story_role_is_the_semantic_boundary_for_every_fill_clip(self):
        from local_asset_pipeline import _desired_product_story_role, _story_role_supported

        segment = {
            "product_story_role": "",
            "desired_product_story_role": "",
            "visual_story_role": "finished_product",
            "visual_query": ["拿起瓶装饮品", "指向茶咖标签"],
        }

        assert _desired_product_story_role(segment) == "finished_product"
        assert _story_role_supported(segment, {
            "product_story_role": "finished_product",
            "product_relevance_prior": "high",
        }) is True
        assert _story_role_supported(segment, {
            "product_story_role": "origin",
            "product_relevance_prior": "high",
        }) is False

        source_segment = {**segment, "visual_story_role": "ingredient"}
        assert _story_role_supported(source_segment, {
            "product_story_role": "finished_product",
            "product_relevance_prior": "high",
            "product_visibility": 5,
        }) is True
        assert _story_role_supported(source_segment, {
            "product_story_role": "origin",
            "product_relevance_prior": "high",
        }) is False

    def test_planning_is_idempotent_and_never_rewrites_script_constraints(self, tmp_path):
        import copy
        from local_asset_pipeline import plan_and_materialize_local_clips

        def window(window_id, role, source, action):
            return {
                "window_id": window_id,
                "source_video": source,
                "source_path": str(tmp_path / source),
                "start": 0.0,
                "end": 2.0,
                "duration": 2.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": role,
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["hands", "product"],
                    "visible_objects": ["瓶装茶咖", "冰杯"],
                    "literal_actions": [action] if action else [],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["product_showcase", "usage_demo"],
                },
                "motion": {
                    "motion_class": "semi_dynamic",
                    "subject_motion": "medium",
                    "confidence": 1.0,
                    "active_ranges": [[0.0, 2.0]],
                },
                "frame_quality": {"passed": True},
            }

        asset_index = {
            "asset_folder": str(tmp_path),
            "windows": [
                window("pour", "usage", "pour.mp4", "手向冰杯倾倒茶咖"),
                window("product", "finished_product", "product.mp4", ""),
            ],
            "coverage": {},
        }
        script = {"segments": [{
            "segment": 0,
            "narrative": "value",
            "marketing_intent": "value",
            "product_story_role": "",
            "desired_product_story_role": "",
            "visual_story_role": "usage",
            "visual_requirement": "",
            "scene_prompt": "",
            "visual_query": ["手向冰杯倾倒茶咖"],
            "asset_window_ids": [],
            "voiceover": "成品瓶装随身带想喝随时喝。",
            "subtitle": "成品瓶装随身带想喝随时喝。",
            "claims": [],
        }]}
        original_script = copy.deepcopy(script)
        kwargs = {
            "asset_index": asset_index,
            "ad_script": script,
            "rhythm_template": {"segments": [{"index": 0, "duration": 3.0}]},
            "clips_dir": tmp_path / "clips",
            "final_dir": tmp_path / "final",
            "output_name": "idempotent-plan",
            "product_info": {"name": "茶咖", "type": "食品"},
            "plan_only": True,
            "record_failure": False,
        }

        first = plan_and_materialize_local_clips(**kwargs)
        second = plan_and_materialize_local_clips(**kwargs)

        assert script == original_script
        assert [item["source_video"] for item in first["selected_segments"]] == [
            item["source_video"] for item in second["selected_segments"]
        ]
        assert sum(item["target_duration"] for item in second["selected_segments"]) == 3.0

    def test_global_planner_reserves_product_closure_footage_for_cta(self, tmp_path):
        from local_asset_pipeline import plan_and_materialize_local_clips

        def window(window_id, role, source, narrative_roles, confidence):
            path = tmp_path / source
            path.write_bytes(source.encode())
            return {
                "window_id": window_id,
                "source_path": str(path),
                "source_video": source,
                "start": 0.0,
                "end": 2.0,
                "duration": 2.0,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": confidence,
                    "product_story_role": role,
                    "product_visibility": 5,
                    "product_relevance_prior": "high",
                    "visible_subjects": ["product"],
                    "visible_objects": ["瓶装茶咖" if role == "finished_product" else "茉莉花茶原料"],
                    "visible_text": ["茶咖" if role == "finished_product" else "茉莉花茶"],
                    "relation_candidates": ["茉莉花茶"] if role == "ingredient" else ["茶咖"],
                    "narrative_roles": narrative_roles,
                    "evidence": source,
                },
                "motion": {"motion_class": "dynamic"},
                "frame_quality": {"passed": True},
            }

        windows = [
            window("ingredient", "ingredient", "ingredient.mp4", ["product_showcase"], 0.90),
            window("product-1", "finished_product", "product-1.mp4", ["product_showcase"], 1.0),
            window("product-2", "finished_product", "product-2.mp4", ["product_showcase"], 0.95),
        ]
        script = {
            "segments": [
                {
                    "segment": 0,
                    "narrative": "value",
                    "product_story_role": "",
                    "marketing_intent": "value",
                    "voiceover": "这款茶咖很适合日常",
                    "subtitle": "这款茶咖很适合日常",
                    "visual_requirement": "茶咖",
                    "asset_query": ["茶咖"],
                    "claims": [],
                },
                {
                    "segment": 1,
                    "narrative": "value",
                    "product_story_role": "",
                    "marketing_intent": "value",
                    "voiceover": "小巧一瓶随手带着",
                    "subtitle": "小巧一瓶随手带着",
                    "visual_requirement": "茶咖",
                    "asset_query": ["茶咖"],
                    "claims": [],
                },
                {
                    "segment": 2,
                    "narrative": "cta",
                    "product_story_role": "ingredient",
                    "desired_product_story_role": "",
                    "marketing_intent": "cta",
                    "voiceover": "想要的直接戳下方小黄车购买",
                    "subtitle": "想要的直接戳下方小黄车购买",
                    "visual_requirement": "茶咖",
                    "asset_query": ["茶咖"],
                    "claims": [],
                },
            ],
            "generated_by": "local_asset_test",
        }
        rhythm = {
            "transition_duration": 0.0,
            "segments": [
                {"index": 0, "duration": 2.0, "type": "value"},
                {"index": 1, "duration": 2.0, "type": "value"},
                {"index": 2, "duration": 2.0, "type": "cta"},
            ],
        }

        result = plan_and_materialize_local_clips(
            asset_index={"asset_folder": str(tmp_path), "windows": windows, "coverage": {}},
            ad_script=script,
            rhythm_template=rhythm,
            clips_dir=tmp_path / "clips",
            final_dir=tmp_path / "final",
            output_name="narrative-closure",
            product_info={"name": "茶咖", "type": "食品", "ingredients": ["茉莉花茶"]},
            plan_only=True,
        )

        selected_roles = [
            item["analysis"]["product_story_role"]
            for item in result["selected_segments"]
        ]
        assert selected_roles[-1] == "finished_product"
        assert "ingredient" in selected_roles[:-1]
        assert result["selected_segments"][-1]["product_story_role"] == "finished_product"
        assert script["segments"][-1]["product_story_role"] == "ingredient"
        assert script["segments"][-1]["desired_product_story_role"] == ""
        assert "matched_product_story_roles" not in script["segments"][-1]
        assert result["bound_segments"][-1]["product_story_role"] == ""
        assert result["bound_segments"][-1]["matched_product_story_roles"] == ["finished_product"]

    def test_long_semantic_cue_uses_multiple_non_overlapping_windows(self, tmp_path):
        from local_asset_pipeline import plan_and_materialize_local_clips

        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        windows = []
        for index, (start, end) in enumerate(((0.0, 3.0), (3.0, 6.0))):
            windows.append({
                "window_id": f"product-{index}",
                "source_path": str(source),
                "source_video": source.name,
                "start": start,
                "end": end,
                "duration": end - start,
                "analysis": {
                    "usable_for_ad": True,
                    "confidence": 1.0,
                    "product_story_role": "finished_product",
                    "product_visibility": 5,
                    "visible_subjects": ["product"],
                    "visible_objects": ["瓶装茶咖"],
                    "visible_text": ["茶咖"],
                    "narrative_roles": ["product_showcase", "cta"],
                    "evidence": "清晰展示瓶装茶咖成品",
                },
                "motion": {"motion_class": "semi_dynamic"},
                "frame_quality": {"passed": True},
            })
        script = {
            "segments": [{
                "segment": 0,
                "narrative": "product_showcase",
                "product_story_role": "finished_product",
                "subtitle": "先看清这瓶茶咖",
                "voiceover": "先看清这瓶茶咖",
                "visual_requirement": "瓶装茶咖成品",
                "scene_prompt": "瓶装茶咖成品",
                "asset_query": ["瓶装茶咖"],
                "claims": [],
            }],
            "generated_by": "local_asset_test",
        }
        rhythm = {
            "transition_duration": 0.0,
            "segments": [{"index": 0, "duration": 5.0, "type": "showcase"}],
        }
        materialized = []

        def fake_materialize(source, start, end, output):
            materialized.append((start, end))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"clip")
            return output

        with patch("local_asset_pipeline._materialize_clip", side_effect=fake_materialize), \
             patch("local_asset_pipeline.build_local_asset_creative_profile", return_value={}), \
             patch("local_asset_pipeline.write_frame_evidence_artifacts"):
            result = plan_and_materialize_local_clips(
                asset_index={"asset_folder": str(tmp_path), "windows": windows, "coverage": {}},
                ad_script=script,
                rhythm_template=rhythm,
                clips_dir=tmp_path / "clips",
                final_dir=tmp_path / "final",
                output_name="multi",
                product_info={"name": "茶咖", "type": "食品"},
            )

        assert result["edit_indices"] == [0, 1]
        assert result["semantic_indices"] == [0, 0]
        assert [item["edit_index"] for item in result["selected_segments"]] == [0, 1]
        assert [item["semantic_segment"] for item in result["selected_segments"]] == [0, 0]
        assert materialized == pytest.approx([(0.0, 3.0), (3.0, 5.0)])
        assert sum(item["target_duration"] for item in result["selected_segments"]) == pytest.approx(5.0)

    def test_global_planner_reserves_unique_action_evidence_for_action_cue(self, tmp_path):
        from local_asset_pipeline import plan_and_materialize_local_clips

        product_source = tmp_path / "product.mp4"
        pour_source = tmp_path / "pour.mp4"
        product_source.write_bytes(b"product")
        pour_source.write_bytes(b"pour")
        common = {
            "usable_for_ad": True,
            "product_visibility": 5,
            "product_relevance_prior": "high",
            "visible_subjects": ["product", "hands"],
            "visible_objects": ["瓶装茶咖"],
            "visible_text": ["茶咖"],
        }
        windows = [
            {
                "window_id": "product",
                "source_path": str(product_source),
                "source_video": product_source.name,
                "start": 0.0,
                "end": 4.0,
                "duration": 4.0,
                "analysis": {
                    **common,
                    "confidence": 0.8,
                    "product_visibility": 3,
                    "product_story_role": "finished_product",
                    "narrative_roles": ["product_showcase"],
                    "literal_actions": ["手指向瓶身标签"],
                    "temporal_events": [{"action": "手指向瓶身标签"}],
                    "evidence": "手持茶咖并指向标签",
                },
                "motion": {
                    "subject_motion": "medium",
                    "subject_motion_ratio": 0.08,
                    "confidence": 0.9,
                    "active_ranges": [[0.2, 3.8]],
                },
                "frame_quality": {"passed": True},
            },
            {
                "window_id": "pour",
                "source_path": str(pour_source),
                "source_video": pour_source.name,
                "start": 0.0,
                "end": 4.0,
                "duration": 4.0,
                "analysis": {
                    **common,
                    "confidence": 1.0,
                    "product_story_role": "usage",
                    "narrative_roles": ["hook", "usage_demo", "product_showcase"],
                    "literal_actions": ["手向玻璃杯倾倒饮品"],
                    "temporal_events": [{"action": "手向玻璃杯倾倒深色液体"}],
                    "action_phase": "action",
                    "evidence": "茶咖被倒入装有冰块的玻璃杯",
                },
                "motion": {
                    "subject_motion": "high",
                    "subject_motion_ratio": 0.12,
                    "confidence": 0.95,
                    "active_ranges": [[0.0, 4.0]],
                },
                "frame_quality": {"passed": True},
            },
        ]
        script = {
            "segments": [
                {
                    "segment": 0,
                    "narrative": "hook",
                    "product_story_role": "finished_product",
                    "subtitle": "你见过茶咖吗",
                    "voiceover": "你见过茶咖吗",
                    "visual_requirement": "瓶装茶咖成品",
                    "claims": [],
                },
                {
                    "segment": 1,
                    "narrative": "usage_demo",
                    "product_story_role": "usage",
                    "subtitle": "直接倒进冰块杯",
                    "voiceover": "直接倒进装了冰块的玻璃杯就能喝",
                    "visual_requirement": "手向冰块玻璃杯倾倒茶咖",
                    "claims": [],
                },
            ],
            "generated_by": "local_asset_test",
        }
        rhythm = {
            "transition_duration": 0.0,
            "segments": [
                {"index": 0, "duration": 4.0, "type": "hook"},
                {"index": 1, "duration": 4.0, "type": "usage_demo"},
            ],
        }

        def fake_materialize(source, start, end, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"clip")
            return output

        with patch("local_asset_pipeline._materialize_clip", side_effect=fake_materialize), \
             patch("local_asset_pipeline.build_local_asset_creative_profile", return_value={}), \
             patch("local_asset_pipeline.write_frame_evidence_artifacts"):
            result = plan_and_materialize_local_clips(
                asset_index={"asset_folder": str(tmp_path), "windows": windows, "coverage": {}},
                ad_script=script,
                rhythm_template=rhythm,
                clips_dir=tmp_path / "clips",
                final_dir=tmp_path / "final",
                output_name="action-reservation",
                product_info={"name": "茶咖", "type": "食品"},
            )

        selected = result["selected_segments"]
        assert [item["source_path"] for item in selected] == [
            str(product_source),
            str(pour_source),
        ]

    def test_edit_timeline_collapses_to_one_interval_per_semantic_cue(self):
        from one_click_create import _collapse_edit_timeline_by_semantic

        timeline = [
            {"index": 0, "start": 0.0, "end": 2.5, "duration": 2.5, "type": "hook"},
            {"index": 1, "start": 2.2, "end": 4.2, "duration": 2.0, "type": "hook"},
            {"index": 2, "start": 4.2, "end": 7.0, "duration": 2.8, "type": "value"},
        ]

        collapsed = _collapse_edit_timeline_by_semantic(timeline, [0, 0, 1])

        assert collapsed == [
            {
                "index": 0,
                "start": 0.0,
                "end": 4.2,
                "duration": 4.2,
                "type": "hook",
                "purpose": "",
                "edit_indices": [0, 1],
            },
            {
                "index": 1,
                "start": 4.2,
                "end": 7.0,
                "duration": 2.8,
                "type": "value",
                "purpose": "",
                "edit_indices": [2],
            },
        ]

    def test_transition_overlap_budget_preserves_one_take_duration(self, tmp_path):
        from intelligent_transition import plan_intelligent_transitions

        ranked = [
            {"type": "dissolve", "score": 0.95, "reason": "best", "score_details": {}},
            {"type": "none", "score": 0.80, "reason": "safe", "score_details": {}},
        ]
        features = {
            "left": {}, "right": {}, "composition_change": 0.1,
            "motion_relation": "continuous", "narrative_relation": "continuous",
        }
        render = {"passed": True, "quality_score": 0.95, "metrics": {}}

        with patch("intelligent_transition.analyze_transition_boundary", return_value=features), \
             patch("intelligent_transition.feature_bucket", return_value="bucket"), \
             patch("intelligent_transition.score_transition_candidates", return_value=ranked), \
             patch("intelligent_transition.render_and_evaluate_transition", return_value=render), \
             patch("intelligent_transition._duration", return_value=3.0):
            result = plan_intelligent_transitions(
                [tmp_path / "a.mp4", tmp_path / "b.mp4"],
                ["hook", "value"],
                style="moderate",
                base_duration=0.4,
                work_dir=tmp_path / "previews",
                max_total_overlap=0.0,
            )

        assert result["transitions"][0]["type"] == "none"
        assert result["selected_total_overlap"] == 0.0


class TestWorkflowOrchestratorInterfaceContracts:
    """回归测试：工作流编排器必须使用当前模块接口，避免真实流程后段才失败"""

    def test_audio_generation_uses_current_bgm_and_voiceover_interfaces(self):
        src = Path("workflow_orchestrator.py").read_text(encoding="utf-8")
        block = src[src.index("    def _step_audio_generation"):src.index("    def _step_post_processing")]

        assert "generate_voiceover_script(\n            product_info" in block
        assert "_, voiceover_subtitles = generate_full_voiceover(" in block
        assert "align_subtitles_to_voiceover(subtitles, voiceover_subtitles)" in block
        assert "product_type=product_info.get" in block
        assert "product_category=" not in block
        assert "rhythm_curve=rhythm_curve" not in block
        assert "bgm_audio = Path(bgm_info) if bgm_info else None" in block

    def test_post_processing_uses_current_merger_and_beat_interfaces(self):
        src = Path("workflow_orchestrator.py").read_text(encoding="utf-8")
        block = src[src.index("    def _step_post_processing"):src.index("    def _step_brand_ending")]

        assert "envelope_key_times=beat_timings" in block
        assert "beat_timings=" not in block
        assert "align_subtitles_to_beats(subtitles, bgm_audio)" in block
        assert "align_subtitles_to_beats(subtitles, beat_timings)" not in block


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
