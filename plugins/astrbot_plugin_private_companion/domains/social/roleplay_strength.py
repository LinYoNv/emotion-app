"""Deterministic roleplay-strength projection for the Companion persona.

The persona should exaggerate (play along) when a group is teasing or
bantering, and dampen when the group is serious, tense, silent, or when a
member has asked to stop.  This module maps the group mood and the current
expression band into a single ``exaggerate`` scalar and a stable strength
band.  Prompt wording belongs to the group-observation adapter.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .group_mood import MOOD_LABELS, project_group_mood


ROLEPLAY_STRENGTH_VERSION = "roleplay_strength.v1"

# Base exaggeration per mood (0-100).  The persona leans into playful moods,
# and steps back from serious / tense / silent ones.
_MOOD_BASE_EXAGGERATE = {
    "tease": 72.0,
    "banter": 66.0,
    "confession": 38.0,
    "serious": 14.0,
    "tension": 10.0,
    "dead_silence": 8.0,
}

# Expression-band modifiers: warmer bands allow more playfulness, reserved
# bands cap it.
_BAND_MODIFIER = {
    "avoidant": -40.0,
    "hurt": -50.0,
    "relaxed": 0.0,
    "lively": 16.0,
    "warm": 8.0,
    "close": 10.0,
    "affectionate": 4.0,
}

# Social-tension penalty: the hotter the air, the less we add fire.
_TENSION_DAMPEN_FACTOR = 0.5


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _band(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _BAND_MODIFIER else "relaxed"


def project_roleplay_strength(
    mood: Any,
    *,
    expression_band: Any = "relaxed",
    now: Any = None,
) -> dict[str, Any]:
    """Project a mood snapshot into a roleplay/exaggeration instruction.

    Args:
        mood: A group-mood snapshot (or raw mapping) as produced by
            :func:`group_mood.settle_group_mood`.
        expression_band: Current expression band value.
        now: Optional timestamp used for mood projection.
    """
    projected = project_group_mood(mood, now=now)
    if not projected:
        projected = {"mood_scores": {}, "top_mood": "dead_silence", "social_tension": 0.0}
    top_mood = str(projected.get("top_mood") or "dead_silence")
    if top_mood not in MOOD_LABELS:
        top_mood = "dead_silence"
    band = _band(expression_band)
    scores = projected.get("mood_scores") or {}
    # Blend the base mood exaggeration with the dominant mood weight so a weak
    # tease signal does not crank the persona to full meme mode.
    dominant = _finite(scores.get(top_mood), 0.0)
    base = _MOOD_BASE_EXAGGERATE.get(top_mood, 8.0)
    weight = min(1.0, max(0.0, dominant) / 50.0) if top_mood != "dead_silence" else min(1.0, dominant / 60.0)
    blended = base * weight + 8.0 * (1.0 - weight)
    modifier = _BAND_MODIFIER.get(band, 0.0)
    exaggerate = max(0.0, min(100.0, blended + modifier))

    # Tension dampening: keep the persona from escalating confrontation.
    tension = _finite(projected.get("social_tension"))
    if tension > 0:
        exaggerate = max(0.0, exaggerate - tension * _TENSION_DAMPEN_FACTOR * 0.3)

    if exaggerate >= 60:
        strength_band = "playful_high"
    elif exaggerate >= 35:
        strength_band = "playful_moderate"
    elif exaggerate >= 12:
        strength_band = "reserved"
    else:
        strength_band = "minimal"

    return {
        "version": ROLEPLAY_STRENGTH_VERSION,
        "exaggerate": round(exaggerate, 2),
        "top_mood": top_mood,
        "expression_band": band,
        "social_tension": round(tension, 2),
        "strength_band": strength_band,
    }


__all__ = ["ROLEPLAY_STRENGTH_VERSION", "project_roleplay_strength"]
