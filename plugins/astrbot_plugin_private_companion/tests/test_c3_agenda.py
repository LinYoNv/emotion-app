# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from activity_capture import ActivityCapture
from agenda_contracts import (
    SCHEDULE_WINDOWS,
    interval_overlaps_window,
    migrate_store,
    normalize_observed_activity,
    normalize_plan_item,
    stable_id,
    window_bounds,
    window_for_datetime,
    window_for_minutes,
    window_for_plan_minutes,
)
from agenda_runtime import AgendaRuntimeMixin
if str(PACKAGE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR.parent))
from astrbot_plugin_private_companion.daily_state import DailyStateMixin
from schedule_reconciler import reconcile
from unified_agenda import format_agenda_context


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_contract_has_five_complete_windows_and_all_boundaries() -> None:
    assert len(SCHEDULE_WINDOWS) == 5
    assert sum((end - start) % 1440 for _slug, _name, start, end in SCHEDULE_WINDOWS) == 1440
    expected = {
        0: "late_night",
        30: "late_night",
        5 * 60 + 59: "late_night",
        6 * 60: "morning",
        11 * 60: "noon",
        14 * 60 + 30: "afternoon",
        18 * 60: "evening",
        21 * 60: "late_night",
    }
    for minute, slug in expected.items():
        assert window_for_minutes(minute) == slug


@pytest.mark.parametrize(
    ("value", "slug", "window_date"),
    [
        ("2026-07-30T21:00:00+08:00", "late_night", "2026-07-30"),
        ("2026-07-30T00:30:00+08:00", "late_night", "2026-07-29"),
        ("2026-07-30T05:59:00+08:00", "late_night", "2026-07-29"),
        ("2026-07-30T06:00:00+08:00", "morning", "2026-07-30"),
        ("2026-07-30T11:00:00+08:00", "noon", "2026-07-30"),
        ("2026-07-30T14:30:00+08:00", "afternoon", "2026-07-30"),
        ("2026-07-30T18:00:00+08:00", "evening", "2026-07-30"),
        ("2026-07-30T21:00:00+08:00", "late_night", "2026-07-30"),
    ],
)
def test_datetime_boundaries_and_cross_midnight(value: str, slug: str, window_date: str) -> None:
    actual_slug, actual_date, start, end = window_for_datetime(dt(value))
    assert (actual_slug, actual_date) == (slug, window_date)
    assert start < dt(value) < end or start == dt(value)
    assert (end - start).total_seconds() == (9 * 60 * 60 if slug == "late_night" else {"morning": 5, "noon": 3.5, "afternoon": 3.5, "evening": 3}[slug] * 3600)


def test_plan_minutes_and_interval_overlap_are_cross_midnight_safe() -> None:
    assert window_for_plan_minutes("2026-07-30", 30) == ("late_night", "2026-07-29")
    assert window_for_plan_minutes("2026-07-30", 21 * 60) == ("late_night", "2026-07-30")
    start, end = window_bounds("2026-07-29", "late_night")
    assert interval_overlaps_window({"start_at": "2026-07-30T00:30:00+08:00", "end_at": "2026-07-30T01:00:00+08:00"}, start, end)
    assert not interval_overlaps_window({"start_at": "2026-07-30T06:00:00+08:00", "end_at": "2026-07-30T06:10:00+08:00"}, start, end)


def test_activity_capture_aggregates_by_conversation_bucket_and_deduplicates_refs() -> None:
    capture = ActivityCapture(window_minutes=30, min_sustained_messages=3)
    base = dt("2026-07-30T10:01:00+08:00")
    assert capture.capture_message(text="我们继续开发插件", event_time=base, source_ref="m1", conversation_id="c1") is None
    assert capture.capture_message(text="继续排查测试", event_time=base.replace(minute=10), source_ref="m2", conversation_id="c1") is None
    activity = capture.capture_message(text="已经跑完测试", event_time=base.replace(minute=20), source_ref="m3", conversation_id="c1", participant="user")
    assert activity is not None
    assert activity["source_refs"] == ["m1", "m2", "m3"]
    assert set(activity["participants"]) == {"user", "bot"}
    same = capture.capture_message(text="重复投递", event_time=base.replace(minute=20), source_ref="m3", conversation_id="c1")
    assert same is not None
    assert same["activity_id"] == activity["activity_id"]
    assert same["source_refs"] == activity["source_refs"]


def test_hard_fact_keeps_evidence_visibility_and_source_refs() -> None:
    fact = ActivityCapture().capture_hard_fact(
        title="运行工具",
        start_at=dt("2026-07-30T12:00:00+08:00"),
        end_at=dt("2026-07-30T12:05:00+08:00"),
        source="tool",
        source_refs=["tool-1"],
        participants=["bot"],
        visibility="private",
    )
    assert fact["source_kind"] == "observed"
    assert fact["evidence_level"] == "L3"
    assert fact["visibility"] == "private"
    assert fact["source_refs"] == ["tool-1"]


