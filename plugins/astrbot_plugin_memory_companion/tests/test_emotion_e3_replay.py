from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.emotion_event_contract import normalize_emotion_event  # noqa: E402
from tests.emotion_eval_cases import build_emotion_eval_cases, emotion_eval_fingerprint  # noqa: E402


class EmotionE3ReplayTests(unittest.TestCase):
    def test_120_case_matrix_normalizes_identically(self) -> None:
        cases = build_emotion_eval_cases()
        self.assertEqual(120, len(cases))
        self.assertEqual(emotion_eval_fingerprint(), emotion_eval_fingerprint())
        ids: set[str] = set()
        for case in cases:
            source = case["event"]
            event = normalize_emotion_event({
                "event_type": source["event_type"],
                "intensity": source["intensity"],
                "confidence": source["confidence"],
                "session_id": "qq:FriendMessage:u1",
                "dedupe_key": case["case_id"],
                "occurred_at": case["clock"],
            }, producer_plugin="emotion_eval")
            self.assertNotIn(event["event_id"], ids)
            ids.add(event["event_id"])
            self.assertEqual(source["event_type"], event["event_type"])
            self.assertEqual(source["intensity"], event["intensity"])


if __name__ == "__main__":
    unittest.main()
