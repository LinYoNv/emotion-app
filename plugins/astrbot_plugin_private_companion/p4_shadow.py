"""Metadata-only P4 shadow records for the chat-side companion.

P4 records are advisory telemetry.  They cannot execute policy or influence a
normal chat response, and unsafe request content is never copied into them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


SHADOW_SCHEMA_VERSION = "chat.p4.shadow.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_FORBIDDEN = frozenset({
    "raw_prompt", "prompt", "text", "content", "chat_text", "messages",
    "transcript", "private_object", "private_object_ref", "database", "db",
})
_ALLOWED_STATUS = frozenset({"shadow", "invalid", "degraded"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field}_invalid")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).strip().lower() in _FORBIDDEN for key in value) or any(
            _has_forbidden(item) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_forbidden(item) for item in value)
    return False


def _safe_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_p4_shadow(
    *,
    source_kind: Any,
    target_kind: Any,
    authority: Any,
    reason_code: Any,
    safe_reference: Any = "",
    safe_hash: Any = "",
    event_id: Any = "",
    operation_id: Any = "",
    timestamp: Any = None,
    status: Any = "shadow",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a safe P4 record; malformed input becomes an explicit invalid record."""
    errors: list[str] = []
    raw_metadata = metadata if isinstance(metadata, dict) else {}
    try:
        if _has_forbidden(raw_metadata):
            raise ValueError("forbidden_metadata")
        source = _safe_id(source_kind, "source_kind")
        target = _safe_id(target_kind, "target_kind")
        auth = _safe_id(authority, "authority")
        reason = _safe_id(reason_code, "reason_code")
        reference = "" if safe_reference in (None, "") else _safe_id(safe_reference, "safe_reference")
        digest = str(safe_hash or "")
        if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("safe_hash_invalid")
        event = _safe_id(event_id, "event_id") if event_id else ""
        operation = _safe_id(operation_id, "operation_id") if operation_id else ""
        if not event and not operation:
            raise ValueError("event_or_operation_required")
        state = status if status in _ALLOWED_STATUS else "invalid"
        record = {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "source_kind": source,
            "target_kind": target,
            "authority": auth,
            "reason_code": reason,
            "safe_reference": reference,
            "safe_hash": digest or _safe_hash({"source_kind": source, "target_kind": target, "reason_code": reason}),
            "event_id": event,
            "operation_id": operation,
            "timestamp": str(timestamp or _now()),
            "status": state,
            "shadow_only": True,
        }
        return record
    except (TypeError, ValueError) as exc:
        errors.append(str(exc) or "invalid_input")
        return {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "source_kind": "invalid",
            "target_kind": "invalid",
            "authority": "invalid",
            "reason_code": "invalid_input",
            "safe_reference": "",
            "safe_hash": "",
            "event_id": "",
            "operation_id": "invalid_input",
            "timestamp": _now(),
            "status": "invalid",
            "shadow_only": True,
            "errors": errors[:8],
        }


def validate_p4_shadow(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["shadow_invalid"]
    errors: list[str] = []
    required = ("schema_version", "source_kind", "target_kind", "authority", "reason_code", "safe_reference", "safe_hash", "event_id", "operation_id", "timestamp", "status", "shadow_only")
    errors.extend(f"missing_{key}" for key in required if key not in value)
    if errors:
        return errors
    if value.get("schema_version") != SHADOW_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    for field in ("source_kind", "target_kind", "authority", "reason_code"):
        if not isinstance(value.get(field), str) or not _IDENTIFIER.fullmatch(value[field]):
            errors.append(f"{field}_invalid")
    if value.get("safe_reference") and (not isinstance(value["safe_reference"], str) or not _IDENTIFIER.fullmatch(value["safe_reference"])):
        errors.append("safe_reference_invalid")
    if not isinstance(value.get("safe_hash"), str) or (value["safe_hash"] and not re.fullmatch(r"sha256:[0-9a-f]{64}", value["safe_hash"])):
        errors.append("safe_hash_invalid")
    if not isinstance(value.get("event_id"), str) or not isinstance(value.get("operation_id"), str) or not (value["event_id"] or value["operation_id"]):
        errors.append("event_or_operation_invalid")
    if value.get("status") not in _ALLOWED_STATUS:
        errors.append("status_invalid")
    if value.get("shadow_only") is not True:
        errors.append("shadow_only_required")
    if _has_forbidden(value):
        errors.append("forbidden_field")
    return sorted(set(errors))


def project_p4_shadow(value: Any) -> dict[str, Any]:
    candidate = dict(value) if isinstance(value, dict) else {}
    errors = validate_p4_shadow(candidate)
    if errors:
        return build_p4_shadow(
            source_kind="invalid", target_kind="invalid", authority="invalid",
            reason_code="projection_rejected", operation_id="invalid_projection",
            status="invalid",
        ) | {"errors": errors[:8]}
    return dict(candidate)


__all__ = ["SHADOW_SCHEMA_VERSION", "build_p4_shadow", "validate_p4_shadow", "project_p4_shadow"]