def test_reconcile_prioritizes_refs_and_marks_past_plan_unknown_without_evidence() -> None:
    plans = [
        normalize_plan_item({"plan_id": "p1", "title": "开发插件", "start_at": "2026-07-30T10:00:00+08:00", "end_at": "2026-07-30T11:00:00+08:00", "source_refs": ["ticket-1"]}),
        normalize_plan_item({"plan_id": "p2", "title": "午休", "start_at": "2026-07-30T12:00:00+08:00", "end_at": "2026-07-30T13:00:00+08:00"}),
    ]
    activities = [
        normalize_observed_activity({"title": "实际开发插件", "start_at": "2026-07-30T10:10:00+08:00", "end_at": "2026-07-30T10:30:00+08:00", "source_refs": ["ticket-1"]}),
    ]
    result = reconcile(plans, activities, now=dt("2026-07-30T14:00:00+08:00"))
    by_id = {item["plan_id"]: item for item in result["plans"]}
    assert by_id["p1"]["status"] == "active"
    assert by_id["p1"]["reconciled_activity_ids"]
    assert by_id["p2"]["status"] == "unknown"
    assert all(item.get("source_kind") != "observed" for item in result["plans"])


def test_normalizers_preserve_contract_fields_and_stable_id() -> None:
    raw = {"title": "x", "status": "completed", "version": 4, "source_refs": ["a"], "visibility": "private", "certainty": "high", "evidence_level": "L3"}
    normalized = normalize_plan_item(raw)
    assert normalized["source_kind"] == "planned"
    assert normalized["status"] == "planned"
    assert normalized["legacy_status"] == "completed"
    assert normalized["version"] == 4
    assert normalized["source_refs"] == ["a"]
    assert stable_id("x", {"b": 2, "a": 1}) == stable_id("x", {"a": 1, "b": 2})
    json.dumps(normalized, ensure_ascii=False)


class Host(AgendaRuntimeMixin):
    def __init__(self, data: dict | None = None):
        self.data = data or {}
        self.calendar_timezone = "Asia/Shanghai"
        self.fixed_now = dt("2026-07-30T22:00:00+08:00")

    def _calendar_now(self) -> datetime:
        return self.fixed_now


class PlanParserHost(DailyStateMixin):
    daily_plan_item_count = 24
    enable_skill_growth_simulation = False
    enable_skill_growth_schedule_influence = False

    def _environment_now(self) -> datetime:
        return dt("2026-07-30T08:00:00+08:00")

    def _align_plan_text_with_skill_bounds(self, value: str) -> str:
        return value

    def _sanitize_daily_plan_social_fact_text(self, value: str, **_kwargs) -> str:
        return value

    def _soften_destructive_daily_plan_text(self, value: str) -> str:
        return value

    def _deemphasize_state_report_preamble(self, value: str, **_kwargs) -> str:
        return value

    def _sanitize_empty_daily_plan_message_seed(self, value: str) -> str:
        return value


def test_runtime_snapshot_is_idempotent_and_context_is_formatted() -> None:
    host = Host({"daily_plan": {"date": "2026-07-30", "items": [{"time": "21:00", "end": "22:00", "activity": "阅读"}]}})
    first = host._agenda_snapshot_window(date_key="2026-07-30", window="late_night")
    second = host._agenda_snapshot_window(date_key="2026-07-30", window="late_night")
    assert first["snapshot_id"] == second["snapshot_id"] == "agenda_snapshot:2026-07-30:late_night"
    assert len(host.data["window_snapshots"]) == 1
    assert len(host.data["agenda_reconciliation_history"]) == 1
    assert "C3日程" in format_agenda_context(host._agenda_build())


def test_current_context_does_not_promote_stale_school_plan_during_vacation() -> None:
    host = Host(
        {
            "daily_plan": {
                "date": "2026-07-30",
                "items": [{"time": "08:00", "end": "09:00", "activity": "上学"}],
            },
            "calendar_events": [{
                "kind": "period",
                "calendar_id": "summer",
                "title": "暑假",
                "start_date": "2026-07-01",
                "end_date": "2026-08-31",
            }],
        }
    )
    host.fixed_now = dt("2026-07-30T08:30:00+08:00")
    assert host._agenda_current_context_item(now=host.fixed_now) is None


