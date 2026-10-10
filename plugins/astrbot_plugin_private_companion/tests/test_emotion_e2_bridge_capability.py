from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_adapter():
    try:
        import astrbot.api  # noqa: F401
    except ImportError:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
        sys.modules.setdefault("astrbot", astrbot)
        sys.modules.setdefault("astrbot.api", api)
    package_name = "emotion_e2_bridge_capability"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.memory_companion_adapter").MemoryCompanionAdapterMixin


MemoryCompanionAdapterMixin = load_adapter()


class Bridge:
    def __init__(self) -> None:
        self.capability = object()
        self.context = object()
        self.registered = None
        self.context_calls: list[dict] = []
        self.record_calls: list[tuple[dict, object]] = []

    def register_emotion_producer(self, producer):
        self.registered = producer
        return self.capability

    def create_emotion_producer_context(self, capability, **kwargs):
        if capability is not self.capability:
            return None
        self.context_calls.append(kwargs)
        return self.context

    async def record_emotion_event(self, event, *, producer_context=None):
        if producer_context is not self.context:
            raise AssertionError("producer context is required")
        self.record_calls.append((dict(event), producer_context))
        return {"ok": True}


class EmotionE2BridgeCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self, bridge: Bridge):
        class Host(MemoryCompanionAdapterMixin):
            def _memory_companion_bridge(self):
                return bridge

            @staticmethod
            def _known_bot_self_ids() -> set[str]:
                return {"bot-1"}

        return Host()

    async def test_mirror_binds_a_live_capability_to_one_private_domain(self) -> None:
        bridge = Bridge()
        host = self.make_host(bridge)
        event = {
            "bot_id": "bot-1",
            "platform": "qq",
            "scope": "private",
            "session_id": "qq:FriendMessage:user-1",
            "actor_ref": {"kind": "user", "id": "user-1", "role": "speaker"},
            "event_type": "comfort",
        }

        await host._memory_companion_record_emotion_event(event)

        self.assertIs(bridge.registered, host)
        self.assertEqual(
            {
                "bot_id": "bot-1",
                "scope": "private",
                "platform": "qq",
                "user_id": "user-1",
                "session_id": "qq:FriendMessage:user-1",
            },
            bridge.context_calls[0],
        )
        self.assertEqual(1, len(bridge.record_calls))
        self.assertIs(bridge.context, bridge.record_calls[0][1])

    async def test_mirror_fails_closed_for_group_or_incomplete_identity(self) -> None:
        bridge = Bridge()
        host = self.make_host(bridge)
        base = {
            "bot_id": "bot-1",
            "platform": "qq",
            "scope": "private",
            "session_id": "qq:FriendMessage:user-1",
            "actor_ref": {"kind": "user", "id": "user-1", "role": "speaker"},
            "event_type": "comfort",
        }

        await host._memory_companion_record_emotion_event({**base, "scope": "group"})
        await host._memory_companion_record_emotion_event({**base, "bot_id": ""})
        await host._memory_companion_record_emotion_event({**base, "platform": "wechat"})

        self.assertEqual([], bridge.context_calls)
        self.assertEqual([], bridge.record_calls)


if __name__ == "__main__":
    unittest.main()
