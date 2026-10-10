from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.event_dispatch import EventDispatchMixin


class _Harness(EventDispatchMixin):
    @staticmethod
    def _group_member_identity_label(user_id, name, *, limit=24):
        del limit
        return f"{name}[QQ:{user_id}]"

    @staticmethod
    def _group_member_identity_anchor_note(user_id, name, *, limit=120):
        del user_id, name, limit
        return "稳定身份"


class EventDispatchBackgroundPromptSectionTests(unittest.TestCase):
    def test_smart_debounce_uses_body_only_json_contract(self) -> None:
        section = EventDispatchMixin._smart_message_debounce_prompt_section(
            private_chat=True,
            sender_name="用户",
            sender_id="10001",
            cleaned="我还有",
            recent=["前一句"],
            example_lines=["- false_complete: A / B => 很快补话"],
        )

        rendered = render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY)

        self.assertIsInstance(section, PromptSection)
        self.assertEqual("background.smart_message_debounce", section.key)
        self.assertIn('只输出 JSON：{"decision":"complete|incomplete"', rendered)
        self.assertIn("会话类型：私聊", rendered)
        self.assertIn("缓冲中的前文：前一句", rendered)
        self.assertNotIn("【", rendered)

    def test_group_air_guard_keeps_reply_silence_wire(self) -> None:
        section = _Harness()._group_air_reply_guard_prompt_section(
            sender_id="10001",
            sender_name="甲",
            text="晚安",
            scene={"trigger": "reply_bot", "reason": "continuation"},
            recent_bot_count=2,
            recent_bot_lines="- 22:00: 晚安",
            recent_flow="甲：晚安",
        )

        rendered = render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY)

        self.assertEqual("background.group_air_reply_guard", section.key)
        self.assertTrue(rendered.startswith("判断群聊里 Bot 现在是否应该继续回复。只回答 REPLY 或 SILENCE"))
        self.assertIn("当前发言者：甲[QQ:10001]", rendered)
        self.assertIn("窗口内 Bot 已回复次数：2", rendered)

    def test_group_followup_keeps_yes_no_wire(self) -> None:
        section = _Harness()._group_followup_judge_prompt_section(
            sender_id="10002",
            sender_name="乙",
            text="然后呢",
            active={
                "sender_id": "10001",
                "sender_name": "甲",
                "last_text": "你觉得呢",
                "last_bot_reply": "我觉得可以。",
            },
            scene={"trigger": "context", "talking_to": "group"},
            recent_flow="甲：你觉得呢\nBot：我觉得可以。\n乙：然后呢",
        )

        rendered = render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY)

        self.assertEqual("background.group_followup_judge", section.key)
        self.assertIn("只回答 YES 或 NO，不要解释。", rendered)
        self.assertIn("上一次明确和 Bot 对话的人：甲[QQ:10001]", rendered)
        self.assertIn("当前发言者身份锚点：稳定身份", rendered)


if __name__ == "__main__":
    unittest.main()
