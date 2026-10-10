"""Pure chat-side provenance contract and in-memory migration projection.

The module accepts metadata only.  It never reads or stores prompt text,
conversation content, media, credentials, paths, or arbitrary source prose,
and it performs no I/O.  A later SQLite adapter may use the plans and CAS
results, but this module deliberately has no knowledge of that adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
import re
from typing import Any


PROVENANCE_CONTRACT_NAME = "ops.p5.provenance.v1"
PROVENANCE_CONTRACT_VERSION = "1.0"
P3_CONTRACT_NAME = "ops.context_orchestration.v1"
P3_CONTRACT_VERSION = "1.0"
P3_CONTRACT_FINGERPRINT = "3bb7a12af05bb4d9a47cef9f31e78752cec512b90b1d50ae49feef54470974f8"
COMPANION_ATTESTATION_ISSUER = "private_companion"
ATTESTATION_SCHEMA_VERSION = "ops.p5.attestation.v1"

TRUST_LEVELS = ("T0", "T1", "T2", "T3", "T4")
PROVENANCE_STATES = ("observed", "legacy_unresolved", "owner_recovered", "invalid")
SOURCE_KINDS = (
    "policy_config", "verified_authorization", "current_user_intent",
    "forwarded_text", "quoted_text", "vision_summary", "tool_output",
    "web_extract", "memory_recall", "derived_summary", "legacy_memory", "unknown",
)
FIREWALL_STATUSES = (
    "allowed", "sanitized", "blocked", "rejected", "quarantined", "unavailable", "unknown",
)
DISPOSITIONS = ("allow", "shadow_quarantine", "deny_high_risk")
HASH_FORMAT = "sha256_hex_lower"

RECORD_FIELDS = (
    "contract_name", "contract_version", "contract_fingerprint", "memory_id",
    "source_kind", "source_trust", "firewall_status", "source_event_ref_hash",
    "authority_attestation_ref_hash", "provenance_state", "migration_operation_ref",
    "recovery_operation_ref", "record_revision",
)
OPERATION_FIELDS = (
    "contract_name", "contract_version", "contract_fingerprint", "operation_kind",
    "operation_ref_hash", "memory_id", "expected_revision", "before_record_digest",
    "after_record_digest", "before_record", "after_record",
)

_SOURCE_KIND_SET = frozenset(SOURCE_KINDS)
_TRUST_SET = frozenset(TRUST_LEVELS)
_FIREWALL_SET = frozenset(FIREWALL_STATUSES)
_DISPOSITION_SET = frozenset(DISPOSITIONS)
_SINKS = frozenset({
    "prompt_forward_quote", "prompt_vision_summary", "memory_recall",
    "bridge_serialization", "tool_retrieval", "external_export", "cross_user_read",
})
_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_RAW_FIELDS = frozenset({
    "body", "content", "text", "message", "prompt", "conversation", "media",
    "raw_id", "source_id", "source_event_id", "event_id", "attestation_id",
    "authority_attestation_id", "request_id", "session_id", "credential",
    "credentials", "password", "secret", "token", "path", "absolute_path", "url",
})
_SNAPSHOT_FIELDS = frozenset({
    "schema_version", "contract_name", "contract_version", "contract_fingerprint",
    "issuer", "issuer_epoch", "p3_contract_name", "p3_contract_version",
    "p3_contract_fingerprint", "source_kind", "source_trust", "trust", "firewall_status",
    "source_event_ref_hash", "source_hash", "authority_attestation_ref_hash",
    "attestation_ref_hash", "disposition", "reason_codes", "request_hash", "session_hash",
    "derived_hash", "derived_from_ref_hash", "provenance_state", "sink",
})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def contract_descriptor() -> dict[str, Any]:
    """Return a fresh, closed, JSON-safe descriptor."""

    return {
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "record_fields": list(RECORD_FIELDS),
        "provenance_states": list(PROVENANCE_STATES),
        "trust_levels": list(TRUST_LEVELS),
        "source_kinds": list(SOURCE_KINDS),
        "firewall_statuses": list(FIREWALL_STATUSES),
        "dispositions": list(DISPOSITIONS),
        "hash_format": HASH_FORMAT,
        "attestation_issuer": COMPANION_ATTESTATION_ISSUER,
        "p3_contract_name": P3_CONTRACT_NAME,
        "p3_contract_version": P3_CONTRACT_VERSION,
    }


def contract_fingerprint() -> str:
    """Return the canonical descriptor SHA-256 fingerprint."""

    return _digest(contract_descriptor())


provenance_contract_descriptor = contract_descriptor
provenance_contract_fingerprint = contract_fingerprint


def _safe_memory_id(value: Any) -> str:
    return value if isinstance(value, str) and _MEMORY_ID_RE.fullmatch(value) else ""


def _safe_hash(value: Any) -> str:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) and value == value.lower() else ""


def _normalize_hash(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value[7:] if value.startswith("sha256:") else value
    return raw.lower() if _SHA256_RE.fullmatch(raw) else ""


def _safe_revision(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _known(value: Any, allowed: frozenset[str] | tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in allowed


def _forbidden(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in _FORBIDDEN_RAW_FIELDS)


def _error_result(*codes: str) -> dict[str, Any]:
    return {"ok": False, "error_codes": sorted(set(codes))}


def _blank_record(memory_id: Any = "", *, revision: int = 0, state: str = "invalid") -> dict[str, Any]:
    return {
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "memory_id": _safe_memory_id(memory_id),
        "source_kind": "unknown",
        "source_trust": "T4",
        "firewall_status": "unknown",
        "source_event_ref_hash": "",
        "authority_attestation_ref_hash": "",
        "provenance_state": state,
        "migration_operation_ref": "",
        "recovery_operation_ref": "",
        "record_revision": revision if _safe_revision(revision) is not None else 0,
    }


def validate_provenance_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return static validation errors or a deep-copied safe record.

    Invalid input values are never returned, so a caller can safely serialize
    the result without leaking hostile identifiers or source prose.
    """

    if not isinstance(record, Mapping):
        return _error_result("record_not_mapping")
    errors: list[str] = []
    if _forbidden(record):
        errors.append("forbidden_raw_field")
    if set(record) != set(RECORD_FIELDS):
        errors.append("record_fields_mismatch")
    if record.get("contract_name") != PROVENANCE_CONTRACT_NAME:
        errors.append("contract_name_mismatch")
    if record.get("contract_version") != PROVENANCE_CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    if record.get("contract_fingerprint") != contract_fingerprint():
        errors.append("contract_fingerprint_mismatch")
    if not _safe_memory_id(record.get("memory_id")):
        errors.append("memory_id_invalid")
    if not _known(record.get("source_kind"), _SOURCE_KIND_SET):
        errors.append("source_kind_invalid")
    if not _known(record.get("source_trust"), _TRUST_SET):
        errors.append("source_trust_invalid")
    if not _known(record.get("firewall_status"), _FIREWALL_SET):
        errors.append("firewall_status_invalid")
    if not _known(record.get("provenance_state"), PROVENANCE_STATES):
        errors.append("provenance_state_invalid")
    if _safe_revision(record.get("record_revision")) is None:
        errors.append("record_revision_invalid")

    source = record.get("source_event_ref_hash")
    authority = record.get("authority_attestation_ref_hash")
    source_hash = _safe_hash(source)
    authority_hash = _safe_hash(authority)
    if source != "" and not source_hash:
        errors.append("source_event_ref_hash_invalid")
    if authority != "" and not authority_hash:
        errors.append("authority_attestation_ref_hash_invalid")
    migration = record.get("migration_operation_ref")
    recovery = record.get("recovery_operation_ref")
    if migration != "" and not _safe_hash(migration):
        errors.append("migration_operation_ref_invalid")
    if recovery != "" and not _safe_hash(recovery):
        errors.append("recovery_operation_ref_invalid")

    state = record.get("provenance_state")
    if state == "legacy_unresolved":
        if (record.get("source_kind"), record.get("source_trust"), record.get("firewall_status")) != ("unknown", "T4", "unknown"):
            errors.append("legacy_defaults_required")
        if source != "" or authority != "":
            errors.append("legacy_hashes_forbidden")
    elif state in {"observed", "owner_recovered"}:
        if record.get("source_kind") == "unknown":
            errors.append("observed_source_kind_required")
        if not source_hash or not authority_hash:
            errors.append("attestation_hashes_required")
        if source_hash and source_hash == authority_hash:
            errors.append("attestation_hashes_conflict")
        if state == "owner_recovered" and not _safe_hash(recovery):
            errors.append("recovery_operation_ref_required")
    elif state == "invalid" and (source_hash or authority_hash):
        errors.append("invalid_hashes_forbidden")
    if errors:
        return _error_result(*errors)
    return {"ok": True, "record": deepcopy(dict(record))}


