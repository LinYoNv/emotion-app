from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migration_coordinator import MigrationCoordinator
from migration_outbox import MigrationOutbox
from req041_observability import Req041Observability
from identity_namespace import NamespaceContext
from relationship_account_store import RelationshipAccountStore


class Req041ObservabilityTests(unittest.TestCase):
    @staticmethod
    def _context() -> NamespaceContext:
        return NamespaceContext(
            kind="private", identity_id="person_aaaaaaaaaaaaaaaaaaaaaaaa", group_id="",
            assurance="verified", profile_status="active", policy_version="req041-v1",
            migration_epoch="req041-test-epoch",
        )

    def test_snapshot_is_bounded_redacted_and_uses_correct_hit_denominator(self) -> None:
        metrics = Req041Observability(sample_limit=64, clock=lambda: 100.0)
        metrics.cache_event("scoped_projection", "hit", namespace_kind="private", latency_ms=1)
        metrics.cache_event("scoped_projection", "miss", namespace_kind="group_member", latency_ms=2)
        metrics.cache_event("scoped_projection", "bypass", namespace_kind="private", latency_ms=3)
        metrics.cache_event("scoped_projection", "stale_reject", namespace_kind="group_shared", latency_ms=4)
        metrics.cache_event("raw-user-id", "hit", namespace_kind="raw-group-id", latency_ms=999)
        metrics.observe("memory_rules", 8, external=True)
        metrics.increment("cross_scope_denied")
        snapshot = metrics.snapshot()
        scoped = snapshot["caches"]["scoped_projection"]
        self.assertEqual(scoped["hit_rate"], 0.5)
        self.assertEqual(scoped["latency_ms"]["samples"], 4)
        self.assertEqual(snapshot["counters"]["cross_scope_denied"], 1)
        self.assertNotIn("raw-user-id", str(snapshot))
        self.assertNotIn("raw-group-id", str(snapshot))

    def test_admin_aggregates_contain_no_identity_stream_or_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = MigrationCoordinator(root)
            status = coordinator.initialize_fresh_runtime(
                policy_version="req041-v1", target_schema_version="req041-v1",
                companion_version="test", memory_version="test",
            )
            coordinator.register_identity("person-secret-001", assurance="verified")
            aggregate = coordinator.safe_admin_summary()
            self.assertEqual(sum(item["count"] for item in aggregate["identities"]), 1)
            self.assertNotIn("person-secret-001", str(aggregate))

            outbox = MigrationOutbox(root / "outbox.db")
            outbox.begin_epoch(status["migration_epoch"], policy_version="req041-v1")
            queue = outbox.safe_admin_summary(status["migration_epoch"])
            self.assertEqual(queue["backlog"], 0)
            self.assertNotIn(status["migration_epoch"], str(queue))

    def test_relationship_cache_is_revision_validated_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relationship.db"
            metrics = Req041Observability()
            first = RelationshipAccountStore(
                path, active_migration_epoch="req041-test-epoch", observability=metrics,
            )
            context = self._context()
            first.create_account(
                context, operation_id="create", actor="administrator", score=10,
            )
            self.assertEqual(first.account(context)["relationship_score"], 10)
            self.assertEqual(first.account(context)["relationship_score"], 10)
            second = RelationshipAccountStore(path, active_migration_epoch="req041-test-epoch")
            second.apply_event(
                context, event_id="external-write", actor="private_pipeline",
                reason_code="support", delta=5,
            )
            self.assertGreater(first.account(context)["relationship_score"], 10)
            cache = metrics.snapshot()["caches"]["relationship"]
            self.assertEqual(cache["outcomes"]["hit"], 1)
            self.assertEqual(cache["outcomes"]["miss"], 2)


if __name__ == "__main__":
    unittest.main()
