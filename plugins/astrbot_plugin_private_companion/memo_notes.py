# -*- coding: utf-8 -*-
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .helpers import _safe_float, _single_line


MEMO_NOTE_COLORS = {"yellow", "blue", "green", "rose", "gray"}
MEMO_NOTE_REPEATS = {"none", "daily", "weekly", "monthly", "yearly"}


def clean_memo_note_content(value: Any, limit: int = 800) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()[:limit]


def normalize_memo_note(raw: Any, *, now: float = 0.0) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    note_id = _single_line(raw.get("id"), 64)
    title = _single_line(raw.get("title"), 60)
    content = clean_memo_note_content(raw.get("content"), 800)
    if not note_id or not (title or content):
        return None
    current = now if now > 0 else datetime.now().timestamp()
    status = _single_line(raw.get("status"), 20).lower()
    if status not in {"active", "completed"}:
        status = "active"
    repeat = _single_line(raw.get("repeat"), 20).lower()
    if repeat not in MEMO_NOTE_REPEATS:
        repeat = "none"
    color = _single_line(raw.get("color"), 20).lower()
    if color not in MEMO_NOTE_COLORS:
        color = "yellow"
    due_at = max(0.0, _safe_float(raw.get("due_at"), 0.0))
    created_at = max(0.0, _safe_float(raw.get("created_at"), current)) or current
    updated_at = max(created_at, _safe_float(raw.get("updated_at"), created_at))
    return {
        "id": note_id,
        "title": title,
        "content": content,
        "color": color,
        "pinned": bool(raw.get("pinned")),
        "status": status,
        "due_at": due_at,
        "repeat": repeat,
        "repeat_anchor_day": max(0, min(31, int(_safe_float(raw.get("repeat_anchor_day"), 0.0)))),
        "repeat_anchor_month": max(0, min(12, int(_safe_float(raw.get("repeat_anchor_month"), 0.0)))),
        "remind_enabled": bool(raw.get("remind_enabled", True)) and due_at > 0,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": max(0.0, _safe_float(raw.get("completed_at"), 0.0)),
        "last_completed_at": max(0.0, _safe_float(raw.get("last_completed_at"), 0.0)),
        "completion_count": max(0, int(_safe_float(raw.get("completion_count"), 0.0))),
        "last_reminder_offer_at": max(0.0, _safe_float(raw.get("last_reminder_offer_at"), 0.0)),
        "last_reminder_attempt_at": max(0.0, _safe_float(raw.get("last_reminder_attempt_at"), 0.0)),
    }


def memo_note_due_state(note: dict[str, Any], *, now: float) -> str:
    if str(note.get("status") or "") == "completed":
        return "completed"
    due_at = _safe_float(note.get("due_at"), 0.0)
    if due_at <= 0:
        return "none"
    delta = due_at - now
    if delta < -60:
        return "overdue"
    if delta <= 30 * 60:
        return "due"
    if delta <= 24 * 3600:
        return "today"
    return "upcoming"


def advance_recurring_memo_due(
    due_at: float,
    repeat: str,
    *,
    now: float,
    fromtimestamp: Callable[[float], datetime] = datetime.fromtimestamp,
    anchor_day: int = 0,
    anchor_month: int = 0,
) -> float:
    repeat_key = str(repeat or "none").lower()
    if due_at <= 0 or repeat_key not in MEMO_NOTE_REPEATS - {"none"}:
        return 0.0
    candidate = fromtimestamp(due_at)
    target = fromtimestamp(now)
    for _ in range(500):
        if repeat_key == "daily":
            candidate += timedelta(days=1)
        elif repeat_key == "weekly":
            candidate += timedelta(days=7)
        elif repeat_key == "monthly":
            year = candidate.year + (1 if candidate.month == 12 else 0)
            month = 1 if candidate.month == 12 else candidate.month + 1
            day = min(anchor_day or candidate.day, calendar.monthrange(year, month)[1])
            candidate = candidate.replace(year=year, month=month, day=day)
        elif repeat_key == "yearly":
            year = candidate.year + 1
            month = anchor_month or candidate.month
            day = min(anchor_day or candidate.day, calendar.monthrange(year, month)[1])
            candidate = candidate.replace(year=year, month=month, day=day)
        if candidate > target:
            return candidate.timestamp()
    return 0.0


def memo_note_sort_key(note: dict[str, Any], *, now: float) -> tuple[Any, ...]:
    status = str(note.get("status") or "active")
    due_at = _safe_float(note.get("due_at"), 0.0)
    due_bucket = 0 if due_at > 0 and due_at <= now else 1 if due_at > 0 else 2
    due_sort = due_at if due_at > 0 else float("inf")
    return (
        1 if status == "completed" else 0,
        0 if note.get("pinned") else 1,
        due_bucket,
        due_sort,
        -_safe_float(note.get("updated_at"), 0.0),
    )