def legacy_unresolved(memory_id: str, *, record_revision: int = 0) -> dict[str, Any]:
    """Represent a historical record without guessing its provenance."""

    result = _blank_record(memory_id, revision=record_revision, state="legacy_unresolved")
    return result if result["memory_id"] and validate_provenance_record(result)["ok"] else _blank_record(state="invalid")


def _snapshot_mapping(snapshot: Any) -> Mapping[str, Any] | None:
    if isinstance(snapshot, Mapping):
        return snapshot
    if is_dataclass(snapshot):
        value = asdict(snapshot)
        return value if isinstance(value, Mapping) else None
    return None


def _paired_hash(snapshot: Mapping[str, Any], *keys: str) -> tuple[str, str]:
    values = [_normalize_hash(snapshot[key]) for key in keys if key in snapshot]
    if not values:
        return "missing", ""
    if not all(values):
        return "invalid", ""
    return ("present", values[0]) if len(set(values)) == 1 else ("conflict", "")


def _paired_label(snapshot: Mapping[str, Any], allowed: frozenset[str], *keys: str) -> tuple[str, str]:
    values = [snapshot[key] for key in keys if key in snapshot]
    if not values:
        return "missing", ""
    if not all(_known(value, allowed) for value in values):
        return "invalid", ""
    return ("present", values[0]) if len(set(values)) == 1 else ("conflict", "")


