# -*- coding: utf-8 -*-
"""Extract and merge user-authored calendar observations.

The observer is intentionally local and conservative.  It creates a durable
candidate with evidence instead of pretending that one conversational hint is
an execution fact.  A later explicit confirmation, or repeated consistent
evidence for a phase/rhythm, can promote the candidate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from typing import Any, Iterable

try:
    from .agenda_contracts import stable_id, timezone_or_default
    from .calendar_contracts import (
        advance_calendar_lifecycle,
        calendar_candidate_from_record,
        normalize_calendar_record,
    )
except ImportError:
    from agenda_contracts import stable_id, timezone_or_default
    from calendar_contracts import advance_calendar_lifecycle, calendar_candidate_from_record, normalize_calendar_record


_WEEKDAY_NAMES = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6,
    "天": 6,
}
_PHASE_WORDS = (
    "暑假", "寒假", "假期", "休假", "放假", "出差", "旅行", "旅游", "住院",
    "备考", "实习", "开学", "搬家", "戒断", "恢复期",
)
_RHYTHM_WORDS = ("每天", "每日", "每周", "每星期", "工作日", "周末", "通常", "平时", "固定")
_CONFIRM_WORDS = (
    "预约好了", "已经预约", "定好了", "已经安排", "安排好了", "买好了", "订好了",
    "已经确定", "确定好了", "确认了", "已经报名", "已经买票", "已经订票",
)
_CANCEL_WORDS = ("取消", "不用了", "算了", "结束了", "已经结束", "不去了", "不去", "不参加", "不安排", "不打算", "改掉")
_COMPLETE_WORDS = ("完成了", "搞定了", "办完了", "回来了", "到了", "已经到了", "结束了")
_DATE_MARKERS = r"今天|明天|后天|大后天|昨天|前天|下周[一二三四五六日天]?|这周[一二三四五六日天]?|本周[一二三四五六日天]?|\d{1,3}天后|\d{1,2}月\d{1,2}[日号]?|\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?"


def _text(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _now_local(now: datetime | None, timezone_name: str) -> datetime:
    tz = timezone_or_default(timezone_name)
    if isinstance(now, datetime):
        return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    return datetime.now(tz)


def _date_from_phrase(phrase: str, base: date) -> date | None:
    value = _text(phrase, 32).replace("号", "日")
    if value in {"今天"}:
        return base
    if value in {"明天"}:
        return base + timedelta(days=1)
    if value in {"后天"}:
        return base + timedelta(days=2)
    if value in {"大后天"}:
        return base + timedelta(days=3)
    if value in {"昨天"}:
        return base - timedelta(days=1)
    if value in {"前天"}:
        return base - timedelta(days=2)
    relative = re.fullmatch(r"(\d{1,3})天后", value)
    if relative:
        return base + timedelta(days=int(relative.group(1)))
    weekday = re.fullmatch(r"(?:下周|这周|本周)([一二三四五六日天])?", value)
    if weekday:
        target_weekday = _WEEKDAY_NAMES.get(weekday.group(1) or "一", 0)
        days = (target_weekday - base.weekday()) % 7
        if value.startswith("下周") or days == 0:
            days += 7
        return base + timedelta(days=days)
    absolute = re.fullmatch(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})日?", value)
    if absolute:
        year = int(absolute.group(1) or base.year)
        month, day = int(absolute.group(2)), int(absolute.group(3))
        try:
            result = date(year, month, day)
        except ValueError:
            return None
        if not absolute.group(1) and result < base:
            result = date(base.year + 1, month, day)
        return result
    return None


def _date_range(text: str, base: date) -> tuple[date | None, date | None]:
    matches = list(re.finditer(_DATE_MARKERS, text))
    if not matches:
        return None, None
    start = _date_from_phrase(matches[0].group(0), base)
    end = None
    if len(matches) > 1:
        end = _date_from_phrase(matches[1].group(0), base)
    if start is None:
        return None, None
    return start, end


def _time_range(text: str) -> tuple[str, str]:
    match = re.search(r"(?:(上午|中午|下午|晚上|凌晨)\s*)?(\d{1,2})(?::(\d{1,2}))?\s*[点时](?:半)?(?:\s*(?:到|至|-|~)\s*(?:(上午|中午|下午|晚上|凌晨)\s*)?(\d{1,2})(?::(\d{1,2}))?\s*[点时]?)?", text)
    if not match:
        match = re.search(r"(\d{1,2}):(\d{2})(?:\s*[-~到至]\s*(\d{1,2}):(\d{2}))?", text)
        if not match:
            return "", ""
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}", (
            f"{int(match.group(3)):02d}:{int(match.group(4)):02d}" if match.group(3) else ""
        )

    def clock(meridiem: str | None, hour: str, minute: str | None) -> str:
        value = int(hour)
        if meridiem in {"下午", "晚上"} and value < 12:
            value += 12
        if meridiem == "凌晨" and value == 12:
            value = 0
        if "半" in match.group(0) and minute is None:
            minute = "30"
        return f"{value % 24:02d}:{int(minute or 0):02d}"

    start = clock(match.group(1), match.group(2), match.group(3))
    end = clock(match.group(4), match.group(5), match.group(6)) if match.group(5) else ""
    return start, end


def _title_from_message(text: str, *, start: date, end: date | None) -> str:
    value = text
    for marker in (*_CONFIRM_WORDS, *_CANCEL_WORDS, *_COMPLETE_WORDS):
        value = value.replace(marker, "")
    value = re.sub(_DATE_MARKERS, "", value)
    value = re.sub(r"(?:上午|中午|下午|晚上|凌晨)?\s*\d{1,2}(?::\d{2})?\s*[点时](?:半)?", "", value)
    value = re.sub(r"(?:明天|后天|今天|下周|这周|本周|最近|这段时间|以后|到时候)", "", value)
    value = re.sub(r"^(?:我|咱们|我们|你|他|她)?\s*(?:要|会|得|准备|打算|计划|想|去|在|有|安排|记得|帮我记一下|提醒我)", "", value)
    value = re.sub(r"(?:吧|呀|啊|呢|哦|啦|了)$", "", value)
    value = re.split(r"[，。！？!?；;]", value, maxsplit=1)[0]
    value = _text(value.strip(" ：:、,，"), 80)
    if not value:
        value = "生活安排"
    return value


def _confidence(text: str, *, recurring: bool, phase: bool, explicit: bool) -> float:
    score = 0.56
    if explicit:
        score += 0.22
    if recurring:
        score += 0.04
    if phase:
        score += 0.03
    if re.search(_DATE_MARKERS, text):
        score += 0.08
    return min(0.96, score)


def extract_calendar_candidates(
    text: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
    subject_actor_id: str = "bot_self",
    source_user_id: str = "",
    source_message_id: str = "",
    conversation_id: str = "",
    target_user_id: str = "",
) -> list[dict[str, Any]]:
    """Extract bounded, evidence-backed *proposals* from one user message.

    A proposal is never written to the formal calendar by this function.  The
    runtime decides whether an explicit confirmation is strong enough to
    materialize it; ordinary future-tense language remains pending.
    """

    cleaned = _text(text, 320)
    if not cleaned or len(cleaned) < 3:
        return []
    if re.match(r"^(?:陪伴|/陪伴|bot)\s", cleaned, re.I):
        return []
    # A direct negation is a lifecycle signal for an existing proposal, not a
    # new future event.  Questions remain candidates because they are useful
    # prompts for a later confirmation.
    if re.search(r"(?<!去)(?:不去|没去|没有去|不参加|不安排|不打算)", cleaned):
        return []
    local_now = _now_local(now, timezone_name)
    base = local_now.date()
    recurring = any(word in cleaned for word in _RHYTHM_WORDS)
    phase = any(word in cleaned for word in _PHASE_WORDS)
    has_action = bool(re.search(r"(?:要|会|得|准备|打算|计划|安排|预约|去|上课|上学|上班|考试|开会|见|回|出发|住|旅行|休假)", cleaned))
    start, end = _date_range(cleaned, base)
    if start is None and not (phase or recurring):
        return []
    explicit = any(word in cleaned for word in _CONFIRM_WORDS)
    if not has_action and not phase and not recurring:
        return []
    if start is None:
        start = base
    if phase and end is None:
        # This is a horizon for a candidate only, never an asserted fact.
        end = start + timedelta(days=7)
    kind = "recurrence" if recurring else "period" if phase or end else "event"
    title = _title_from_message(cleaned, start=start, end=end)
    start_time, end_time = _time_range(cleaned)
    frequency = "daily" if any(word in cleaned for word in ("每天", "每日")) else "weekly"
    weekdays = []
    weekday_match = re.search(r"(?:每周|每星期|工作日|周)([一二三四五六日天])", cleaned)
    if weekday_match:
        weekdays = [_WEEKDAY_NAMES.get(weekday_match.group(1), base.weekday())]
    if "工作日" in cleaned:
        weekdays = [0, 1, 2, 3, 4]
    if "周末" in cleaned:
        weekdays = [5, 6]
    confidence = _confidence(cleaned, recurring=recurring, phase=phase, explicit=explicit)
    record_id = stable_id("calendar_observation", subject_actor_id, kind, title, start.isoformat(), end.isoformat() if end else "", frequency, weekdays)
    evidence = {
        "evidence_id": stable_id(
            "calendar_evidence",
            source_message_id or cleaned,
            "message" if source_message_id else local_now.date().isoformat(),
        ),
        "source_type": "message",
        "source_id": _text(source_message_id, 160),
        "quote": _text(cleaned, 320),
        "observed_at": local_now.isoformat(timespec="seconds"),
        "actor": _text(source_user_id, 120),
    }
    record: dict[str, Any] = {
        "kind": kind,
        "calendar_id": record_id,
        "title": title,
        "start_date": start.isoformat(),
        "date": start.isoformat(),
        "subject_actor_id": _text(subject_actor_id, 120) or "bot_self",
        "source_user_id": _text(source_user_id, 120),
        "source_message_id": _text(source_message_id, 160),
        "source": "user_message_observation",
        "lifecycle": "candidate",
        "lifecycle_state": "candidate",
        "status": "tentative",
        "commitment_level": "tentative",
        "epistemic_status": "asserted" if explicit else "inferred",
        "confidence": confidence,
        "calendar_effective": False,
        "evidence_count": 1,
        "evidence": [evidence],
        "created_at": local_now.isoformat(timespec="seconds"),
        "updated_at": local_now.isoformat(timespec="seconds"),
    }
    if end:
        record["end_date"] = end.isoformat()
    if start_time:
        record["start_time"] = start_time
    if end_time:
        record["end_time"] = end_time
    if recurring:
        record.update({"frequency": frequency, "interval": 1, "by_weekday": weekdays or [start.weekday()]})
    if phase and end and not _date_range(cleaned, base)[1]:
        record["end_date_inferred"] = True
    proposed = calendar_candidate_from_record(
        record,
        evidence=evidence,
        confidence=confidence,
        now=local_now,
        timezone_name=timezone_name,
    )
    candidate_id = stable_id("calendar_candidate", subject_actor_id, record_id)
    expires_at = local_now + timedelta(days=60 if kind in {"period", "recurrence"} else 30)
    proposed["source_user_id"] = _text(source_user_id, 120)
    proposed["source_message_id"] = _text(source_message_id, 160)
    proposed["source"] = "user_message_observation"
    proposed["candidate_id"] = candidate_id
    proposed["lifecycle_status"] = "pending_confirmation"
    proposed["observation_intent"] = "confirm" if explicit else "observe"
    proposed["confirmation_requested"] = bool(explicit)
    proposed["source_excerpt"] = _text(cleaned, 320)
    proposed["source_message_at"] = local_now.isoformat(timespec="seconds")
    proposed["conversation_id"] = _text(conversation_id, 160)
    proposed["target_user_id"] = _text(target_user_id or source_user_id, 160)
    proposed["created_at"] = local_now.isoformat(timespec="seconds")
    proposed["updated_at"] = local_now.isoformat(timespec="seconds")
    proposed["expires_at"] = expires_at.isoformat(timespec="seconds")
    proposed["decision_trace"] = []
    proposed["revision"] = 1
    proposed["proposed_record"] = deepcopy(
        {key: value for key, value in proposed.items() if key not in {
            "candidate_id", "lifecycle_status", "observation_intent", "confirmation_requested",
            "source_excerpt", "source_message_at", "conversation_id", "target_user_id",
            "created_at", "updated_at", "expires_at", "decision_trace", "revision", "proposed_record",
        }}
    )
    return [proposed]


def _topic_score(left: str, right: str) -> float:
    left_text = _text(left, 120)
    right_text = _text(right, 260)
    if left_text and right_text and (left_text in right_text or right_text in left_text):
        return 1.0
    left_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9_]{3,24}", left_text))
    right_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9_]{3,24}", right_text))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _append_evidence(record: dict[str, Any], evidence: dict[str, Any], *, limit: int = 8) -> None:
    existing = record.get("evidence") if isinstance(record.get("evidence"), list) else []
    ids = {str(item.get("evidence_id")) for item in existing if isinstance(item, dict)}
    if str(evidence.get("evidence_id")) not in ids:
        existing.append(deepcopy(evidence))
    record["evidence"] = existing[-limit:]
    record["evidence_count"] = len(existing)


_CANDIDATE_WRAPPER_FIELDS = {
    "candidate_id", "lifecycle_status", "observation_intent", "confirmation_requested",
    "source_excerpt", "source_message_at", "conversation_id", "target_user_id",
    "created_at", "updated_at", "expires_at", "decision_trace", "revision",
    "proposed_record",
}


def _candidate_proposed_record(candidate: dict[str, Any]) -> dict[str, Any]:
    proposed = candidate.get("proposed_record") if isinstance(candidate.get("proposed_record"), dict) else {}
    if proposed:
        return deepcopy(proposed)
    return deepcopy({key: value for key, value in candidate.items() if key not in _CANDIDATE_WRAPPER_FIELDS})


def _sync_candidate_projection(candidate: dict[str, Any]) -> None:
    proposed = _candidate_proposed_record(candidate)
    candidate["proposed_record"] = proposed
    for key in ("calendar_id", "kind", "type", "title", "start_date", "end_date", "date", "start_time", "end_time", "frequency", "interval", "by_weekday", "all_day", "priority", "timezone", "subject_actor_id", "source_refs", "evidence", "confidence"):
        if key in proposed:
            candidate[key] = deepcopy(proposed[key])
    state = str(candidate.get("lifecycle_state") or candidate.get("lifecycle") or "candidate")
    status = {
        "candidate": "pending_confirmation",
        "tentative": "pending_confirmation",
        "confirmed": "confirmed",
        "active": "active",
        "completed": "completed",
        "cancelled": "cancelled",
        "expired": "expired",
    }.get(state, "pending_confirmation")
    proposed["lifecycle_state"] = state
    proposed["lifecycle"] = state
    proposed["status"] = "active" if state == "active" else "confirmed" if state == "confirmed" else "expired" if state == "completed" else "cancelled" if state == "cancelled" else "tentative"
    proposed["commitment_level"] = "confirmed" if state in {"confirmed", "active", "completed"} else "tentative"
    proposed["calendar_effective"] = state in {"confirmed", "active"}
    candidate["lifecycle_state"] = state
    candidate["lifecycle"] = state
    candidate["status"] = proposed["status"]
    candidate["commitment_level"] = proposed["commitment_level"]
    candidate["lifecycle_status"] = status
    candidate["calendar_effective"] = state in {"confirmed", "active"}


def _preserve_candidate_metadata(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in _CANDIDATE_WRAPPER_FIELDS | {"candidate_id", "materialized_calendar_id", "materialized_at"}:
        if key in before and key not in after:
            after[key] = deepcopy(before[key])


def merge_calendar_observations(
    records: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
    *,
    text: str = "",
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Merge candidates and lifecycle signals into the pending candidate lane."""

    local_now = _now_local(now, timezone_name)
    result = [deepcopy(item) for item in records if isinstance(item, dict)]
    audit: list[dict[str, Any]] = []
    changed = False
    candidate_list = [deepcopy(item) for item in candidates if isinstance(item, dict)]
    confirmation = any(marker in _text(text, 260) for marker in _CONFIRM_WORDS)
    completion = any(marker in _text(text, 260) for marker in _COMPLETE_WORDS)
    cancellation = any(marker in _text(text, 260) for marker in _CANCEL_WORDS)

    for candidate in candidate_list:
        record_id = str(candidate.get("calendar_id") or "")
        candidate_id = str(candidate.get("candidate_id") or record_id)
        existing = next(
            (
                item for item in result
                if str(item.get("candidate_id") or item.get("calendar_id") or "") == candidate_id
                or (record_id and str(item.get("calendar_id") or "") == record_id and str(item.get("target_user_id") or "") == str(candidate.get("target_user_id") or ""))
            ),
            None,
        )
        if existing is None:
            candidate.setdefault("candidate_id", candidate_id)
            _sync_candidate_projection(candidate)
            if candidate.get("observation_intent") == "confirm":
                promoted = advance_calendar_lifecycle(
                    candidate,
                    "confirm",
                    evidence=(candidate.get("evidence") or [{}])[-1],
                    now=local_now,
                    timezone_name=timezone_name,
                )
                _preserve_candidate_metadata(candidate, promoted)
                candidate.clear()
                candidate.update(promoted)
                candidate["decision_trace"] = [{"operation": "confirmed", "at": local_now.isoformat(timespec="seconds"), "source_message_id": str(candidate.get("source_message_id") or "")}]
            _sync_candidate_projection(candidate)
            result.append(candidate)
            changed = True
            operation = "created_confirmed" if candidate.get("lifecycle_state") == "confirmed" else "created_candidate"
        else:
            before = deepcopy(existing)
            source_message_id = str(candidate.get("source_message_id") or "")
            is_replay = bool(source_message_id) and any(
                isinstance(item, dict) and str(item.get("source_id") or item.get("source_message_id") or "") == source_message_id
                for item in (existing.get("evidence") or [])
            )
            proposed = _candidate_proposed_record(existing)
            incoming = _candidate_proposed_record(candidate)
            if not is_replay:
                for key, value in incoming.items():
                    if key in {"evidence", "source_refs"}:
                        continue
                    if value not in (None, ""):
                        proposed[key] = deepcopy(value)
            existing["proposed_record"] = proposed
            for key in ("source_message_id", "source_user_id", "source_excerpt", "source_message_at", "conversation_id", "updated_at", "confidence", "observation_intent", "confirmation_requested"):
                if is_replay:
                    continue
                if candidate.get(key) not in (None, ""):
                    existing[key] = deepcopy(candidate[key])
            for evidence in candidate.get("evidence") or []:
                if isinstance(evidence, dict):
                    _append_evidence(existing, evidence)
            if not is_replay:
                proposed["evidence"] = deepcopy(existing.get("evidence") or proposed.get("evidence") or [])
                proposed["source_refs"] = deepcopy(existing.get("source_refs") or proposed.get("source_refs") or [])
            if candidate.get("observation_intent") == "confirm" and str(existing.get("lifecycle_state") or "candidate") not in {"confirmed", "active", "completed", "cancelled", "expired"}:
                promoted = advance_calendar_lifecycle(
                    existing,
                    "confirm",
                    evidence=(candidate.get("evidence") or [{}])[-1],
                    now=local_now,
                    timezone_name=timezone_name,
                )
                _preserve_candidate_metadata(existing, promoted)
                existing.clear()
                existing.update(promoted)
                existing["decision_trace"] = list(before.get("decision_trace") or []) + [{"operation": "confirmed", "at": local_now.isoformat(timespec="seconds"), "source_message_id": str(candidate.get("source_message_id") or "")}]
            elif (
                not is_replay
                and str(existing.get("kind") or "") in {"period", "recurrence"}
                and len(existing.get("evidence") or []) >= 2
                and str(existing.get("lifecycle_state") or existing.get("lifecycle") or "candidate") in {"candidate", "tentative"}
            ):
                promoted = advance_calendar_lifecycle(
                    existing,
                    "confirm",
                    evidence=(candidate.get("evidence") or [{}])[-1],
                    now=local_now,
                    timezone_name=timezone_name,
                )
                _preserve_candidate_metadata(existing, promoted)
                existing.clear()
                existing.update(promoted)
                existing["decision_trace"] = list(before.get("decision_trace") or []) + [{"operation": "repeated_evidence_confirmed", "at": local_now.isoformat(timespec="seconds")}]
            if not is_replay:
                existing["updated_at"] = local_now.isoformat(timespec="seconds")
                existing["revision"] = max(1, int(existing.get("revision") or 1)) + 1
            _sync_candidate_projection(existing)
            changed = changed or existing != before
            operation = "promoted" if existing.get("lifecycle_state") == "confirmed" and before.get("lifecycle_state") != "confirmed" else "evidence_added"
        audit.append({"operation": operation, "record_id": record_id, "candidate_id": candidate_id, "at": local_now.isoformat(timespec="seconds"), "evidence": deepcopy((candidate.get("evidence") or [{}])[-1])})

    # A short follow-up such as “确认了” carries an operation but no new
    # date/title.  Apply it to the most recently observed pending candidate so
    # the conversation does not need to repeat the entire arrangement.
    if confirmation and not candidate_list:
        pending = next(
            (
                item for item in reversed(result)
                if str(item.get("lifecycle_state") or item.get("lifecycle") or "candidate") in {"candidate", "tentative"}
            ),
            None,
        )
        if pending is not None:
            before = deepcopy(pending)
            promoted = advance_calendar_lifecycle(
                pending,
                "confirm",
                evidence={
                    "source_type": "message",
                    "quote": _text(text, 260),
                    "observed_at": local_now.isoformat(timespec="seconds"),
                },
                now=local_now,
                timezone_name=timezone_name,
            )
            _preserve_candidate_metadata(before, promoted)
            pending.clear()
            pending.update(promoted)
            pending["decision_trace"] = list(before.get("decision_trace") or []) + [{"operation": "confirmed", "at": local_now.isoformat(timespec="seconds"), "source_text": _text(text, 260)}]
            _sync_candidate_projection(pending)
            changed = True
            audit.append({"operation": "promoted", "record_id": str(pending.get("calendar_id") or ""), "candidate_id": str(pending.get("candidate_id") or ""), "at": local_now.isoformat(timespec="seconds"), "source_text": _text(text, 260)})

    if completion or cancellation:
        signal_text = _text(text, 260)
        for record in reversed(result):
            if str(record.get("lifecycle_state") or record.get("lifecycle")) in {"completed", "cancelled", "expired"}:
                continue
            topic_score = _topic_score(str(record.get("title") or ""), signal_text)
            pending_count = sum(
                1 for item in result
                if isinstance(item, dict)
                and str(item.get("lifecycle_state") or item.get("lifecycle") or "candidate") not in {"completed", "cancelled", "expired"}
            )
            if topic_score < 0.25 and not (pending_count == 1 and len(signal_text) <= 24):
                continue
            before = str(record.get("lifecycle_state") or record.get("lifecycle") or "candidate")
            lifecycle = "cancelled" if cancellation and not completion else "completed"
            transitioned = advance_calendar_lifecycle(record, lifecycle, evidence={"source_type": "message", "quote": signal_text, "observed_at": local_now.isoformat(timespec="seconds")}, now=local_now, timezone_name=timezone_name)
            record.clear()
            record.update(transitioned)
            record["resolved_at"] = local_now.isoformat(timespec="seconds")
            _sync_candidate_projection(record)
            changed = True
            audit.append({"operation": lifecycle, "record_id": str(record.get("calendar_id") or ""), "candidate_id": str(record.get("candidate_id") or ""), "previous_lifecycle": before, "at": local_now.isoformat(timespec="seconds"), "source_text": signal_text})
            break
    return result[-500:], audit[-100:], {"changed": changed, "created": sum(item.get("operation", "").startswith("created") for item in audit), "promoted": sum(item.get("operation") in {"promoted", "created_confirmed"} for item in audit), "resolved": sum(item.get("operation") in {"completed", "cancelled"} for item in audit)}


__all__ = ["extract_calendar_candidates", "merge_calendar_observations"]
