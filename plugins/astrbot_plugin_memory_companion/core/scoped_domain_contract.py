"""REQ-041 payload contract for profile, memory and learning projections."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


SCHEMA_VERSION = "req041.scoped-domain.v1"
DOMAINS = frozenset({"profile", "memory", "learning"})
SOURCE_KINDS = frozenset({"private", "group_member", "group_shared", "persona_global"})
APPROVAL_STATES = frozenset({"not_applicable", "pending", "approved", "rejected", "revoked"})
DOMAIN_RECORD_KINDS = {
    "profile": frozenset({"profile_fact"}),
    "memory": frozenset({"memory", "summary"}),
    "learning": frozenset({"rule", "evidence"}),
}
_FORBIDDEN_KEYS = frozenset({
    "relationship_score", "relationship_role", "relationship_mode", "owner", "authorized",
    "tool_permission", "tool_permissions", "proactive_permission", "raw_prompt", "system_prompt",
})


class ScopedDomainContractError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ScopedDomainContractError("scoped_domain_content_too_deep")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScopedDomainContractError("scoped_domain_content_non_finite")
        return value
    if isinstance(value, list):
        return [_safe(item, depth=depth + 1) for item in value[:512]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:512]:
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 96:
                raise ScopedDomainContractError("scoped_domain_content_key_invalid")
            key = raw_key.strip()
            if key.lower() in _FORBIDDEN_KEYS:
                raise ScopedDomainContractError("scoped_domain_privilege_field_denied")
            result[key] = _safe(item, depth=depth + 1)
        return result
    raise ScopedDomainContractError("scoped_domain_content_type_invalid")


def build_scoped_domain_payload(
    *,
    domain: str,
    source_kind: str,
    content: Any,
    source_revision: int = 0,
    approval_state: str = "not_applicable",
    approved_by: str = "",
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "domain": str(domain or "").strip(),
        "source_kind": str(source_kind or "").strip(),
        "source_revision": max(0, int(source_revision or 0)),
        "approval_state": str(approval_state or "").strip(),
        "approved_by": str(approved_by or "").strip(),
        "content": _safe(content),
    }
    validate_scoped_domain_payload(None, "", payload, envelope_only=True)
    payload["content_hash"] = hashlib.sha256(_canonical(payload["content"]).encode("utf-8")).hexdigest()
    return payload


def validate_scoped_domain_payload(
    context: Any,
    record_kind: str,
    payload: Any,
    *,
    envelope_only: bool = False,
) -> None:
    if not isinstance(payload, dict):
        raise ScopedDomainContractError("scoped_domain_payload_invalid")
    required = {
        "schema_version", "domain", "source_kind", "source_revision", "approval_state", "approved_by", "content"
    }
    allowed = required | {"content_hash"}
    if frozenset(payload) not in {frozenset(required), frozenset(allowed)}:
        raise ScopedDomainContractError("scoped_domain_payload_fields_invalid")
    domain = str(payload.get("domain") or "")
    source_kind = str(payload.get("source_kind") or "")
    approval = str(payload.get("approval_state") or "")
    approved_by = str(payload.get("approved_by") or "")
    if payload.get("schema_version") != SCHEMA_VERSION or domain not in DOMAINS:
        raise ScopedDomainContractError("scoped_domain_schema_invalid")
    if source_kind not in SOURCE_KINDS or approval not in APPROVAL_STATES:
        raise ScopedDomainContractError("scoped_domain_source_invalid")
    if not isinstance(payload.get("source_revision"), int) or int(payload["source_revision"]) < 0:
        raise ScopedDomainContractError("scoped_domain_revision_invalid")
    content = _safe(payload.get("content"))
    digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    if "content_hash" in payload and payload.get("content_hash") != digest:
        raise ScopedDomainContractError("scoped_domain_content_hash_mismatch")
    if envelope_only:
        return
    if record_kind not in DOMAIN_RECORD_KINDS[domain]:
        raise ScopedDomainContractError("scoped_domain_record_kind_mismatch")
    kind = str(getattr(context, "kind", "") or "")
    if kind != source_kind:
        raise ScopedDomainContractError("scoped_domain_namespace_mismatch")
    if domain == "profile" and kind not in {"private", "group_member"}:
        raise ScopedDomainContractError("scoped_profile_namespace_denied")
    if domain == "memory" and kind == "persona_global":
        raise ScopedDomainContractError("scoped_memory_persona_global_denied")
    if domain != "learning" and approval != "not_applicable":
        raise ScopedDomainContractError("scoped_domain_approval_invalid")
    if domain == "learning":
        if record_kind == "rule" and approval not in {"pending", "approved", "rejected", "revoked"}:
            raise ScopedDomainContractError("scoped_rule_approval_required")
        if kind == "persona_global" and (
            approval not in {"approved", "rejected", "revoked"} or approved_by != "administrator"
        ):
            raise ScopedDomainContractError("persona_global_rule_approval_required")


__all__ = [
    "APPROVAL_STATES", "DOMAINS", "DOMAIN_RECORD_KINDS", "SCHEMA_VERSION", "SOURCE_KINDS",
    "ScopedDomainContractError", "build_scoped_domain_payload", "validate_scoped_domain_payload",
]
