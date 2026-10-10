# -*- coding: utf-8 -*-
"""Deterministic plan/actual reconciliation for the local C3 agenda.

The reconciler is deliberately conservative.  A plan describes a future
commitment only; clock time, title similarity, confidence, or an LLM supplied
status cannot turn it into an execution fact.  Only an explicitly linked and
compatible observation can update the plan's C3 status.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

try:
    from .agenda_contracts import (
        agenda_entry_from_activity,
        agenda_entry_from_plan,
        normalize_observed_activity,
        normalize_plan_item,
        parse_datetime,
        stable_id,
        validate_structured_schedule_ref,
    )
except ImportError:
    from agenda_contracts import (
        agenda_entry_from_activity,
        agenda_entry_from_plan,
        normalize_observed_activity,
        normalize_plan_item,
        parse_datetime,
        stable_id,
        validate_structured_schedule_ref,
    )


EVIDENCE_KINDS = {
    "none",
    "interaction",
    "self_state_commit",
    "tool_action",
    "external_record",
    "external_commitment",
}
HARD_SCHEDULE_AUTHORITIES = {"calendar", "timetable", "roster", "appointment", "user_confirmation"}
EXECUTION_EVIDENCE_KINDS = {"tool_action", "external_record"}
CHAT_EVIDENCE_KINDS = {"interaction"}
PRESERVED_PLAN_STATUSES = {"planned", "cancelled", "deferred", "overridden", "unknown"}
STATUS_ALIASES = {
    "canceled": "cancelled",
    "postponed": "deferred",
    "changed": "overridden",
    "rescheduled": "overridden",
    "revoked": "cancelled",
}
CHAT_HINTS = {
    "chat",
    "conversation",
    "interaction",
    "talk",
    "message",
    "聊天",
    "对话",
    "陪聊",
    "交流",
    "沟通",
    "说话",
    "继续聊",
}
INTERNAL_HINTS = {"rest", "sleep", "study", "休息", "睡觉", "学习", "陪伴", "聊天"}


def _trace_event(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _tokens(value: Any) -> set[str]:
    text = _normalized_text(value)
    if not text:
        return set()
    tokens = {text}
    tokens.update(text[index : index + 2] for index in range(max(0, len(text) - 1)))
    tokens.update(text[index : index + 3] for index in range(max(0, len(text) - 2)))
    return {token for token in tokens if len(token) >= 2}


def _interval(item: dict[str, Any], now: datetime) -> tuple[datetime | None, datetime | None]:
    def _value(keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return None

    start_value = _value(("start_at", "start", "starts_at", "time"))
    end_value = _value(("end_at", "end", "ends_at", "end_time"))
    try:
        # Legacy daily-plan rows use ``date`` plus bare clocks.  Attach the
        # date before parsing; otherwise a 23:30-00:30 item is interpreted as
        # a same-day interval and immediately becomes past.
        if isinstance(start_value, str) and len(start_value.strip()) <= 8 and ":" in start_value and "T" not in start_value and "-" not in start_value:
            date_value = str(item.get("date") or item.get("window_date") or now.date().isoformat()).strip()
            if date_value:
                start_value = f"{date_value}T{start_value.strip()}"
        start = parse_datetime(start_value, default=now)
    except Exception:
        return None, None
    try:
        if isinstance(end_value, str) and len(end_value.strip()) <= 8 and ":" in end_value and "T" not in end_value and "-" not in end_value:
            date_value = str(item.get("date") or item.get("window_date") or now.date().isoformat()).strip()
            if date_value:
                end_value = f"{date_value}T{end_value.strip()}"
        end = parse_datetime(end_value, default=start)
    except Exception:
        end = start
    if end <= start:
        raw_start = str(start_value or "").rsplit("T", 1)[-1][:5]
        raw_end = str(end_value or "").rsplit("T", 1)[-1][:5]
        if raw_start and raw_end and raw_end <= raw_start:
            end = end + timedelta(days=1)
        else:
            end = start + timedelta(seconds=1)
    return start, end


def _overlaps(plan: dict[str, Any], activity: dict[str, Any], now: datetime) -> bool:
    ps, pe = _interval(plan, now)
    a_s, a_e = _interval(activity, now)
    return bool(ps and pe and a_s and a_e and ps < a_e and a_s < pe)


def _title_similarity(plan: dict[str, Any], activity: dict[str, Any]) -> float:
    plan_text = _normalized_text(plan.get("title") or plan.get("activity"))
    activity_text = _normalized_text(activity.get("title") or activity.get("summary"))
    if not plan_text or not activity_text:
        return 0.0
    if plan_text in activity_text or activity_text in plan_text:
        return 1.0
    plan_tokens = _tokens(plan_text)
    activity_tokens = _tokens(activity_text)
    shared = plan_tokens.intersection(activity_tokens)
    if not shared:
        return 0.0
    return len(shared) / max(1, len(plan_tokens.union(activity_tokens)))


def _refs(item: dict[str, Any]) -> set[str]:
    value = item.get("source_refs") or item.get("evidence_refs") or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(ref).strip() for ref in value if str(ref).strip()}


def _revision_key(value: Any) -> tuple[Any, ...]:
    parts: list[tuple[int, Any]] = []
    for part in re.split(r"([0-9]+)", str(value or "").strip()[:80]):
        if part.isdigit():
            parts.append((1, int(part)))
        elif part:
            parts.append((0, part.lower()))
    return tuple(parts)


def _mark_superseded_revisions(plans: list[dict[str, Any]], *, now: datetime) -> None:
    """Hide older valid schedule revisions before direct reconciliation output."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for plan in plans:
        if str(plan.get("schedule_ref_status") or "").lower() != "valid":
            continue
        ref = plan.get("schedule_ref")
        if not isinstance(ref, dict):
            continue
        key = (str(ref.get("namespace") or ""), str(ref.get("event_id") or ""))
        if key != ("", ""):
            groups.setdefault(key, []).append(plan)
    for items in groups.values():
        if len(items) < 2:
            continue

        def rank(item: dict[str, Any]) -> tuple[Any, ...]:
            ref = item.get("schedule_ref") if isinstance(item.get("schedule_ref"), dict) else {}
            try:
                updated = parse_datetime(ref.get("updated_at"), default=now)
            except Exception:
                updated = now
            return (_revision_key(ref.get("revision")), updated)

        latest = max(items, key=rank)
        for item in items:
            if item is latest:
                continue
            item["schedule_ref_status"] = "revoked"
            item["schedule_ref_reason"] = "schedule_revision_superseded"
            item["status"] = "overridden"
            item["commitment_level"] = "tentative"
            item["fact_eligibility"] = "none"
            item.setdefault("decision_trace", []).append(
                _trace_event("reconciler.schedule_revision_superseded", "older schedule revision hidden")
            )


