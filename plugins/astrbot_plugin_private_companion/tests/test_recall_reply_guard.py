# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.event_dispatch import EventDispatchMixin


class _UnavailableMessageApi:
    async def call_action(self, _action: str, **_kwargs):
        raise RuntimeError("消息不存在")


class _PrivateMessageEvent:
    unified_msg_origin = "default:FriendMessage:995051631"

    def __init__(self) -> None:
        raw = {
            "post_type": "message",
            "message_type": "private",
            "message_id": "1224352998",
        }
        self.message_obj = SimpleNamespace(raw_message=raw, message_id="1224352998")
        self.bot = SimpleNamespace(api=_UnavailableMessageApi())


class _RecallGuardHarness(EventDispatchMixin):
    @staticmethod
    def _group_current_reply_quote_message_id(_event) -> str:
        return ""


class RecallReplyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_private_message_lookup_does_not_cancel_reply(self) -> None:
        harness = _RecallGuardHarness()
        event = _PrivateMessageEvent()

        self.assertIsNone(
            await harness._platform_message_exists_for_cancel_check(event, "1224352998")
        )
        self.assertEqual(
            "",
            await harness._should_cancel_reply_for_missing_or_recalled_trigger(event),
        )


if __name__ == "__main__":
    unittest.main()
