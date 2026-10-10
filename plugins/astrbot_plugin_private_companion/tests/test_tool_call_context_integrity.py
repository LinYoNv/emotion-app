# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class ToolCallContextIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        self.event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")

    @staticmethod
    def _assistant(*call_ids: str) -> dict:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
                for call_id in call_ids
            ],
        }

    @staticmethod
    def _tool(call_id: str, content: str = "ok") -> dict:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def test_incomplete_parallel_tool_group_is_removed_atomically(self) -> None:
        contexts = [
            {"role": "user", "content": "查两项"},
            self._assistant("call_a", "call_b"),
            self._tool("call_a"),
            {"role": "assistant", "content": "继续"},
        ]
        req = SimpleNamespace(contexts=contexts)

        self.plugin._repair_incomplete_tool_context_groups(self.event, req)

        self.assertEqual(
            req.contexts,
            [
                {"role": "user", "content": "查两项"},
                {"role": "assistant", "content": "继续"},
            ],
        )

    def test_complete_tool_group_is_preserved_and_duplicate_result_is_removed(self) -> None:
        assistant = self._assistant("call_a", "call_b")
        first = self._tool("call_a", "one")
        second = self._tool("call_b", "two")
        req = SimpleNamespace(
            contexts=[assistant, first, self._tool("call_a", "duplicate"), second]
        )

        self.plugin._repair_incomplete_tool_context_groups(self.event, req)

        self.assertEqual(req.contexts, [assistant, first, second])

    def test_orphan_tool_message_is_removed(self) -> None:
        req = SimpleNamespace(
            contexts=[
                {"role": "user", "content": "你好"},
                self._tool("missing"),
                {"role": "assistant", "content": "你好"},
            ]
        )

        self.plugin._repair_incomplete_tool_context_groups(self.event, req)

        self.assertEqual([item["role"] for item in req.contexts], ["user", "assistant"])

    def test_deepseek_tool_guard_requests_sequential_calls_without_disabling_tools(self) -> None:
        self.plugin._llm_request_provider_identity_parts = lambda _event, _req: [
            "deepseek/deepseek-v4-pro"
        ]
        tool_set = object()
        req = SimpleNamespace(func_tool=tool_set, system_prompt="原提示")

        changed = self.plugin._append_deepseek_tool_protocol_guard(self.event, req)

        self.assertTrue(changed)
        self.assertIs(req.func_tool, tool_set)
        self.assertIn("按顺序逐个调用", req.system_prompt)
        self.assertIn("每条 assistant 消息只发起一个工具调用", req.system_prompt)
        self.assertIn("普通文字时，直接输出最终回复", req.system_prompt)
        self.assertIn("plain 文本不得为空", req.system_prompt)

    def test_tool_guard_does_not_change_non_deepseek_provider(self) -> None:
        self.plugin._llm_request_provider_identity_parts = lambda _event, _req: [
            "openai/gpt-5.4"
        ]
        req = SimpleNamespace(func_tool=object(), system_prompt="原提示")

        changed = self.plugin._append_deepseek_tool_protocol_guard(self.event, req)

        self.assertFalse(changed)
        self.assertEqual(req.system_prompt, "原提示")


if __name__ == "__main__":
    unittest.main()
