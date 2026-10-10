from __future__ import annotations

import hashlib
import inspect
import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.tts_enhancement import TtsEnhancementMixin


class _RuleHarness(TtsEnhancementMixin):
    def __init__(self) -> None:
        self.tts_generation_mode = "fast_tag"
        self.tts_frequency_control_mode = "global"
        self.tts_delivery_mode = "voice_and_text"
        self.tts_foreign_text_mode = "translation"
        self.tts_conversion_scope = "partial"
        self.tts_voice_language = "zh"
        self.tts_extra_prompt = ""
        self.config = {}


class TtsPromptSectionAuthoringTests(unittest.TestCase):
    def test_rule_section_is_canonical_and_legacy_wrapper_is_equivalent(self) -> None:
        harness = _RuleHarness()
        section = harness._build_tts_rule_prompt_section("generic")
        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.LABELED_BLOCK,
        )

        self.assertIsInstance(section, PromptSection)
        self.assertEqual("tts.rule", section.key)
        self.assertEqual("语音消息规则", section.title)
        self.assertEqual("tts_enhancement", section.source)
        self.assertNotIn("【语音消息规则】", str(section.content))
        self.assertEqual(rendered, harness._build_tts_rule_prompt("generic"))

    def test_default_foreign_and_full_rule_wires_match_golden_hashes(self) -> None:
        harness = _RuleHarness()
        cases = (
            (
                "default",
                "zh",
                "partial",
                498,
                "e2a9379a4ac2cd84470ff309144c67f1e108ff6a6dc3e47199a5f2f691896866",
            ),
            (
                "foreign",
                "ja",
                "partial",
                767,
                "1987dab93f40ad556867a1b43e4501827cf1ea0390b93d7400bd88c879c6402f",
            ),
            (
                "full",
                "ja",
                "full",
                868,
                "9aa1f80299b3c58989b62984617ad8992457af4fa8dcd6676252fdb521791aaa",
            ),
        )

        for label, language, scope, expected_length, expected_hash in cases:
            with self.subTest(label=label):
                harness.tts_voice_language = language
                harness.tts_conversion_scope = scope
                rendered = harness._build_tts_rule_prompt("generic")
                self.assertEqual(expected_length, len(rendered))
                self.assertEqual(
                    expected_hash,
                    hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                )

    def test_postprocess_section_and_wrapper_keep_legacy_wire(self) -> None:
        harness = _RuleHarness()
        event = SimpleNamespace(message_str="普通聊天")
        section = harness._build_tts_postprocess_mode_prompt_section(
            event,
            full_scope=True,
            turn_voice_language="",
        )
        rendered = harness._build_tts_postprocess_mode_prompt(
            event,
            full_scope=True,
            turn_voice_language="",
        )

        self.assertEqual("tts.rule", section.key)
        self.assertEqual("TTS 后处理模式", section.title)
        self.assertNotIn("【TTS 后处理模式】", str(section.content))
        self.assertEqual(
            render_prompt_sections([section], mode=PromptRenderMode.LABELED_BLOCK),
            rendered,
        )
        self.assertEqual(343, len(rendered))
        self.assertEqual(
            "6a3d99aea9c90d0108195cbd1fc975917a0aa3440d61fa28490964ac308a701d",
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )

    def test_business_producers_do_not_handwrite_legacy_headings(self) -> None:
        self.assertNotIn(
            "【语音消息规则】",
            inspect.getsource(TtsEnhancementMixin._build_tts_rule_prompt_section),
        )
        self.assertNotIn(
            "【TTS 后处理模式】",
            inspect.getsource(
                TtsEnhancementMixin._build_tts_postprocess_mode_prompt_section
            ),
        )


if __name__ == "__main__":
    unittest.main()
