from __future__ import annotations

import unittest

from migration_stability import advance_migration_stability
from req041_observability import Req041Observability


class _Coordinator:
    def __init__(self) -> None:
        self.value = {"phase": "S6", "checkpoint": "cutover", "state": "active"}
        self.pending = 0
        self.generation = "new"

    def status(self):
        return dict(self.value)

    def safe_admin_summary(self):
        return {
            "identities": [{
                "assurance": "verified", "state": "new_read",
                "read_generation": self.generation, "count": 1,
            }],
            "pending": {"total": self.pending, "reasons": []},
        }

    def transition(self, phase, *, checkpoint):
        self.value.update({"phase": phase, "checkpoint": checkpoint})
        return self.status()


class _Outbox:
    def __init__(self) -> None:
        self.backlog = 0
        self.checkpoints = []

    def safe_admin_summary(self, _epoch):
        return {"backlog": self.backlog, "states": {}}

    def set_epoch_state(self, _epoch, state, *, checkpoint):
        self.checkpoints.append((state, checkpoint))


class MigrationStabilityTests(unittest.TestCase):
    @staticmethod
    def _metrics() -> Req041Observability:
        metrics = Req041Observability()
        for _ in range(20):
            metrics.observe("permission_profile_relationship", 10)
        return metrics

    def test_s7_cycles_require_a_real_restart_before_s9(self) -> None:
        coordinator, outbox, metrics = _Coordinator(), _Outbox(), self._metrics()
        kwargs = {
            "coordinator": coordinator, "outbox": outbox,
            "migration_epoch": "epoch", "replay_ok": True, "scoped_ok": True,
            "memory_bound": True, "observability": metrics, "boot_ref": "boot-a",
        }
        self.assertEqual(advance_migration_stability(**kwargs)["phase"], "S7")
        self.assertEqual(advance_migration_stability(**kwargs)["phase"], "S7")
        self.assertEqual(advance_migration_stability(**kwargs)["phase"], "S8")
        self.assertEqual(advance_migration_stability(**kwargs)["code"], "s8_restart_required")
        kwargs["boot_ref"] = "boot-b"
        result = advance_migration_stability(**kwargs)
        self.assertEqual(result["phase"], "S9")
        self.assertEqual(outbox.checkpoints[-1][1], "s9_verified_dual_write_retained")

    def test_pending_or_shadow_mismatch_blocks_advancement(self) -> None:
        coordinator, outbox, metrics = _Coordinator(), _Outbox(), self._metrics()
        coordinator.pending = 1
        result = advance_migration_stability(
            coordinator=coordinator, outbox=outbox, migration_epoch="epoch",
            replay_ok=True, scoped_ok=True, memory_bound=True,
            observability=metrics, boot_ref="boot-a",
        )
        self.assertEqual(result["code"], "pending_identity_records")
        coordinator.pending = 0
        metrics.increment("shadow_read_mismatch")
        result = advance_migration_stability(
            coordinator=coordinator, outbox=outbox, migration_epoch="epoch",
            replay_ok=True, scoped_ok=True, memory_bound=True,
            observability=metrics, boot_ref="boot-a",
        )
        self.assertEqual(result["code"], "shadow_mismatch")


if __name__ == "__main__":
    unittest.main()