def test_calendar_context_does_not_delete_generic_plan_rows() -> None:
    base = {
        "daily_plan": {"date": "2026-07-30", "items": [{"time": "08:00", "end": "09:00", "activity": "上学"}]},
        "calendar_events": [
            {"kind": "period", "calendar_id": "summer", "title": "暑假", "start_date": "2026-07-01", "end_date": "2026-08-31"},
            {"kind": "event", "calendar_id": "exam", "title": "考试", "date": "2026-07-30", "start_time": "10:00", "end_time": "11:00"},
        ],
    }
    host = Host(base)
    host.fixed_now = dt("2026-07-30T08:30:00+08:00")
    assert host._agenda_current_context_item(now=host.fixed_now) is None

    host.data["daily_plan"]["items"][0]["activity"] = "参加考试"
    assert host._agenda_calendar_allows_item(host.data["daily_plan"]["items"][0], now=host.fixed_now)
    host.data["daily_plan"]["items"][0]["activity"] = "补课"
    assert host._agenda_calendar_allows_item(host.data["daily_plan"]["items"][0], now=host.fixed_now)


def test_plan_parser_accepts_common_aliases_and_time_ranges() -> None:
    host = PlanParserHost()
    payload = """```json
    {"tasks": [{"start_time": "09:00-10:30", "title": "整理资料"},
                {"time": "14点", "activity": "散步", "until": "14:40"}]}
    ```"""
    items = host._parse_plan_items(payload)
    assert [(item["time"], item["end"], item["activity"]) for item in items] == [
        ("09:00", "10:30", "整理资料"),
        ("14:00", "14:40", "散步"),
    ]


def test_disclosure_view_is_memoized_until_plan_or_activity_changes() -> None:
    host = Host({"daily_plan": {"date": "2026-07-30", "items": [{"time": "21:00", "end": "22:00", "activity": "阅读"}]}})
    with patch.object(host, "_agenda_build", wraps=host._agenda_build) as build:
        first = host._agenda_disclosure_view("future_schedule", now=host.fixed_now)
        second = host._agenda_disclosure_view("future_schedule", now=host.fixed_now)
        assert first == second
        assert build.call_count == 1

        host.data["daily_plan"]["items"][0]["activity"] = "写作"
        host.data["daily_plan"]["items"][0]["changed_at"] = "21:10"
        host._agenda_disclosure_view("future_schedule", now=host.fixed_now)
        assert build.call_count == 2

        host.data["observed_activities"] = [{
            "activity_id": "activity-1",
            "title": "临时聊天",
            "start_at": "2026-07-30T21:15:00+08:00",
            "end_at": "2026-07-30T21:20:00+08:00",
            "version": 1,
        }]
        host._agenda_disclosure_view("future_schedule", now=host.fixed_now)
        assert build.call_count == 3


def test_current_interruption_context_is_diagnostic_only() -> None:
    host = Host(
        {
            "daily_plan": {
                "date": "2026-07-30",
                "items": [{
                    "time": "21:00",
                    "end": "22:00",
                    "activity": "写作业",
                    "subject_actor_id": "bot_self",
                    "actor_type": "bot",
                }],
            },
            "observed_activities": [{
                "activity_id": "chat-live-1",
                "title": "和用户持续聊天",
                "start_at": "2026-07-30T21:20:00+08:00",
                "end_at": "2026-07-30T21:40:00+08:00",
                "source": "conversation",
                "subject_actor_id": "bot_self",
                "actor_type": "bot",
            }],
        }
    )
    host.fixed_now = dt("2026-07-30T21:34:00+08:00")
    context = host._agenda_current_interruption_context(now=host.fixed_now)
    assert context["active"] is True
    assert context["plan_title"] == "写作业"
    assert context["confidence"] == "low"
    agenda = host._agenda_build(now=host.fixed_now)
    assert agenda["plans"][0]["status"] == "planned"
    assert agenda["reconciliations"] == []


def test_closed_windows_use_window_date_and_maintenance_is_local_only() -> None:
    host = Host({})
    host.fixed_now = dt("2026-07-30T06:00:00+08:00")
    closed = host._agenda_closed_windows(host.fixed_now)
    assert ("2026-07-29", "late_night") in closed
    settled = host._agenda_maintenance_tick()
    assert any(item["window_date"] == "2026-07-29" for item in settled)
    assert "world_gateway" not in host.data


def test_migrate_store_is_additive_for_old_json() -> None:
    old = {"users": {"u": {"name": "keep"}}, "daily_plan": {"date": "2026-07-30", "items": [{"time": "10:00", "activity": "旧计划"}]}}
    migrated, changed = migrate_store(old)
    assert changed is True
    assert migrated["users"] == {"u": {"name": "keep"}}
    assert migrated["daily_plan"]["items"][0]["source_kind"] == "planned"
    assert migrated["agenda_version"] == 1
    json.dumps(migrated, ensure_ascii=False)