def _snapshot_safe(snapshot: Mapping[str, Any]) -> bool:
    if set(snapshot) - _SNAPSHOT_FIELDS or _forbidden(snapshot):
        return False
    if not _known(snapshot.get("disposition"), _DISPOSITION_SET):
        return False
    codes = snapshot.get("reason_codes", ())
    if not isinstance(codes, (list, tuple)) or len(codes) > 16:
        return False
    if not all(isinstance(code, str) and _TOKEN_RE.fullmatch(code) for code in codes):
        return False
    for key in ("request_hash", "session_hash", "issuer_epoch"):
        if not _normalize_hash(snapshot.get(key)):
            return False
    derived = [snapshot[key] for key in ("derived_hash", "derived_from_ref_hash") if key in snapshot and snapshot[key] != ""]
    if derived and (any(not _normalize_hash(value) for value in derived) or len({_normalize_hash(value) for value in derived}) != 1):
        return False
    return snapshot.get("provenance_state") == "observed" and _known(snapshot.get("sink"), _SINKS)


def observed_from_companion_snapshot(memory_id: str, snapshot: Any, *, record_revision: int = 0) -> dict[str, Any]:
    """Create ``observed`` only from a complete, attested Companion snapshot."""

    safe_id = _safe_memory_id(memory_id)
    revision = _safe_revision(record_revision)
    data = _snapshot_mapping(snapshot)
    if not safe_id or revision is None or data is None or not _snapshot_safe(data):
        return _blank_record(safe_id, revision=revision or 0)
    expected = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "issuer": COMPANION_ATTESTATION_ISSUER,
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "p3_contract_name": P3_CONTRACT_NAME,
        "p3_contract_version": P3_CONTRACT_VERSION,
        "p3_contract_fingerprint": P3_CONTRACT_FINGERPRINT,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        return _blank_record(safe_id, revision=revision)
    source_status, source_hash = _paired_hash(data, "source_event_ref_hash", "source_hash")
    authority_status, authority_hash = _paired_hash(data, "authority_attestation_ref_hash", "attestation_ref_hash")
    trust_status, trust = _paired_label(data, _TRUST_SET, "source_trust", "trust")
    source_kind = data.get("source_kind")
    firewall = data.get("firewall_status")
    if (
        source_status != "present" or authority_status != "present" or trust_status != "present"
        or source_hash == authority_hash or not _known(source_kind, _SOURCE_KIND_SET - {"unknown"})
        or not _known(firewall, _FIREWALL_SET)
    ):
        return _blank_record(safe_id, revision=revision)
    record = _blank_record(safe_id, revision=revision, state="observed")
    record.update({
        "source_kind": source_kind, "source_trust": trust, "firewall_status": firewall,
        "source_event_ref_hash": source_hash, "authority_attestation_ref_hash": authority_hash,
    })
    return record if validate_provenance_record(record)["ok"] else _blank_record(safe_id, revision=revision)


