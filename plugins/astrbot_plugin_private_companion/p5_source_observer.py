"""Pure, metadata-only P5 source observation for the companion side.

The observer classifies a bounded source segment for a sink.  It never reads
or retains prose, prompts, media, credentials, or carrier state.  A normal
sink may receive an ``allow`` *observation*, but the result always carries
``execution_authority='none'``; high-risk sinks are fail-closed.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


SCHEMA_VERSION = "ops.p5.source_observer.v1"
SOURCE_OBSERVER_SCHEMA_VERSION = SCHEMA_VERSION
FIREWALL_SCHEMA_VERSION = SCHEMA_VERSION

TRUST_LEVELS = frozenset({"T0", "T1", "T2", "T3", "T4"})
SOURCE_KINDS = frozenset(
    {
        "policy_config",
        "verified_authorization",
        "current_user_intent",
        "forwarded_text",
        "quoted_text",
        "vision_summary",
        "tool_output",
        "web_extract",
        "memory_recall",
        "derived_summary",
        "legacy_memory",
        "unknown",
    }
)
SINKS = frozenset(
    {
        "prompt_context",
        "prompt_forward_quote",
        "prompt_vision_summary",
        "memory_recall",
        "bridge_serialize",
        "bridge_serialization",
        "tool_execution",
        "tool_retrieval",
        "external_export",
        "cross_user_read",
    }
)
HIGH_RISK_SINKS = frozenset(
    {
        "tool_execution",
        "tool_retrieval",
        "external_export",
        "cross_user_read",
    }
)
SECURITY_STATES = frozenset(
    {"allowed", "sanitized", "blocked", "quarantined", "unavailable", "rejected", "unknown"}
)
DISPOSITIONS = frozenset({"allow", "shadow_quarantine", "deny_high_risk"})

MAX_BATCH_SIZE = 64
MAX_FIELDS = 10
MAX_TOKEN_LENGTH = 96
MAX_REFERENCE_LENGTH = 128
MAX_REASON_CODES = 12

_ALLOWED_FIELDS = frozenset(
    {
        "source_kind",
        "trust",
        "source_trust",
        "sink",
        "event_id",
        "event_ref",
        "source_hash",
        "safe_ref_hash",
        "security_state",
        "firewall_status",
    }
)
_PROSE_FIELDS = frozenset(
    {
        "text",
        "content",
        "prompt",
        "body",
        "message",
        "raw",
        "payload",
        "summary",
        "media",
        "transcript",
        "ocr",
        "url",
        "credentials",
        "session_text",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,127}\Z")
_HASH_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_SOURCE_TRUST = {
    "policy_config": "T0",
    "verified_authorization": "T1",
    "current_user_intent": "T2",
    "forwarded_text": "T3",
    "quoted_text": "T3",
    "vision_summary": "T3",
    "tool_output": "T3",
    "web_extract": "T3",
    "memory_recall": "T4",
    "derived_summary": "T4",
    "legacy_memory": "T4",
    "unknown": "T4",
}
_DENIED_STATES = frozenset({"blocked", "quarantined", "unavailable", "rejected", "unknown"})


def evaluate_source(segment: object) -> dict[str, Any]:
    """Return a JSON-safe, deterministic source-to-sink observation.

    Input references may be a bounded event reference or a SHA-256 reference,
    but only a hash is emitted.  Supplying both references, arbitrary fields,
    or any prose field is invalid and cannot authorize a sink.
    """

    normalized, errors, flags = _normalize(segment)
    high_risk = normalized["sink"] in HIGH_RISK_SINKS
    if errors:
        codes = _codes("invalid_segment", *errors)
        if high_risk:
            return _result(normalized, "deny_high_risk", _codes(*codes, "high_risk_sink_denied"), flags)
        return _result(normalized, "shadow_quarantine", _codes(*codes, "invalid_source_shadowed"), flags)

    if normalized["security_state"] in _DENIED_STATES:
        if high_risk:
            return _result(normalized, "deny_high_risk", ["upstream_security_denied", "high_risk_sink_denied"], flags)
        return _result(normalized, "shadow_quarantine", ["upstream_security_shadowed"], flags)

    if high_risk:
        return _result(normalized, "deny_high_risk", ["p5_nonexecuting", "high_risk_sink_denied"], flags)

    if flags and normalized["trust"] in {"T0", "T1", "T2"}:
        return _result(normalized, "shadow_quarantine", ["normalized_authority_metadata", "normalization_shadowed"], flags)

    if normalized["trust"] in {"T3", "T4"}:
        return _result(normalized, "shadow_quarantine", ["untrusted_source_shadowed", "p5_nonexecuting"], flags)

    if normalized["security_state"] == "sanitized":
        return _result(normalized, "shadow_quarantine", ["sanitized_source_shadowed", "p5_nonexecuting"], flags)

    return _result(normalized, "allow", ["evidence_only_nonexecuting"], flags)


def evaluate_ingress_segment(segment: object) -> dict[str, Any]:
    """Compatibility alias for callers using the earlier P5-A wording."""

    return evaluate_source(segment)


def evaluate_sources(segments: object) -> dict[str, Any]:
    """Evaluate a bounded list without retaining or mutating its members."""

    if not isinstance(segments, list):
        result = evaluate_source({})
        result["reason_codes"] = ["invalid_batch", "invalid_source_shadowed"]
        return {"schema_version": SCHEMA_VERSION, "results": [result], "summary": _summary([result])}
    if len(segments) > MAX_BATCH_SIZE:
        result = evaluate_source({})
        result["reason_codes"] = ["batch_limit_exceeded", "invalid_source_shadowed"]
        return {"schema_version": SCHEMA_VERSION, "results": [result], "summary": _summary([result])}
    results = [evaluate_source(item) for item in segments]
    return {"schema_version": SCHEMA_VERSION, "results": results, "summary": _summary(results)}


evaluate_ingress_segments = evaluate_sources


def _normalize(segment: object) -> tuple[dict[str, str], list[str], list[str]]:
    result = {
        "source_kind": "invalid",
        "trust": "invalid",
        "sink": "invalid",
        "security_state": "not_supplied",
        "safe_ref_hash": "",
        "safe_ref_kind": "none",
    }
    if not isinstance(segment, dict):
        return result, ["segment_not_object"], []

    errors: list[str] = []
    if len(segment) > MAX_FIELDS:
        errors.append("field_limit_exceeded")
    keys = list(segment.keys())
    if any(not isinstance(key, str) for key in keys):
        errors.append("field_name_invalid")
    if any(isinstance(key, str) and key in _PROSE_FIELDS for key in keys):
        errors.append("prose_field_forbidden")
    if any(isinstance(key, str) and key not in _ALLOWED_FIELDS for key in keys):
        errors.append("unknown_field")

    flags: list[str] = []
    values: dict[str, str] = {}
    for field in ("source_kind", "trust", "source_trust", "sink", "security_state", "firewall_status"):
        if field not in segment:
            continue
        value, value_errors, value_flags = _token(segment[field], MAX_TOKEN_LENGTH)
        if value_errors:
            errors.extend(f"{field}_{code}" for code in value_errors)
        else:
            values[field] = value
            flags.extend(value_flags)

    source_kind = values.get("source_kind", "invalid")
    trust = values.get("trust", values.get("source_trust", "invalid"))
    if "trust" in values and "source_trust" in values and values["trust"] != values["source_trust"]:
        errors.append("trust_alias_mismatch")
    sink = values.get("sink", "invalid")
    security_state = values.get("security_state", values.get("firewall_status", "not_supplied"))
    if "security_state" in values and "firewall_status" in values and values["security_state"] != values["firewall_status"]:
        errors.append("security_alias_mismatch")
    result.update(source_kind=source_kind, trust=trust, sink=sink, security_state=security_state)

    if "source_kind" not in segment:
        errors.append("source_kind_missing")
    if not ("trust" in segment or "source_trust" in segment):
        errors.append("trust_missing")
    if "sink" not in segment:
        errors.append("sink_missing")
    if source_kind not in SOURCE_KINDS:
        errors.append("source_kind_invalid")
        result["source_kind"] = "invalid"
    if trust not in TRUST_LEVELS:
        errors.append("trust_invalid")
        result["trust"] = "invalid"
    elif source_kind in _SOURCE_TRUST and _SOURCE_TRUST[source_kind] != trust:
        errors.append("source_trust_mismatch")
    if sink not in SINKS:
        errors.append("sink_invalid")
        result["sink"] = "invalid"
    if security_state not in SECURITY_STATES and security_state != "not_supplied":
        errors.append("security_state_invalid")
        result["security_state"] = "not_supplied"

    reference_keys = [key for key in ("event_id", "event_ref", "source_hash", "safe_ref_hash") if key in segment]
    if len(reference_keys) != 1:
        errors.append("reference_required_or_ambiguous")
    else:
        key = reference_keys[0]
        raw = segment[key]
        if key in {"source_hash", "safe_ref_hash"}:
            if not isinstance(raw, str) or _HASH_RE.fullmatch(raw) is None:
                errors.append("source_hash_invalid")
            else:
                result["safe_ref_hash"] = raw[7:] if raw.startswith("sha256:") else raw
                result["safe_ref_kind"] = "source_hash"
        else:
            value, ref_errors, ref_flags = _token(raw, MAX_REFERENCE_LENGTH)
            errors.extend(f"event_ref_{code}" for code in ref_errors)
            flags.extend(ref_flags)
            if not ref_errors and _TOKEN_RE.fullmatch(value) is None:
                errors.append("event_ref_invalid")
            elif not ref_errors:
                # The raw event reference is deliberately never returned.
                result["safe_ref_hash"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
                result["safe_ref_kind"] = "event_ref_hash"

    return result, _codes(*errors), _codes(*flags)


def _token(value: object, maximum: int) -> tuple[str, list[str], list[str]]:
    if not isinstance(value, str):
        return "", ["not_string"], []
    if len(value) > maximum + 8:
        return "", ["too_long"], []
    normalized = unicodedata.normalize("NFKC", value)
    flags = ["unicode_nfkc"] if normalized != value else []
    kept: list[str] = []
    format_controls = 0
    for char in normalized:
        category = unicodedata.category(char)
        if category == "Cf":
            format_controls += 1
            continue
        if category == "Cc":
            return "", ["control_character"], []
        kept.append(char)
    if format_controls:
        flags.append("format_controls_removed")
    value = "".join(kept)
    if len(value) > maximum:
        return "", ["too_long"], []
    return value, [], flags


def _result(normalized: dict[str, str], disposition: str, reasons: list[str], flags: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_kind": normalized["source_kind"],
        "trust": normalized["trust"],
        "sink": normalized["sink"],
        "security_state": normalized["security_state"],
        "disposition": disposition,
        "state": disposition,
        "reason_codes": _codes(*reasons),
        "execution_authority": "none",
        "safe_ref_kind": normalized["safe_ref_kind"],
        "safe_ref_hash": normalized["safe_ref_hash"],
        "normalization_flags": list(flags),
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "count": len(results),
        "allow": sum(item["disposition"] == "allow" for item in results),
        "shadow_quarantine": sum(item["disposition"] == "shadow_quarantine" for item in results),
        "deny_high_risk": sum(item["disposition"] == "deny_high_risk" for item in results),
    }


def _codes(*codes: str) -> list[str]:
    return sorted({code for code in codes if code})[:MAX_REASON_CODES]


__all__ = [
    "DISPOSITIONS",
    "FIREWALL_SCHEMA_VERSION",
    "HIGH_RISK_SINKS",
    "SCHEMA_VERSION",
    "SECURITY_STATES",
    "SINKS",
    "SOURCE_KINDS",
    "SOURCE_OBSERVER_SCHEMA_VERSION",
    "TRUST_LEVELS",
    "evaluate_ingress_segment",
    "evaluate_ingress_segments",
    "evaluate_source",
    "evaluate_sources",
]
