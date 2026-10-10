"""REQ-041 durable ownership contract for expression evidence and rules."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


BINDING_SCHEMA_VERSION = "req041.expression-scope.v1"
PROFILE_SCHEMA_VERSION = "req041.expression-profile-scope.v1"
APPROVAL_STATES = frozenset({"pending", "approved", "rejected", "revoked"})
OWNER_TYPES = frozenset({"private", "group", "persona"})
_BINDING_FIELDS = frozenset({
    "schema_version", "owner_type", "owner_id", "source_namespace",
    "application_namespace", "approval_state", "revision", "approved_by",
})
_PROFILE_FIELDS = frozenset({
    "schema_version", "owner_type", "owner_id", "source_namespace",
    "application_namespace", "revision",
})


class ExpressionScopeError(ValueError):
    """Stable fail-closed error for expression ownership violations."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _context_parts(context: Any) -> dict[str, str]:
    errors = getattr(context, "errors", None)
    if callable(errors):
        found = errors()
        if found:
            raise ExpressionScopeError(str(found[0]))
    kind = str(getattr(context, "kind", "") or "").strip()
    persona_id = str(getattr(context, "persona_id", "") or "").strip()
    identity_id = str(getattr(context, "identity_id", "") or "").strip()
    group_id = str(getattr(context, "group_id", "") or "").strip()
    assurance = str(getattr(context, "assurance", "") or "").strip()
    profile_status = str(getattr(context, "profile_status", "") or "").strip()
    policy_version = str(getattr(context, "policy_version", "") or "").strip()
    migration_epoch = str(getattr(context, "migration_epoch", "") or "").strip()
    if kind not in {"private", "group_shared", "persona_global"}:
        raise ExpressionScopeError("expression_scope_kind_denied")
    if assurance not in {"verified", "explicit_linked"} or profile_status != "active":
        raise ExpressionScopeError("expression_scope_assurance_denied")
    if not persona_id or not policy_version or not migration_epoch:
        raise ExpressionScopeError("expression_scope_context_incomplete")
    if kind == "private" and (not identity_id or group_id):
        raise ExpressionScopeError("expression_private_scope_invalid")
    if kind == "group_shared" and (identity_id or not group_id):
        raise ExpressionScopeError("expression_group_scope_invalid")
    if kind == "persona_global" and (identity_id or group_id):
        raise ExpressionScopeError("expression_persona_scope_invalid")
    return {
        "kind": kind,
        "persona_id": persona_id,
        "identity_id": identity_id,
        "group_id": group_id,
        "policy_version": policy_version,
        "migration_epoch": migration_epoch,
    }


def _scope_identity(context: Any) -> dict[str, str]:
    parts = _context_parts(context)
    kind = parts["kind"]
    owner_type = "private" if kind == "private" else "group" if kind == "group_shared" else "persona"
    owner_material = {
        "owner_type": owner_type,
        "persona_id": parts["persona_id"],
        "identity_id": parts["identity_id"],
        "group_id": parts["group_id"],
    }
    namespace_material = {
        **owner_material,
        "kind": kind,
        "policy_version": parts["policy_version"],
        "migration_epoch": parts["migration_epoch"],
    }
    return {
        "owner_type": owner_type,
        "owner_id": _digest("owner-", owner_material),
        "namespace": _digest("namespace-", namespace_material),
    }


def build_expression_profile_scope(context: Any, *, revision: int = 1) -> dict[str, Any]:
    identity = _scope_identity(context)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "owner_type": identity["owner_type"],
        "owner_id": identity["owner_id"],
        "source_namespace": identity["namespace"],
        "application_namespace": identity["namespace"],
        "revision": max(1, int(revision or 1)),
    }


def validate_expression_profile_scope(binding: Any, context: Any) -> dict[str, Any]:
    if not isinstance(binding, dict) or frozenset(binding) != _PROFILE_FIELDS:
        raise ExpressionScopeError("expression_profile_scope_fields_invalid")
    expected = build_expression_profile_scope(context, revision=int(binding.get("revision") or 0))
    if binding != expected:
        raise ExpressionScopeError("expression_profile_scope_mismatch")
    return deepcopy(binding)


