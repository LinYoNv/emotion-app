"""P6 minimal read-only status projection."""
from __future__ import annotations

import hashlib
import json
from typing import Any


P6_READONLY_STATUS_SCHEMA = "ops.p6.read_only_status.v1"
_STATUS_FIELDS = ("profiles", "identity_links", "audit_events", "operations")
_HEALTH = frozenset({"ready", "degraded", "unverifiable"})


def _fingerprint() -> str:
    payload = {"schema_version": P6_READONLY_STATUS_SCHEMA, "count_fields": _STATUS_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


P6_READONLY_STATUS_FINGERPRINT = _fingerprint()


def _count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 10_000_000 else 0


def build_p6_readonly_status(status: Any, *, health: str = "ready", reason_code: str = "") -> dict[str, Any]:
    """Return bounded counts only; P4 and identity authority never crosses this boundary."""
    status_available = type(status) is dict
    if status_available:
        for key in status:
            if type(key) is not str:
                status_available = False
                break
    source = status if status_available else {}
    normalized_health = health if type(health) is str and health in _HEALTH else "unverifiable"
    normalized_reason = reason_code if type(reason_code) is str and len(reason_code) <= 80 else "invalid_reason_code"
    if not status_available:
        normalized_health = "unverifiable"
        normalized_reason = "registry_status_unavailable"
    return {
        "schema_version": P6_READONLY_STATUS_SCHEMA,
        "source_plugin": "private_companion",
        "contract_fingerprint": P6_READONLY_STATUS_FINGERPRINT,
        "health": normalized_health,
        "reason_code": normalized_reason,
        "counts": {field: _count(source.get(field)) for field in _STATUS_FIELDS},
    }


__all__ = ["P6_READONLY_STATUS_SCHEMA", "P6_READONLY_STATUS_FINGERPRINT", "build_p6_readonly_status"]
