from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.atrelay import AtRelayMixin
from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.llm_tool_actions import LlmToolActionsMixin


class ToolPromptSectionTests(unittest.TestCase):
    @staticmethod
    def _tool_host() -> LlmToolActionsMixin:
        host = object.__new__(LlmToolActionsMixin)
        host.enabled = True
        host.enable_cross_user_memory_bridge = True
        host.enable_worldbook_member_recognition = True
        host.enable_creative_work_read_guard = True
        host.enable_photo_text_action = True
        host.enable_user_requested_photo_generation = True
        host.natural_language_photo_generation_mode = "tool_first"
        host.enable_qzone_integration = True
        host._photo_generation_runtime_available = lambda: True
        host._reaction_image_provider_available = lambda: False
        host._user_requested_photo_generation_allowed = lambda _event=None: True
        host._qzone_available = lambda _event=None: True
        return host

    def test_tool_prompt_producers_return_canonical_sections(self) -> None:
        host = self._tool_host()
        expected = (
            (host._cross_user_memory_query_prompt_section(), "tools.cross_user_memory", "跨用户记忆互通"),
            (host._relation_lookup_prompt_section(), "tools.relation_lookup", "关系网查询"),
            (host._qzone_tool_instruction_prompt_section(), "tools.qzone", "QQ 空间动态工具"),
            (host._creative_work_tool_prompt_section(), "tools.creative_work", "资料柜与自己的创作读取工具"),
            (host._memo_management_tool_prompt_section(), "tools.memo_management", "备忘便签工具"),
            (host._schedule_management_tool_prompt_section(), "tools.schedule_management", "指定日程管理工具"),
        )
        for section, key, title in expected:
            with self.subTest(key=key):
                self.assertIsNotNone(section)
                self.assertEqual(key, section.key)
                self.assertEqual(title, section.title)
                self.assertEqual("tools", section.source)

        photo = host._photo_generation_tool_prompt_section()
        self.assertEqual("tools.photo_generation", photo.key)
        self.assertEqual("生图工具", photo.title)
        self.assertEqual("tools", photo.source)

    def test_media_truth_sections_are_all_canonical(self) -> None:
        sections = self._tool_host()._media_delivery_truth_prompt_sections()

        self.assertEqual(3, len(sections))
        self.assertEqual(
            [
                "tools.media_delivery_truth.history_marker",
                "tools.media_delivery_truth.explicit_generation",
                "tools.media_delivery_truth.hard_rule",
            ],
            [section.key for section in sections],
        )
        self.assertTrue(all(section.source == "tools" for section in sections))

    def test_disabled_conditional_tools_return_none_and_empty_legacy_text(self) -> None:
        host = self._tool_host()
        host.enable_cross_user_memory_bridge = False
        host.enable_worldbook_member_recognition = False
        host.enable_creative_work_read_guard = False
        host.enable_photo_text_action = False

        self.assertIsNone(host._cross_user_memory_query_prompt_section())
        self.assertEqual("", host._cross_user_memory_query_instruction())
        self.assertIsNone(host._relation_lookup_prompt_section())
        self.assertEqual("", host._relation_lookup_instruction())
        self.assertIsNone(host._creative_work_tool_prompt_section())
        self.assertEqual("", host._creative_work_tool_instruction())
        self.assertIsNone(host._photo_generation_tool_prompt_section())
        self.assertEqual("", host._photo_generation_tool_instruction())

    def test_atrelay_has_typed_producer_and_legacy_wrapper(self) -> None:
        host = object.__new__(AtRelayMixin)
        host.enabled = True
        host.enable_atrelay_tools = True

        section = host._atrelay_tool_prompt_section()
        self.assertEqual("tools.atrelay", section.key)
        self.assertEqual("跨会话转述与 @ 群友工具", section.title)
        self.assertEqual("tools", section.source)
        self.assertTrue(host._atrelay_tool_instruction().startswith("【跨会话转述与 @ 群友工具】\n"))
        self.assertNotIn(
            "【跨会话转述与 @ 群友工具】",
            render_prompt_sections(
                [section],
                mode=PromptRenderMode.BODY_ONLY,
            ),
        )


if __name__ == "__main__":
    unittest.main()