def _same_source(plan: dict[str, Any], activity: dict[str, Any]) -> bool:
    plan_id = str(plan.get("plan_id") or plan.get("event_id") or "").strip()
    plan_refs = _refs(plan)
    activity_refs = _refs(activity)
    authority = str(plan.get("authority_kind") or "").strip().lower()
    declared_authority = str(plan.get("legacy_authority_kind") or "").strip().lower() or authority
    # A hard calendar/timetable/appointment commitment cannot be used as an
    # execution link unless its structured reference passed the authority
    # adapter.  This check must precede the convenient plan_id shortcut.
    if declared_authority in HARD_SCHEDULE_AUTHORITIES and (
        str(plan.get("schedule_ref_status") or "").strip().lower() != "valid"
        or not bool(plan.get("source_refs_trusted"))
    ):
        return False
    # A plan's own source_refs are untrusted generation input unless a
    # schedule/evidence adapter marked them trusted.  An activity may still
    # explicitly target the canonical plan_id, which is the unambiguous link
    # used by tool/action adapters.
    if plan_id and plan_id in activity_refs:
        return True
    if plan_refs.intersection(activity_refs):
        # For an ordinary intent, a concrete observation carrying the same
        # external/evidence reference is an explicit link.  It can support
        # execution status through ``_supports_execution`` but never promotes
        # the plan's schedule commitment or authority.
        return True
    return False


