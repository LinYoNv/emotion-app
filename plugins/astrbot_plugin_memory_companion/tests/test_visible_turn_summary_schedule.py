from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.service import MemoryCompanionService


class VisibleTurnSummaryScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_bridge_reply_schedules_session_summary(self) -> None:
        service = MemoryCompanionService.__new__(MemoryCompanionService)
        service.store = SimpleNamespace(add_timeline_event=AsyncMock(return_value="tl_reply_1"))
        service._schedule_session_summary = Mock()

        event_id = await service.record_visible_turn(
            role="assistant",
            content="实际发送给用户的回复",
            scope="private",
            session_id="test_platform:private:test_user_001",
            platform="astrbot",
            user_id="test_user_001",
            user_name="测试用户",
            message_id="test_message_001",
            source="test_confirmed_reply",
            metadata={"bot_id": "test_bot_001", "delivery_confirmed": True},
        )

        self.assertEqual("tl_reply_1", event_id)
        service._schedule_session_summary.assert_called_once()
        ctx = service._schedule_session_summary.call_args.args[0]
        self.assertEqual("test_platform:private:test_user_001", ctx.session_id)
        self.assertEqual("private", ctx.scope)
        self.assertEqual("astrbot", ctx.platform)
        self.assertEqual("test_user_001", ctx.user_id)
        self.assertEqual("测试用户", ctx.user_name)
        self.assertEqual("test_bot_001", ctx.bot_id)
        self.assertEqual("test_message_001", ctx.message_id)
        self.assertEqual(
            "bridge_visible_turn",
            service._schedule_session_summary.call_args.kwargs["reason"],
        )

    async def test_empty_session_does_not_schedule(self) -> None:
        service = MemoryCompanionService.__new__(MemoryCompanionService)
        service.store = SimpleNamespace(add_timeline_event=AsyncMock(return_value="tl_reply_2"))
        service._schedule_session_summary = Mock()

        event_id = await service.record_visible_turn(
            role="assistant",
            content="可见回复",
            scope="private",
            session_id="",
            user_id="u1",
        )

        self.assertEqual("tl_reply_2", event_id)
        service._schedule_session_summary.assert_not_called()

    async def test_visible_user_turn_waits_for_confirmed_bot_reply(self) -> None:
        service = MemoryCompanionService.__new__(MemoryCompanionService)
        service.store = SimpleNamespace(add_timeline_event=AsyncMock(return_value="tl_user_1"))
        service._schedule_session_summary = Mock()

        event_id = await service.record_visible_turn(
            role="user",
            content="用户刚发出的消息",
            scope="private",
            session_id="test_platform:private:test_user_001",
            platform="astrbot",
            user_id="test_user_001",
            message_id="test_message_002",
        )

        self.assertEqual("tl_user_1", event_id)
        service._schedule_session_summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
