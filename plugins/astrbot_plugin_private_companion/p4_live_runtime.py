"""Read-only live P4 runtime projection for the Companion request boundary."""
from __future__ import annotations

from typing import Any

try:
    from .p4_affinity_confinement import copy_runtime_state, validate_runtime_state
    from .p4_runtime_gate import apply_confinement_gate
except ImportError:  # Direct unittest execution from the plugin root.
    from p4_affinity_confinement import copy_runtime_state, validate_runtime_state
    from p4_runtime_gate import apply_confinement_gate


def decide_live_request(state: Any, *, now: Any | None = None) -> dict[str, Any]:
    """Never creates state; absent authority stays distinct from invalid authority."""
    if state is None:
        return {"observed": False, "status": "absent", "decision": "skip"}
    copied = copy_runtime_state(state)
    result = apply_confinement_gate({}, None, copied if copied is not None else state, now=now)
    return {
        "observed": True,
        "status": validate_runtime_state(state, now=now),
        "decision": result["decision"],
        "code": result["code"],
        "warmth_projection": result["warmth_projection"],
        "reply_template": result["reply_template"],
        "context_disposition": result["context_disposition"],
    }


__all__ = ["decide_live_request"]
