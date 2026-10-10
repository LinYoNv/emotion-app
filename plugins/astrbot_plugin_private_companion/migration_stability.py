"""S6-S9 stability-window and restart-resume gates for REQ-041."""
from __future__ import annotations

import hashlib
import re
from typing import Any


_S7_CHECKPOINT = re.compile(r"^s7_stable_cycle_(\d+)$")
_S8_CHECKPOINT = re.compile(r"^s8_restart_pending_([0-9a-f]{16})$")


def _boot_digest(boot_ref: str) -> str:
    return hashlib.sha256(str(boot_ref or "").encode("utf-8")).hexdigest()[:16]


def _local_p95(metrics: dict[str, Any]) -> tuple[float, int]:
    maximum = 0.0
    samples = 0
    for item in (metrics.get("stages") or {}).values():
        local = item.get("local") if isinstance(item, dict) else None
        if not isinstance(local, dict):
            continue
        maximum = max(maximum, float(local.get("p95") or 0.0))
        samples += int(local.get("samples") or 0)
    return maximum, samples


def advance_migration_stability(
    *, coordinator: Any, outbox: Any, migration_epoch: str,
    replay_ok: bool, scoped_ok: bool, memory_bound: bool,
    observability: Any, boot_ref: str, required_cycles: int = 3,
    minimum_local_samples: int = 20, local_p95_budget_ms: float = 125.0,
) -> dict[str, Any]:
    """Advance only on aggregate evidence; never stops legacy dual writes."""
    status = coordinator.status()
    phase = str(status.get("phase") or "")
    if phase not in {"S6", "S7", "S8", "S9"}:
        return {"advanced": False, "phase": phase, "code": "stability_not_ready"}
    aggregate = coordinator.safe_admin_summary()
    queue = outbox.safe_admin_summary(migration_epoch)
    metrics = observability.snapshot() if observability is not None else {}
    counters = metrics.get("counters") if isinstance(metrics.get("counters"), dict) else {}
    p95, local_samples = _local_p95(metrics)
    pending = int((aggregate.get("pending") or {}).get("total") or 0)
    backlog = int(queue.get("backlog") or 0)
    identity_rows = aggregate.get("identities") if isinstance(aggregate.get("identities"), list) else []
    formal_rows = [
        row for row in identity_rows
        if isinstance(row, dict) and row.get("assurance") in {"verified", "explicit_linked"}
    ]
    all_formal_new = all(row.get("read_generation") == "new" for row in formal_rows)
    reasons: list[str] = []
    if not replay_ok:
        reasons.append("replay_not_healthy")
    if not scoped_ok or not memory_bound:
        reasons.append("scoped_runtime_not_healthy")
    if pending:
        reasons.append("pending_identity_records")
    if backlog:
        reasons.append("outbox_backlog")
    if not all_formal_new:
        reasons.append("formal_identity_not_cut_over")
    if int(counters.get("migration_mismatch") or 0) or int(counters.get("shadow_read_mismatch") or 0):
        reasons.append("shadow_mismatch")
    if local_samples < max(1, int(minimum_local_samples)):
        reasons.append("insufficient_local_samples")
    if local_samples and p95 > float(local_p95_budget_ms):
        reasons.append("local_p95_over_budget")
    if reasons:
        return {
            "advanced": False, "phase": phase, "code": reasons[0],
            "reasons": reasons, "backlog": backlog, "pending": pending,
            "local_samples": local_samples, "local_p95_ms": p95,
        }
    checkpoint = str(status.get("checkpoint") or "")
    required = max(2, min(20, int(required_cycles)))
    if phase == "S6":
        next_status = coordinator.transition("S7", checkpoint="s7_stable_cycle_1")
        return {"advanced": True, "phase": next_status["phase"], "code": "s7_stability_started", "cycle": 1}
    if phase == "S7":
        matched = _S7_CHECKPOINT.fullmatch(checkpoint)
        cycle = int(matched.group(1)) if matched else 0
        cycle += 1
        if cycle < required:
            next_status = coordinator.transition("S7", checkpoint=f"s7_stable_cycle_{cycle}")
            return {"advanced": True, "phase": next_status["phase"], "code": "s7_stability_progress", "cycle": cycle}
        marker = _boot_digest(boot_ref)
        next_status = coordinator.transition("S8", checkpoint=f"s8_restart_pending_{marker}")
        outbox.set_epoch_state(migration_epoch, "active", checkpoint="s8_restart_resume_pending")
        return {"advanced": True, "phase": next_status["phase"], "code": "s8_restart_required", "cycle": cycle}
    if phase == "S8":
        matched = _S8_CHECKPOINT.fullmatch(checkpoint)
        if not matched or matched.group(1) == _boot_digest(boot_ref):
            return {"advanced": False, "phase": "S8", "code": "s8_restart_required"}
        next_status = coordinator.transition("S9", checkpoint="s9_restart_resume_verified")
        outbox.set_epoch_state(migration_epoch, "active", checkpoint="s9_verified_dual_write_retained")
        return {"advanced": True, "phase": next_status["phase"], "code": "s9_verified_dual_write_retained"}
    return {"advanced": False, "phase": "S9", "code": "s9_already_verified"}


__all__ = ["advance_migration_stability"]
