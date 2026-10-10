from __future__ import annotations

import ast
from pathlib import Path
import unittest

from emotion_diagnostics import build_emotion_trace_projection, emotion_trace_summary


ROOT = Path(__file__).resolve().parents[1]


class EmotionE9DiagnosticProjectionTests(unittest.TestCase):
    @staticmethod
    def _user() -> dict:
        return {
            "current_interaction": {
                "expression_band": "hurt",
                "source": "automatic",
                "load": 42.0,
                "recovery_band": "recovering",
                "operator": "raw-user-id",
                "reason": "PRIVATE CHAT",
            },
            "emotion_event_ledger": [
                {
                    "event_id": "emo-1",
                    "trace_id": "trace-1",
                    "revision": 1,
                    "origin_kind": "interaction",
                    "event_type": "hurt",
                    "intensity": 80,
                    "confidence": 0.9,
                    "status": "observed",
                    "source_rule": "direct_bot_target",
                    "occurred_at": "2026-08-05T12:00:00+08:00",
                    "actor_ref": {"kind": "user", "id": "raw-user-id", "role": "speaker"},
                    "target_ref": {"kind": "bot", "id": "raw-target-id", "role": "bot_self"},
                    "raw_text": "PRIVATE CHAT",
                },
            ],
        }

    def test_local_projection_is_redacted_and_uses_bounded_fields(self) -> None:
        user = self._user()
        user["emotion_event_ledger"][0]["source_rule"] = "PRIVATE CHAT /tmp/private"
        result = build_emotion_trace_projection(
            user,
            "trace-1",
            daily_state={
                "energy": 61,
                "affect_modulation": {
                    "schema_version": "affect_modulation.v1",
                    "valence": -0.2,
                    "arousal": 0.3,
                    "vulnerability": 0.2,
                    "confidence": 0.9,
                    "source_event_ids": ["emo-1"],
                },
            },
            state_conditions=[
                {
                    "id": "/tmp/private-afterglow",
                    "kind": "memory_afterglow",
                    "trace_id": "trace-1",
                    "source_event_id": "emo-1",
                    "source_revision": 1,
                    "energy_delta": -4,
                    "intensity": 80,
                    "start_ts": 1,
                    "end_ts": 2,
                    "half_life_seconds": 1800,
                    "label": "PRIVATE CHAT",
                    "modulation": {"valence": -0.2, "arousal": 0.3, "vulnerability": 0.2, "confidence": 0.9},
                },
            ],
            expression_decision={
                "contract": "companion_interaction_expression.v2",
                "expression_band": "hurt",
                "tone": "careful",
                "warmth": 28,
                "response_length": "brief",
                "followup": False,
                "initiative": "passive_only",
                "proactive_budget": 0,
                "tts_style": "soft",
                "pacing": "slow",
                "directness": "indirect",
                "validation_style": "acknowledge",
                "self_disclosure": "none",
                "humor_mode": "off",
                "topic_initiative": "reply_only",
                "reason_codes": ["PRIVATE CHAT"],
            },
        )

        self.assertEqual("ready", result["state"])
        self.assertTrue(result["read_only"])
        self.assertEqual("trace-1", result["trace_id"])
        self.assertEqual("unavailable", result["memory_diagnostic"]["state"])
        self.assertEqual("diagnostic_authority_unavailable", result["memory_diagnostic"]["reason_code"])
        self.assertEqual(1, len(result["events"]))
        self.assertEqual("", result["events"][0]["source_rule"])
        self.assertEqual(1, len(result["afterglow"]))
        self.assertNotIn("condition_id", result["afterglow"][0])
        self.assertEqual(
            {
                "contract", "expression_band", "tone", "warmth", "response_length", "followup",
                "initiative", "proactive_budget", "proactive_target", "tts_style", "pacing", "directness",
                "validation_style", "self_disclosure", "humor_mode", "topic_initiative", "safety_mode", "blocker",
            },
            set(result["expression_decision"]),
        )
        rendered = repr(result)
        for raw_value in ("PRIVATE CHAT", "raw-user-id", "raw-target-id", "/tmp/private"):
            self.assertNotIn(raw_value, rendered)

    def test_summary_keeps_latest_revision_for_each_trace(self) -> None:
        user = self._user()
        user["emotion_event_ledger"].append(
            {
                **user["emotion_event_ledger"][0],
                "revision": 2,
                "status": "revised",
                "occurred_at": "2026-08-05T12:01:00+08:00",
            }
        )
        summary = emotion_trace_summary(user)
        self.assertEqual(1, len(summary))
        self.assertEqual(2, summary[0]["revision"])
        self.assertEqual("revised", summary[0]["status"])

    def test_projection_allows_only_known_classifier_rule_codes(self) -> None:
        projection = build_emotion_trace_projection(self._user(), "trace-1")
        self.assertEqual("direct_bot_target", projection["events"][0]["source_rule"])

    def test_adapter_and_page_do_not_make_remote_trace_calls_or_forge_context(self) -> None:
        for path in (ROOT / "memory_companion_adapter.py", ROOT / "page_api_users_groups.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            constants = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertNotIn("get_emotion_trace_diagnostic", calls)
            self.assertNotIn("get_emotion_trace", calls)
            self.assertNotIn("is_admin", constants)


if __name__ == "__main__":
    unittest.main()
