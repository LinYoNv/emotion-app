# -*- coding: utf-8 -*-
"""Read model and prompt formatter for the local C3 agenda.

The write-side reconciler keeps plan, evidence, and temporal phase separate.
This module exposes purpose-specific views so callers do not accidentally use
a future plan as a current or historical fact.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from .agenda_contracts import (
        SCHEDULE_WINDOWS,
        interval_overlaps_window,
        parse_datetime,
        window_bounds,
        window_for_datetime,
    )
    from .schedule_reconciler import reconcile
except ImportError:
    from agenda_contracts import (
        SCHEDULE_WINDOWS,
        interval_overlaps_window,
        parse_datetime,
        window_bounds,
        window_for_datetime,
    )
    from schedule_reconciler import reconcile


_CURRENT_FACT_ELIGIBILITY = {"current_internal", "current_observed"}
_HISTORY_FACT_ELIGIBILITY = {"history_observed"}
_SCHEDULE_COMMITMENT_LEVELS = {"confirmed", "routine"}


def _fact_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Keep interaction evidence from being rendered as an arbitrary action."""

    result = dict(item)
    if str(item.get("evidence_kind") or "").lower() == "interaction":
        result["title"] = "与用户互动"
        result["content_granularity"] = "commitment"
        result["evidence_summary"] = "interaction only"
        for key in ("activity", "description", "scene_details", "message_seed", "raw_text"):
            result.pop(key, None)
    return result


def _entry_phase(item: dict[str, Any], now: datetime) -> str:
    phase = str(item.get("temporal_phase") or "").lower()
    if phase in {"future", "current", "past"}:
        return phase
    # Legacy entries may have been built before temporal_phase was introduced.
    try:
        start = parse_datetime(item.get("start_at") or item.get("start"), default=now)
        end = parse_datetime(item.get("end_at") or item.get("end"), default=start)
    except Exception:
        return "future"
    if end <= start:
        end = start
    current = parse_datetime(now).astimezone(start.tzinfo)
    if current < start:
        return "future"
    if current >= end:
        return "past"
    return "current"


def _is_current_fact(item: dict[str, Any], now: datetime) -> bool:
    if _entry_phase(item, now) != "current":
        return False
    eligibility = str(item.get("fact_eligibility") or "").lower()
    if eligibility in _CURRENT_FACT_ELIGIBILITY:
        return True
    # Compatibility for old observed entries.  A planned item is never
    # considered current merely because its interval contains ``now``.
    return item.get("kind") == "observed" and str(item.get("status") or "") in {"active", "partially_completed"}


def _is_history_fact(item: dict[str, Any], now: datetime) -> bool:
    if _entry_phase(item, now) != "past":
        return False
    eligibility = str(item.get("fact_eligibility") or "").lower()
    if eligibility in _HISTORY_FACT_ELIGIBILITY:
        return True
    return (
        item.get("kind") == "observed"
        and str(item.get("status") or "") == "completed"
        and str(item.get("evidence_kind") or "").lower() != "self_state_commit"
    )


def _is_schedule_item(item: dict[str, Any]) -> bool:
    if item.get("kind") not in {"planned", None} and item.get("source_kind") != "planned":
        return False
    status = str(item.get("status") or "planned").lower()
    materialization = str(item.get("materialization_state") or "none").lower()
    return status in {"planned"} and materialization != "expired"


def _is_future_schedule(item: dict[str, Any], now: datetime) -> bool:
    if not _is_schedule_item(item):
        return False
    return _entry_phase(item, now) in {"future", "current"}


def _is_commitment(item: dict[str, Any]) -> bool:
    return (
        _is_schedule_item(item)
        and str(item.get("commitment_level") or "").lower() in _SCHEDULE_COMMITMENT_LEVELS
        and str(item.get("fact_eligibility") or "schedule_commitment").lower() == "schedule_commitment"
    )


def _item_for_date(item: dict[str, Any], target_date: str, *, timezone_name: str) -> bool:
    if not target_date:
        return True
    explicit_date = str(item.get("date") or item.get("window_date") or "")[:10]
    if explicit_date == target_date:
        return True
    # A late-night window is keyed by its starting date but extends into the
    # next calendar day.  Checking all canonical windows handles that case and
    # avoids dropping 00:30 entries from a requested prior-day agenda.
    for slug, _name, _start_minute, _end_minute in SCHEDULE_WINDOWS:
        try:
            start, end = window_bounds(target_date, slug, timezone_name=timezone_name)
        except Exception:
            continue
        if interval_overlaps_window(item, start, end, timezone_name=timezone_name):
            return True
    return False


