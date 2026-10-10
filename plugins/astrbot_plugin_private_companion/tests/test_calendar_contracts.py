# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from agenda_contracts import migrate_store
from agenda_runtime import AgendaRuntimeMixin
from calendar_contracts import (
    advance_calendar_lifecycle,
    calendar_candidate_from_record,
    calendar_lifecycle_summary,
    detect_calendar_conflicts,
    expand_calendar_records,
    merge_calendar_evidence,
    normalize_calendar_record,
    normalize_calendar_evidence_chain,
    resolve_calendar_snapshot,
    resolve_calendar_timeline,
)


def test_normalize_period_and_recurrence_use_stable_json_fields() -> None:
    period = normalize_calendar_record(
        {
            "type": "interval",
            "title": "暑假",
            "start_date": "2026-07-01",
            "end_date": "2026-08-31",
            "subject_actor_id": "bot_self",
        }
    )
    rule = normalize_calendar_record(
        {
            "kind": "rule",
            "title": "工作日上学",
            "start_date": "2026-09-01",
            "frequency": "weekly",
            "weekdays": ["周一", "wed"],
        }
    )
    assert period["kind"] == "period"
    assert period["end_date"] == "2026-08-31"
    assert rule["kind"] == "recurrence"
    assert rule["by_weekday"] == [0, 2]
    assert rule["calendar_id"]


def test_weekly_recurrence_expands_only_selected_weekdays() -> None:
    rule = {
        "kind": "recurrence",
        "calendar_id": "school",
        "title": "上学",
        "start_date": "2026-09-01",  # Tuesday
        "frequency": "weekly",
        "by_weekday": [0, 2, 4],
        "start_time": "08:00",
        "end_time": "16:00",
    }
    rows = expand_calendar_records([rule], "2026-09-01", "2026-09-07")
    assert [row["occurrence_date"] for row in rows] == ["2026-09-02", "2026-09-04", "2026-09-07"]
    assert rows[0]["start_at"].startswith("2026-09-02T08:00:00")


def test_start_at_is_converted_to_calendar_timezone_and_count_limits_occurrences() -> None:
    event = normalize_calendar_record(
        {
            "kind": "event",
            "calendar_id": "utc-event",
            "title": "跨时区事件",
            "start_at": "2026-08-20T23:30:00Z",
            "end_at": "2026-08-21T00:30:00Z",
        },
        timezone_name="Asia/Shanghai",
    )
    assert event["start_date"] == "2026-08-21"
    assert event["start_time"].startswith("07:30")

    rows = expand_calendar_records(
        [
            {
                "kind": "recurrence",
                "calendar_id": "limited",
                "title": "限次提醒",
                "start_date": "2026-08-20",
                "frequency": "daily",
                "count": 2,
            }
        ],
        "2026-08-20",
        "2026-08-25",
    )
    assert [row["occurrence_date"] for row in rows] == ["2026-08-20", "2026-08-21"]

    weekly = expand_calendar_records(
        [{"kind": "recurrence", "calendar_id": "weekly-limit", "title": "每两周", "start_date": "2026-08-03", "frequency": "weekly", "interval": 2, "by_weekday": [0], "count": 2}],
        "2026-08-03",
        "2026-09-01",
    )
    assert [row["occurrence_date"] for row in weekly] == ["2026-08-03", "2026-08-17"]


def test_timed_multi_day_event_is_one_continuous_instance_and_boolean_text_is_parsed() -> None:
    record = normalize_calendar_record(
        {
            "kind": "event",
            "calendar_id": "conference",
            "title": "跨日会议",
            "start_at": "2026-08-20T23:30:00Z",
            "end_at": "2026-08-22T01:30:00Z",
            "all_day": "false",
        },
        timezone_name="Asia/Shanghai",
    )
    assert record["date"] == "2026-08-21"
    assert record["start_date"] == "2026-08-21"
    assert record["end_date"] == "2026-08-22"
    assert record["all_day"] is False

    rows = expand_calendar_records([record], "2026-08-21", "2026-08-22")
    assert len(rows) == 1
    assert rows[0]["occurrence_date"] == "2026-08-21"
    assert rows[0]["start_at"].startswith("2026-08-21T07:30:00")
    assert rows[0]["end_at"].startswith("2026-08-22T09:30:00")

    assert resolve_calendar_snapshot([record], "2026-08-22")["events"][0]["calendar_id"] == "conference"


