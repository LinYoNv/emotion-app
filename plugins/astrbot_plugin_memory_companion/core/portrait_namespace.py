"""REQ-041 namespace boundary for Memory-owned portrait data."""
from __future__ import annotations

import hashlib
from typing import Any

from .namespace import AssurancePolicy, NamespaceContext, build_namespace_context


def _legacy_scope(value: Any) -> str:
    scope = str(value or "").strip()
    if scope == "private":
        return scope
    if scope.startswith("group:") and len(scope) <= 240:
        return scope
    return ""


def portrait_scope_kind(value: Any) -> str:
    scope = str(value or "").strip()
    if scope == "private" or scope.startswith("private@"):
        return "private"
    if scope.startswith("group:") or scope.startswith("group_member@"):
        return "group_member"
    return ""


def portrait_scope_persona(value: Any) -> str:
    scope = str(value or "").strip()
    parts = scope.split("@")
    if len(parts) != 3 or len(parts[1]) != 16:
        return ""
    return parts[1]


def portrait_namespace_decision(
    value: Any,
    *,
    person_id: Any,
    legacy_scope: Any,
    purpose: str,
    namespace_present: bool | None = None,
) -> dict[str, Any]:
    """Resolve a portrait scope without ever embedding raw identity/group IDs."""
    clean_person = str(person_id or "").strip()
    clean_legacy = _legacy_scope(legacy_scope)
    if not clean_person or not clean_legacy:
        return {"ok": False, "code": "portrait_namespace_invalid", "state": "invalid"}
    present = value is not None if namespace_present is None else bool(namespace_present)
    if not present:
        return {
            "ok": True,
            "code": "portrait_namespace_legacy",
            "state": "legacy",
            "source_scope": clean_legacy,
            "legacy_scope": clean_legacy,
            "context": None,
        }
    if value is None:
        return {"ok": False, "code": "portrait_namespace_invalid", "state": "invalid"}
    context = build_namespace_context(value)
    if context is None or context.errors():
        return {"ok": False, "code": "portrait_namespace_invalid", "state": "invalid"}
    decision = AssurancePolicy.authorize(context, purpose)
    if not decision.allowed:
        return {"ok": False, "code": decision.code, "state": "denied"}
    expected_kind = "private" if clean_legacy == "private" else "group_member"
    if (
        context.kind != expected_kind
        or context.identity_id != clean_person
        or context.profile_status != "active"
    ):
        return {"ok": False, "code": "portrait_namespace_mismatch", "state": "invalid"}
    persona_digest = hashlib.sha256(context.persona_id.encode("utf-8")).hexdigest()[:16]
    scope_digest = hashlib.sha256(context.cache_scope().encode("utf-8")).hexdigest()[:48]
    return {
        "ok": True,
        "code": "portrait_namespace_exact",
        "state": "exact",
        "source_scope": f"{context.kind}@{persona_digest}@{scope_digest}",
        "legacy_scope": clean_legacy if context.persona_id == "default" else "",
        "context": context,
    }


__all__ = [
    "portrait_namespace_decision", "portrait_scope_kind", "portrait_scope_persona",
]
