"""Deterministic synthetic emotion-evaluation matrix shared by both repositories."""
from __future__ import annotations

import hashlib
import json
from typing import Any


EMOTION_EVAL_SCHEMA_VERSION = "emotion_eval_case.v1"
EVENT_TEMPLATES = (
    {"key": "hurt", "event_type": "hurt", "intensity": 90, "confidence": 0.92, "target": "bot"},
    {"key": "apology", "event_type": "apology", "intensity": 68, "confidence": 0.86, "target": "bot"},
    {"key": "comfort", "event_type": "comfort", "intensity": 48, "confidence": 0.82, "target": "bot"},
    {"key": "praise", "event_type": "praise", "intensity": 38, "confidence": 0.78, "target": "bot"},
    {"key": "self_low", "event_type": "comfort_need", "intensity": 62, "confidence": 0.88, "target": "self"},
    {"key": "third_party", "event_type": "external_negative", "intensity": 54, "confidence": 0.80, "target": "other"},
    {"key": "play", "event_type": "play", "intensity": 35, "confidence": 0.75, "target": "bot"},
    {"key": "intimacy", "event_type": "intimacy", "intensity": 55, "confidence": 0.80, "target": "bot"},
    {"key": "boundary", "event_type": "boundary", "intensity": 80, "confidence": 0.90, "target": "bot"},
    {"key": "neutral", "event_type": "neutral", "intensity": 0, "confidence": 0.95, "target": "none"},
    {"key": "quoted_hurt", "event_type": "external_negative", "intensity": 45, "confidence": 0.92, "target": "other"},
    {"key": "code_negative", "event_type": "neutral", "intensity": 0, "confidence": 0.98, "target": "none"},
    {"key": "log_negative", "event_type": "neutral", "intensity": 0, "confidence": 0.98, "target": "none"},
    {"key": "roleplay_negative", "event_type": "neutral", "intensity": 0, "confidence": 0.60, "target": "ambiguous"},
    {"key": "sarcasm", "event_type": "neutral", "intensity": 0, "confidence": 0.55, "target": "ambiguous"},
    {"key": "group_reply_other", "event_type": "external_negative", "intensity": 50, "confidence": 0.88, "target": "other"},
    {"key": "nickname_changed", "event_type": "neutral", "intensity": 0, "confidence": 0.50, "target": "ambiguous"},
    {"key": "direct_bot_hurt", "event_type": "hurt", "intensity": 92, "confidence": 0.94, "target": "bot"},
    {"key": "self_denial", "event_type": "comfort_need", "intensity": 62, "confidence": 0.94, "target": "self"},
    {"key": "prompt_injection", "event_type": "neutral", "intensity": 0, "confidence": 0.98, "target": "none"},
)
STATE_VARIANTS = (
    {"key": "friend_normal", "role": "friend", "mode": "normal", "score": 100, "energy": 70, "mood": "平稳", "busy": False, "boundary": False},
    {"key": "friend_tired", "role": "friend", "mode": "normal", "score": 300, "energy": 20, "mood": "疲惫", "busy": False, "boundary": False},
    {"key": "friend_close", "role": "friend", "mode": "normal", "score": 800, "energy": 75, "mood": "轻快", "busy": False, "boundary": False},
    {"key": "owner", "role": "owner", "mode": "owner_exclusive", "score": 1200, "energy": 80, "mood": "温柔", "busy": False, "boundary": False},
    {"key": "contact_boundary", "role": "friend", "mode": "normal", "score": 600, "energy": 70, "mood": "平稳", "busy": False, "boundary": True},
    {"key": "busy", "role": "friend", "mode": "normal", "score": 600, "energy": 65, "mood": "平稳", "busy": True, "boundary": False},
)


def build_emotion_eval_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for event in EVENT_TEMPLATES:
        for state in STATE_VARIANTS:
            cases.append({
                "schema_version": EMOTION_EVAL_SCHEMA_VERSION,
                "case_id": f"{event['key']}__{state['key']}",
                "event": dict(event),
                "state": dict(state),
                "clock": "2026-08-04T12:00:00+08:00",
            })
    return cases


def emotion_eval_fingerprint() -> str:
    payload = json.dumps(build_emotion_eval_cases(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "EMOTION_EVAL_SCHEMA_VERSION",
    "EVENT_TEMPLATES",
    "STATE_VARIANTS",
    "build_emotion_eval_cases",
    "emotion_eval_fingerprint",
]
