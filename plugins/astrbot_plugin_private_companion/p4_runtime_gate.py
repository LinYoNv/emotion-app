"""Fail-closed P4 gate used before any optional private-chat enrichment."""
from __future__ import annotations

from typing import Any

try:
    from .p4_affinity_confinement import validate_runtime_state
except ImportError:  # Direct unittest execution from the plugin root.
    from p4_affinity_confinement import validate_runtime_state


P4_RUNTIME_GATE_RESULT_SCHEMA = "chat.p4.runtime_gate.v1"
SAFE_CONFINEMENT_REPLY = "现在只能进行简短、尊重且安全的交流。"
_BOUNDARIES = {
    "guarded": "请保持尊重、简短的交流。",
    "neutral": "保持尊重、自然交流。",
    "warm": "保持尊重与分寸。",
    "close": "保持尊重与分寸。",
}


def build_warmth_projection(state: Any, *, now: Any | None = None) -> dict[str, str]:
    status = validate_runtime_state(state, now=now)
    tier = state.get("warmth") if status == "normal" and type(state) is dict else "guarded"
    return {"tier": tier, "boundary": _BOUNDARIES[tier]}


def apply_confinement_gate(request: Any, event: Any, state: Any, *, now: Any | None = None) -> dict[str, Any]:
    """Evaluate the state only; caller-owned event input cannot authorize release."""
    del event
    status = validate_runtime_state(state, now=now)
    projection = build_warmth_projection(state, now=now)
    if status in {"active", "invalid"}:
        return {
            "schema_version": P4_RUNTIME_GATE_RESULT_SCHEMA,
            "decision": "block",
            "code": "p4_confinement_active" if status == "active" else "p4_state_invalid",
            "reply_template": SAFE_CONFINEMENT_REPLY,
            "warmth_projection": projection,
            "context_disposition": {"prompt": "cleared", "tool": "cleared", "external": "cleared"},
        }
    return {
        "schema_version": P4_RUNTIME_GATE_RESULT_SCHEMA,
        "decision": "allow",
        "code": "p4_confinement_expired" if status == "expired" else "p4_confinement_released" if status == "released" else "p4_normal",
        "reply_template": "",
        "warmth_projection": projection,
        "context_disposition": {"prompt": "preserved", "tool": "preserved", "external": "preserved"},
    }


__all__ = ["P4_RUNTIME_GATE_RESULT_SCHEMA", "SAFE_CONFINEMENT_REPLY", "apply_confinement_gate", "build_warmth_projection"]