def test_explicit_record_timezone_is_used_for_timestamp_normalization() -> None:
    record = normalize_calendar_record(
        {
            "kind": "event",
            "calendar_id": "new-york",
            "title": "纽约会议",
            "timezone": "America/New_York",
            "start_at": "2026-08-20T23:30:00Z",
            "end_at": "2026-08-21T00:30:00Z",
        },
        timezone_name="Asia/Shanghai",
    )
    assert record["timezone"] == "America/New_York"
    assert record["start_date"] == "2026-08-20"
    assert record["start_time"].startswith("19:30")
    assert record["end_time"].startswith("20:30")


def test_snapshot_applies_cancel_exception_and_reports_cross_entry_conflict() -> None:
    records = [
        {
            "kind": "period",
            "calendar_id": "summer-break",
            "title": "暑假",
            "start_date": "2026-07-01",
            "end_date": "2026-08-31",
            "priority": 600,
        },
        {
            "kind": "event",
            "calendar_id": "makeup-school",
            "title": "补课",
            "date": "2026-08-20",
            "start_time": "08:00",
            "end_time": "16:00",
            "priority": 700,
        },
        {
            "kind": "exception",
            "calendar_id": "cancel-makeup",
            "target_id": "makeup-school",
            "date": "2026-08-20",
            "action": "cancel",
        },
    ]
    snapshot = resolve_calendar_snapshot(records, "2026-08-20")
    assert [row["calendar_id"] for row in snapshot["events"]] == ["summer-break"]
    assert snapshot["applied_exceptions"] == ["cancel-makeup"]
    assert not snapshot["has_conflicts"]

    conflict = resolve_calendar_snapshot(
        [
            records[0],
            {**records[1], "calendar_id": "exam", "title": "考试", "priority": 600},
        ],
        "2026-08-20",
    )
    assert conflict["has_conflicts"]
    assert conflict["conflicts"][0]["unresolved"] is True


def test_detect_conflicts_prefers_explicit_event_priority() -> None:
    rows = expand_calendar_records(
        [
            {"kind": "period", "calendar_id": "vacation", "title": "假期", "start_date": "2026-08-20", "end_date": "2026-08-20"},
            {"kind": "event", "calendar_id": "appointment", "title": "预约", "date": "2026-08-20", "start_time": "10:00", "end_time": "11:00", "priority": 900},
        ],
        "2026-08-20",
    )
    conflicts = detect_calendar_conflicts(rows)
    assert len(conflicts) == 1
    assert conflicts[0]["winner_id"] == "appointment"
    assert conflicts[0]["unresolved"] is False


def test_snapshot_keeps_phase_and_rhythm_visible_during_vacation() -> None:
    snapshot = resolve_calendar_snapshot(
        [
            {
                "kind": "period",
                "calendar_id": "summer",
                "title": "暑假",
                "start_date": "2026-07-01",
                "end_date": "2026-08-31",
            },
            {
                "kind": "recurrence",
                "calendar_id": "school",
                "title": "工作日上学",
                "start_date": "2026-07-01",
                "frequency": "weekly",
                "by_weekday": [0, 1, 2, 3, 4],
            },
        ],
        "2026-07-30",
    )
    all_events = {item["calendar_id"]: item for item in snapshot["events"]}
    effective_events = {item["calendar_id"]: item for item in snapshot["effective_events"]}
    assert all_events["school"]["calendar_effective"] is True
    assert all_events["school"].get("overridden_by") is None
    assert "summer" in effective_events
    assert "school" in effective_events
    assert snapshot["has_conflicts"]

    timeline = resolve_calendar_timeline(
        [
            {
                "kind": "period",
                "calendar_id": "summer",
                "title": "暑假",
                "start_date": "2026-07-01",
                "end_date": "2026-08-31",
            },
            {
                "kind": "recurrence",
                "calendar_id": "school",
                "title": "工作日上学",
                "start_date": "2026-07-01",
                "frequency": "weekly",
                "by_weekday": [0, 1, 2, 3, 4],
            },
        ],
        "2026-07-30",
    )
    assert timeline["current_phase"][0]["title"] == "暑假"
    assert timeline["rhythms"][0]["title"] == "工作日上学"
    assert timeline["continuity"]["certainty"] == "confirmed"


