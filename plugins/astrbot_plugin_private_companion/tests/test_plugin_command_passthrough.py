# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.pipeline.process_stage.stage import StarRequestSubStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.message_pipeline import handle_group_message


class _Event(AstrMessageEvent):
    def __init__(self, text: str, *, private: bool = True, message_id: str = "command-1") -> None:
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
        message.self_id = "bot-1"
        message.sender = MessageMember(user_id="user-1", nickname="测试用户")
        message.message_id = message_id
        message.group_id = "" if private else "group-1"
        message.message_str = text
        message.message = [Plain(text)]
        message.raw_message = {
            "post_type": "message",
            "message_type": "private" if private else "group",
            "user_id": "user-1",
            "group_id": message.group_id,
        }
        super().__init__(
            text, message, SimpleNamespace(name="aiocqhttp", id="default"),
            "user-1" if private else "group-1",
        )
        self.sent = []

    async def send(self, message, **kwargs) -> None:
        self.sent.append(message)
        self._has_send_oper = True


class _OtherPlugin:
    def __init__(self) -> None:
        self.calls = []

    async def sub_list(self, event):
        self.calls.append(("sub_list", {}))
        return event.plain_result("原插件订阅列表")

    async def sub_add(self, event, uid: int):
        self.calls.append(("sub_add", {"uid": uid}))
        return event.plain_result(f"原插件添加订阅：{uid}")

    async def observe(self, event):
        pass


def _handler(owner, method: str, filters: list, *, module: str = "tests.other_plugin.main"):
    metadata = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module}_{method}",
        handler_name=method,
        handler_module_path=module,
        handler=getattr(type(owner), method),
        event_filters=filters,
    )
    for handler_filter in filters:
        if isinstance(handler_filter, CommandFilter):
            handler_filter.init_handler_md(metadata)
    metadata.handler = getattr(owner, method)
    return metadata


class PluginCommandPassthroughTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        self.plugin.enable_message_debounce = True
        self.plugin.text_message_debounce_seconds = 1.0
        self.plugin.group_message_debounce_seconds = 1.0
        self.plugin._semantic_message_buffers = {}
        self.plugin._data_lock = AsyncMock()
        self.plugin.data = {"users": {"user-1": {"user_id": "user-1"}}}
        self.plugin._private_user_id_for_event = Mock(return_value="user-1")
        self.plugin._canonical_private_user_id = Mock(side_effect=lambda value: value)
        self.other = _OtherPlugin()
        self.command_filter = CommandFilter("bili_sub_list", alias={"订阅列表"})
        self.command = _handler(self.other, "sub_list", [self.command_filter])
        self.listener = _handler(
            self.other, "observe", [EventMessageTypeFilter(EventMessageType.ALL)],
        )

    async def _wake(self, event, handlers=None, *, excluded=(), prefixes=None) -> None:
        stage = WakingCheckStage()
        await stage.initialize(SimpleNamespace(astrbot_config={
            "platform_settings": {}, "admins_id": [],
            "wake_prefix": prefixes or ["/"], "plugin_set": ["*"],
        }))
        registry = SimpleNamespace(get_handlers_by_event_type=Mock(
            return_value=handlers if handlers is not None else [self.listener, self.command],
        ))
        with patch("astrbot.core.pipeline.waking_check.stage.star_handlers_registry", registry), patch(
            "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
            new=AsyncMock(side_effect=lambda event, matched: [
                handler for handler in matched if handler.handler_full_name not in excluded
            ]),
        ):
            await stage.process(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual([], event.sent)

    async def test_framework_commands_survive_prefix_removal_without_reparsing(self) -> None:
        for text, prefix, private in (
            ("/bili_sub_list", "/", True),
            ("/订阅列表", "/", True),
            ("订阅列表", "/", True),
            ("小助手 订阅列表", "小助手 ", True),
            ("/订阅列表", "/", False),
        ):
            with self.subTest(text=text, private=private), patch.object(
                self.command_filter, "filter", wraps=self.command_filter.filter,
            ) as command_matcher:
                event = _Event(text, private=private)
                await self._wake(event, prefixes=[prefix])
                self.assertFalse(event.message_str.startswith(prefix))
                self.assertFalse(getattr(event, "is_command", False))
                handlers = event.get_extra("activated_handlers")
                params = event.get_extra("handlers_parsed_params")
                self.assertEqual({}, params[self.command.handler_full_name])
                self.assertTrue(self.plugin._message_debounce_command_text(event, event.message_str))
                self.assertEqual("command_text", self.plugin._tts_functional_command_reason(event))
                self.assertIs(handlers, event.get_extra("activated_handlers"))
                self.assertIs(params, event.get_extra("handlers_parsed_params"))
                self.assertEqual(1, command_matcher.call_count)

    async def test_command_does_not_merge_or_invalidate_pending_private_or_group_reply(self) -> None:
        for private in (True, False):
            with self.subTest(private=private):
                first = _Event("上一条普通聊天", private=private, message_id="chat-1")
                self.plugin._message_debounce_mark_llm_pending(first)
                pending = deepcopy(self.plugin._message_debounce_pending_llm)
                command = _Event("/bili_sub_list", private=private)
                await self._wake(command)

                self.assertFalse(self.plugin._message_debounce_absorb_pending_message(command, command.message_str))
                self.plugin._message_debounce_mark_llm_pending(command)
                await self.plugin.guard_pending_message_debounce(command)

                self.assertEqual(pending, self.plugin._message_debounce_pending_llm)
                self.assertEqual({}, self.plugin._semantic_message_buffers)
                self.assertFalse(command.is_stopped())
                response = SimpleNamespace(completion_text="正常聊天回复")
                await self.plugin.settle_pending_message_debounce(first, response)
                self.assertEqual("正常聊天回复", response.completion_text)

    async def test_only_matched_and_session_enabled_commands_bypass_chat(self) -> None:
        for text, excluded in (
            ("在吗", ()),
            ("/在吗", ()),
            ("bili_sub_list_extra", ()),
            ("/订阅列表", (self.command.handler_full_name,)),
        ):
            with self.subTest(text=text, excluded=excluded):
                self.plugin._semantic_message_buffers = {}
                self.plugin._message_debounce_mark_llm_pending(_Event("第一句", message_id="chat-1"))
                event = _Event(text)
                await self._wake(event, excluded=excluded)
                self.assertTrue(event.is_at_or_wake_command)
                self.assertEqual([self.listener], event.get_extra("activated_handlers"))
                self.assertFalse(self.plugin._message_debounce_command_text(event, event.message_str))
                self.assertTrue(self.plugin._message_debounce_absorb_pending_message(event, event.message_str))
                messages = self.plugin._semantic_message_buffers["private:user-1:user-1"]["messages"]
                self.assertEqual(["第一句", event.message_str], [item["text"] for item in messages])

    async def test_private_command_skips_pending_consent_and_wakeup_feedback(self) -> None:
        event = _Event("/订阅列表")
        await self._wake(event)
        self.plugin.data["users"]["user-1"]["reality_touch_pending_consent"] = {"capability": "local_audio"}
        before = deepcopy(self.plugin.data)
        self.plugin._reality_touch_apply_pending_confirmation = Mock(return_value="误确认")
        self.plugin._maybe_handle_wakeup_feedback = AsyncMock(return_value=True)
        self.plugin._reply = AsyncMock()

        self.assertFalse(await self.plugin._handle_private_message_preflight(event))

        self.plugin._reality_touch_apply_pending_confirmation.assert_not_called()
        self.plugin._maybe_handle_wakeup_feedback.assert_not_called()
        self.plugin._data_lock.__aenter__.assert_not_awaited()
        self.plugin._reply.assert_not_awaited()
        self.assertEqual(before, self.plugin.data)
        self.assertFalse(event.is_stopped())

    async def test_framework_dispatch_reaches_original_plugin_with_grouped_command_params(self) -> None:
        parent = CommandGroupFilter("bili", alias={"B站"})
        grouped = _handler(self.other, "sub_add", [CommandFilter(
            "add", alias={"添加"}, parent_command_names=parent.get_complete_command_names(),
        )])
        companion = _handler(
            self.plugin, "on_private_message",
            [EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE)],
            module="tests.companion.main",
        )
        self.plugin._bot_scope_allows_event = Mock(return_value=True)
        self.plugin._activate_persona_for_event_context = Mock(return_value=(None, ""))
        self.plugin._deactivate_persona_for_event = Mock()
        self.plugin._ensure_auto_private_user_profile = Mock(side_effect=AssertionError("指令不应进入画像创建"))
        self.plugin._reality_touch_apply_pending_confirmation = Mock(return_value="误确认")
        self.plugin._maybe_handle_wakeup_feedback = AsyncMock(return_value=True)

        for text, command, expected in (
            ("/bili_sub_list", self.command, ("sub_list", {})),
            ("/订阅列表", self.command, ("sub_list", {})),
            ("/B站 添加 12345", grouped, ("sub_add", {"uid": 12345})),
        ):
            with self.subTest(text=text):
                event = _Event(text)
                await self._wake(event, [companion, command])
                params = deepcopy(event.get_extra("handlers_parsed_params"))
                results = []
                with patch("astrbot_plugin_private_companion.main.mark_hdsi_route"), patch(
                    "astrbot_plugin_private_companion.main.record_hdsi_inbound_event", new=AsyncMock(),
                ), patch("astrbot.core.pipeline.process_stage.method.star_request.star_map", {
                    companion.handler_module_path: SimpleNamespace(name="companion"),
                    command.handler_module_path: SimpleNamespace(name="other"),
                }):
                    async for _ in StarRequestSubStage().process(event):
                        if event.get_result() is not None:
                            results.append(event.get_result().get_plain_text())

                self.assertEqual(expected, self.other.calls[-1])
                self.assertEqual(1, len(results))
                self.assertTrue(results[0].startswith("原插件"))
                self.assertEqual(params, event.get_extra("handlers_parsed_params"))
                self.assertFalse(event.is_stopped())
                self.assertFalse(event.call_llm)
                self.assertIsNone(event.plugins_name)
        self.plugin._ensure_auto_private_user_profile.assert_not_called()
        self.plugin._data_lock.__aenter__.assert_not_awaited()
        self.plugin._maybe_handle_wakeup_feedback.assert_not_called()
        self.plugin._reality_touch_apply_pending_confirmation.assert_not_called()

    async def test_group_command_skips_observation_and_reply_takeover(self) -> None:
        event = _Event("/订阅列表", private=False)
        await self._wake(event)
        self.plugin._qzone_note_event_bot = Mock()
        self.plugin._feature_enabled_or_temp_unlocked = Mock(return_value=True)
        self.plugin._group_enabled_for_event = Mock(return_value=True)
        self.plugin._group_observation_event_text = Mock(return_value=event.message_str)
        self.plugin._capture_group_observation_event = AsyncMock()
        await handle_group_message(self.plugin, event)
        self.plugin._capture_group_observation_event.assert_not_awaited()
        self.plugin._data_lock.__aenter__.assert_not_awaited()
        self.assertFalse(event.is_stopped())

    def test_legacy_command_markers_work_without_framework_metadata(self) -> None:
        for text, flags in (
            ("/help", {}), ("！帮助", {}), ("陪伴 状态", {}),
            ("status", {"is_command": True}), ("status", {"is_admin_command": True}),
        ):
            with self.subTest(text=text, flags=flags):
                self.assertTrue(self.plugin._message_debounce_command_text(SimpleNamespace(**flags), text))
        self.assertFalse(self.plugin._message_debounce_command_text(SimpleNamespace(), "普通聊天"))


if __name__ == "__main__":
    unittest.main()
