from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.bridge import MemoryCompanionBridge  # noqa: E402
from core.emotion_event_contract import (  # noqa: E402
    EMOTION_EVENT_CONTRACT_FINGERPRINT,
    normalize_emotion_event,
)
from core.store import MemoryStore  # noqa: E402


class EmotionE2EventStoreTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    async def test_event_revisions_are_independent_from_memory_retrieval(self) -> None:
        store = self.make_store()
        first = normalize_emotion_event(
            {
                "event_type": "hurt",
                "session_id": "qq:FriendMessage:u1",
                "dedupe_key": "m1",
                "trace_id": "trace-1",
                "revision": 1,
            },
            producer_plugin="private_companion",
        )
        second = {**first, "revision": 2, "status": "revised", "event_type": "neutral", "correction_of": first["event_id"]}
        await store.upsert_emotion_event(first)
        await store.upsert_emotion_event(first)
        await store.upsert_emotion_event(second)
        trace = await store.get_emotion_trace("trace-1")
        self.assertEqual([1, 2], [item["revision"] for item in trace])
        self.assertEqual(0, store._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        self.assertEqual(2, store._conn.execute("SELECT COUNT(*) FROM emotion_events").fetchone()[0])

    def make_attested_bridge(self, store: MemoryStore) -> tuple[MemoryCompanionBridge, object]:
        companion = object()
        context = SimpleNamespace(
            get_all_stars=lambda: [
                SimpleNamespace(
                    star_cls=companion,
                    root_dir_name="astrbot_plugin_private_companion",
                    name="PrivateCompanion",
                    activated=True,
                )
            ]
        )
        service = SimpleNamespace(store=store, context=context)
        bridge = MemoryCompanionBridge(service)
        capability = bridge.register_emotion_producer(companion)
        self.assertIsNotNone(capability)
        producer_context = bridge.create_emotion_producer_context(
            capability,
            bot_id="bot-1",
            scope="private",
            platform="qq",
            user_id="user-1",
            session_id="qq:FriendMessage:user-1",
        )
        self.assertIsNotNone(producer_context)
        return bridge, producer_context

    async def test_bridge_requires_attested_producer_and_redacts_trace(self) -> None:
        store = self.make_store()
        bridge, producer_context = self.make_attested_bridge(store)
        denied = await bridge.record_emotion_event({"event_type": "comfort"})
        self.assertEqual("forbidden", denied["state"])

        event = await bridge.record_emotion_event({
            "producer_plugin": "untrusted_plugin",
            "origin_kind": "memory_recall",
            "bot_id": "other-bot",
            "scope": "group",
            "platform": "other-platform",
            "actor_ref": {"kind": "user", "id": "other-user", "role": "speaker"},
            "target_ref": {"kind": "bot", "id": "other-bot", "role": "bot_self"},
            "event_type": "comfort",
            "session_id": "qq:FriendMessage:other-user",
            "dedupe_key": "m2",
            "trace_id": "trace-2",
            "raw_text": "PRIVATE",
        }, producer_context=producer_context)
        self.assertEqual("private_companion", event["producer_plugin"])
        self.assertEqual("interaction", event["origin_kind"])
        self.assertEqual("bot-1", event["bot_id"])
        self.assertEqual("user-1", event["actor_ref"]["id"])

        raw_trace = await bridge.get_emotion_trace("trace-2")
        self.assertEqual("forbidden", raw_trace["state"])

        page = SimpleNamespace(plugin=bridge._plugin)
        page_capability = bridge.bind_emotion_page_api(page)
        admin_context = bridge.create_emotion_admin_context(
            page_capability,
            bot_id="bot-1",
            scope="private",
            session_id="qq:FriendMessage:user-1",
        )
        trace = await bridge.get_emotion_trace("trace-2", requester_context=admin_context)
        self.assertEqual("ready", trace["state"])
        self.assertEqual(event["event_id"], trace["items"][0]["event_id"])
        rendered = repr(trace)
        for secret in ("PRIVATE", "user-1", "qq:FriendMessage:user-1"):
            self.assertNotIn(secret, rendered)
        self.assertTrue(EMOTION_EVENT_CONTRACT_FINGERPRINT)

    async def test_legacy_cross_window_read_reports_migration_requirement(self) -> None:
        bridge = MemoryCompanionBridge(SimpleNamespace())
        state = bridge.get_recent_emotional_state()
        self.assertEqual("migration_required", state["state"])
        self.assertEqual("delivery_context_required", state["error_code"])
        self.assertEqual(0, state["total"])


if __name__ == "__main__":
    unittest.main()
