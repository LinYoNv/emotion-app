"""Read-only, redacted diagnostics for the Companion-local emotion pipeline."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


DIAGNOSTIC_SCHEMA_VERSION = "companion_emotion_diagnostic_projection.v1"

_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+-]{1,40}$")
_EVENT_TYPES = frozenset({
    "neutral", "hurt", "boundary_violation", "apology", "comfort", "praise", "comfort_need",
    "external_negative", "scar_touched", "warm_memory", "vulnerable_resonance",
    "play", "intimacy", "boundary",
})
_EVENT_ORIGINS = frozenset({"interaction", "memory_recall", "system_condition"})
_EVENT_STATUSES = frozenset({"observed", "revised", "applied", "ignored", "expired"})
_INTERACTION_BANDS = frozenset({"avoidant", "hurt", "relaxed", "lively", "warm", "close", "affectionate"})
_INTERACTION_SOURCES = frozenset({"automatic", "manual"})
_RECOVERY_BANDS = frozenset({"steady", "recovering", "reinforced"})
_TONES = frozenset({"reserved", "careful", "steady", "bright", "gentle", "intimate", "affectionate"})
_RESPONSE_LENGTHS = frozenset({"none", "brief", "balanced", "expanded"})
_INITIATIVES = frozenset({"allowed", "passive_only", "blocked"})
_PACING = frozenset({"slow", "steady", "bright"})
_DIRECTNESS = frozenset({"indirect", "natural", "direct"})
_VALIDATION_STYLES = frozenset({"none", "acknowledge", "support_first"})
_SELF_DISCLOSURE = frozenset({"none", "light", "allowed"})
_HUMOR_MODES = frozenset({"off", "light", "playful"})
_TOPIC_INITIATIVES = frozenset({"reply_only", "followup", "shared_topic"})
_SOURCE_RULES = frozenset({
    "direct_bot_target", "self_low", "diagnostic_skip", "structured_text", "quoted_negative",
    "third_party_target", "direct_positive_target", "negative_target_uncertain", "atrelay_skip",
    "playful_or_single_boundary", "boundary_goes_relationship", "third_party_negative", "severe_hurt",
    "identity_hurt", "mild_hurt", "explicit_boundary_violation", "apology", "comfort", "praise", "llm_emotion_judgement",
    "short_chat_rule", "self_negative", "target_none",
})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _selector(value: Any) -> str:
    candidate = _text(value, 96)
    return candidate if _SELECTOR_RE.fullmatch(candidate) else ""


def _code(value: Any, allowed: frozenset[str], default: str = "") -> str:
    candidate = _text(value, 96).lower()
    return candidate if candidate in allowed else default


def _safe_code(value: Any) -> str:
    candidate = _text(value, 80).lower()
    return candidate if _CODE_RE.fullmatch(candidate) else ""


def _timestamp(value: Any) -> str:
    candidate = _text(value, 48)
    return candidate if _TIMESTAMP_RE.fullmatch(candidate) else ""


def _integer(value: Any, default: int = 0, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return round(max(minimum, min(maximum, parsed)), 4)


def _event_projection(event: Mapping[str, Any], trace_id: str) -> dict[str, Any] | None:
    if _selector(event.get("trace_id")) != trace_id:
        return None
    event_id = _selector(event.get("event_id"))
    if not event_id:
        return None
    return {
        "event_id": event_id,
        "trace_id": trace_id,
        "revision": _integer(event.get("revision"), 1, 1),
        "origin_kind": _code(event.get("origin_kind"), _EVENT_ORIGINS, "interaction"),
        "event_type": _code(event.get("event_type"), _EVENT_TYPES, "neutral"),
        "intensity": _number(event.get("intensity"), 0.0, 0.0, 100.0),
        "confidence": _number(event.get("confidence"), 0.0, 0.0, 1.0),
        "valence": _number(event.get("valence_hint"), 0.0, -1.0, 1.0),
        "arousal": _number(event.get("arousal_hint"), 0.0, 0.0, 1.0),
        "vulnerability": _number(event.get("vulnerability_hint"), 0.0, 0.0, 1.0),
        "status": _code(event.get("status"), _EVENT_STATUSES, "observed"),
        "source_rule": _code(event.get("source_rule"), _SOURCE_RULES),
        "occurred_at": _timestamp(event.get("occurred_at")),
        "applied_interaction": _code(event.get("applied_interaction"), _INTERACTION_BANDS),
        "correction_of": _selector(event.get("correction_of")),
    }


def _afterglow_projection(condition: Mapping[str, Any], trace_id: str) -> dict[str, Any] | None:
    if condition.get("kind") != "memory_afterglow" or _selector(condition.get("trace_id")) != trace_id:
        return None
    source_event_id = _selector(condition.get("source_event_id"))
    if not source_event_id:
        return None
    modulation = _mapping(condition.get("modulation"))
    return {
        "trace_id": trace_id,
        "source_event_id": source_event_id,
        "source_revision": _integer(condition.get("source_revision"), 1, 1),
        "energy_delta": _number(condition.get("energy_delta"), 0.0, -8.0, 5.0),
        "intensity": _integer(condition.get("intensity"), 0, 0, 100),
        "start_ts": _number(condition.get("start_ts"), 0.0, 0.0, 99_999_999_999.0),
        "end_ts": _number(condition.get("end_ts"), 0.0, 0.0, 99_999_999_999.0),
        "half_life_seconds": _number(condition.get("half_life_seconds"), 0.0, 0.0, 86_400.0),
        "affect_modulation": {
            "valence": _number(modulation.get("valence"), 0.0, -1.0, 1.0),
            "arousal": _number(modulation.get("arousal"), 0.0, 0.0, 1.0),
            "vulnerability": _number(modulation.get("vulnerability"), 0.0, 0.0, 1.0),
            "confidence": _number(modulation.get("confidence"), 0.0, 0.0, 1.0),
        },
    }


def _interaction_projection(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return {
        "expression_band": _code(source.get("expression_band"), _INTERACTION_BANDS, "relaxed"),
        "source": _code(source.get("source"), _INTERACTION_SOURCES, "automatic"),
        "updated_at": _number(source.get("updated_at"), 0.0, 0.0, 99_999_999_999.0),
        "expires_at": _number(source.get("expires_at"), 0.0, 0.0, 99_999_999_999.0),
        "last_event_id": _selector(source.get("last_event_id")),
        "trace_id": _selector(source.get("trace_id")),
        "load": _number(source.get("load"), 0.0, 0.0, 100.0),
        "peak_intensity": _number(source.get("peak_intensity"), 0.0, 0.0, 100.0),
        "recovery_band": _code(source.get("recovery_band"), _RECOVERY_BANDS, "steady"),
    }


def _expression_projection(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return {
        "contract": _safe_code(source.get("contract")),
        "expression_band": _code(source.get("expression_band"), _INTERACTION_BANDS, "relaxed"),
        "tone": _code(source.get("tone"), _TONES, "steady"),
        "warmth": _integer(source.get("warmth"), 0, 0, 100),
        "response_length": _code(source.get("response_length"), _RESPONSE_LENGTHS, "balanced"),
        "followup": bool(source.get("followup")),
        "initiative": _code(source.get("initiative"), _INITIATIVES, "passive_only"),
        "proactive_budget": _integer(source.get("proactive_budget"), 0, 0, 30),
        "proactive_target": _integer(source.get("proactive_target"), 0, 0, 30),
        "tts_style": _safe_code(source.get("tts_style")),
        "pacing": _code(source.get("pacing"), _PACING, "steady"),
        "directness": _code(source.get("directness"), _DIRECTNESS, "natural"),
        "validation_style": _code(source.get("validation_style"), _VALIDATION_STYLES, "none"),
        "self_disclosure": _code(source.get("self_disclosure"), _SELF_DISCLOSURE, "none"),
        "humor_mode": _code(source.get("humor_mode"), _HUMOR_MODES, "off"),
        "topic_initiative": _code(source.get("topic_initiative"), _TOPIC_INITIATIVES, "reply_only"),
        "safety_mode": _safe_code(source.get("safety_mode")),
        "blocker": _safe_code(source.get("blocker")),
    }


def _daily_state_projection(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    modulation = _mapping(source.get("affect_modulation"))
    return {
        "energy": _integer(source.get("energy"), 0, 0, 100),
        "affect_modulation": {
            "schema_version": _safe_code(modulation.get("schema_version")),
            "valence": _number(modulation.get("valence"), 0.0, -1.0, 1.0),
            "arousal": _number(modulation.get("arousal"), 0.0, 0.0, 1.0),
            "vulnerability": _number(modulation.get("vulnerability"), 0.0, 0.0, 1.0),
            "confidence": _number(modulation.get("confidence"), 0.0, 0.0, 1.0),
        },
    }


def emotion_trace_summary(user: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return the latest safe revision for each locally stored trace."""

    ledger = _mapping(user).get("emotion_event_ledger")
    if not isinstance(ledger, list):
        return []
    latest: dict[str, Mapping[str, Any]] = {}
    for item in ledger:
        event = _mapping(item)
        trace_id = _selector(event.get("trace_id"))
        event_id = _selector(event.get("event_id"))
        if not trace_id or not event_id:
            continue
        previous = latest.get(trace_id)
        if previous is None or (
            _integer(event.get("revision"), 1, 1), _timestamp(event.get("occurred_at")), event_id
        ) >= (
            _integer(previous.get("revision"), 1, 1), _timestamp(previous.get("occurred_at")), _selector(previous.get("event_id"))
        ):
            latest[trace_id] = event
    bounded_limit = _integer(limit, 20, 1, 100)
    ordered = sorted(
        latest.values(),
        key=lambda item: (_timestamp(item.get("occurred_at")), _integer(item.get("revision"), 1, 1)),
        reverse=True,
    )
    return [
        {
            "trace_id": _selector(item.get("trace_id")),
            "event_id": _selector(item.get("event_id")),
            "revision": _integer(item.get("revision"), 1, 1),
            "event_type": _code(item.get("event_type"), _EVENT_TYPES, "neutral"),
            "status": _code(item.get("status"), _EVENT_STATUSES, "observed"),
            "occurred_at": _timestamp(item.get("occurred_at")),
        }
        for item in ordered[:bounded_limit]
    ]


