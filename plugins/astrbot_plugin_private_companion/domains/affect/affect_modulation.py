"""Compose condition-level affect deltas without creating another state authority."""

from __future__ import annotations

from typing import Any

try:
    from ...affect_modulation_contract import normalize_affect_modulation
except ImportError:  # pragma: no cover - direct-module compatibility
    from affect_modulation_contract import normalize_affect_modulation


def compose_affect_modulation(conditions: list[dict[str, Any]], *, now: float) -> dict[str, Any]:
    valence = arousal = vulnerability = confidence_total = 0.0
    source_ids: list[str] = []
    for condition in conditions[:64]:
        if not isinstance(condition, dict):
            continue
        modulation = normalize_affect_modulation(condition.get("modulation"))
        confidence = modulation["confidence"]
        if confidence <= 0:
            continue
        start = condition.get("start_ts")
        half_life = condition.get("half_life_seconds")
        decay = 1.0
        if type(start) in {int, float} and type(half_life) in {int, float} and float(half_life) >= 60:
            decay = 0.5 ** (max(0.0, now - float(start)) / min(86400.0, float(half_life)))
        weight = confidence * decay
        valence += modulation["valence"] * weight
        arousal += modulation["arousal"] * weight
        vulnerability += modulation["vulnerability"] * weight
        confidence_total += weight
        event_id = str(condition.get("source_event_id") or "")[:96]
        if event_id and event_id not in source_ids:
            source_ids.append(event_id)
    divisor = max(1.0, confidence_total)
    return normalize_affect_modulation({
        "valence": valence / divisor,
        "arousal": arousal / divisor,
        "vulnerability": vulnerability / divisor,
        "confidence": min(1.0, confidence_total),
        "source_event_ids": source_ids,
        "computed_at": now,
    })


__all__ = ["compose_affect_modulation"]