def test_exception_add_uses_its_own_source_and_reschedule_moves_occurrence() -> None:
    records = [
        {
            "kind": "event",
            "calendar_id": "appointment",
            "title": "预约",
            "date": "2026-08-20",
            "start_time": "10:00",
            "end_time": "11:00",
        },
        {
            "kind": "exception",
            "calendar_id": "move-appointment",
            "target_id": "appointment",
            "date": "2026-08-20",
            "new_date": "2026-08-22",
            "action": "reschedule",
        },
    ]
    assert resolve_calendar_snapshot(records, "2026-08-20")["events"] == []
    moved = resolve_calendar_snapshot(records, "2026-08-22")
    assert [item["occurrence_date"] for item in moved["events"]] == ["2026-08-22"]

    added = resolve_calendar_snapshot(
        [
            {
                "kind": "exception",
                "calendar_id": "add-extra",
                "target_id": "appointment",
                "date": "2026-08-20",
                "action": "add",
                "title": "临时安排",
            },
            records[0],
        ],
        "2026-08-20",
    )
    assert added["events"][0]["source_calendar_id"] == "add-extra:added"

    standalone = normalize_calendar_record(
        {
            "kind": "exception",
            "calendar_id": "add-standalone",
            "date": "2026-08-20",
            "action": "add",
        }
    )
    assert standalone["target_id"] == ""
    assert standalone["title"] == "临时安排"


def test_tentative_exception_does_not_override_confirmed_event_and_cross_midnight_is_visible_next_day() -> None:
    tentative = resolve_calendar_snapshot(
        [
            {"kind": "event", "calendar_id": "class", "title": "课程", "date": "2026-08-20", "start_time": "10:00", "end_time": "11:00"},
            {"kind": "exception", "calendar_id": "maybe-cancel", "target_id": "class", "title": "暂定取消", "date": "2026-08-20", "action": "cancel", "status": "tentative"},
        ],
        "2026-08-20",
    )
    assert [item["calendar_id"] for item in tentative["events"]] == ["class"]
    assert tentative["applied_exceptions"] == []

    late = {"kind": "event", "calendar_id": "late", "title": "跨午夜值守", "date": "2026-08-20", "start_time": "23:00", "end_time": "01:00"}
    next_day = resolve_calendar_snapshot([late], "2026-08-21")
    assert next_day["events"][0]["calendar_id"] == "late"
    assert next_day["events"][0]["occurrence_date"] == "2026-08-20"

    tentative_event = resolve_calendar_snapshot(
        [
            {"kind": "recurrence", "calendar_id": "school", "title": "上学", "start_date": "2026-08-20", "frequency": "daily"},
            {"kind": "event", "calendar_id": "maybe-exam", "title": "考试（暂定）", "date": "2026-08-20", "status": "tentative", "start_time": "10:00", "end_time": "11:00"},
        ],
        "2026-08-20",
    )
    school = next(item for item in tentative_event["events"] if item["calendar_id"] == "school")
    assert school["calendar_effective"] is True


def test_migrate_store_adds_calendar_sections_without_dropping_legacy_data() -> None:
    data, changed = migrate_store({"daily_plan": {"date": "2026-08-20", "items": []}, "important_dates": ["legacy"]})
    assert changed
    assert data["important_dates"] == ["legacy"]
    assert data["calendar_events"] == []
    assert data["calendar_rules"] == []
    assert data["calendar_exceptions"] == []
    assert data["calendar_candidates"] == []


