# -*- coding: utf-8 -*-
from datetime import datetime

from schedule_authority import ScheduleAuthorityAdapter
from schedule_reconciler import reconcile
from unified_agenda import build_unified_agenda


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _trusted_timetable_fields() -> dict:
    adapter = ScheduleAuthorityAdapter(clock=lambda: _dt("2026-07-30T21:34:00+08:00"))
    ref = adapter.issue_or_update(
        {
            "namespace": "test",
            "event_id": "class-1",
            "provider": "school",
            "revision": "1",
            "authority_kind": "timetable",
            "timezone": "Asia/Shanghai",
            "updated_at": "2026-07-30T08:00:00+08:00",
            "effective_from": "2026-07-31T09:00:00+08:00",
            "effective_to": "2026-07-31T10:00:00+08:00",
            "title": "明早有课",
        },
        "bot-1",
    )
    assert hasattr(ref, "to_plan_fields")
    return ref.to_plan_fields()


def test_adapter_schedule_ref_supplies_signed_interval_to_future_view() -> None:
    result = build_unified_agenda(
        plans=[
            {
                "plan_id": "signed-class",
                "title": "明早有课",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                **_trusted_timetable_fields(),
            }
        ],
        activities=[],
        now=_dt("2026-07-30T21:34:00+08:00"),
        date_key="2026-07-31",
    )
    commitment = next(item for item in result["schedule_commitment"] if item["plan_id"] == "signed-class")
    assert commitment["start_at"] == "2026-07-31T09:00:00+08:00"
    assert commitment["end_at"] == "2026-07-31T10:00:00+08:00"