def apply_memo_note_action(
    raw_notes: Any,
    payload: dict[str, Any],
    *,
    now: float,
    fromtimestamp: Callable[[float], datetime] = datetime.fromtimestamp,
    partial: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply one memo mutation and return normalized notes plus the affected note."""
    source = raw_notes if isinstance(raw_notes, list) else []
    notes = [note for note in (normalize_memo_note(item, now=now) for item in source) if note]
    action = _single_line(payload.get("action"), 20).lower() or "save"
    note_id = _single_line(payload.get("id"), 64)
    existing = next((item for item in notes if item.get("id") == note_id), None) if note_id else None

    if action == "delete":
        if not existing:
            raise ValueError("没有找到这张便签")
        deleted = dict(existing)
        return [item for item in notes if item.get("id") != note_id][-200:], deleted

    if action in {"complete", "reopen", "pin", "unpin"}:
        if not existing:
            raise ValueError("没有找到这张便签")
        if action == "reopen":
            existing["status"] = "active"
            existing["completed_at"] = 0.0
        elif action == "pin":
            existing["pinned"] = True
        elif action == "unpin":
            existing["pinned"] = False
        elif existing.get("repeat") != "none" and _safe_float(existing.get("due_at"), 0.0) > 0:
            existing["due_at"] = advance_recurring_memo_due(
                _safe_float(existing.get("due_at"), 0.0),
                str(existing.get("repeat") or "none"),
                now=now,
                fromtimestamp=fromtimestamp,
                anchor_day=int(_safe_float(existing.get("repeat_anchor_day"), 0.0)),
                anchor_month=int(_safe_float(existing.get("repeat_anchor_month"), 0.0)),
            )
            existing["status"] = "active"
            existing["last_completed_at"] = now
            existing["completion_count"] = int(_safe_float(existing.get("completion_count"), 0.0)) + 1
            existing["last_reminder_offer_at"] = 0.0
            existing["last_reminder_attempt_at"] = 0.0
        else:
            existing["status"] = "completed"
            existing["completed_at"] = now
            existing["last_completed_at"] = now
            existing["completion_count"] = int(_safe_float(existing.get("completion_count"), 0.0)) + 1
        existing["updated_at"] = now
        return notes[-200:], existing

    if action != "save":
        raise ValueError("不支持的便签操作")

    def value_for(key: str, default: Any) -> Any:
        if partial and existing is not None and key not in payload:
            return existing.get(key, default)
        return payload.get(key, default)

    title = _single_line(value_for("title", ""), 60)
    content = clean_memo_note_content(value_for("content", ""), 800)
    if not title and not content:
        raise ValueError("标题和内容至少填写一项")
    repeat = _single_line(value_for("repeat", "none"), 20).lower() or "none"
    if repeat not in MEMO_NOTE_REPEATS:
        repeat = "none"
    color = _single_line(value_for("color", "yellow"), 20).lower() or "yellow"
    if color not in MEMO_NOTE_COLORS:
        color = "yellow"
    due_at = max(0.0, _safe_float(value_for("due_at", 0.0), 0.0))
    if due_at > datetime(2100, 1, 1).timestamp():
        raise ValueError("到期时间超出支持范围")
    if repeat != "none" and due_at <= 0:
        raise ValueError("重复便签需要设置到期时间")

    if existing is None:
        existing = {
            "id": f"memo-{uuid.uuid4().hex[:16]}",
            "created_at": now,
            "status": "active",
            "completed_at": 0.0,
            "last_completed_at": 0.0,
            "completion_count": 0,
            "last_reminder_offer_at": 0.0,
            "last_reminder_attempt_at": 0.0,
        }
        notes.append(existing)
    prior_due_at = _safe_float(existing.get("due_at"), 0.0)
    existing.update({
        "title": title,
        "content": content,
        "color": color,
        "pinned": bool(value_for("pinned", False)),
        "due_at": due_at,
        "repeat": repeat,
        "remind_enabled": bool(value_for("remind_enabled", True)) and due_at > 0,
        "updated_at": now,
    })
    if due_at > 0 and due_at != prior_due_at:
        due_dt = fromtimestamp(due_at)
        existing["repeat_anchor_day"] = due_dt.day
        existing["repeat_anchor_month"] = due_dt.month
    if existing.get("status") == "completed" and bool(payload.get("reopen")):
        existing["status"] = "active"
        existing["completed_at"] = 0.0
    if due_at != prior_due_at:
        existing["last_reminder_offer_at"] = 0.0
        existing["last_reminder_attempt_at"] = 0.0
    return notes[-200:], existing
