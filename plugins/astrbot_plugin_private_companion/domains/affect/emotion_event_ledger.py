"""Bounded recent emotion-event ledger owned by Companion user profiles."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

try:
    from .emotion_event_contract import normalize_emotion_event
except ImportError:  # direct-module tests
    from emotion_event_contract import normalize_emotion_event  # type: ignore


DEFAULT_RECENT_EVENT_LIMIT = 64


def record_recent_emotion_event(
    user: dict[str, Any],
    value: Any,
    *,
    limit: int = DEFAULT_RECENT_EVENT_LIMIT,
) -> tuple[dict[str, Any], bool]:
    event = normalize_emotion_event(value, producer_plugin="private_companion")
    ledger = user.get("emotion_event_ledger")
    if not isinstance(ledger, list):
        ledger = []
    kept: list[dict[str, Any]] = []
    duplicate = False
    for item in ledger:
        if not isinstance(item, dict):
            continue
        if item.get("event_id") == event["event_id"] and int(item.get("revision") or 0) == event["revision"]:
            duplicate = True
            kept.append(item)
        else:
            kept.append(item)
    if not duplicate:
        kept.append(event)
    kept.sort(key=lambda item: (str(item.get("occurred_at") or ""), int(item.get("revision") or 0)))
    user["emotion_event_ledger"] = kept[-max(8, min(256, int(limit or DEFAULT_RECENT_EVENT_LIMIT))):]
    user["last_emotion_event"] = {
        "event_id": event["event_id"],
        "trace_id": event["trace_id"],
        "revision": event["revision"],
        "event_type": event["event_type"],
        "status": event["status"],
        "occurred_at": event["occurred_at"],
    }
    return deepcopy(event), not duplicate


def emotion_trace_from_user(user: Any, trace_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not isinstance(user, dict) or not isinstance(trace_id, str) or not trace_id:
        return []
    ledger = user.get("emotion_event_ledger")
    if not isinstance(ledger, list):
        return []
    result = [deepcopy(item) for item in ledger if isinstance(item, dict) and item.get("trace_id") == trace_id]
    result.sort(key=lambda item: int(item.get("revision") or 0))
    return result[-max(1, min(100, int(limit or 20))):]


__all__ = ["DEFAULT_RECENT_EVENT_LIMIT", "emotion_trace_from_user", "record_recent_emotion_event"]
