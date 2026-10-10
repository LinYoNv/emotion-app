# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace

from astrbot.api.message_components import Plain
from astrbot_plugin_private_companion.conversation_prompt_section import (
    prompt_cdata,
    prompt_section,
)
from astrbot_plugin_private_companion.event_dispatch import EventDispatchMixin
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin
from astrbot_plugin_private_companion.segmented_message import (
    has_fenced_llm_segment_marker,
    LLM_SEGMENT_MARKER,
    parse_llm_segment_control,
    sanitize_llm_segment_control_tokens,
    split_llm_controlled_text,
    strip_llm_segment_marker_lines,
)
from astrbot_plugin_private_companion.tts_enhancement import TtsEnhancementMixin


class _Event:
    unified_msg_origin = ""


class _Harness(ProactiveMessageMixin, EventDispatchMixin):
    def __init__(self) -> None:
        self.persona_values: dict[str, object] = {}
        self.enable_segmented_proactive_reply = True
        self.enable_llm_controlled_segmenting = True
        self.enable_segmented_plugin_rules = True
        self.enable_segmented_proactive_chat_profiles = False
        self.segmented_proactive_scope = "all_llm"
        self.segmented_proactive_chat_scope = "all"
        self.segmented_proactive_threshold = 500
        self.segmented_proactive_min_segment_chars = 1
        self.segmented_proactive_max_segments = 3
        self.segmented_proactive_split_mode = "words"
        self.segmented_proactive_split_words = ["。"]
        self.segmented_proactive_match_width_variants = True
        self.segmented_proactive_regex = r".*?[。？！~…\n]+|.+$"
        self.enable_segmented_proactive_content_cleanup = False
        self.segmented_proactive_content_cleanup_scope = "all"
        self.segmented_proactive_content_cleanup_rule = r"[\n]"
        self.segmented_proactive_content_cleanup_words = ["\n"]
        self.enable_segmented_proactive_content_replacement = False
        self.segmented_proactive_content_replacements = []
        self.segmented_proactive_interval_method = "random"
        self.segmented_proactive_interval_min = 1.0
        self.segmented_proactive_interval_max = 1.0
        self.segmented_proactive_log_base = 1.8

    def persona_setting(self, key: str, default: object = None) -> object:
        return self.persona_values.get(key, getattr(self, key, default))

    def _feature_enabled_or_temp_unlocked(self, key: str, default: bool = False) -> bool:
        return bool(self.persona_setting(key, default))

    def _segmented_scope_allows_event(self, _event: object) -> bool:
        return True

    def _segmented_scope_allows_umo(self, _umo: str) -> bool:
        return True

    def _segmented_platform_allows(self, **_kwargs: object) -> bool:
        return True


class _TtsHarness(TtsEnhancementMixin, _Harness):
    pass


