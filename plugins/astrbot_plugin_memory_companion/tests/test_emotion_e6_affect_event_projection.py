from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.affect_modulation import normalize_affect_modulation  # noqa: E402
from core.emotion_event_contract import normalize_emotion_event  # noqa: E402
from core.store import MemoryStore  # noqa: E402


class EmotionE6AffectEventProjectionTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> MemoryStore:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = MemoryStore(Path(temp_dir.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store

    async def test_delivery_contains_bounded_event_delta_without_mood_command(self) -> None:
        store = self.make_store()
        event = normalize_emotion_event({
            "event_id": "emo-affect",
            "origin_kind": "memory_recall",
            "bot_id": "bot-1",
            "scope": "private",
            "platform": "qq",
            "event_type": "warm_memory",
            "session_id": "session-a",
            "actor_ref": {"kind": "user", "id": "user-1", "role": "speaker"},
            "target_ref": {"kind": "bot", "id": "bot-1", "role": "bot_self"},
            "valence_hint": 2.0,
            "arousal_hint": 0.6,
            "vulnerability_hint": 0.4,
            "confidence": 0.8,
            "dedupe_key": "affect",
        }, producer_plugin="memory_companion")
        await store.upsert_emotion_event(event)
        page = await store.list_emotion_event_deliveries(
            consumer_id="private_companion.daily_state",
            bot_id="bot-1",
            scope="private",
            platform="qq",
            user_id="user-1",
            session_id="session-a",
            limit=5,
        )
        delivered = page["events"][0]
        self.assertEqual(1.0, delivered["affect_modulation"]["valence"])
        self.assertEqual(["emo-affect"], delivered["affect_modulation"]["source_event_ids"])
        self.assertNotIn("mood_hint", delivered)
        self.assertNotIn("memory_id", delivered)

    def test_old_or_nonfinite_values_degrade_to_neutral(self) -> None:
        result = normalize_affect_modulation({"valence": "1", "arousal": float("nan")})
        self.assertEqual(0.0, result["valence"])
        self.assertEqual(0.0, result["arousal"])


if __name__ == "__main__":
    unittest.main()