def build_emotion_trace_projection(
    user: Any,
    trace_id: Any,
    *,
    daily_state: Any = None,
    state_conditions: Any = None,
    expression_decision: Any = None,
) -> dict[str, Any]:
    """Build a local-only trace view without consulting the Memory plugin."""

    source = _mapping(user)
    requested_trace = _selector(trace_id)
    ledger = source.get("emotion_event_ledger")
    ledger_items = ledger if isinstance(ledger, list) else []
    events = [
        projected
        for item in ledger_items
        for projected in [_event_projection(_mapping(item), requested_trace)]
        if projected is not None
    ] if requested_trace else []
    events.sort(key=lambda item: (item["revision"], item["occurred_at"], item["event_id"]))

    state = _mapping(daily_state)
    conditions = state_conditions if isinstance(state_conditions, list) else state.get("conditions")
    condition_items = conditions if isinstance(conditions, list) else []
    afterglow = [
        projected
        for item in condition_items
        for projected in [_afterglow_projection(_mapping(item), requested_trace)]
        if projected is not None
    ] if requested_trace else []
    afterglow.sort(key=lambda item: (item["source_revision"], item["source_event_id"]))

    local_trace = requested_trace if events or afterglow else ""
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "state": "ready" if local_trace else "not_found",
        "read_only": True,
        "trace_id": local_trace,
        "events": events,
        "afterglow": afterglow[:20],
        "current_interaction": _interaction_projection(source.get("current_interaction")),
        "daily_state": _daily_state_projection(state),
        "expression_decision": _expression_projection(expression_decision),
        "memory_diagnostic": {
            "available": False,
            "state": "unavailable",
            "reason_code": "diagnostic_authority_unavailable",
        },
    }


__all__ = ["DIAGNOSTIC_SCHEMA_VERSION", "build_emotion_trace_projection", "emotion_trace_summary"]
