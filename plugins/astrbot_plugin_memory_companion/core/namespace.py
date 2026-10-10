"""REQ-041 strict namespace and identity-assurance contract.

This module is intentionally pure and dependency free so Companion and Memory
can validate the exact same boundary before either side enables new reads.
Legacy callers do not use this module implicitly: missing context always fails
closed and an explicit legacy switch must select the old path.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, ClassVar


CONTRACT_NAME = "chat.namespace_context.v1"
CONTRACT_VERSION = "1.0"
NAMESPACE_KINDS = frozenset({"private", "group_member", "group_shared", "persona_global", "pending"})
IDENTITY_ASSURANCE = frozenset({"unverified", "observed", "verified", "explicit_linked"})
PROFILE_STATUS = frozenset({"active", "suspended", "quarantined", "deleted"})
FORMAL_ASSURANCE = frozenset({"verified", "explicit_linked"})
FORMAL_PURPOSES = frozenset({
    "relationship_read", "relationship_write", "profile_read", "profile_write",
    "memory_read", "memory_write", "rule_read", "rule_write", "cross_domain_projection",
})
CONTEXT_FIELDS = (
    "contract_name", "contract_version", "contract_fingerprint", "kind", "persona_id", "identity_id", "group_id",
    "assurance", "profile_status", "policy_version", "migration_epoch",
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _shape() -> dict[str, Any]:
    return {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "fields": list(CONTEXT_FIELDS),
        "kinds": sorted(NAMESPACE_KINDS),
        "assurance": sorted(IDENTITY_ASSURANCE),
        "profile_status": sorted(PROFILE_STATUS),
        "formal_purposes": sorted(FORMAL_PURPOSES),
    }


CONTRACT_FINGERPRINT = hashlib.sha256(_canonical(_shape()).encode("utf-8")).hexdigest()[:16]


def _token(value: Any, *, version: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    result = value.strip()
    pattern = _VERSION_RE if version else _ID_RE
    return result if pattern.fullmatch(result) else ""


@dataclass(frozen=True, slots=True)
class NamespaceContext:
    kind: str
    identity_id: str
    group_id: str
    assurance: str
    profile_status: str
    policy_version: str
    migration_epoch: str
    persona_id: str = "default"

    contract_name: ClassVar[str] = CONTRACT_NAME
    contract_version: ClassVar[str] = CONTRACT_VERSION
    contract_fingerprint: ClassVar[str] = CONTRACT_FINGERPRINT

    def errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.kind not in NAMESPACE_KINDS:
            errors.append("namespace_kind_invalid")
        if not _token(self.persona_id):
            errors.append("namespace_persona_required")
        identity = _token(self.identity_id)
        group = _token(self.group_id)
        if self.kind in {"private", "group_member", "pending"} and not identity:
            errors.append("namespace_identity_required")
        if self.kind in {"group_shared", "persona_global"} and self.identity_id:
            errors.append("namespace_identity_forbidden")
        if self.kind in {"group_member", "group_shared"} and not group:
            errors.append("namespace_group_required")
        if self.kind in {"private", "persona_global", "pending"} and self.group_id:
            errors.append("namespace_group_forbidden")
        if self.assurance not in IDENTITY_ASSURANCE:
            errors.append("namespace_assurance_invalid")
        if self.profile_status not in PROFILE_STATUS:
            errors.append("namespace_profile_status_invalid")
        if not _token(self.policy_version, version=True):
            errors.append("namespace_policy_version_required")
        if not _token(self.migration_epoch, version=True):
            errors.append("namespace_migration_epoch_required")
        return tuple(dict.fromkeys(errors))

    def to_dict(self) -> dict[str, str]:
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "kind": self.kind,
            "persona_id": self.persona_id,
            "identity_id": self.identity_id,
            "group_id": self.group_id,
            "assurance": self.assurance,
            "profile_status": self.profile_status,
            "policy_version": self.policy_version,
            "migration_epoch": self.migration_epoch,
        }

    def cache_scope(self) -> str:
        identity_hash = hashlib.sha256(self.identity_id.encode("utf-8")).hexdigest()[:16] if self.identity_id else "none"
        group_hash = hashlib.sha256(self.group_id.encode("utf-8")).hexdigest()[:16] if self.group_id else "none"
        persona_hash = hashlib.sha256(self.persona_id.encode("utf-8")).hexdigest()[:16]
        return f"{self.kind}:{persona_hash}:{identity_hash}:{group_hash}:{self.policy_version}:{self.migration_epoch}"


def build_namespace_context(value: Any) -> NamespaceContext | None:
    if not isinstance(value, dict):
        return None
    return NamespaceContext(
        kind=str(value.get("kind") or "").strip(),
        persona_id=str(value.get("persona_id") or "").strip(),
        identity_id=str(value.get("identity_id") or "").strip(),
        group_id=str(value.get("group_id") or "").strip(),
        assurance=str(value.get("assurance") or "").strip(),
        profile_status=str(value.get("profile_status") or "").strip(),
        policy_version=str(value.get("policy_version") or "").strip(),
        migration_epoch=str(value.get("migration_epoch") or "").strip(),
    )


def validate_namespace_context(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["namespace_context_missing"]
    errors: list[str] = []
    if set(value) != set(CONTEXT_FIELDS):
        errors.append("namespace_context_fields_invalid")
    if value.get("contract_name") != CONTRACT_NAME or value.get("contract_version") != CONTRACT_VERSION:
        errors.append("namespace_contract_mismatch")
    if value.get("contract_fingerprint") != CONTRACT_FINGERPRINT:
        errors.append("namespace_contract_fingerprint_mismatch")
    context = build_namespace_context(value)
    errors.extend(context.errors() if context is not None else ("namespace_context_invalid",))
    return list(dict.fromkeys(errors))


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    code: str


class AssurancePolicy:
    """Single fail-closed interpretation of assurance and profile lifecycle."""

    @staticmethod
    def authorize(context: NamespaceContext | None, purpose: str) -> AccessDecision:
        if context is None:
            return AccessDecision(False, "namespace_context_missing")
        errors = context.errors()
        if errors:
            return AccessDecision(False, errors[0])
        if purpose not in FORMAL_PURPOSES:
            return AccessDecision(False, "namespace_purpose_invalid")
        if context.profile_status != "active":
            return AccessDecision(False, f"profile_{context.profile_status}")
        if context.kind == "pending":
            return AccessDecision(False, "namespace_pending_denied")
        if context.assurance not in FORMAL_ASSURANCE:
            return AccessDecision(False, "identity_assurance_insufficient")
        if context.kind == "group_shared" and purpose in {"relationship_read", "relationship_write", "profile_read", "profile_write"}:
            return AccessDecision(False, "group_shared_subject_access_denied")
        if context.kind == "persona_global" and purpose not in {"rule_read", "rule_write"}:
            return AccessDecision(False, "persona_global_purpose_denied")
        return AccessDecision(True, "namespace_access_allowed")


def contract_descriptor() -> dict[str, Any]:
    return {**_shape(), "fingerprint": CONTRACT_FINGERPRINT}


def contract_self_check() -> list[str]:
    expected = hashlib.sha256(_canonical(_shape()).encode("utf-8")).hexdigest()[:16]
    return [] if expected == CONTRACT_FINGERPRINT else ["namespace_contract_fingerprint_stale"]


__all__ = [
    "AccessDecision", "AssurancePolicy", "CONTRACT_FINGERPRINT", "CONTRACT_NAME", "CONTRACT_VERSION",
    "CONTEXT_FIELDS", "FORMAL_ASSURANCE", "FORMAL_PURPOSES", "IDENTITY_ASSURANCE", "NAMESPACE_KINDS",
    "NamespaceContext", "PROFILE_STATUS", "build_namespace_context", "contract_descriptor", "contract_self_check",
    "validate_namespace_context",
]