def provenance_record_digest(record: Mapping[str, Any] | None) -> str:
    checked = validate_provenance_record(record)
    return _digest(checked["record"]) if checked["ok"] else ""


def _record_or_none(value: Any) -> dict[str, Any] | None:
    checked = validate_provenance_record(value if isinstance(value, Mapping) else None)
    return deepcopy(checked["record"]) if checked["ok"] else None


def _operation(*, operation_kind: str, operation_ref_hash: str, before_record: Mapping[str, Any] | None, after_record: Mapping[str, Any]) -> dict[str, Any] | None:
    before = _record_or_none(before_record)
    after = _record_or_none(after_record)
    reference = _safe_hash(operation_ref_hash)
    if operation_kind not in {"migration", "recovery"} or not reference or after is None:
        return None
    if before is not None and before["memory_id"] != after["memory_id"]:
        return None
    expected = before["record_revision"] if before is not None else after["record_revision"] - 1
    if expected < 0 or after["record_revision"] != expected + 1:
        return None
    operation = {
        "contract_name": PROVENANCE_CONTRACT_NAME, "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(), "operation_kind": operation_kind,
        "operation_ref_hash": reference, "memory_id": after["memory_id"], "expected_revision": expected,
        "before_record_digest": _digest(before) if before is not None else "",
        "after_record_digest": _digest(after), "before_record": before, "after_record": after,
    }
    return operation if validate_operation(operation)["ok"] else None


