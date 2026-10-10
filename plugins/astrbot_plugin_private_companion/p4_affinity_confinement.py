"""Narrow P4 runtime-state contract for the chat-side Companion.

This module deliberately has no score model and no persistence.  The legacy
relationship score remains separate; it can only be isolated by an explicit
compatibility switch in the host.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from typing import Any


P4_RUNTIME_STATE_SCHEMA = "chat.p4.runtime_state.v1"
P4_RUNTIME_AUTHORITY = "private_companion_p4"
P4_RUNTIME_FIELDS = frozenset({
    "schema_version", "authority", "confinement_state", "confinement_until", "warmth",
})
P4_WARMTH_TIERS = frozenset({"guarded", "neutral", "warm", "close"})
P4_CONFINEMENT_STATES = frozenset({"none", "active", "released"})


def _has_exact_str_fields(value: dict[Any, Any], fields: frozenset[str]) -> bool:
    if len(value) != len(fields):
        return False
    for key in value:
        if type(key) is not str or key not in fields:
            return False
    return True


def validate_runtime_state(value: Any, *, now: Any | None = None) -> str:
    """Return a stable status; malformed state is never repaired or trusted."""
    if type(value) is not dict or not _has_exact_str_fields(value, P4_RUNTIME_FIELDS):
        return "invalid"
    schema_version = value.get("schema_version")
    authority = value.get("authority")
    if type(schema_version) is not str or schema_version != P4_RUNTIME_STATE_SCHEMA:
        return "invalid"
    if type(authority) is not str or authority != P4_RUNTIME_AUTHORITY:
        return "invalid"
    state = value.get("confinement_state")
    warmth = value.get("warmth")
    until = value.get("confinement_until")
    if type(state) is not str or state not in P4_CONFINEMENT_STATES:
        return "invalid"
    if type(warmth) is not str or warmth not in P4_WARMTH_TIERS or type(until) is not str:
        return "invalid"
    if state == "active":
        deadline = _timestamp(until)
        moment = _timestamp(now) if now is not None else datetime.now(timezone.utc)
        if deadline is None or moment is None:
            return "invalid"
        return "expired" if moment >= deadline else "active"
    if until:
        return "invalid"
    return "released" if state == "released" else "normal"


def copy_runtime_state(value: Any) -> dict[str, str] | None:
    """Copy only an exact valid state shape for a request-local gate."""
    if validate_runtime_state(value) == "invalid" or type(value) is not dict:
        return None
    return deepcopy(value)


def apply_legacy_relationship_delta(user: Any, delta: Any, *, isolate: bool) -> bool:
    """Preserve legacy writes by default; isolation intentionally does nothing."""
    if isolate or type(user) is not dict or type(delta) is not int or isinstance(delta, bool):
        return False
    current = user.get("relationship_score")
    if type(current) is int:
        score = current
    elif type(current) is float and isfinite(current):
        score = int(current)
    elif type(current) is str:
        try:
            score = int(current)
        except ValueError:
            score = 0
    else:
        score = 0
    user["relationship_score"] = max(0, score) + delta
    return True


def _timestamp(value: Any) -> datetime | None:
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    if type(value) is not str or not value or len(value) > 64 or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "P4_RUNTIME_AUTHORITY", "P4_RUNTIME_FIELDS", "P4_RUNTIME_STATE_SCHEMA",
    "P4_WARMTH_TIERS", "apply_legacy_relationship_delta", "copy_runtime_state",
    "validate_runtime_state",
]
