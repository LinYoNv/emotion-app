# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.core_store import CoreStoreMixin
from astrbot_plugin_private_companion.conversation_prompt_section import prompt_section
from astrbot_plugin_private_companion.main import PrivateCompanionExtensionAPI


class _Plugin:
    bot_name = "小星"
    target_platform = "aiocqhttp"

    def __init__(self, self_ids):
        self._self_ids = set(self_ids)
        self._external_realtime_activities = {}
        self._external_realtime_continuity = {}
        self.data = {"users": {"10001": {"nickname": "流星"}}}

    def _known_bot_self_ids(self):
        return set(self._self_ids)

    def _build_companion_scene_snapshot(self, user):
        return {"relationship": {"name": (user or {}).get("nickname", "")}}

    @staticmethod
    def _format_companion_scene_snapshot(snapshot, *, purpose="prompt"):
        return f"场景：{snapshot['relationship']['name']}；用途：{purpose}"

    @staticmethod
    def _format_companion_scene_snapshot_prompt_section(snapshot, *, purpose="prompt"):
        return prompt_section(
            key="scene.snapshot",
            title="陪伴场景快照",
            source="scene_context",
            content=f"场景：{snapshot['relationship']['name']}；用途：{purpose}",
        )


class RealtimeExtensionAPITests(unittest.TestCase):
    def test_unique_qq_identity_can_supply_avatar(self) -> None:
        api = PrivateCompanionExtensionAPI(_Plugin({"12345678"}))

        identity = api.get_bot_identity()

        self.assertEqual("12345678", identity["selected_id"])
        self.assertEqual("12345678", identity["qq_id"])
        self.assertIn("nk=12345678", identity["avatar"]["remote_url"])

    def test_multiple_bot_accounts_are_not_guessed(self) -> None:
        api = PrivateCompanionExtensionAPI(_Plugin({"12345678", "22345678"}))

        identity = api.get_bot_identity()

        self.assertEqual("", identity["selected_id"])
        self.assertEqual("", identity["qq_id"])
        self.assertTrue(identity["ambiguous"])

    def test_realtime_context_includes_active_shared_activity(self) -> None:
        api = PrivateCompanionExtensionAPI(_Plugin({"12345678"}))
        api.notify_external_activity_started(
            "together:room",
            user_id="10001",
            kind="shared_watch",
            label="正在一起看《测试影片》",
            source_plugin="astrbot_plugin_together_companion",
        )

        context = api.get_realtime_context("10001", purpose="together")

        self.assertIn("正在一起看《测试影片》", context["prompt"])
        self.assertEqual("shared_watch", context["external_activity"]["kind"])
        self.assertTrue(api.notify_external_activity_ended("together:room"))
        self.assertEqual({}, api.get_external_activity(user_id="10001"))

    def test_short_term_continuity_preserves_speaker_attribution_and_public_view(self) -> None:
        api = PrivateCompanionExtensionAPI(_Plugin({"12345678"}))

        recorded = api.record_external_realtime_continuity(
            "10001",
            summary="小星：我想吃汤圆；主要用户：我去找店，到店再打给你",
            public_summary="正在和主要用户约会",
            facts=["小星提出想吃汤圆", "主要用户答应去找店"],
        )

        self.assertGreater(recorded["expires_at"], recorded["updated_at"] + 21000)
        private = api.get_external_realtime_continuity(user_id="10001", public=False)
        public = api.get_external_realtime_continuity(user_id="10001", public=True)
        self.assertIn("小星：我想吃汤圆", private["summary"])
        self.assertEqual("正在和主要用户约会", public["summary"])
        self.assertNotIn("汤圆", public["summary"])
        self.assertEqual([], public["facts"])


class RealtimeVoiceExtensionAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_voice_api_forwards_to_plugin_helper(self) -> None:
        plugin = _Plugin({"12345678"})
        plugin._realtime_voice_config = lambda: {
            "available": True,
            "voice_language": "ja",
            "browser_language": "ja-JP",
        }
        plugin._synthesize_realtime_voice = AsyncMock(
            return_value={"available": True, "audio_path": "voice.wav"}
        )
        api = PrivateCompanionExtensionAPI(plugin)
        provider = object()

        self.assertEqual("ja-JP", api.get_realtime_voice_config()["browser_language"])
        result = await api.synthesize_realtime_voice(
            "你好",
            tts_provider=provider,
            source="together_companion",
            play_local=False,
        )

        self.assertEqual("voice.wav", result["audio_path"])
        plugin._synthesize_realtime_voice.assert_awaited_once_with(
            "你好",
            tts_provider=provider,
            provider_settings=None,
            source="together_companion",
            play_local=False,
        )


class _IdentitySourceHarness(CoreStoreMixin):
    pass


class _BotScopeHarness(CoreStoreMixin):
    def __init__(self) -> None:
        self.bot_scope_mode = "all"
        self.bot_scope_ids = []
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(
                get_insts=lambda: [
                    SimpleNamespace(
                        self_id="bot-a",
                        bot=SimpleNamespace(_wsr_api_clients={"bot-a": object()}),
                        meta=lambda: SimpleNamespace(id="adapter-a", name="Adapter A"),
                    ),
                    SimpleNamespace(
                        self_id="bot-b",
                        bot=SimpleNamespace(_wsr_api_clients={"bot-b": object()}),
                        meta=lambda: SimpleNamespace(id="adapter-b", name="Adapter B"),
                    ),
                ]
            )
        )

    @staticmethod
    def _event_self_id(event) -> str:
        return str(getattr(event, "self_id", "") or "")


class BotScopeTests(unittest.TestCase):
    @staticmethod
    def _event(bot_id: str, adapter_id: str):
        return SimpleNamespace(
            self_id=bot_id,
            adapter_instance_id=adapter_id,
            unified_msg_origin=f"{adapter_id}:FriendMessage:user",
            message_obj=None,
        )

    def test_all_mode_accepts_both_bots(self) -> None:
        harness = _BotScopeHarness()

        self.assertTrue(harness._bot_scope_allows_event(self._event("bot-a", "adapter-a")))
        self.assertTrue(harness._bot_scope_allows_event(self._event("bot-b", "adapter-b")))

    def test_allowlist_accepts_self_id_or_adapter_id(self) -> None:
        harness = _BotScopeHarness()
        harness.bot_scope_mode = "allowlist"
        harness.bot_scope_ids = ["bot-a"]

        self.assertTrue(harness._bot_scope_allows_event(self._event("bot-a", "adapter-a")))
        self.assertTrue(harness._bot_scope_allows_umo("adapter-a:FriendMessage:user"))
        self.assertFalse(harness._bot_scope_allows_event(self._event("bot-b", "adapter-b")))
        self.assertFalse(harness._bot_scope_allows_umo("adapter-b:FriendMessage:user"))

        harness.bot_scope_ids = ["adapter-b"]
        self.assertTrue(harness._bot_scope_allows_event(self._event("bot-b", "adapter-b")))

    def test_denylist_and_empty_list_semantics(self) -> None:
        harness = _BotScopeHarness()
        harness.bot_scope_mode = "denylist"
        harness.bot_scope_ids = ["bot-b"]

        self.assertTrue(harness._bot_scope_allows_umo("adapter-a:FriendMessage:user"))
        self.assertFalse(harness._bot_scope_allows_umo("adapter-b:FriendMessage:user"))

        harness.bot_scope_ids = []
        self.assertTrue(harness._bot_scope_allows_umo("adapter-b:FriendMessage:user"))
        harness.bot_scope_mode = "allowlist"
        self.assertFalse(harness._bot_scope_allows_umo("adapter-a:FriendMessage:user"))


class BotIdentitySourceTests(unittest.TestCase):
    def test_onebot_connection_id_is_used_instead_of_internal_client_uuid(self) -> None:
        harness = _IdentitySourceHarness()
        harness.context = SimpleNamespace(
            platform_manager=SimpleNamespace(
                platform_insts=[
                    SimpleNamespace(
                        client_self_id="test-internal-client-id",
                        bot=SimpleNamespace(_wsr_api_clients={"900000001": object()}),
                    )
                ]
            )
        )

        self.assertEqual({"900000001"}, harness._known_bot_self_ids())


if __name__ == "__main__":
    unittest.main()
