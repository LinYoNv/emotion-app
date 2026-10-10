"""Deterministic recovery dynamics behind the seven interaction bands."""

from __future__ import annotations

import math
from typing import Any, Mapping


DYNAMICS_VERSION = "interaction_dynamics.v1"
NEGATIVE_BANDS = {"avoidant", "hurt"}
POSITIVE_BANDS = {"lively", "warm", "close", "affectionate"}
POSITIVE_BAND_SEQUENCE = ("relaxed", "lively", "warm", "close", "affectionate")


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def project_interaction_dynamics(value: Any, *, now: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    if raw.get("dynamics_version") != DYNAMICS_VERSION:
        return {}
    current = max(0.0, min(100.0, _finite(raw.get("load"))))
    peak = max(current, min(100.0, _finite(raw.get("peak_intensity"), current)))
    started = max(0.0, _finite(raw.get("decay_started_at"), _finite(raw.get("updated_at"))))
    current_ts = max(started, _finite(now, started))
    half_life = max(300.0, min(86400.0, _finite(raw.get("half_life"), 3600.0)))
    load = current * (0.5 ** (max(0.0, current_ts - started) / half_life))
    polarity = -1 if int(_finite(raw.get("polarity"), 0)) < 0 else 1 if int(_finite(raw.get("polarity"), 0)) > 0 else 0
    stored_band = str(raw.get("base_band") or raw.get("expression_band") or "relaxed")
    if polarity < 0:
        band = "avoidant" if load >= 70.0 else "hurt" if load >= 12.0 else "relaxed"
    elif polarity > 0 and stored_band in POSITIVE_BANDS and load >= 8.0:
        band = stored_band
    else:
        band = "relaxed"
    recovery_band = "steady"
    if load > 0.5 and load < peak * 0.9:
        recovery_band = "recovering"
    elif load >= peak * 0.98 and peak > 0:
        recovery_band = "reinforced"
    remaining = half_life * math.log2(max(1.0, load)) if load > 1.0 else 0.0
    return {
        "dynamics_version": DYNAMICS_VERSION,
        "expression_band": band,
        "base_band": stored_band,
        "load": round(load, 4),
        "peak_intensity": round(peak, 4),
        "decay_started_at": started,
        "half_life": half_life,
        "polarity": polarity,
        "recovery_band": recovery_band,
        "expires_at": current_ts + remaining if remaining > 0 else 0.0,
        "projection_revision": max(1, min(1000000, int(_finite(raw.get("projection_revision"), 1)))),
    }


def settle_interaction_dynamics(
    existing: Any,
    *,
    requested_band: str,
    event_kind: str,
    intensity: Any,
    now: Any,
) -> dict[str, Any]:
    current_ts = max(0.0, _finite(now))
    prior = project_interaction_dynamics(existing, now=current_ts)
    prior_load = _finite(prior.get("load"))
    prior_polarity = int(_finite(prior.get("polarity"), 0))
    event_intensity = max(0.0, min(100.0, _finite(intensity)))
    kind = str(event_kind or "neutral").strip().lower()
    requested = requested_band if requested_band in NEGATIVE_BANDS | POSITIVE_BANDS | {"relaxed"} else "relaxed"

    if kind == "hurt" and requested in NEGATIVE_BANDS:
        base = prior_load if prior_polarity < 0 else 0.0
        load = min(100.0, base + max(20.0, event_intensity) * max(0.25, 1.0 - base / 140.0))
        polarity = -1
        base_band = requested
        half_life = 3600.0
    elif prior_polarity < 0 and kind in {"apology", "comfort", "praise", "intimacy", "play"}:
        factor = {"apology": 0.35, "comfort": 0.25, "praise": 0.12, "intimacy": 0.20, "play": 0.10}[kind]
        load = max(0.0, prior_load - max(6.0, event_intensity * factor))
        polarity = -1 if load > 0.5 else 0
        base_band = str(prior.get("base_band") or "hurt")
        half_life = _finite(prior.get("half_life"), 3600.0)
    elif requested in POSITIVE_BANDS:
        base = prior_load if prior_polarity > 0 else 0.0
        load = min(100.0, base * 0.75 + max(10.0, event_intensity * 0.65))
        polarity = 1
        prior_band = str(prior.get("base_band") or "relaxed") if prior_polarity > 0 else "relaxed"
        if prior_band not in POSITIVE_BAND_SEQUENCE:
            prior_band = "relaxed"
        prior_index = POSITIVE_BAND_SEQUENCE.index(prior_band)
        requested_index = POSITIVE_BAND_SEQUENCE.index(requested)
        # A single event may warm or cool the expression by one band. Stronger
        # movement needs repeated events, so the visible voice cannot jump from
        # flat/low straight to intimate excitement in one turn.
        next_index = max(prior_index - 1, min(prior_index + 1, requested_index))
        base_band = POSITIVE_BAND_SEQUENCE[next_index]
        half_life = 7200.0
    else:
        return prior

    peak = max(load, _finite(prior.get("peak_intensity")) if polarity == prior_polarity else 0.0)
    raw = {
        "dynamics_version": DYNAMICS_VERSION,
        "expression_band": base_band,
        "base_band": base_band,
        "load": load,
        "peak_intensity": peak,
        "decay_started_at": current_ts,
        "half_life": half_life,
        "polarity": polarity,
        "projection_revision": int(_finite(prior.get("projection_revision"), 0)) + 1,
    }
    return project_interaction_dynamics(raw, now=current_ts)


__all__ = ["DYNAMICS_VERSION", "project_interaction_dynamics", "settle_interaction_dynamics"]
