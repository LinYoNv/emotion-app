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


class EmotionE4DeliveryAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    def make_bridge(self, store: MemoryStore) -> tuple[MemoryCompanionBridge, object]:
        companion = object()
        plugin = SimpleNamespace(
            store=store,
            config=SimpleNamespace(
                bool=lambda key, default=False: (
                    True
                    if key == "private_companion_bridge.cross_window_emotional_continuity_enabled"
                    else default
                )
            ),
            context=SimpleNamespace(
                get_all_stars=lambda: [
                    SimpleNamespace(
                        star_cls=companion,
                        root_dir_name="astrbot_plugin_private_companion",
                        name="PrivateCompanion",
                        activated=True,
                    )
                ]
            ),
        )
        bridge = MemoryCompanionBridge(plugin)
        capability = bridge.register_emotion_producer(companion)
        self.assertIsNotNone(capability)
        return bridge, capability

    @staticmethod
    def delivery_context(
        bridge: MemoryCompanionBridge,
        capability: object,
        *,
        bot_id: str = "bot-1",
        platform: str = "qq",
        scope: str = "private",
        user_id: str = "user-1",
        session_id: str = "qq:session-a",
        allow_cross_window: bool = False,
    ) -> object | None:
        return bridge.create_emotion_delivery_context(
            capability,
            bot_id=bot_id,
            scope=scope,
            platform=platform,
            user_id=user_id,
            session_id=session_id,
            allow_cross_window=allow_cross_window,
        )

    async def add_event(
        self,
        store: MemoryStore,
        *,
        suffix: str,
        bot_id: str = "bot-1",
        platform: str = "qq",
        scope: str = "private",
        user_id: str = "user-1",
        session_id: str = "qq:session-a",
    ) -> dict:
        event = normalize_emotion_event(
            {
                "event_id": f"event-{suffix}",
                "trace_id": f"trace-{suffix}",
                "origin_kind": "memory_recall",
                "bot_id": bot_id,
                "scope": scope,
                "platform": platform,
                "session_id": session_id,
                "actor_ref": {"kind": "user", "id": user_id, "role": "speaker"},
                "target_ref": {"kind": "bot", "id": bot_id, "role": "bot_self"},
                "event_type": "warm_memory",
                "dedupe_key": suffix,
            },
            producer_plugin="memory_companion",
        )
        return await store.upsert_emotion_event(event)

    async def test_delivery_and_ack_are_bound_to_one_private_identity_domain(self) -> None:
        store = self.make_store()
        bridge, capability = self.make_bridge(store)
        same = await self.add_event(store, suffix="same")
        cross_window = await self.add_event(store, suffix="same-user-window", session_id="qq:session-b")
        other_user = await self.add_event(store, suffix="other-user", user_id="user-2", session_id="qq:session-c")
        other_bot = await self.add_event(store, suffix="other-bot", bot_id="bot-2", session_id="qq:session-d")
        other_platform = await self.add_event(store, suffix="other-platform", platform="wechat", session_id="wechat:session-e")
        group_scope = await self.add_event(store, suffix="group", scope="group", session_id="qq:session-f")

        denied = await bridge.list_emotion_events(consumer_id="private_companion.daily_state")
        self.assertEqual("forbidden", denied["state"])
        denied_ack = await bridge.ack_emotion_events([{"event_id": same["event_id"], "revision": 1}])
        self.assertEqual(0, denied_ack["acked"])

        same_context = self.delivery_context(bridge, capability)
        self.assertIsNotNone(same_context)
        same_page = await bridge.list_emotion_events(delivery_context=same_context)
        self.assertEqual([same["event_id"]], [item["event_id"] for item in same_page["events"]])

        cross_context = self.delivery_context(
            bridge,
            capability,
            allow_cross_window=True,
        )
        self.assertIsNotNone(cross_context)
        cross_page = await bridge.list_emotion_events(delivery_context=cross_context)
        self.assertEqual(
            {same["event_id"], cross_window["event_id"]},
            {item["event_id"] for item in cross_page["events"]},
        )
        rendered = repr(cross_page)
        for raw_identifier in ("session-a", "session-b", "user-1", "bot-1", "actor_ref", "target_ref"):
            self.assertNotIn(raw_identifier, rendered)

        for context, forbidden_event in (
            (
            self.delivery_context(bridge, capability, user_id="user-2", session_id="qq:session-c"),
                same,
            ),
            (
                self.delivery_context(bridge, capability, bot_id="bot-2", session_id="qq:session-d"),
                same,
            ),
            (
                self.delivery_context(bridge, capability, platform="wechat", session_id="wechat:session-e"),
                same,
            ),
        ):
            self.assertIsNotNone(context)
            page = await bridge.list_emotion_events(delivery_context=context)
            self.assertNotIn(forbidden_event["event_id"], [item["event_id"] for item in page["events"]])

        self.assertIsNone(self.delivery_context(bridge, capability, scope="group"))
        self.assertNotIn(group_scope["event_id"], [item["event_id"] for item in cross_page["events"]])
        foreign_context = self.delivery_context(bridge, capability, user_id="user-2", session_id="qq:session-c")
        foreign_ack = await bridge.ack_emotion_events(
            [{"event_id": same["event_id"], "revision": same["revision"]}],
            delivery_context=foreign_context,
        )
        self.assertEqual(0, foreign_ack["acked"])
        own_ack = await bridge.ack_emotion_events(
            [{"event_id": same["event_id"], "revision": same["revision"]}],
            delivery_context=same_context,
        )
        self.assertEqual(1, own_ack["acked"])

    async def test_contexts_are_immutable_and_domain_creation_fails_closed(self) -> None:
        store = self.make_store()
        bridge, capability = self.make_bridge(store)
        context = self.delivery_context(bridge, capability)
        self.assertIsNotNone(context)
        with self.assertRaises(AttributeError):
            context.user_id = "user-2"
        with self.assertRaises(AttributeError):
            context.allow_cross_window = True

        self.assertIsNone(
            self.delivery_context(
                bridge,
                capability,
                session_id="session-without-platform-prefix",
            )
        )
        bridge._plugin.config = SimpleNamespace(bool=lambda _key, default=False: default)
        self.assertIsNone(
            self.delivery_context(
                bridge,
                capability,
                allow_cross_window=True,
            )
        )

    async def test_keyset_pagination_skips_foreign_flood_and_reaches_every_local_event(self) -> None:
        store = self.make_store()
        bridge, capability = self.make_bridge(store)
        local_ids: set[str] = set()
        for index in range(1002):
            local = normalize_emotion_event(
                {
                    "event_id": f"local-{index:04d}",
                    "trace_id": f"local-trace-{index:04d}",
                    "origin_kind": "memory_recall",
                    "bot_id": "bot-1",
                    "scope": "private",
                    "platform": "qq",
                    "session_id": "qq:session-a",
                    "actor_ref": {"kind": "user", "id": "user-1", "role": "speaker"},
                    "target_ref": {"kind": "bot", "id": "bot-1", "role": "bot_self"},
                    "event_type": "warm_memory",
                    "occurred_at": "2026-08-05T00:00:00+00:00",
                    "dedupe_key": f"local-{index:04d}",
                },
                producer_plugin="memory_companion",
            )
            store._upsert_emotion_event_sync(local)
            local_ids.add(local["event_id"])
        for index in range(1002):
            foreign = normalize_emotion_event(
                {
                    "event_id": f"foreign-{index:04d}",
                    "trace_id": f"foreign-trace-{index:04d}",
                    "origin_kind": "memory_recall",
                    "bot_id": "bot-1",
                    "scope": "private",
                    "platform": "qq",
                    "session_id": "qq:foreign-session",
                    "actor_ref": {"kind": "user", "id": "user-2", "role": "speaker"},
                    "target_ref": {"kind": "bot", "id": "bot-1", "role": "bot_self"},
                    "event_type": "warm_memory",
                    "occurred_at": "2030-08-05T00:00:00+00:00",
                    "dedupe_key": f"foreign-{index:04d}",
                },
                producer_plugin="memory_companion",
            )
            store._upsert_emotion_event_sync(foreign)

        delivery_context = self.delivery_context(bridge, capability)
        self.assertIsNotNone(delivery_context)
        cursor = ""
        received: set[str] = set()
        for _ in range(60):
            page = await bridge.list_emotion_events(
                delivery_context=delivery_context,
                cursor=cursor,
                limit=20,
            )
            ids = {item["event_id"] for item in page["events"]}
            self.assertFalse(ids - local_ids)
            self.assertFalse(received & ids)
            received.update(ids)
            if not page["has_more"]:
                break
            self.assertTrue(page["next_cursor"])
            cursor = page["next_cursor"]
        else:
            self.fail("keyset pagination did not finish")
        self.assertEqual(local_ids, received)


if __name__ == "__main__":
    unittest.main()