def test_raw_completed_plan_without_evidence_is_unknown_after_window() -> None:
    result = reconcile(
        [
            {
                "plan_id": "soft-1",
                "title": "睡前放松",
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
                "status": "completed",
            }
        ],
        [],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    plan = result["plans"][0]
    assert plan["status"] == "unknown"
    assert plan["temporal_phase"] == "past"
    assert plan["evidence_kind"] == "none"
    assert plan["fact_eligibility"] == "none"


def test_similarity_only_activity_is_diagnostic_and_does_not_complete_plan() -> None:
    result = reconcile(
        [
            {
                "plan_id": "soft-2",
                "title": "睡前放松刷穿搭",
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:30:00+08:00",
            }
        ],
        [
            {
                "activity_id": "chat-1",
                "title": "刷穿搭",
                "start_at": "2026-07-30T21:00:00+08:00",
                "end_at": "2026-07-30T21:10:00+08:00",
                "source": "conversation",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "unknown"
    assert result["reconciliation_candidates"][0]["status"] == "pending_verification"


def test_live_plan_chat_overlap_creates_interruption_hint_without_completion() -> None:
    result = reconcile(
        [
            {
                "plan_id": "writing-1",
                "title": "写作业",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T21:00:00+08:00",
                "end_at": "2026-07-30T22:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "chat-live-1",
                "title": "和用户持续聊天",
                "start_at": "2026-07-30T21:20:00+08:00",
                "end_at": "2026-07-30T21:40:00+08:00",
                "source": "conversation",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    hints = [item for item in result["reconciliation_candidates"] if item.get("status") == "possible_interruption"]
    assert len(hints) == 1
    assert hints[0]["plan_title"] == "写作业"
    assert result["plans"][0]["status"] == "planned"
    assert result["reconciliations"] == []


def test_future_tool_activity_cannot_materialize_a_future_plan() -> None:
    result = reconcile(
        [
            {
                "plan_id": "future-1",
                "title": "整理桌面",
                "start_at": "2026-07-30T22:30:00+08:00",
                "end_at": "2026-07-30T23:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "tool-future",
                "title": "整理桌面",
                "start_at": "2026-07-30T22:30:00+08:00",
                "end_at": "2026-07-30T23:00:00+08:00",
                "source": "tool",
                "source_refs": ["future-1"],
                "status": "completed",
                "evidence_level": "L3",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "planned"


def test_self_state_commit_is_current_only_and_never_history() -> None:
    result = reconcile(
        [
            {
                "plan_id": "internal-1",
                "title": "休息",
                "authority_kind": "state",
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "state-1",
                "title": "休息",
                "source": "self_state_commit",
                "source_refs": ["internal-1"],
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
                "status": "completed",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    assert result["plans"][0]["status"] == "unknown"
    assert result["activities"][0]["fact_eligibility"] == "none"
    assert result["reconciliations"] == []


def test_current_self_state_does_not_activate_the_plan() -> None:
    result = reconcile(
        [
            {
                "plan_id": "internal-current",
                "title": "rest",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T21:00:00+08:00",
                "end_at": "2026-07-30T22:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "state-current",
                "title": "resting state",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "source": "self_state_commit",
                "source_refs": ["internal-current"],
                "runtime_origin_refs": ["runtime:state-current"],
                "committed_at": "2026-07-30T21:30:00+08:00",
                "valid_until": "2026-07-30T21:40:00+08:00",
                "status": "active",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "planned"
    assert result["plans"][0]["fact_eligibility"] == "none"
    assert result["activities"][0]["fact_eligibility"] == "current_internal"
    assert result["reconciliations"] == []


def test_user_evidence_cannot_reconcile_a_bot_plan() -> None:
    result = reconcile(
        [
            {
                "plan_id": "bot-class",
                "title": "上课",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "user-class",
                "title": "上课",
                "subject_actor_id": "user-1",
                "source": "tool",
                "source_refs": ["bot-class"],
                "status": "completed",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        now=_dt("2026-07-30T14:00:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "unknown"


def test_calendar_commitment_reference_does_not_prove_attendance() -> None:
    result = reconcile(
        [
            {
                "plan_id": "calendar-1",
                "title": "上课",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "calendar-ref",
                "title": "上课",
                "subject_actor_id": "bot-1",
                "source": "calendar",
                "source_refs": ["calendar-1"],
                "status": "completed",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        now=_dt("2026-07-30T14:00:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "unknown"


def test_interaction_fact_is_rendered_as_interaction_not_scene_action() -> None:
    result = build_unified_agenda(
        plans=[],
        activities=[
            {
                "activity_id": "chat-now",
                "title": "刷穿搭",
                "source": "conversation",
                "start_at": "2026-07-30T21:20:00+08:00",
                "end_at": "2026-07-30T21:40:00+08:00",
                "status": "active",
            }
        ],
        now=_dt("2026-07-30T21:34:00+08:00"),
        date_key="2026-07-30",
    )
    assert result["current"]["title"] == "与用户互动"
    assert all(item.get("title") != "刷穿搭" for item in result["current_fact"])


def test_cancelled_schedule_ref_cannot_remain_confirmed() -> None:
    result = reconcile(
        [
            {
                "plan_id": "cancelled-class",
                "title": "上课",
                "authority_kind": "timetable",
                "source_refs": ["class-1"],
                "source_refs_trusted": True,
                "schedule_ref": {"state": "cancelled"},
                "start_at": "2026-07-31T09:00:00+08:00",
                "end_at": "2026-07-31T10:00:00+08:00",
            }
        ],
        [],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    plan = result["plans"][0]
    assert plan["status"] == "cancelled"
    assert plan["commitment_level"] == "tentative"
    assert plan["fact_eligibility"] == "none"


def test_cancelled_observation_cannot_reconcile_as_active() -> None:
    result = reconcile(
        [
            {
                "plan_id": "cancelled-observation",
                "title": "运行工具",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "cancelled-tool",
                "title": "运行工具",
                "source": "tool",
                "source_refs": ["cancelled-observation"],
                "status": "cancelled",
                "start_at": "2026-07-30T10:00:00+08:00",
                "end_at": "2026-07-30T11:00:00+08:00",
            }
        ],
        now=_dt("2026-07-30T14:00:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "unknown"


def test_reconciler_hides_older_valid_schedule_revision() -> None:
    adapter = ScheduleAuthorityAdapter(clock=lambda: _dt("2026-07-30T21:34:00+08:00"))
    first = adapter.issue_or_update(
        {
            "namespace": "test",
            "event_id": "revisioned-class",
            "provider": "school",
            "revision": "1",
            "authority_kind": "timetable",
            "timezone": "Asia/Shanghai",
            "updated_at": "2026-07-30T08:00:00+08:00",
            "effective_from": "2026-07-31T09:00:00+08:00",
            "effective_to": "2026-07-31T10:00:00+08:00",
        },
        "bot-1",
    )
    second = adapter.issue_or_update(
        {
            "namespace": "test",
            "event_id": "revisioned-class",
            "provider": "school",
            "revision": "2",
            "authority_kind": "timetable",
            "timezone": "Asia/Shanghai",
            "updated_at": "2026-07-30T08:05:00+08:00",
            "effective_from": "2026-07-31T11:00:00+08:00",
            "effective_to": "2026-07-31T12:00:00+08:00",
        },
        "bot-1",
    )
    assert hasattr(first, "to_plan_fields") and hasattr(second, "to_plan_fields")
    result = reconcile(
        [
            {"plan_id": "old", "title": "class", "actor_type": "bot", "subject_actor_id": "bot-1", **first.to_plan_fields()},
            {"plan_id": "new", "title": "class", "actor_type": "bot", "subject_actor_id": "bot-1", **second.to_plan_fields()},
        ],
        [],
        now=_dt("2026-07-30T21:34:00+08:00"),
    )
    by_id = {item["plan_id"]: item for item in result["plans"]}
    assert by_id["old"]["status"] == "overridden"
    assert by_id["old"]["schedule_ref_reason"] == "schedule_revision_superseded"
    assert by_id["new"]["status"] == "planned"
    assert by_id["new"]["fact_eligibility"] == "schedule_commitment"


def test_explicit_tool_evidence_populates_history_view_only_after_completion() -> None:
    payload = {
        "plans": [
            {
                "plan_id": "done-1",
                "title": "运行工具",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
            },
            {
                "plan_id": "future-2",
                "title": "明早有课",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-31T09:00:00+08:00",
                "end_at": "2026-07-31T10:00:00+08:00",
                **_trusted_timetable_fields(),
            },
        ],
        "activities": [
            {
                "activity_id": "done-activity",
                "title": "运行工具",
                "actor_type": "bot",
                "subject_actor_id": "bot-1",
                "start_at": "2026-07-30T20:30:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
                "source": "tool",
                "source_refs": ["done-1"],
                "status": "completed",
                "evidence_level": "L3",
            }
        ],
        "now": _dt("2026-07-30T21:34:00+08:00"),
    }
    result = build_unified_agenda(
        **payload,
        date_key="2026-07-30",
    )
    assert any(item["plan_id"] == "done-1" for item in result["history_fact"])
    assert not any(item.get("plan_id") == "future-2" for item in result["history_fact"])
    tomorrow = build_unified_agenda(**payload, date_key="2026-07-31")
    assert any(item["plan_id"] == "future-2" for item in tomorrow["schedule_commitment"])


def test_legacy_clock_plan_keeps_cross_midnight_phase_current() -> None:
    plan = {
        "plan_id": "midnight-1",
        "title": "late rest",
        "date": "2026-07-30",
        "time": "23:30",
        "end": "00:30",
        "actor_type": "bot",
        "subject_actor_id": "bot-1",
    }
    for now, expected_phase in (
        ("2026-07-30T23:45:00+08:00", "current"),
        ("2026-07-31T00:15:00+08:00", "current"),
        ("2026-07-31T00:31:00+08:00", "past"),
    ):
        result = reconcile([plan], [], now=_dt(now))
        assert result["plans"][0]["temporal_phase"] == expected_phase
        assert result["plans"][0]["status"] == ("unknown" if expected_phase == "past" else "planned")


def test_bare_clock_plan_without_date_uses_now_date_for_interval_resolution() -> None:
    result = reconcile(
        [{"plan_id": "midnight-no-date", "title": "late rest", "time": "23:30", "end": "00:30"}],
        [],
        now=_dt("2026-07-30T23:45:00+08:00"),
    )
    assert result["plans"][0]["temporal_phase"] == "current"