def _plan_is_past(plan: dict[str, Any], now: datetime) -> bool:
    _start, end = _interval(plan, now)
    return bool(end and end <= now)


def _temporal_phase(item: dict[str, Any], now: datetime) -> str:
    start, end = _interval(item, now)
    if start is None:
        return "future" if item.get("source_kind") == "planned" else "unknown"
    try:
        current = parse_datetime(now)
    except Exception:
        current = now
    if current < start:
        return "future"
    if end is not None and current >= end:
        return "past"
    return "current"


def _evidence_kind(item: dict[str, Any]) -> str:
    candidate = str(item.get("evidence_kind") or "").strip().lower()
    if candidate in EVIDENCE_KINDS:
        return candidate
    source = _normalized_text(item.get("source") or item.get("kind"))
    if any(token in source for token in ("selfstate", "selfcommit", "internalstate", "内部状态")):
        return "self_state_commit"
    if any(token in source for token in ("tool", "action", "工具", "执行")):
        return "tool_action"
    if any(token in source for token in ("calendar", "timetable", "roster", "appointment", "日历", "课表", "班表", "预约")):
        return "external_record"
    if any(token in source for token in ("commitment", "承诺", "安排")):
        return "external_commitment"
    if any(token in source for token in ("conversation", "chat", "message", "interaction", "聊天", "对话", "互动")):
        return "interaction"
    # Captured C3 activities historically defaulted to conversation.
    if item.get("source_kind") == "observed":
        return "interaction"
    return "none"


def _actor_compatible(plan: dict[str, Any], activity: dict[str, Any]) -> bool:
    plan_actor = str(plan.get("subject_actor_id") or "").strip()
    activity_actor = str(activity.get("subject_actor_id") or "").strip()
    # A missing subject cannot inherit the other record's actor.  Two legacy
    # unbound records may still be reconciled for compatibility, but the
    # resulting view remains diagnostic until an actor is attached.
    if bool(plan_actor) != bool(activity_actor):
        return False
    return not plan_actor or plan_actor == activity_actor


def _actor_bound(item: dict[str, Any]) -> bool:
    return bool(
        str(item.get("subject_actor_id") or "").strip()
        and str(item.get("actor_type") or "").strip().lower() in {"bot", "interlocutor_user", "external_party", "system"}
    )


def _has_hint(value: Any, hints: set[str]) -> bool:
    raw = str(value or "").lower()
    normalized = _normalized_text(value)
    return any(hint in raw or _normalized_text(hint) in normalized for hint in hints)


def _is_chat_plan(plan: dict[str, Any]) -> bool:
    if str(plan.get("evidence_kind") or "").lower() == "interaction":
        return True
    for key in ("kind", "category", "activity_type", "type", "title", "activity"):
        if _has_hint(plan.get(key), CHAT_HINTS):
            return True
    return False


def _is_internal_plan(plan: dict[str, Any]) -> bool:
    granularity = str(plan.get("content_granularity") or "").lower()
    if granularity == "scene":
        return False
    if str(plan.get("authority_kind") or "").lower() in {"state", "persona"}:
        return True
    return any(_has_hint(plan.get(key), INTERNAL_HINTS) for key in ("title", "activity", "kind", "category"))


def _supports_execution(plan: dict[str, Any], activity: dict[str, Any], *, explicit_ref: bool) -> bool:
    kind = _evidence_kind(activity)
    if kind in EXECUTION_EVIDENCE_KINDS:
        return True
    if kind == "interaction":
        # Conversation evidence proves only a chat-like plan.  A source
        # reference resolves which record is being discussed, but cannot turn
        # chat text into proof of an unrelated external action.
        return _is_chat_plan(plan)
    if kind == "self_state_commit":
        # SelfStateCommit is a standalone short-lived internal state.  It may
        # be disclosed as ``current_internal`` through the activity record,
        # but it must never mutate a plan to active/completed or create a
        # reconciliation that can later be replayed as execution evidence.
        return False
    # A commitment (calendar/timetable/appointment) proves an arrangement,
    # never attendance or execution.
    return False


