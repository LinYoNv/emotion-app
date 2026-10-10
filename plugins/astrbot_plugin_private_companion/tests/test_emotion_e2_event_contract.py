from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emotion_event_contract import (  # noqa: E402
    EMOTION_EVENT_CONTRACT_FINGERPRINT,
    EMOTION_EVENT_SCHEMA_VERSION,
    normalize_emotion_event,
)
from emotion_event_ledger import emotion_trace_from_user, record_recent_emotion_event  # noqa: E402


class EmotionE2ContractTests(unittest.TestCase):
    def test_contract_is_bounded_redacted_and_finite(self) -> None:
        event = normalize_emotion_event(
            {
                "event_type": "hurt",
                "session_id": "qq:FriendMessage:u1",
                "intensity": math.inf,
                "confidence": 9,
                "raw_text": "PRIVATE_DO_NOT_KEEP",
                "actor_ref": {"kind": "user", "id": "u1", "name": "private"},
                "reason_codes": ["hurt", "hurt", "x" * 200],
            },
            producer_plugin="private_companion",
        )
        self.assertEqual(EMOTION_EVENT_SCHEMA_VERSION, event["schema_version"])
        self.assertEqual(0.0, event["intensity"])
        self.assertEqual(1.0, event["confidence"])
        self.assertNotIn("raw_text", event)
        self.assertNotIn("name", event["actor_ref"])
        self.assertNotIn("PRIVATE_DO_NOT_KEEP", repr(event))
        self.assertTrue(EMOTION_EVENT_CONTRACT_FINGERPRINT)

    def test_recent_ledger_is_idempotent_and_traceable(self) -> None:
        user: dict = {}
        payload = {
            "event_type": "praise",
            "session_id": "qq:FriendMessage:u1",
            "dedupe_key": "message-1",
            "status": "applied",
        }
        first, created = record_recent_emotion_event(user, payload)
        replay, replay_created = record_recent_emotion_event(user, payload)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertEqual(1, len(user["emotion_event_ledger"]))
        self.assertEqual([first], emotion_trace_from_user(user, first["trace_id"]))


if __name__ == "__main__":
    unittest.main()