class _CalendarHost(AgendaRuntimeMixin):
    calendar_timezone = "Asia/Shanghai"

    def __init__(self) -> None:
        self.data = {}
        self.fixed_now = datetime.fromisoformat("2026-08-20T12:00:00+08:00")

    def _calendar_now(self) -> datetime:
        return self.fixed_now


def test_runtime_calendar_upsert_and_snapshot_are_separate_from_daily_plan() -> None:
    host = _CalendarHost()
    saved: list[set[str]] = []
    host._schedule_data_save = lambda *, sections: saved.append(set(sections))
    host._agenda_upsert_calendar_record(
        {"kind": "period", "calendar_id": "summer", "title": "暑假", "start_date": "2026-08-01", "end_date": "2026-08-31"}
    )
    host._agenda_upsert_calendar_record(
        {"kind": "event", "calendar_id": "visit", "title": "出门", "date": "2026-08-20", "start_time": "15:00", "end_time": "16:00"}
    )
    snapshot = host._agenda_calendar_snapshot("2026-08-20")
    assert [item["calendar_id"] for item in snapshot["events"]] == ["summer", "visit"]
    assert {"calendar_events"} in saved
    assert host.data.get("daily_plan") is None


def test_calendar_candidate_keeps_inference_tentative_and_deduplicates_evidence() -> None:
    now = datetime.fromisoformat("2026-08-20T12:00:00+08:00")
    evidence = {
        "source_type": "message",
        "message_id": "msg-1",
        "quote": "下周一去医院",
        "observed_at": "2026-08-20T11:58:00+08:00",
        "confidence": 82,
        "actor": "user-1",
    }
    candidate = calendar_candidate_from_record(
        {"kind": "event", "calendar_id": "hospital", "title": "去医院", "date": "2026-08-24"},
        evidence=evidence,
        confidence=82,
        now=now,
    )
    assert candidate["status"] == "tentative"
    assert candidate["commitment_level"] == "tentative"
    assert candidate["lifecycle_state"] == "candidate"
    assert candidate["confidence"] == 0.82
    assert len(candidate["evidence"]) == 1
    assert candidate["source_refs"] == ["msg-1"]
    merged = merge_calendar_evidence(candidate, evidence, now=now)
    assert len(merged["evidence"]) == 1
    assert normalize_calendar_evidence_chain([evidence, evidence], now=now)[0]["source_id"] == "msg-1"
    assert normalize_calendar_record(
        {"kind": "event", "title": "候选", "date": "2026-08-24", "lifecycle_state": "candidate", "status": "confirmed"}
    )["status"] == "tentative"


def test_calendar_lifecycle_confirmation_activation_and_completion_are_auditable() -> None:
    now = datetime.fromisoformat("2026-08-20T12:00:00+08:00")
    candidate = calendar_candidate_from_record(
        {"kind": "event", "calendar_id": "appointment", "title": "预约", "date": "2026-08-21"},
        evidence={"source_id": "message-1", "quote": "明天预约"},
        now=now,
    )
    confirmed = advance_calendar_lifecycle(
        candidate,
        {"action": "confirm", "evidence": {"source_id": "message-2", "quote": "确认了"}},
        now=now,
    )
    assert confirmed["lifecycle_state"] == "confirmed"
    assert confirmed["status"] == "confirmed"
    assert len(confirmed["lifecycle_history"]) == 2
    assert confirmed["lifecycle_history"][-1]["from_state"] == "candidate"
    active = advance_calendar_lifecycle(confirmed, "started", now=now)
    assert active["lifecycle_state"] == "active"
    completed = advance_calendar_lifecycle(active, "done", now=now)
    assert completed["lifecycle_state"] == "completed"
    assert completed["status"] == "expired"
    summary = calendar_lifecycle_summary(completed)
    assert summary["lifecycle_state"] == "completed"
    assert summary["evidence_count"] == 2
    assert summary["last_transition"]["to_state"] == "completed"