def _dedupe_entries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("entry_id") or item.get("plan_id") or item.get("activity_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(item)
    return result


def _entry_key(item: dict[str, Any]) -> str:
    return str(item.get("entry_id") or item.get("plan_id") or item.get("activity_id") or "")


def build_unified_agenda(
    *,
    plans: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    now: datetime,
    date_key: str = "",
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    result = reconcile(plans or [], activities or [], now=now)
    current_slug, current_window_date, _current_start, _current_end = window_for_datetime(
        now,
        timezone_name=timezone_name,
    )
    target_date = str(date_key or current_window_date)[:10]

    all_entries = [item for item in result["entries"] if _item_for_date(item, target_date, timezone_name=timezone_name)]
    current_facts = [_fact_projection(item) for item in all_entries if _is_current_fact(item, now)]
    history_facts = [_fact_projection(item) for item in all_entries if _is_history_fact(item, now)]
    future_raw = [item for item in all_entries if _is_future_schedule(item, now)]
    future_schedule = [_future_projection(item) for item in future_raw]
    current_ids = {
        _entry_key(item)
        for item in current_facts
    }
    history_ids = {
        _entry_key(item)
        for item in history_facts
    }
    future_ids = {
        _entry_key(item)
        for item in future_raw
    }
    schedule_commitments = [item for item in future_schedule if _is_commitment(item)]
    # ``memory_write`` is intentionally narrower than the complete agenda:
    # projections, plans, and unresolved candidates cannot become history.
    memory_write = [item for item in history_facts if item.get("fact_eligibility") in _HISTORY_FACT_ELIGIBILITY]
    current = None
    for item in sorted(current_facts, key=lambda value: str(value.get("start_at") or "")):
        if str(item.get("status") or "").lower() in {"cancelled", "unknown", "deferred", "overridden"}:
            continue
        current = item
        break

    windows: list[dict[str, Any]] = []
    for slug, _name, _start_minute, _end_minute in SCHEDULE_WINDOWS:
        start, end = window_bounds(target_date, slug, timezone_name=timezone_name)
        window_plans = [
            item for item in result["plans"] if interval_overlaps_window(item, start, end, timezone_name=timezone_name)
        ]
        window_activities = [
            item
            for item in result["activities"]
            if interval_overlaps_window(item, start, end, timezone_name=timezone_name)
        ]
        plan_ids = {str(item.get("plan_id")) for item in window_plans}
        window_reconciliations = [
            item for item in result["reconciliations"] if str(item.get("plan_id")) in plan_ids
        ]
        windows.append(
            {
                "slug": slug,
                "window": slug,
                "window_date": target_date,
                "start_at": start.isoformat(timespec="seconds"),
                "end_at": end.isoformat(timespec="seconds"),
                "planned": window_plans,
                "observed": window_activities,
                "reconciled": window_reconciliations,
                "current_fact": [_fact_projection(item) for item in window_activities if _is_current_fact(item, now)],
                "history_fact": [_fact_projection(item) for item in window_activities if _is_history_fact(item, now)],
                "future_schedule": [
                    _future_projection(item)
                    for item in window_plans
                    if _is_future_schedule(item, now)
                ],
                "schedule_commitment": [_future_projection(item) for item in window_plans if _is_commitment(item)],
            }
        )

    disclosure_trace = []
    for item in all_entries:
        disclosure_trace.append(
            {
                "entry_id": item.get("entry_id"),
                "plan_id": item.get("plan_id"),
                "activity_id": item.get("activity_id"),
                "temporal_phase": _entry_phase(item, now),
                "status": item.get("status"),
                "fact_eligibility": item.get("fact_eligibility") or "none",
                "decision": (
                    "current_fact"
                    if _entry_key(item) in current_ids
                    else "history_fact"
                    if _entry_key(item) in history_ids
                    else "future_schedule"
                    if _entry_key(item) in future_ids
                    else "diagnostic"
                ),
            }
        )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": target_date,
        "window_date": target_date,
        "current_window": current_slug if target_date == current_window_date else "",
        "current": current,
        "current_fact": _dedupe_entries(current_facts),
        "history_fact": _dedupe_entries(history_facts),
        "future_schedule": _dedupe_entries(future_schedule),
        "schedule_commitment": _dedupe_entries(schedule_commitments),
        "memory_write": _dedupe_entries(memory_write),
        "entries": all_entries,
        "plans": result["plans"],
        "activities": result["activities"],
        "matched": result["matched"],
        "reconciliations": result["reconciliations"],
        "reconciliation_candidates": result.get("reconciliation_candidates", []),
        "disclosure_trace": disclosure_trace,
        "windows": windows,
    }


def _safe_future_title(item: dict[str, Any]) -> str:
    granularity = str(item.get("content_granularity") or "").lower()
    materialization = str(item.get("materialization_state") or "").lower()
    if granularity in {"scene", "candidate"} or materialization == "candidate":
        return "临近时段的软安排"
    return str(item.get("title") or "未命名安排")[:80]


def _future_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Redact scene prose from the ordinary future-schedule projection."""

    result = dict(item)
    granularity = str(item.get("content_granularity") or "").lower()
    materialization = str(item.get("materialization_state") or "").lower()
    if granularity in {"scene", "candidate"} or materialization == "candidate":
        result["title"] = "临近时段的软安排"
        result["content_granularity"] = "candidate"
        result["materialization_state"] = "candidate"
        result["disclosure_reason"] = "scene detail withheld from ordinary future view"
        for key in (
            "activity",
            "description",
            "scene",
            "scene_details",
            "message_seed",
            "candidates",
            "chain",
            "raw",
            "raw_text",
        ):
            result.pop(key, None)
    return result


def format_agenda_context(agenda: dict[str, Any], *, max_entries: int = 8) -> str:
    """Format a safe, purpose-neutral prompt context.

    Current and historical lines come from evidence views.  Future lines are
    limited to confirmed/routine commitments; tentative scene candidates stay
    in diagnostics and cannot leak into a current/history prompt.
    """

    if not isinstance(agenda, dict):
        return ""
    date_text = agenda.get("window_date") or agenda.get("date") or "unknown"
    # Keep the established marker so existing prompt consumers and regression
    # fixtures can identify this context block.
    lines = [f"C3日程（{date_text}）"]
    current = agenda.get("current")
    if isinstance(current, dict):
        current_evidence = str(current.get("evidence_kind") or "").lower()
        current_eligibility = str(current.get("fact_eligibility") or "").lower()
        current_phase = str(current.get("temporal_phase") or "current").lower()
        if current_phase != "current":
            current = None
        elif current.get("kind") != "observed" and current_eligibility not in _CURRENT_FACT_ELIGIBILITY:
            current = None
        elif current_evidence == "none" and current_eligibility not in _CURRENT_FACT_ELIGIBILITY:
            current = None
    if isinstance(current, dict):
        lines.append(
            f"当前实际：{str(current.get('title') or '未命名')[:80]} "
            f"[{current.get('evidence_level') or 'L?'}|{current.get('status') or 'unknown'}]"
        )
    entries: list[dict[str, Any]] = []
    if isinstance(agenda.get("current_fact"), list):
        entries.extend(item for item in agenda["current_fact"] if isinstance(item, dict))
    if isinstance(agenda.get("history_fact"), list):
        entries.extend(item for item in agenda["history_fact"] if isinstance(item, dict))
    if isinstance(agenda.get("schedule_commitment"), list):
        entries.extend(item for item in agenda["schedule_commitment"] if isinstance(item, dict))
    if isinstance(agenda.get("future_schedule"), list):
        entries.extend(
            item
            for item in agenda["future_schedule"]
            if isinstance(item, dict)
            and str(item.get("commitment_level") or "tentative").lower() == "tentative"
        )
    # Compatibility with agendas built by older callers that do not expose
    # purpose-specific lists yet.
    if not entries and isinstance(agenda.get("entries"), list):
        entries.extend(
            item for item in agenda["entries"]
            if isinstance(item, dict)
            and (
                item.get("kind") == "observed"
                or str(item.get("commitment_level") or "").lower() in _SCHEDULE_COMMITMENT_LEVELS
                or (
                    str(item.get("commitment_level") or "").lower() == "tentative"
                    and str(item.get("temporal_phase") or "future").lower() in {"future", "current"}
                )
            )
        )
    for item in _dedupe_entries(entries)[: max(0, int(max_entries))]:
        is_actual = item.get("kind") == "observed"
        kind = "实际" if is_actual else (
            "可能安排" if str(item.get("commitment_level") or "").lower() == "tentative" else "安排"
        )
        title = str(item.get("title") or "未命名")[:80]
        if not is_actual:
            title = _safe_future_title(item)
        status = str(item.get("status") or "unknown")
        reason = str(item.get("reconciliation_reason") or "")
        suffix = f"；{reason}" if reason and is_actual else ""
        lines.append(f"- {kind}：{title} [{status}]{suffix}")
    if len(lines) == 1:
        lines.append("- 暂无已确认日程或可用观察")
    return "\n".join(lines)