class LlmControlledSegmentingTests(unittest.TestCase):
    def test_marker_is_strict_and_standalone(self) -> None:
        marker = LLM_SEGMENT_MARKER
        self.assertEqual((['a', 'b'], True), split_llm_controlled_text(f"a\n{marker}\nb"))
        self.assertEqual((["a b"], False), split_llm_controlled_text(f"a {marker} b"))
        self.assertEqual((["a\n[]\nb"], False), split_llm_controlled_text("a\n[]\nb"))
        self.assertEqual(
            (["a\n```\n\n```\nb"], False),
            split_llm_controlled_text(f"a\n```\n{marker}\n```\nb"),
        )
        four_tick_fence = f"a\n````text\n```\n{marker}\nstill code\n````\nb"
        self.assertEqual(
            ([four_tick_fence.replace(marker, "")], False),
            split_llm_controlled_text(four_tick_fence),
        )
        self.assertTrue(has_fenced_llm_segment_marker(four_tick_fence))

    def test_prompt_names_only_the_real_control_marker(self) -> None:
        prompt = PrivateCompanionPlugin._llm_controlled_segmenting_prompt(_Harness())
        self.assertEqual(2, prompt.count(LLM_SEGMENT_MARKER))
        self.assertNotIn("[]", prompt)
        self.assertNotIn("[[]]", prompt)

    def test_custom_prompt_replaces_placeholder_and_empty_uses_default(self) -> None:
        harness = _Harness()
        harness.persona_values["llm_controlled_segmenting_prompt"] = (
            "短句之间使用 {{ split_marker }}，不要用空行代替。"
        )
        custom = PrivateCompanionPlugin._llm_controlled_segmenting_prompt(harness)
        self.assertEqual(
            f"短句之间使用 {LLM_SEGMENT_MARKER}，不要用空行代替。",
            custom,
        )
        harness.persona_values["llm_controlled_segmenting_prompt"] = ""
        self.assertEqual(
            2,
            PrivateCompanionPlugin._llm_controlled_segmenting_prompt(harness).count(
                LLM_SEGMENT_MARKER
            ),
        )

    def test_plugin_rules_do_not_split_a_fenced_marker_example(self) -> None:
        harness = _Harness()
        harness.segmented_proactive_max_segments = 8
        text = (
            f"before\n````text\n```\n{LLM_SEGMENT_MARKER}\n"
            "still code\n````\nafter"
        )
        self.assertEqual(
            [text.replace(LLM_SEGMENT_MARKER, "")],
            PrivateCompanionPlugin._split_llm_controlled_text_for_event(
                harness,
                _Event(),
                text,
            ),
        )

    def test_marker_cleanup_removes_fenced_and_quoted_examples(self) -> None:
        marker = LLM_SEGMENT_MARKER
        source = f"a\n{marker}\n```text\n{marker}\n```\n> {marker}\nb"
        self.assertEqual("a\n```text\n\n```\n\nb", strip_llm_segment_marker_lines(source))
        four_tick_fence = f"a\n````text\n```\n{marker}\nstill code\n````\nb"
        self.assertEqual(
            four_tick_fence.replace(marker, ""),
            strip_llm_segment_marker_lines(four_tick_fence),
        )

    def test_recovers_only_one_sided_newline_errors(self) -> None:
        marker = LLM_SEGMENT_MARKER
        for source in (f"a{marker}\nb", f"a\n{marker}b"):
            with self.subTest(source=source):
                parsed = parse_llm_segment_control(source)
                self.assertTrue(parsed.controlled)
                self.assertEqual(("a", "b"), parsed.segments)
                self.assertEqual(1, parsed.recovered_boundary_count)

        inline = parse_llm_segment_control(f"a{marker}b")
        self.assertFalse(inline.controlled)
        self.assertEqual("a b", inline.sanitized_text)
        self.assertEqual(1, inline.cleaned_only_count)

    def test_cleanup_removes_reserved_variants_and_is_idempotent(self) -> None:
        source = (
            "a {{ split_marker }} b "
            "&lt;&lt;PRIVATE_COMPANION_SPLIT&gt;&gt; c "
            "<< PRIVATE _ COMPANION _ SPLIT >> d "
            r"\<\<PRIVATE_COMPANION_SPLIT\>\> e"
        )
        cleaned = sanitize_llm_segment_control_tokens(source)
        self.assertEqual("a b c d e", cleaned)
        self.assertEqual(cleaned, sanitize_llm_segment_control_tokens(cleaned))

    def test_legacy_persona_migration_adds_new_defaults(self) -> None:
        from astrbot_plugin_private_companion.persona_config import (
            build_scope_manifest,
            load_schema,
            migrate_persona_profile,
        )

        manifest = build_scope_manifest(load_schema())
        migrated = migrate_persona_profile(
            {
                "persona_settings": {
                    "enable_segmented_proactive_reply": False,
                }
            },
            manifest=manifest,
            persona_id="legacy",
        )
        self.assertFalse(migrated["persona_settings"]["enable_llm_controlled_segmenting"])
        self.assertTrue(migrated["persona_settings"]["enable_segmented_plugin_rules"])

    def test_llm_segments_consume_global_remaining_plugin_budget(self) -> None:
        harness = _Harness()
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        event = _Event()
        text = f"短段。\n{LLM_SEGMENT_MARKER}\n这是最长的一段。再一段。"
        segments = splitter(harness, event, text)
        self.assertEqual(3, len(segments))
        self.assertEqual("短段。", segments[0])
        self.assertEqual("这是最长的一段。", segments[1])
        self.assertEqual("再一段。", segments[2])
        self.assertEqual(
            (0, 1, 1),
            event._private_companion_llm_planned_segment_ids,
        )

    def test_llm_segments_are_not_limited_when_over_plugin_budget(self) -> None:
        harness = _Harness()
        harness.segmented_proactive_max_segments = 2
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        event = _Event()
        text = f"一\n{LLM_SEGMENT_MARKER}\n二\n{LLM_SEGMENT_MARKER}\n三"
        self.assertEqual(["一", "二", "三"], splitter(harness, event, text))

    def test_plugin_budget_is_global_across_component_text_buffers(self) -> None:
        harness = _Harness()
        harness.segmented_proactive_max_segments = 3
        planner = PrivateCompanionPlugin._split_llm_controlled_text_buffers_for_event
        buffers = [
            f"甲一。甲二。\n{LLM_SEGMENT_MARKER}\n甲三。甲四。",
            f"乙一。乙二。\n{LLM_SEGMENT_MARKER}\n乙三。乙四。",
        ]
        planned = planner(harness, _Event(), buffers)
        self.assertEqual(4, sum(len(parts) for parts in planned))
        self.assertEqual(
            [["甲一。甲二。", "甲三。甲四。"], ["乙一。乙二。", "乙三。乙四。"]],
            planned,
        )

    def test_plugin_split_that_exceeds_remaining_budget_is_rejected(self) -> None:
        harness = _Harness()
        harness.segmented_proactive_max_segments = 3
        original_splitter = harness._split_proactive_text

        def oversized_split(text: str, **kwargs: object) -> list[str]:
            if kwargs.get("common_transforms_only"):
                return original_splitter(text, **kwargs)
            if text.startswith("最长"):
                return ["最长一", "最长二", "最长三"]
            return original_splitter(text, **kwargs)

        harness._split_proactive_text = oversized_split  # type: ignore[method-assign]
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        text = f"短段\n{LLM_SEGMENT_MARKER}\n最长段落"
        self.assertEqual(["短段", "最长段落"], splitter(harness, _Event(), text))

    def test_plugin_rules_can_be_disabled_without_disabling_llm_boundaries(self) -> None:
        harness = _Harness()
        harness.enable_segmented_plugin_rules = False
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        event = _Event()
        text = f"一\n{LLM_SEGMENT_MARKER}\n二。三。"
        self.assertEqual(["一", "二。三。"], splitter(harness, event, text))

    def test_master_switch_disables_both_pipelines(self) -> None:
        harness = _Harness()
        harness.enable_segmented_proactive_reply = False
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        event = _Event()
        text = f"一\n{LLM_SEGMENT_MARKER}\n二。三。"
        self.assertEqual(["一\n二。三。"], splitter(harness, event, text))

    def test_llm_segments_still_use_shared_content_replacement(self) -> None:
        harness = _Harness()
        harness.enable_segmented_plugin_rules = False
        harness.enable_segmented_proactive_content_replacement = True
        harness.segmented_proactive_content_replacements = ["旧称 => 新称"]
        splitter = PrivateCompanionPlugin._split_llm_controlled_text_for_event
        event = _Event()
        text = f"旧称一\n{LLM_SEGMENT_MARKER}\n旧称二"
        self.assertEqual(["新称一", "新称二"], splitter(harness, event, text))

    def test_native_proactive_generation_receives_the_marker_contract(self) -> None:
        harness = _Harness()
        harness._llm_controlled_segmenting_prompt = lambda: (
            PrivateCompanionPlugin._llm_controlled_segmenting_prompt(harness)
        )
        harness._llm_controlled_segmenting_prompt_section = lambda: (
            PrivateCompanionPlugin._llm_controlled_segmenting_prompt_section(harness)
        )
        hint = harness._proactive_llm_segmenting_instruction(
            umo="default:FriendMessage:1",
        )
        self.assertIn(LLM_SEGMENT_MARKER, hint)
        self.assertIn("<![CDATA[", hint)

        harness._llm_controlled_segmenting_prompt_section = lambda: prompt_section(
            key="reply.segmentation",
            title="回复分段控制",
            source="segmented_reply",
            content=prompt_cdata("前半]]>后半"),
        )
        escaped_hint = harness._proactive_llm_segmenting_instruction(
            umo="default:FriendMessage:1",
        )
        self.assertIn("前半]]]]><![CDATA[>后半", escaped_hint)
        self.assertNotIn("<![CDATA[前半]]>后半]]>", escaped_hint)

        harness.enable_llm_controlled_segmenting = False
        self.assertEqual(
            "",
            harness._proactive_llm_segmenting_instruction(
                umo="default:FriendMessage:1",
            ),
        )

    def test_tts_plugin_rules_respect_platform_segment_permission(self) -> None:
        harness = _TtsHarness()
        harness.segmented_proactive_scope = "all_llm"
        harness.segmented_proactive_max_segments = 5
        harness.tts_voice_language = "zh"
        harness._segmented_platform_allows = lambda **_kwargs: False
        event = SimpleNamespace(unified_msg_origin="qq_official:GroupMessage:group-1")

        chunks = harness._tts_segment_plain_chunk_for_ordered_send(
            event,
            [Plain("第一段。第二段。")],
        )

        self.assertEqual(1, len(chunks))
        self.assertEqual("第一段。第二段。", chunks[0][0].text)

    def test_native_proactive_finalization_preserves_explicit_boundaries(self) -> None:
        harness = _Harness()
        harness._looks_like_internal_provider_error_text = lambda _text: False
        harness._sanitize_proactive_text = lambda text: str(text or "").strip()
        harness._sanitize_action_boundaries = lambda text, **_kwargs: text
        harness._repair_proactive_recipient_address = lambda text, *_args: (text, "")
        harness._wrong_proactive_recipient_address = lambda *_args: ""
        harness._is_overabstract_proactive_text = lambda *_args, **_kwargs: False
        harness._apply_proactive_style_variation = lambda text, _user: text
        harness._collapse_multi_candidate_proactive_text = lambda text, **_kwargs: text
        harness._repair_proactive_subject_drift = lambda text, **_kwargs: text
        harness._visible_text_without_tts_reading = lambda text, **_kwargs: text
        harness._unexecuted_relay_claim_reason = lambda *_args, **_kwargs: ""
        harness._should_drop_vague_generic_proactive = lambda *_args, **_kwargs: False
        harness._should_drop_misstaged_proactive_text = lambda *_args, **_kwargs: False

        async def review(_user: object, text: str, **_kwargs: object) -> str:
            return text

        harness._review_proactive_message_stance = review
        harness._trim_proactive_status_inventory = lambda text: text
        harness._trim_performative_self_state_tail = lambda text: text
        harness._normalize_proactive_sentence_flow = lambda text: text
        text = f"第一段\n{LLM_SEGMENT_MARKER}\n第二段"
        finalized, failure = asyncio.run(
            ProactiveMessageMixin._finalize_proactive_generated_text(
                harness,
                {"user_id": "1"},
                text,
                name="用户",
                reason="check_in",
                action="message",
            )
        )
        self.assertEqual(text, finalized)
        self.assertEqual("", failure)

        harness.enable_llm_controlled_segmenting = False
        finalized, failure = asyncio.run(
            ProactiveMessageMixin._finalize_proactive_generated_text(
                harness,
                {"user_id": "1"},
                text,
                name="用户",
                reason="check_in",
                action="message",
            )
        )
        self.assertEqual(text, finalized)
        self.assertEqual("", failure)

    def test_instruction_is_injected_for_main_conversation_only_when_enabled(self) -> None:
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        values = {
            "enable_segmented_proactive_reply": True,
            "enable_llm_controlled_segmenting": True,
            "segmented_proactive_scope": "all_llm",
            "passive_injection_position": "prompt",
        }
        plugin.persona_setting = lambda key, default=None, persona_id="": values.get(key, default)
        plugin._segmented_setting = lambda name, **kwargs: values.get(
            f"segmented_proactive_{name}", kwargs.get("default")
        )
        plugin._segmented_scope_allows_event = lambda _event: True
        plugin._segmented_platform_allows = lambda **_kwargs: True
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:1")
        req = SimpleNamespace(
            prompt="你好",
            system_prompt="",
            contexts=[],
            extra_user_content_parts=[],
        )

        asyncio.run(
            PrivateCompanionPlugin.inject_llm_controlled_segmenting_instruction(
                plugin,
                event,
                req,
            )
        )
        injected = "\n".join(
            str(getattr(part, "text", "") or "")
            for part in req.extra_user_content_parts
        )
        self.assertIn(LLM_SEGMENT_MARKER, injected)
        self.assertIn("长文", injected)

        values["enable_llm_controlled_segmenting"] = False
        req2 = SimpleNamespace(
            prompt="你好",
            system_prompt="",
            contexts=[],
            extra_user_content_parts=[],
        )
        asyncio.run(
            PrivateCompanionPlugin.inject_llm_controlled_segmenting_instruction(
                plugin,
                event,
                req2,
            )
        )
        self.assertEqual([], req2.extra_user_content_parts)

        values["enable_llm_controlled_segmenting"] = True
        plugin._segmented_platform_allows = lambda **_kwargs: False
        req3 = SimpleNamespace(
            prompt="你好",
            system_prompt="",
            contexts=[],
            extra_user_content_parts=[],
        )
        asyncio.run(
            PrivateCompanionPlugin.inject_llm_controlled_segmenting_instruction(
                plugin,
                event,
                req3,
            )
        )
        self.assertEqual([], req3.extra_user_content_parts)


if __name__ == "__main__":
    unittest.main()
