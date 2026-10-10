"""Chat-side P3 context orchestration.

This module is deliberately a read-only coordinator.  It creates a bounded
projection for the companion runtime; it does not alter the normal response
path, sinks, or any persistent person store.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

try:  # package import
    from .person_context_contract import (
        P3_CONTRACT_NAME,
        P3_CONTRACT_VERSION,
        P3_SLOT_NAMES,
        P3_SLOT_OWNERS,
        build_context_projection,
        make_context_slot,
        merge_context_slots,
        validate_context_projection,
        validate_context_slot,
    )
except ImportError:  # direct test/import fallback
    from person_context_contract import (  # type: ignore
        P3_CONTRACT_NAME,
        P3_CONTRACT_VERSION,
        P3_SLOT_NAMES,
        P3_SLOT_OWNERS,
        build_context_projection,
        make_context_slot,
        merge_context_slots,
        validate_context_projection,
        validate_context_slot,
    )


_GROUP_KEYS = frozenset({
    "group", "group_id", "group_name", "group_member", "group_members",
    "member", "members", "群", "群聊", "群成员", "群信息",
})


def _as_mapping(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _contains_group_fact(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).strip().lower() in _GROUP_KEYS for key in value) or any(
            _contains_group_fact(item) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_group_fact(item) for item in value)
    return False


def _slot_payload(name: str, value: Any) -> tuple[dict[str, Any], list[str]]:
    payload = _as_mapping(value)
    warnings: list[str] = []
    if name == "person" and _contains_group_fact(payload):
        return {}, ["person_group_domain_mixed"]
    return payload, warnings


def _make_slot(name: str, value: Any, *, revision: int, bridge_available: bool) -> dict[str, Any]:
    payload, warnings = _slot_payload(name, value)
    if not bridge_available:
        return make_context_slot(
            name, payload, revision=revision, state="degraded",
            warnings=warnings + ["bridge_unavailable"],
        )
    if name == "person" and warnings:
        return make_context_slot(name, {}, revision=revision, state="invalid", warnings=warnings)
    return make_context_slot(name, payload, revision=revision, state="ready", warnings=warnings)


def build_context(
    slots: dict[str, Any] | None = None,
    *,
    persona: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    person: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
    revisions: dict[str, int] | None = None,
    bridge_available: bool = True,
    existing: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a four-slot P3 projection without exposing raw conversation data.

    ``slots`` is accepted for callers that already have a slot map.  Explicit
    keyword values take precedence when supplied.  Existing projections are
    merged by slot and revision, so replaying an identical event is harmless.
    """
    provided = _as_mapping(slots)
    explicit = {"persona": persona, "runtime": runtime, "person": person, "scene": scene}
    for name, value in explicit.items():
        if value is not None:
            provided[name] = value
    revision_map = revisions if isinstance(revisions, dict) else {}
    built: dict[str, dict[str, Any]] = {}
    for name in P3_SLOT_NAMES:
        revision = revision_map.get(name, 1)
        try:
            revision = max(1, int(revision))
        except (TypeError, ValueError):
            revision = 1
        built[name] = _make_slot(
            name, provided.get(name, {}), revision=revision,
            bridge_available=bridge_available,
        )
    missing_bridge_context = not provided
    state = "degraded" if not bridge_available else ("legacy_local" if missing_bridge_context else "ready")
    if missing_bridge_context and bridge_available:
        for slot in built.values():
            slot["state"] = "legacy_local"
            slot["warnings"] = list(slot.get("warnings") or []) + ["p3_context_missing"]
    result = build_context_projection(
        built, state=state, revision=max([slot["revision"] for slot in built.values()], default=1),
        warnings=list(warnings or []) + (["bridge_unavailable"] if not bridge_available else (["p3_context_missing"] if missing_bridge_context else [])),
    )
    if existing is not None:
        result = merge_context_slots(existing, result)
    return result


def project_context(context: Any, *, bridge_available: bool = True) -> dict[str, Any]:
    """Return a defensive P3 projection, degrading rather than trusting bad input."""
    if not isinstance(context, dict):
        return build_context(bridge_available=bridge_available, warnings=["context_invalid"])
    candidate = deepcopy(context)
    errors = validate_context_projection(candidate)
    if errors:
        return build_context(
            bridge_available=False,
            warnings=["context_rejected"] + errors[:8],
        )
    if not bridge_available:
        candidate["state"] = "degraded"
        candidate["warnings"] = list(candidate.get("warnings") or []) + ["bridge_unavailable"]
    return candidate


def validate_context(context: Any) -> list[str]:
    """Expose the shared contract validator as the orchestration API."""
    return validate_context_projection(context)


__all__ = [
    "P3_CONTRACT_NAME", "P3_CONTRACT_VERSION", "P3_SLOT_NAMES", "P3_SLOT_OWNERS",
    "build_context", "project_context", "validate_context", "build_context_projection",
    "make_context_slot", "merge_context_slots", "validate_context_slot",
]
