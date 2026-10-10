from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion_interaction_expression import build_expression_decision  # noqa: E402
from emotion_event_contract import normalize_emotion_event  # noqa: E402
from tests.emotion_eval_cases import build_emotion_eval_cases, emotion_eval_fingerprint  # noqa: E402


EVENT_BANDS = {
    "hurt": "hurt",
    "apology": "warm",
    "comfort": "warm",
    "praise": "lively",
    "comfort_need": "warm",
    "external_negative": "warm",
    "play": "lively",
    "intimacy": "warm",
    "boundary": "avoidant",
    "neutral": "relaxed",
}


class EmotionE3ReplayTests(unittest.TestCase):
    def test_120_case_matrix_is_deterministic_and_safe(self) -> None:
        cases = build_emotion_eval_cases()
        self.assertEqual(120, len(cases))
        self.assertEqual(emotion_eval_fingerprint(), emotion_eval_fingerprint())
        for case in cases:
            event_data = case["event"]
            state = case["state"]
            event = normalize_emotion_event({
                "event_type": event_data["event_type"],
                "intensity": event_data["intensity"],
                "confidence": event_data["confidence"],
                "session_id": "qq:FriendMessage:u1",
                "dedupe_key": case["case_id"],
                "occurred_at": case["clock"],
            }, producer_plugin="emotion_eval")
            decision = build_expression_decision({
                "relationship_role": state["role"],
                "relationship_mode": state["mode"],
                "relationship_score": state["score"],
                "current_interaction": EVENT_BANDS[event["event_type"]],
                "bot_state": {"energy": state["energy"], "mood": state["mood"]},
                "schedule": {"mode": "busy" if state["busy"] else "ordinary"},
                "safety_constraints": {"contact_boundary": state["boundary"]},
            })
            if state["boundary"]:
                self.assertEqual("contact_boundary", decision.blocker)
            else:
                self.assertIsNone(decision.blocker)
            self.assertNotIn("raw_text", repr(event))


if __name__ == "__main__":
    unittest.main()