def validate_operation(operation: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(operation, Mapping):
        return _error_result("operation_not_mapping")
    errors: list[str] = []
    if _forbidden(operation):
        errors.append("forbidden_raw_field")
    if set(operation) != set(OPERATION_FIELDS):
        errors.append("operation_fields_mismatch")
    if operation.get("contract_name") != PROVENANCE_CONTRACT_NAME:
        errors.append("contract_name_mismatch")
    if operation.get("contract_version") != PROVENANCE_CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    if operation.get("contract_fingerprint") != contract_fingerprint():
        errors.append("contract_fingerprint_mismatch")
    if operation.get("operation_kind") not in {"migration", "recovery"}:
        errors.append("operation_kind_invalid")
    if not _safe_hash(operation.get("operation_ref_hash")):
        errors.append("operation_ref_invalid")
    if not _safe_memory_id(operation.get("memory_id")):
        errors.append("memory_id_invalid")
    expected = _safe_revision(operation.get("expected_revision"))
    if expected is None:
        errors.append("expected_revision_invalid")
    before = _record_or_none(operation.get("before_record"))
    after = _record_or_none(operation.get("after_record"))
    if operation.get("before_record") is not None and before is None:
        errors.append("before_record_invalid")
    if after is None:
        errors.append("after_record_invalid")
    if before is None and operation.get("before_record_digest") != "":
        errors.append("before_digest_required_empty")
    if before is not None and operation.get("before_record_digest") != _digest(before):
        errors.append("before_digest_mismatch")
    if after is not None and operation.get("after_record_digest") != _digest(after):
        errors.append("after_digest_mismatch")
    if after is not None:
        if operation.get("memory_id") != after["memory_id"]:
            errors.append("memory_id_mismatch")
        if expected is not None and after["record_revision"] != expected + 1:
            errors.append("revision_progression_invalid")
        ref_field = "migration_operation_ref" if operation.get("operation_kind") == "migration" else "recovery_operation_ref"
        if after.get(ref_field) != operation.get("operation_ref_hash"):
            errors.append("operation_ref_not_bound")
    if before is not None and after is not None:
        if before["memory_id"] != after["memory_id"]:
            errors.append("before_after_memory_mismatch")
        if expected is not None and before["record_revision"] != expected:
            errors.append("before_revision_mismatch")
    return _error_result(*errors) if errors else {"ok": True, "operation": deepcopy(dict(operation))}


def plan_legacy_migration(records: Iterable[Mapping[str, Any]], *, operation_ref_hash: str) -> dict[str, Any]:
    """Return a non-mutating preview for unresolved legacy records."""

    reference = _safe_hash(operation_ref_hash)
    result: dict[str, Any] = {
        "mode": "preview", "readonly": True, "write_count": 0,
        "operation_ref_hash": reference, "operations": [], "skipped": [], "error_codes": [],
    }
    if not reference:
        result["error_codes"].append("operation_ref_invalid")
        return result
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        result["error_codes"].append("records_not_iterable")
        return result
    for candidate in records:
        if not isinstance(candidate, Mapping) or _forbidden(candidate):
            result["error_codes"].append("candidate_invalid")
            continue
        existing = _record_or_none(candidate)
        if existing is not None:
            result["skipped"].append({"memory_id": existing["memory_id"], "reason_code": "already_provenanced"})
            continue
        if set(candidate) != {"memory_id", "record_revision"}:
            result["error_codes"].append("candidate_fields_mismatch")
            continue
        memory_id = _safe_memory_id(candidate.get("memory_id"))
        revision = _safe_revision(candidate.get("record_revision"))
        if not memory_id or revision is None:
            result["error_codes"].append("candidate_invalid")
            continue
        after = legacy_unresolved(memory_id, record_revision=revision + 1)
        after["migration_operation_ref"] = reference
        operation = _operation(operation_kind="migration", operation_ref_hash=reference, before_record=None, after_record=after)
        if operation is None:
            result["error_codes"].append("operation_invalid")
        else:
            result["operations"].append(operation)
    result["error_codes"] = sorted(set(result["error_codes"]))
    return result


def _minimal_current(current: Mapping[str, Any] | None) -> tuple[str, int | None, dict[str, Any] | None, str]:
    if not isinstance(current, Mapping) or _forbidden(current):
        return "", None, None, "current_invalid"
    record = _record_or_none(current)
    if record is not None:
        return record["memory_id"], record["record_revision"], record, ""
    if set(current) != {"memory_id", "record_revision"}:
        return "", None, None, "current_fields_mismatch"
    return _safe_memory_id(current.get("memory_id")), _safe_revision(current.get("record_revision")), None, ""


def apply_planned_operation(current: Mapping[str, Any] | None, operation: Mapping[str, Any] | None) -> dict[str, Any]:
    """Apply a plan to an in-memory mapping with exact revision CAS."""

    checked = validate_operation(operation)
    if not checked["ok"]:
        return {"ok": False, "mode": "apply", "status": "invalid_operation", "error_codes": checked["error_codes"]}
    op = checked["operation"]
    memory_id, revision, current_record, error = _minimal_current(current)
    if error or not memory_id or revision is None:
        return {"ok": False, "mode": "apply", "status": "invalid_current", "error_codes": [error or "current_invalid"]}
    if current_record is not None and _digest(current_record) == op["after_record_digest"]:
        return {"ok": True, "mode": "apply", "status": "idempotent", "record": deepcopy(op["after_record"]), "changed": False}
    if memory_id != op["memory_id"] or revision != op["expected_revision"]:
        return {"ok": False, "mode": "apply", "status": "revision_conflict", "error_codes": ["revision_or_memory_mismatch"]}
    before = op["before_record"]
    if before is None:
        if current_record is not None:
            return {"ok": False, "mode": "apply", "status": "revision_conflict", "error_codes": ["unexpected_existing_provenance"]}
    elif current_record is None or _digest(current_record) != op["before_record_digest"]:
        return {"ok": False, "mode": "apply", "status": "revision_conflict", "error_codes": ["before_record_mismatch"]}
    return {"ok": True, "mode": "apply", "status": "applied", "record": deepcopy(op["after_record"]), "changed": True}


def rollback_planned_operation(current: Mapping[str, Any] | None, operation: Mapping[str, Any] | None) -> dict[str, Any]:
    """Rollback only when the current record still matches the plan's CAS."""

    checked = validate_operation(operation)
    if not checked["ok"]:
        return {"ok": False, "mode": "rollback", "status": "invalid_operation", "error_codes": checked["error_codes"]}
    op = checked["operation"]
    memory_id, revision, current_record, error = _minimal_current(current)
    if error or current_record is None or revision is None:
        return {"ok": False, "mode": "rollback", "status": "invalid_current", "error_codes": [error or "current_invalid"]}
    if memory_id != op["memory_id"] or revision != op["after_record"]["record_revision"] or _digest(current_record) != op["after_record_digest"]:
        return {"ok": False, "mode": "rollback", "status": "revision_conflict", "error_codes": ["concurrent_update_protected"]}
    before = op["before_record"]
    return {"ok": True, "mode": "rollback", "status": "rolled_back", "record": deepcopy(before) if before is not None else None, "removed": before is None, "changed": True}


def plan_owner_recovery(current: Mapping[str, Any], snapshot: Mapping[str, Any], *, recovery_operation_ref_hash: str) -> dict[str, Any]:
    """Plan attested owner recovery for one legacy-unresolved record."""

    existing = _record_or_none(current)
    reference = _safe_hash(recovery_operation_ref_hash)
    if existing is None or existing["provenance_state"] != "legacy_unresolved" or not reference:
        return {"ok": False, "mode": "preview", "error_codes": ["recovery_precondition_failed"]}
    after = observed_from_companion_snapshot(existing["memory_id"], snapshot, record_revision=existing["record_revision"] + 1)
    if after["provenance_state"] != "observed":
        return {"ok": False, "mode": "preview", "error_codes": ["companion_snapshot_invalid"]}
    after["provenance_state"] = "owner_recovered"
    after["migration_operation_ref"] = existing["migration_operation_ref"]
    after["recovery_operation_ref"] = reference
    operation = _operation(operation_kind="recovery", operation_ref_hash=reference, before_record=existing, after_record=after)
    if operation is None:
        return {"ok": False, "mode": "preview", "error_codes": ["operation_invalid"]}
    return {"ok": True, "mode": "preview", "readonly": True, "write_count": 0, "operation": operation}


__all__ = [
    "ATTESTATION_SCHEMA_VERSION", "COMPANION_ATTESTATION_ISSUER", "DISPOSITIONS",
    "FIREWALL_STATUSES", "HASH_FORMAT", "OPERATION_FIELDS", "P3_CONTRACT_FINGERPRINT",
    "P3_CONTRACT_NAME", "P3_CONTRACT_VERSION", "PROVENANCE_CONTRACT_NAME",
    "PROVENANCE_CONTRACT_VERSION", "PROVENANCE_STATES", "RECORD_FIELDS", "SOURCE_KINDS",
    "TRUST_LEVELS", "apply_planned_operation", "contract_descriptor", "contract_fingerprint",
    "legacy_unresolved", "observed_from_companion_snapshot", "plan_legacy_migration",
    "plan_owner_recovery", "provenance_contract_descriptor", "provenance_contract_fingerprint",
    "provenance_record_digest", "rollback_planned_operation", "validate_operation",
    "validate_provenance_record",
]