def _evidence_level(items: list[dict[str, Any]]) -> str:
    levels = {str(item.get("evidence_level") or "").upper() for item in items}
    for level in ("L5", "L4", "L3", "L2", "L1", "L0"):
        if level in levels:
            return level
    return "L0"


def _coverage_ratio(plan: dict[str, Any], activities: list[dict[str, Any]], now: datetime) -> float:
    plan_start, plan_end = _interval(plan, now)
    if not plan_start or not plan_end:
        return 0.0
    duration = max(1.0, (plan_end - plan_start).total_seconds())
    covered: list[tuple[datetime, datetime]] = []
    for activity in activities:
        start, end = _interval(activity, now)
        if not start or not end:
            continue
        left, right = max(plan_start, start), min(plan_end, end)
        if left < right:
            covered.append((left, right))
    if not covered:
        return 0.0
    covered.sort()
    total = 0.0
    current_start, current_end = covered[0]
    for start, end in covered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    total += (current_end - current_start).total_seconds()
    return min(1.0, total / duration)


def _entry_semantics(entry: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Copy canonical fields onto legacy agenda entries without changing API shape."""

    fields = (
        "temporal_phase",
        "evidence_kind",
        "authority_kind",
        "commitment_level",
        "epistemic_status",
        "content_granularity",
        "materialization_state",
        "fact_eligibility",
        "confidence",
        "subject_actor_id",
        "source_actor_id",
        "target_user_id",
        "participant_roles",
        "runtime_origin_refs",
        "expires_at",
        "decision_trace",
    )
    for field in fields:
        if field in item:
            entry[field] = item[field]
    return entry


def _prepare_plan(raw: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    plan = normalize_plan_item(raw, now=now)
    raw_status = str(raw.get("status") or raw.get("lifecycle_status") or "").strip().lower()
    canonical_status = STATUS_ALIASES.get(raw_status, raw_status)
    if raw_status and canonical_status not in PRESERVED_PLAN_STATUSES:
        plan["legacy_status"] = raw_status
        plan["status"] = "planned"
        plan.setdefault("decision_trace", []).append(
            _trace_event("reconciler.plan_status_ignored", "plan status ignored until compatible evidence")
        )
    elif canonical_status in PRESERVED_PLAN_STATUSES:
        plan["status"] = canonical_status
    plan["temporal_phase"] = _temporal_phase(plan, now)
    plan["evidence_kind"] = "none"
    plan["epistemic_status"] = str(plan.get("epistemic_status") or "asserted").lower()
    authority = str(plan.get("authority_kind") or "").lower()
    source_refs = _refs(plan)
    schedule_ref = plan.get("schedule_ref") if isinstance(plan.get("schedule_ref"), dict) else {}
    ref_state = str(schedule_ref.get("state") or "").lower()
    schedule_ref_status = str(plan.get("schedule_ref_status") or "not_applicable")
    schedule_ref_reason = str(plan.get("schedule_ref_reason") or "")
    if authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"}:
        schedule_ref_status, schedule_ref_reason = validate_structured_schedule_ref(
            schedule_ref,
            source_refs=source_refs,
            expected_authority=authority,
            expected_subject=plan.get("subject_actor_id"),
            expected_target=plan.get("target_user_id"),
            now=now,
        )
        plan["source_refs_trusted"] = schedule_ref_status == "valid"
        if schedule_ref_status != "valid":
            plan["authority_kind"] = "llm"
            authority = "llm"
            plan["commitment_level"] = "tentative"
            plan.setdefault("decision_trace", []).append(
                _trace_event(
                    "reconciler.untrusted_schedule_ref",
                    "hard schedule authority requires a valid structured schedule_ref",
                )
            )
        elif isinstance(schedule_ref, dict):
            # The signed absolute interval is authoritative.  Adapter
            # ``to_plan_fields()`` intentionally need not duplicate clocks;
            # populate them here so date/window projections remain stable and
            # caller-controlled duplicate times cannot move a commitment.
            signed_start = str(schedule_ref.get("effective_from") or "").strip()
            signed_end = str(schedule_ref.get("effective_to") or "").strip()
            if signed_start and signed_end:
                plan["start_at"] = signed_start
                plan["end_at"] = signed_end
                plan["temporal_phase"] = _temporal_phase(plan, now)
    plan["schedule_ref_status"] = schedule_ref_status
    plan["schedule_ref_reason"] = schedule_ref_reason
    ref_state = ref_state or "active"
    if ref_state in {"cancelled", "revoked"}:
        plan["status"] = "cancelled"
        plan["commitment_level"] = "tentative"
        plan["fact_eligibility"] = "none"
        plan.setdefault("decision_trace", []).append(
            _trace_event("reconciler.schedule_ref_inactive", "inactive schedule reference cannot remain confirmed")
        )
    elif ref_state == "superseded":
        plan["status"] = "overridden"
        plan["commitment_level"] = "tentative"
        plan["fact_eligibility"] = "none"
        plan.setdefault("decision_trace", []).append(
            _trace_event("reconciler.schedule_ref_superseded", "superseded schedule reference is hidden")
        )
    if plan.get("status") in {"cancelled", "overridden"}:
        plan["commitment_level"] = "tentative"
    elif authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"} and plan.get("source_refs_trusted"):
        plan["commitment_level"] = "confirmed"
    elif authority == "routine":
        plan["commitment_level"] = "routine"
    elif str(plan.get("commitment_level") or "").lower() not in {"confirmed", "routine", "tentative"}:
        plan["commitment_level"] = "tentative"
    granularity = str(raw.get("content_granularity") or "").lower()
    plan["content_granularity"] = granularity if granularity in {"commitment", "intent", "candidate", "scene"} else (
        "commitment" if plan["commitment_level"] == "confirmed" else "intent"
    )
    plan["materialization_state"] = str(plan.get("materialization_state") or "none").lower()
    if plan["materialization_state"] not in {"none", "candidate", "active", "rejected", "expired"}:
        plan["materialization_state"] = "none"
    expires_at = str(plan.get("expires_at") or "").strip()
    if expires_at:
        try:
            if parse_datetime(expires_at) <= parse_datetime(now):
                plan["materialization_state"] = "expired"
                plan["commitment_level"] = "tentative"
                plan["fact_eligibility"] = "none"
                plan.setdefault("decision_trace", []).append(
                    _trace_event("reconciler.plan_expired", "expired plan is excluded from future commitments")
                )
        except Exception:
            pass
    plan["fact_eligibility"] = "schedule_commitment" if plan["commitment_level"] in {"confirmed", "routine"} else "none"
    if plan["materialization_state"] == "expired":
        plan["fact_eligibility"] = "none"
    plan.setdefault("decision_trace", [])
    if not isinstance(plan["decision_trace"], list):
        plan["decision_trace"] = [_trace_event("reconciler.trace_coerced", str(plan["decision_trace"]))]
    return plan


def _prepare_activity(raw: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    activity = normalize_observed_activity(raw, now=now)
    activity["temporal_phase"] = _temporal_phase(activity, now)
    activity["evidence_kind"] = _evidence_kind(activity)
    # Calendar-shaped payloads are commitments, not attendance evidence.
    raw_source = str(raw.get("source") or "").strip().lower()
    if raw_source in {"calendar", "timetable", "roster", "appointment"} and not raw.get("evidence_kind"):
        activity["evidence_kind"] = "external_commitment"
    activity["epistemic_status"] = "observed"
    if activity["evidence_kind"] in {"none", "external_commitment"} or activity["temporal_phase"] == "future":
        activity["fact_eligibility"] = "none"
    elif activity["evidence_kind"] == "self_state_commit" and activity["temporal_phase"] == "current":
        activity["fact_eligibility"] = "current_internal"
    elif activity["temporal_phase"] == "past" and activity["evidence_kind"] != "self_state_commit":
        activity["fact_eligibility"] = "history_observed"
    elif activity["evidence_kind"] == "self_state_commit":
        activity["fact_eligibility"] = "none"
    else:
        activity["fact_eligibility"] = "current_observed"
    activity.setdefault(
        "content_granularity",
        "commitment" if activity["evidence_kind"] == "external_commitment" else "intent",
    )
    activity.setdefault("commitment_level", "tentative")
    activity.setdefault("materialization_state", "none")
    activity.setdefault("decision_trace", [])
    if not isinstance(activity["decision_trace"], list):
        activity["decision_trace"] = [_trace_event("reconciler.trace_coerced", str(activity["decision_trace"]))]
    return activity


def _set_evidence_levels(item: dict[str, Any], level: str) -> None:
    """Keep canonical and archive evidence projections in sync after matching."""

    normalized = str(level or "L0").upper()
    item["evidence_level"] = normalized
    item["canonical_evidence_level"] = normalized
    item["archive_evidence_level"] = normalized if normalized in {"L0", "L1", "L2", "L3"} else "L3"
    mapping = item.get("evidence_level_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    mapping.update(
        {
            "canonical_evidence_level": item["canonical_evidence_level"],
            "archive_evidence_level": item["archive_evidence_level"],
            "lossy": item["archive_evidence_level"] != normalized,
        }
    )
    item["evidence_level_mapping"] = mapping


def reconcile(
    plans: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Reconcile C3 plans against compatible evidence.

    Exact source references are verified links.  Time/title-only matches are
    returned as diagnostic candidates and never mutate plan status or fact
    eligibility.  This keeps future intent out of current/history views.
    """

    normalized_plans: list[dict[str, Any]] = []
    for raw in plans or []:
        try:
            if isinstance(raw, dict):
                normalized_plans.append(_prepare_plan(raw, now=now))
        except Exception:
            continue
    _mark_superseded_revisions(normalized_plans, now=now)
    normalized_activities: list[dict[str, Any]] = []
    for raw in activities or []:
        try:
            if isinstance(raw, dict):
                normalized_activities.append(_prepare_activity(raw, now=now))
        except Exception:
            continue

    used_activity_ids: set[str] = set()
    reconciled_plans: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    reconciliation_candidates: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    matched: dict[str, list[str]] = {}

    for plan in normalized_plans:
        plan_id = str(plan.get("plan_id") or stable_id("plan", plan.get("title")))
        plan_result = dict(plan)
        trace = list(plan_result.get("decision_trace") or [])
        explicit = [
            activity
            for activity in normalized_activities
            if _same_source(plan, activity) and _actor_compatible(plan, activity)
            and (_overlaps(plan, activity, now) or not _interval(plan, now)[0] or not _interval(activity, now)[0])
        ]
        executable = [
            activity
            for activity in explicit
            if _temporal_phase(plan, now) != "future"
            and _temporal_phase(activity, now) != "future"
            and str(activity.get("status") or "active").lower()
            in {"active", "completed", "partially_completed"}
            and _supports_execution(plan, activity, explicit_ref=True)
        ]
        # Similarity is useful for diagnostics, but it is never execution
        # evidence and must not consume an activity or alter status.
        similar = [
            activity
            for activity in normalized_activities
            if str(activity.get("activity_id")) not in {str(item.get("activity_id")) for item in explicit}
            and _actor_compatible(plan, activity)
            and _overlaps(plan, activity, now)
            and _title_similarity(plan, activity) >= 0.18
        ]
        if similar:
            similar = similar[:3]
            reconciliation_candidates.append(
                {
                    "candidate_id": stable_id(
                        "reconciliation_candidate",
                        plan_id,
                        [item.get("activity_id") for item in similar],
                    ),
                    "plan_id": plan_id,
                    "activity_ids": [str(item.get("activity_id")) for item in similar],
                    "status": "pending_verification",
                    "source_kind": "reconciled",
                    "temporal_phase": _temporal_phase(plan, now),
                    "evidence_kind": _evidence_kind(similar[0]),
                    "epistemic_status": "inferred",
                    "fact_eligibility": "none",
                    "subject_actor_id": plan.get("subject_actor_id", ""),
                    "reason": "time overlap/title similarity requires verification",
                    "decision_trace": [
                        _trace_event("reconciler.similarity_candidate", "similarity is diagnostic only")
                    ],
                }
            )
            trace.append(
                _trace_event("reconciler.similarity_withheld", "similarity candidate withheld from execution status")
            )

        # A sustained conversation can occupy a live plan window without
        # proving that the planned activity was completed. Keep this as a
        # diagnostic-only interruption hint so prompt consumers may gently
        # acknowledge it without turning chat into execution evidence.
        interruptions = [
            activity
            for activity in normalized_activities
            if str(activity.get("activity_id")) not in {str(item.get("activity_id")) for item in explicit}
            and _actor_compatible(plan, activity)
            and _temporal_phase(plan, now) == "current"
            and _evidence_kind(activity) in CHAT_EVIDENCE_KINDS
            and _overlaps(plan, activity, now)
            and not _is_chat_plan(plan)
        ]
        if interruptions:
            interruptions = interruptions[:3]
            interruption_ids = [str(item.get("activity_id")) for item in interruptions]
            reconciliation_candidates.append(
                {
                    "candidate_id": stable_id(
                        "reconciliation_interruption",
                        plan_id,
                        interruption_ids,
                    ),
                    "plan_id": plan_id,
                    "plan_title": str(plan.get("title") or plan.get("activity") or "当前计划")[:120],
                    "activity_ids": interruption_ids,
                    "activity_summary": str(
                        interruptions[-1].get("title")
                        or interruptions[-1].get("summary")
                        or "一段持续聊天"
                    )[:180],
                    "status": "possible_interruption",
                    "source_kind": "reconciled",
                    "temporal_phase": "current",
                    "evidence_kind": "interaction",
                    "epistemic_status": "inferred",
                    "fact_eligibility": "none",
                    "subject_actor_id": plan.get("subject_actor_id", ""),
                    "reason": "conversation overlapped a live plan window; plan completion remains unproven",
                    "decision_trace": [
                        _trace_event(
                            "reconciler.interaction_interruption_hint",
                            "chat overlap is a possible interruption, not execution evidence",
                        )
                    ],
                }
            )

        if executable:
            ids = [str(item.get("activity_id")) for item in executable]
            matched[plan_id] = ids
            used_activity_ids.update(ids)
            actors_bound = _actor_bound(plan_result) and all(_actor_bound(item) for item in executable)
            if not actors_bound:
                for item in executable:
                    item["fact_eligibility"] = "none"
            evidence_kind = _evidence_kind(executable[0])
            statuses = {str(item.get("status") or "active") for item in executable}
            coverage = _coverage_ratio(plan, executable, now)
            has_completed = "completed" in statuses
            has_partial = "partially_completed" in statuses
            if has_completed:
                next_status = "completed" if coverage >= 0.95 or not _interval(plan, now)[0] else "partially_completed"
            elif has_partial:
                next_status = "partially_completed"
            else:
                next_status = "active"
            plan_result["status"] = next_status
            plan_result["evidence_kind"] = evidence_kind
            _set_evidence_levels(plan_result, _evidence_level(executable))
            plan_result["epistemic_status"] = "observed"
            plan_result["fact_eligibility"] = (
                "history_observed"
                if next_status in {"completed", "partially_completed"}
                and _plan_is_past(plan_result, now)
                else "current_internal" if evidence_kind == "self_state_commit"
                else "current_observed"
            )
            if not actors_bound:
                plan_result["fact_eligibility"] = "none"
            plan_result["reconciliation_reason"] = (
                "explicit source reference matched compatible evidence"
            )
            plan_result["reconciled_activity_ids"] = ids
            plan_result["decision_trace"] = trace + [
                _trace_event("reconciler.compatible_evidence", "status produced by compatible evidence")
            ]
            source_refs: list[str] = []
            for item in executable:
                for ref in _refs(item):
                    if ref not in source_refs:
                        source_refs.append(ref)
            reconciliation = {
                "reconciliation_id": stable_id("reconciliation", plan_id, ids),
                "plan_id": plan_id,
                "status": next_status,
                "source_kind": "reconciled",
                "temporal_phase": _temporal_phase(plan_result, now),
                "evidence_kind": evidence_kind,
                "evidence_level": _evidence_level(executable),
                "epistemic_status": "observed",
                "authority_kind": plan_result.get("authority_kind") or executable[0].get("authority_kind", "state"),
                "commitment_level": plan_result.get("commitment_level", "tentative"),
                "content_granularity": plan_result.get("content_granularity", "intent"),
                "materialization_state": plan_result.get("materialization_state", "none"),
                "confidence": max(
                    float(plan_result.get("confidence") or 0.0),
                    max(float(item.get("confidence") or 0.0) for item in executable),
                ),
                "fact_eligibility": plan_result["fact_eligibility"],
                "source_refs": source_refs,
                "runtime_origin_refs": [
                    *[str(ref) for ref in plan_result.get("runtime_origin_refs") or [] if str(ref)],
                    f"reconcile:{plan_id}",
                ],
                "activity_ids": ids,
                "reason": plan_result["reconciliation_reason"],
                "decision_trace": plan_result["decision_trace"],
                "subject_actor_id": plan_result.get("subject_actor_id") or executable[0].get("subject_actor_id"),
                "source_actor_id": plan_result.get("source_actor_id") or executable[0].get("source_actor_id", "system"),
                "target_user_id": plan_result.get("target_user_id") or executable[0].get("target_user_id", ""),
                "participant_roles": plan_result.get("participant_roles") or executable[0].get("participant_roles", []),
                "expires_at": plan_result.get("expires_at", ""),
            }
            if not actors_bound:
                plan_result["decision_trace"] = trace + [
                    _trace_event("reconciler.missing_subject_withheld", "unbound records remain diagnostic-only")
                ]
                reconciliation["decision_trace"] = plan_result["decision_trace"]
            _set_evidence_levels(reconciliation, reconciliation["evidence_level"])
            reconciliations.append(reconciliation)
            entries.append(
                _entry_semantics(
                    agenda_entry_from_plan(plan_result, reason=plan_result["reconciliation_reason"]),
                    plan_result,
                )
            )
            entries.extend(
                _entry_semantics(agenda_entry_from_activity(item, source_refs=_refs(item)), item)
                for item in executable
            )
        else:
            # Raw active/completed fields are ignored by the plan normalizer;
            # a past window without evidence becomes unknown, never complete.
            if plan_result.get("status") not in {"cancelled", "deferred", "overridden"}:
                if _plan_is_past(plan_result, now):
                    plan_result["status"] = "unknown"
                    trace.append(
                        _trace_event("reconciler.no_evidence", "window ended without compatible observed evidence")
                    )
                else:
                    plan_result["status"] = "planned"
            plan_result["evidence_kind"] = "none"
            plan_result["fact_eligibility"] = (
                "schedule_commitment" if plan_result.get("commitment_level") in {"confirmed", "routine"} else "none"
            )
            plan_result["decision_trace"] = trace
            reason = (
                "window ended without observed evidence"
                if plan_result.get("status") == "unknown"
                else "planned; no compatible execution evidence"
            )
            plan_result["reconciliation_reason"] = plan_result.get("reconciliation_reason") or reason
            entries.append(
                _entry_semantics(
                    agenda_entry_from_plan(plan_result, reason=plan_result["reconciliation_reason"]),
                    plan_result,
                )
            )
        reconciled_plans.append(plan_result)

    for activity in normalized_activities:
        if str(activity.get("activity_id")) not in used_activity_ids:
            entries.append(_entry_semantics(agenda_entry_from_activity(activity), activity))

    entries.sort(key=lambda item: (str(item.get("start_at") or ""), 0 if item.get("kind") == "observed" else 1))
    return {
        "plans": reconciled_plans,
        "activities": normalized_activities,
        "entries": entries,
        "matched": matched,
        "reconciliations": reconciliations,
        "reconciliation_candidates": reconciliation_candidates,
    }
