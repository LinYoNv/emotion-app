"""Read-only consumer for the P3 context projection contract."""

from __future__ import annotations

from typing import Any

from .person_context_contract import (
    P3_SLOT_NAMES,
    build_context_projection,
    merge_context_slots,
    validate_context_projection,
    validate_context_slot,
)


_FORBIDDEN_KEYS = {
    "raw_prompt",
    "prompt",
    "private_object",
    "private_object_ref",
    "object",
    "chat_text",
    "content",
    "messages",
    "transcript",
    "database",
}


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Make a bounded metadata-only copy and drop text-bearing/private data."""

    if depth > 2 or value is None or isinstance(value, bool):
        return value if value is None or isinstance(value, bool) else None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            name = str(key).strip().lower()
            if not name or name in _FORBIDDEN_KEYS:
                continue
            safe = _safe_value(item, depth + 1)
            if safe is not None:
                result[name[:80]] = safe
        return result
    return None


def _slot_ref(slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": slot["slot"],
        "owner": slot["owner"],
        "revision": slot["revision"],
        "contract_fingerprint": slot["contract_fingerprint"],
        "state": slot["state"],
        "payload": _safe_value(slot.get("payload") or {}) or {},
        "warnings": [str(item)[:120] for item in slot.get("warnings", [])[:8]],
    }


def consume_context_projection(
    context: Any,
    expected_person_id: str = "",
    expected_scope: str = "",
    *,
    companion_available: bool = True,
) -> dict[str, Any]:
    """Validate and consume P3 slots, never persisting or mutating input.

    Invalid slots are rejected individually.  Valid slots are merged using the
    shared revision-aware contract helper, so an older duplicate cannot
    replace a newer slot.  The returned context contains only safe metadata.
    """

    base = {"read_only": True, "source": "companion_context"}
    if not companion_available:
        return {**base, "state": "degraded", "degraded": True, "errors": ["companion_unavailable"]}
    if context is None or (isinstance(context, dict) and "slots" not in context):
        return {**base, "state": "legacy_local", "legacy_local": True, "warnings": ["context_missing"]}
    if not isinstance(context, dict):
        return {**base, "state": "invalid", "errors": ["context_invalid"]}

    errors = list(validate_context_projection(context))
    context_state = str(context.get("state") or "ready")
    if context_state not in {"ready", "legacy_local", "invalid", "degraded", "pending"}:
        errors.append("context_state_invalid")
    if context_state == "invalid":
        errors.append("context_invalid_state")
    if expected_person_id:
        context_person_id = context.get("person_id")
        if context_person_id != expected_person_id:
            errors.append("person_id_mismatch")
    if expected_scope:
        context_scope = context.get("scope")
        if context_scope != expected_scope:
            errors.append("scope_mismatch")

    incoming_slots = context.get("slots") if isinstance(context.get("slots"), dict) else {}
    accepted: dict[str, dict[str, Any]] = {}
    rejected: dict[str, list[str]] = {}
    for name in P3_SLOT_NAMES:
        candidate = incoming_slots.get(name)
        slot_errors = validate_context_slot(candidate, name)
        if slot_errors:
            rejected[name] = slot_errors
            continue
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        if expected_person_id and payload.get("person_id") != expected_person_id:
            rejected[name] = ["person_id_mismatch"]
            continue
        if expected_scope and payload.get("scope") != expected_scope:
            rejected[name] = ["scope_mismatch"]
            continue
        if candidate.get("state") == "invalid":
            errors.append(f"{name}_state_invalid")
            rejected[name] = ["slot_state_invalid"]
            continue
        accepted[name] = dict(candidate)

    if errors:
        return {**base, "state": "invalid", "errors": sorted(set(errors)), "rejected_slots": rejected}

    safe_incoming = {"revision": context.get("revision", 1), "slots": accepted}
    merged = merge_context_slots(build_context_projection(), safe_incoming)
    safe_slots = {name: _slot_ref(merged["slots"][name]) for name in P3_SLOT_NAMES}
    slot_states = {str(slot.get("state") or "ready") for slot in accepted.values()}
    if context_state == "degraded" or "degraded" in slot_states:
        state = "degraded"
    elif context_state == "pending" or "pending" in slot_states:
        state = "pending"
    elif context_state == "legacy_local" or slot_states and slot_states.issubset({"legacy_local"}):
        state = "legacy_local"
    else:
        state = "ready"
    result: dict[str, Any] = {
        **base,
        "state": state,
        "degraded": state == "degraded",
        "context_ref": {
            "revision": merged["revision"],
            "contract_fingerprint": merged["contract_fingerprint"],
            "slots": safe_slots,
        },
    }
    if rejected:
        result["rejected_slots"] = rejected
    return result


__all__ = ["consume_context_projection"]
