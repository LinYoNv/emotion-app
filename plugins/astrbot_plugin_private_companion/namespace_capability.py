"""REQ-041 namespace-memory capability negotiation contract."""
from __future__ import annotations

from typing import Any

try:
    from .identity_namespace import (
        CONTRACT_FINGERPRINT as NAMESPACE_CONTRACT_FINGERPRINT,
        CONTRACT_NAME as NAMESPACE_CONTRACT_NAME,
        CONTRACT_VERSION as NAMESPACE_CONTRACT_VERSION,
        CONTEXT_FIELDS,
        FORMAL_PURPOSES,
        NAMESPACE_KINDS,
    )
except ImportError:  # pragma: no cover - direct-module test compatibility
    from identity_namespace import (
        CONTRACT_FINGERPRINT as NAMESPACE_CONTRACT_FINGERPRINT,
        CONTRACT_NAME as NAMESPACE_CONTRACT_NAME,
        CONTRACT_VERSION as NAMESPACE_CONTRACT_VERSION,
        CONTEXT_FIELDS,
        FORMAL_PURPOSES,
        NAMESPACE_KINDS,
    )


CAPABILITY_NAME = "chat.namespace_memory.v1"
CAPABILITY_VERSION = "1.0"
RECORD_KINDS = ("evidence", "memory", "profile_fact", "rule", "summary")
API_METHODS = (
    "erase_scoped_group_scopes", "erase_scoped_persona_scopes", "list_scoped_records", "read_scoped_record", "tombstone_scoped_namespace",
    "tombstone_scoped_identity_scopes", "tombstone_scoped_record", "upsert_scoped_record",
)
CAPABILITY_FIELDS = (
    "capability_name", "capability_version", "namespace_contract_name", "namespace_contract_version",
    "namespace_contract_fingerprint", "context_fields", "supported_kinds", "supported_purposes",
    "record_kinds", "methods", "available", "state", "error_code",
)


def namespace_capability_descriptor(
    *, available: bool = False, methods: Any = (), error_code: str = "namespace_scoped_api_not_bound"
) -> dict[str, Any]:
    supplied = set(methods) if isinstance(methods, (list, tuple, set, frozenset)) else set()
    resolved_methods = [item for item in API_METHODS if item in supplied]
    ready = bool(available) and resolved_methods == list(API_METHODS)
    return {
        "capability_name": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "namespace_contract_name": NAMESPACE_CONTRACT_NAME,
        "namespace_contract_version": NAMESPACE_CONTRACT_VERSION,
        "namespace_contract_fingerprint": NAMESPACE_CONTRACT_FINGERPRINT,
        "context_fields": list(CONTEXT_FIELDS),
        "supported_kinds": sorted(NAMESPACE_KINDS),
        "supported_purposes": sorted(FORMAL_PURPOSES),
        "record_kinds": list(RECORD_KINDS),
        "methods": resolved_methods,
        "available": ready,
        "state": "ready" if ready else "degraded",
        "error_code": "" if ready else str(error_code or "namespace_scoped_api_unavailable")[:80],
    }


def validate_namespace_capability(value: Any, *, require_available: bool = True) -> list[str]:
    if not isinstance(value, dict):
        return ["namespace_capability_missing"]
    errors: list[str] = []
    if set(value) != set(CAPABILITY_FIELDS):
        errors.append("namespace_capability_fields_invalid")
    expected = namespace_capability_descriptor(
        available=bool(value.get("available")),
        methods=value.get("methods"),
        error_code=str(value.get("error_code") or "namespace_scoped_api_unavailable"),
    )
    for field in (
        "capability_name", "capability_version", "namespace_contract_name", "namespace_contract_version",
        "namespace_contract_fingerprint", "context_fields", "supported_kinds", "supported_purposes", "record_kinds",
    ):
        if value.get(field) != expected[field]:
            errors.append(f"{field}_mismatch")
    methods = value.get("methods")
    if not isinstance(methods, list) or any(item not in API_METHODS for item in methods) or len(methods) != len(set(methods)):
        errors.append("methods_invalid")
    if type(value.get("available")) is not bool:
        errors.append("available_invalid")
    if value.get("state") not in {"ready", "degraded"}:
        errors.append("state_invalid")
    if value.get("available") is True and methods != list(API_METHODS):
        errors.append("methods_incomplete")
    if value.get("available") is True and (value.get("state") != "ready" or value.get("error_code") != ""):
        errors.append("available_state_invalid")
    if require_available and value.get("available") is not True:
        errors.append("namespace_capability_unavailable")
    return list(dict.fromkeys(errors))


def negotiate_namespace_capability(value: Any) -> dict[str, Any]:
    errors = validate_namespace_capability(value, require_available=True)
    if errors:
        return {
            "available": False,
            "state": "degraded",
            "code": errors[0],
            "mismatches": errors,
        }
    return {
        "available": True,
        "state": "ready",
        "code": "namespace_capability_ready",
        "mismatches": [],
        "capability": dict(value),
    }


__all__ = [
    "API_METHODS", "CAPABILITY_FIELDS", "CAPABILITY_NAME", "CAPABILITY_VERSION", "RECORD_KINDS",
    "namespace_capability_descriptor", "negotiate_namespace_capability", "validate_namespace_capability",
]
