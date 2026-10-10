from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.emotion_event_contract import normalize_emotion_event  # noqa: E402
from core.store import MemoryStore  # noqa: E402


class EmotionE4AfterglowDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    @staticmethod
    def delivery_domain(*, session_id: str, allow_cross_window: bool = False) -> dict:
        return {
            "consumer_id": "private_companion.daily_state",
            "bot_id": "bot-1",
            "scope": "private",
            "platform": "qq",
            "user_id": "user-1",
            "session_id": session_id,
            "allow_cross_window": allow_cross_window,
        }

    async def add_event(
        self,
        store: MemoryStore,
        *,
        session_id: str,
        suffix: str,
        expires_at: str = "",
    ) -> dict:
        event = normalize_emotion_event({
            "event_id": f"emo-{suffix}",
            "trace_id": f"trace-{suffix}",
            "origin_kind": "memory_recall",
            "bot_id": "bot-1",
            "scope": "private",
            "platform": "qq",
            "session_id": session_id,
            "actor_ref": {"kind": "user", "id": "user-1", "role": "speaker"},
            "target_ref": {"kind": "bot", "id": "bot-1", "role": "bot_self"},
            "event_type": "warm_memory",
            "applied_energy_delta": 3.5,
            "valence_hint": 0.4,
            "dedupe_key": suffix,
            "expires_at": expires_at,
        }, producer_plugin="memory_companion")
        return await store.upsert_emotion_event(event)

    async def test_list_retries_until_ack_and_returns_redacted_projection(self) -> None:
        store = self.make_store()
        event = await self.add_event(store, session_id="session-a", suffix="a")
        first = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a"), limit=5
        )
        retry = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a"), limit=5
        )
        self.assertEqual([event["event_id"]], [item["event_id"] for item in first["events"]])
        self.assertEqual(first["events"], retry["events"])
        self.assertNotIn("memory_id", repr(first))
        self.assertNotIn("actor_ref", repr(first))
        self.assertNotIn("session-a", repr(first))
        ack = await store.ack_emotion_event_deliveries(
            event_refs=[{"event_id": event["event_id"], "revision": 1}],
            **self.delivery_domain(session_id="session-a"),
        )
        self.assertEqual(1, ack["acked"])
        empty = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a"), limit=5
        )
        self.assertEqual([], empty["events"])

    async def test_cross_window_delivery_stays_in_same_identity_domain(self) -> None:
        store = self.make_store()
        event_a = await self.add_event(store, session_id="session-a", suffix="a")
        event_b = await self.add_event(store, session_id="session-b", suffix="b")
        page = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a", allow_cross_window=True), limit=5
        )
        self.assertEqual(
            {event_a["event_id"], event_b["event_id"]},
            {item["event_id"] for item in page["events"]},
        )

    async def test_event_id_cannot_be_revised_from_another_identity_domain(self) -> None:
        store = self.make_store()
        original = await self.add_event(store, session_id="session-a", suffix="shared")
        foreign_revision = normalize_emotion_event({
            **original,
            "revision": 2,
            "session_id": "session-b",
            "actor_ref": {"kind": "user", "id": "user-2", "role": "speaker"},
            "status": "revised",
        }, producer_plugin="memory_companion")

        with self.assertRaisesRegex(ValueError, "another identity domain"):
            await store.upsert_emotion_event(foreign_revision)

        rows = store._conn.execute(
            "SELECT revision, session_id FROM emotion_events WHERE event_id=?",
            (original["event_id"],),
        ).fetchall()
        self.assertEqual([(1, "session-a")], [tuple(row) for row in rows])

    async def test_legacy_cross_domain_high_revision_does_not_hide_original_event(self) -> None:
        store = self.make_store()
        original = await self.add_event(store, session_id="session-a", suffix="legacy-shared")
        store._conn.execute(
            """
            INSERT INTO emotion_events(
                event_id, revision, trace_id, producer_plugin, origin_kind, platform,
                bot_id, scope, session_id, actor_ref, target_ref, quoted_target_ref,
                event_type, intensity, confidence, valence_hint, arousal_hint,
                vulnerability_hint, source_rule, occurred_at, expires_at, dedupe_key,
                payload_hash, privacy_level, applied_interaction, applied_energy_delta,
                correction_of, status, reason_codes, created_at
            )
            SELECT
                event_id, 999, trace_id, producer_plugin, origin_kind, platform,
                bot_id, scope, session_id, json_set(actor_ref, '$.id', 'user-2'),
                target_ref, quoted_target_ref, event_type, intensity, confidence,
                valence_hint, arousal_hint, vulnerability_hint, source_rule, occurred_at,
                expires_at, dedupe_key, payload_hash, privacy_level, applied_interaction,
                applied_energy_delta, event_id, 'revised', reason_codes, created_at
            FROM emotion_events
            WHERE event_id=? AND revision=1
            """,
            (original["event_id"],),
        )
        store._conn.commit()

        original_page = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a"), limit=1
        )
        foreign_page = await store.list_emotion_event_deliveries(
            consumer_id="private_companion.daily_state",
            bot_id="bot-1",
            scope="private",
            platform="qq",
            user_id="user-2",
            session_id="session-a",
            limit=1,
        )
        summary = await store.get_emotion_trace_summary(
            bot_id="bot-1",
            scope="private",
            session_id="session-a",
            limit=5,
        )

        self.assertEqual([(original["event_id"], 1)], [
            (item["event_id"], item["revision"]) for item in original_page["events"]
        ])
        self.assertEqual([(original["event_id"], 999)], [
            (item["event_id"], item["revision"]) for item in foreign_page["events"]
        ])
        self.assertIn(
            (original["event_id"], 1),
            [(item["event_id"], item["revision"]) for item in summary["items"]],
        )

    async def test_expired_or_malformed_event_is_never_delivered_or_acknowledged(self) -> None:
        store = self.make_store()
        now = datetime.now(timezone.utc)
        expired = await self.add_event(
            store,
            session_id="session-a",
            suffix="expired",
            expires_at=(now - timedelta(seconds=1)).isoformat(),
        )
        malformed = await self.add_event(
            store,
            session_id="session-a",
            suffix="malformed",
            expires_at="not-an-iso-time",
        )
        delivered_at = now.isoformat()
        store._conn.execute(
            """
            INSERT INTO emotion_event_deliveries(
                event_id, revision, consumer_id, attempts,
                first_delivered_at, last_delivered_at, acked_at
            ) VALUES(?,?,?,1,?,?,'')
            """,
            (
                expired["event_id"],
                expired["revision"],
                "private_companion.daily_state",
                delivered_at,
                delivered_at,
            ),
        )
        store._conn.commit()
        page = await store.list_emotion_event_deliveries(
            **self.delivery_domain(session_id="session-a"), limit=5
        )
        self.assertEqual([], page["events"])
        ack = await store.ack_emotion_event_deliveries(
            event_refs=[
                {"event_id": expired["event_id"], "revision": expired["revision"]},
                {"event_id": malformed["event_id"], "revision": malformed["revision"]},
            ],
            **self.delivery_domain(session_id="session-a"),
        )
        self.assertEqual(0, ack["acked"])


if __name__ == "__main__":
    unittest.main()