def build_expression_scope_binding(
    context: Any,
    *,
    approval_state: str,
    revision: int = 1,
    approved_by: str = "",
) -> dict[str, Any]:
    identity = _scope_identity(context)
    state = str(approval_state or "").strip()
    actor = str(approved_by or "").strip()
    if state not in APPROVAL_STATES:
        raise ExpressionScopeError("expression_approval_state_invalid")
    if state == "approved" and not actor:
        raise ExpressionScopeError("expression_approved_by_required")
    if state == "pending" and actor:
        raise ExpressionScopeError("expression_pending_approved_by_invalid")
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "owner_type": identity["owner_type"],
        "owner_id": identity["owner_id"],
        "source_namespace": identity["namespace"],
        "application_namespace": identity["namespace"],
        "approval_state": state,
        "revision": max(1, int(revision or 1)),
        "approved_by": actor,
    }


def validate_expression_scope_binding(
    binding: Any,
    context: Any,
    *,
    approval_state: str = "",
) -> dict[str, Any]:
    if not isinstance(binding, dict) or frozenset(binding) != _BINDING_FIELDS:
        raise ExpressionScopeError("expression_scope_binding_fields_invalid")
    state = str(binding.get("approval_state") or "")
    expected = build_expression_scope_binding(
        context,
        approval_state=state,
        revision=int(binding.get("revision") or 0),
        approved_by=str(binding.get("approved_by") or ""),
    )
    if binding != expected:
        raise ExpressionScopeError("expression_scope_binding_mismatch")
    if approval_state and state != approval_state:
        raise ExpressionScopeError("expression_scope_approval_mismatch")
    return deepcopy(binding)


def bind_expression_item(
    item: Any,
    context: Any,
    *,
    approval_state: str,
    approved_by: str = "",
    bump_revision: bool = False,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ExpressionScopeError("expression_item_invalid")
    result = deepcopy(item)
    existing = result.get("scope_binding")
    revision = 1
    if existing is not None:
        checked = validate_expression_scope_binding(existing, context)
        if checked["approval_state"] != approval_state and not bump_revision:
            raise ExpressionScopeError("expression_scope_approval_mismatch")
        revision = int(checked["revision"]) + (1 if bump_revision else 0)
    result["scope_binding"] = build_expression_scope_binding(
        context,
        approval_state=approval_state,
        revision=revision,
        approved_by=approved_by,
    )
    return result


def bind_expression_profile(
    profile: Any,
    context: Any,
    *,
    bump_revision: bool = False,
) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ExpressionScopeError("expression_profile_invalid")
    result = deepcopy(profile)
    existing = result.get("scope_ownership")
    revision = max(1, int(result.get("scope_revision") or 1))
    if existing is not None:
        checked = validate_expression_profile_scope(existing, context)
        if revision != int(checked["revision"]):
            raise ExpressionScopeError("expression_profile_revision_mismatch")
    if bump_revision and existing is not None:
        revision += 1
    result["scope_revision"] = revision
    result["scope_ownership"] = build_expression_profile_scope(context, revision=revision)
    return result


def runtime_binding_is_approved(binding: Any) -> bool:
    """Validate self-contained fields before a scoped rule reaches selection."""
    if not isinstance(binding, dict) or frozenset(binding) != _BINDING_FIELDS:
        return False
    try:
        revision = int(binding.get("revision") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        binding.get("schema_version") == BINDING_SCHEMA_VERSION
        and binding.get("owner_type") in OWNER_TYPES
        and str(binding.get("owner_id") or "").startswith("owner-")
        and len(str(binding.get("owner_id") or "")) == 70
        and str(binding.get("source_namespace") or "").startswith("namespace-")
        and len(str(binding.get("source_namespace") or "")) == 74
        and binding.get("source_namespace") == binding.get("application_namespace")
        and binding.get("approval_state") == "approved"
        and binding.get("approved_by") in {"administrator", "legacy_migration"}
        and revision >= 1
    )


__all__ = [
    "APPROVAL_STATES", "BINDING_SCHEMA_VERSION", "ExpressionScopeError",
    "bind_expression_item", "bind_expression_profile", "build_expression_profile_scope",
    "build_expression_scope_binding", "runtime_binding_is_approved",
    "validate_expression_profile_scope", "validate_expression_scope_binding",
]
