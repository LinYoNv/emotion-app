# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agenda_contracts import (
    interval_overlaps_window,
    normalize_observed_activity,
    normalize_plan_item,
    window_bounds,
)
from agenda_disclosure_policy import AgendaDisclosurePolicy
from schedule_authority import ScheduleAuthorityAdapter


NOW = datetime.fromisoformat("2026-07-30T21:34:00+08:00")


class AgendaContractCanonicalTests(unittest.TestCase):
    def test_ordinary_plan_is_forced_to_intent(self) -> None:
        item = normalize_plan_item(
            {
                "title": "pull curtain",
                "source_kind": "observed",
                "status": "completed",
                "evidence_kind": "tool_action",
                "basis": ["persona"],
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
            },
            now=NOW,
        )
        self.assertEqual(item["source_kind"], "planned")
        self.assertEqual(item["status"], "planned")
        self.assertEqual(item["evidence_kind"], "none")
        self.assertEqual(item["source_refs"], [])
        self.assertEqual(item["subject_actor_id"], "bot-1")
        self.assertIn("normalizer.basis_not_source_refs", {entry["code"] for entry in item["decision_trace"]})
        self.assertEqual(item["legacy_status"], "completed")

    def test_numeric_certainty_is_not_retyped(self) -> None:
        item = normalize_observed_activity(
            {
                "title": "tool action",
                "source": "tool",
                "source_refs": ["tool-1"],
                "certainty": 0.8,
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
            },
            now=NOW,
        )
        self.assertEqual(item["certainty"], 0.8)
        self.assertEqual(item["evidence_kind"], "tool_action")

    def test_legacy_date_and_clock_fields_reach_the_correct_window(self) -> None:
        morning_start, morning_end = window_bounds("2026-07-30", "morning")
        self.assertTrue(
            interval_overlaps_window(
                {"date": "2026-07-30", "time": "09:00", "end": "10:00"},
                morning_start,
                morning_end,
            )
        )
        late_start, late_end = window_bounds("2026-07-30", "late_night")
        self.assertTrue(
            interval_overlaps_window(
                {"date": "2026-07-30", "time": "23:30", "end": "00:30"},
                late_start,
                late_end,
            )
        )


class AgendaDisclosurePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AgendaDisclosurePolicy(bot_id="bot-1", target_user_id="user-1")

    def test_future_plan_never_enters_current_or_history(self) -> None:
        future = {
            "entry_id": "future",
            "title": "scene detail",
            "source_kind": "planned",
            "status": "planned",
            "start_at": "2026-07-30T22:30:00+08:00",
            "end_at": "2026-07-30T23:00:00+08:00",
            "actor_type": "bot",
            "subject_actor_id": "bot-1",
        }
        current = self.policy.build_view({"entries": [future]}, NOW, "current_fact")
        history = self.policy.build_view({"entries": [future]}, NOW, "history_fact")
        self.assertEqual(current.entries, [])
        self.assertEqual(history.entries, [])
        self.assertIn(current.redactions[0]["reason"], {"future_plan_not_current", "planned_without_execution_evidence"})

    def test_hard_schedule_is_separate_from_execution(self) -> None:
        adapter = ScheduleAuthorityAdapter(clock=lambda: NOW)
        ref = adapter.issue_or_update(
            {
                "authority_kind": "calendar",
                "event_id": "event-1",
                "revision": "1",
                "timezone": "Asia/Shanghai",
                "updated_at": "2026-07-30T21:34:00+08:00",
                "effective_from": "2026-07-31T09:00:00+08:00",
                "effective_to": "2026-07-31T10:00:00+08:00",
            },
            "bot-1",
        )
        assert hasattr(ref, "to_plan_fields")
        hard = {
            "entry_id": "class-1",
            "title": "class",
            "source_kind": "planned",
            "status": "planned",
            **ref.to_plan_fields(),
            "start_at": "2026-07-31T09:00:00+08:00",
            "end_at": "2026-07-31T10:00:00+08:00",
            "actor_type": "bot",
            "subject_actor_id": "bot-1",
        }
        policy = AgendaDisclosurePolicy(bot_id="bot-1", schedule_authority=adapter)
        schedule = policy.build_view({"entries": [hard]}, NOW, "schedule_commitment")
        current = policy.build_view({"entries": [hard]}, NOW, "current_fact")
        self.assertEqual([entry["entry_id"] for entry in schedule.entries], ["class-1"])
        self.assertEqual(current.entries, [])

    def test_user_subject_does_not_leak_into_bot_view(self) -> None:
        user_fact = {
            "entry_id": "user-1",
            "title": "user action",
            "source_kind": "observed",
            "status": "completed",
            "evidence_kind": "external_record",
            "source_refs": ["user-record-1"],
            "start_at": "2026-07-30T20:00:00+08:00",
            "end_at": "2026-07-30T20:30:00+08:00",
            "actor_type": "interlocutor_user",
            "subject_actor_id": "user-2",
        }
        view = self.policy.build_view({"entries": [user_fact]}, NOW, "history_fact")
        self.assertEqual(view.entries, [])
        self.assertIn("subject_mismatch_user", view.redactions[0]["reasons"])

    def test_interaction_evidence_cannot_prove_unrelated_activity(self) -> None:
        activity = {
            "entry_id": "chat-evidence",
            "title": "attend class",
            "kind": "conversation",
            "source_kind": "observed",
            "status": "active",
            "evidence_kind": "interaction",
            "source_refs": ["message-1"],
            "start_at": "2026-07-30T21:30:00+08:00",
            "end_at": "2026-07-30T22:00:00+08:00",
            "actor_type": "bot",
            "subject_actor_id": "bot-1",
        }
        view = self.policy.build_view({"entries": [activity]}, NOW, "current_fact")
        self.assertEqual(view.entries, [])
        self.assertIn("interaction_scope_mismatch", view.redactions[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
