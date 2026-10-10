from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bridge import MemoryCompanionBridge  # noqa: E402
from core.emotion_event_contract import normalize_emotion_event  # noqa: E402
from core.store import MemoryStore  # noqa: E402


class EmotionE9TraceQueryTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    async def add_event(self, store: MemoryStore, *, event_id: str, trace_id: str, bot_id: str, session_id: str) -> None:
        await store.upsert_emotion_event(normalize_emotion_event({
            "event_id": event_id,
            "trace_id": trace_id,
            "origin_kind": "memory_recall",
            "bot_id": bot_id,
            "scope": "private",
            "platform": "qq",
            "session_id": session_id,
            "event_type": "warm_memory",
            "actor_ref": {"kind": "user", "id": "raw-user-id", "role": "speaker"},
            "target_ref": {"kind": "bot", "id": bot_id, "role": "bot_self"},
            "dedupe_key": event_id,
            "raw_text": "PRIVATE CHAT /tmp/secret token=abc",
        }, producer_plugin="memory_companion"))

    @staticmethod
    def admin_context(bridge: MemoryCompanionBridge, *, bot_id: str, scope: str, session_id: str):
        page = SimpleNamespace(plugin=bridge._plugin)
        capability = bridge.bind_emotion_page_api(page)
        assert capability is not None
        context = bridge.create_emotion_admin_context(
            capability,
            bot_id=bot_id,
            scope=scope,
            session_id=session_id,
        )
        assert context is not None
        return context

    async def test_admin_scope_filter_redaction_and_delivery_status(self) -> None:
        store = self.make_store()
        await self.add_event(store, event_id="e1", trace_id="trace-shared", bot_id="b1", session_id="s1")
        await self.add_event(store, event_id="e2", trace_id="trace-shared", bot_id="b2", session_id="s2")
        await store.list_emotion_event_deliveries(
            consumer_id="private_companion.daily_state",
            bot_id="b1",
            scope="private",
            platform="qq",
            user_id="raw-user-id",
            session_id="s1",
            limit=5,
        )
        bridge = MemoryCompanionBridge(type("Plugin", (), {"store": store})())

        denied = await bridge.get_emotion_trace_diagnostic("trace-shared", {"is_admin": True})
        self.assertEqual("forbidden", denied["state"])
        result = await bridge.get_emotion_trace_diagnostic(
            "trace-shared",
            self.admin_context(bridge, bot_id="b1", scope="private", session_id="s1"),
        )
        self.assertEqual(1, len(result["items"]))
        item = result["items"][0]
        self.assertEqual("e1", item["event_id"])
        self.assertEqual(12, len(item["actor"]["id_hash"]))
        self.assertEqual(1, item["deliveries"][0]["attempts"])
        rendered = repr(result)
        for secret in ("PRIVATE CHAT", "/tmp/secret", "token=abc", "raw-user-id"):
            self.assertNotIn(secret, rendered)

    async def test_summary_is_hard_limited_and_paginated_over_1000_events(self) -> None:
        store = self.make_store()
        for index in range(1005):
            await self.add_event(store, event_id=f"e{index}", trace_id=f"t{index}", bot_id="b1", session_id="s1")
        bridge = MemoryCompanionBridge(type("Plugin", (), {"store": store})())
        first = await bridge.get_emotion_trace_summary(
            self.admin_context(bridge, bot_id="b1", scope="private", session_id="s1"),
            limit=1000,
        )
        self.assertEqual(100, len(first["items"]))
        self.assertTrue(first["has_more"])
        second = await bridge.get_emotion_trace_summary(
            self.admin_context(bridge, bot_id="b1", scope="private", session_id="s1"),
            cursor=first["next_cursor"],
            limit=100,
        )
        self.assertEqual(100, len(second["items"]))
        self.assertNotEqual(first["items"][0]["event_id"], second["items"][0]["event_id"])


if __name__ == "__main__":
    unittest.main()
