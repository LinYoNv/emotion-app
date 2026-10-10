from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from identity_namespace import build_namespace_context
from migration_coordinator import MigrationCoordinator
from migration_read_router import MigrationRelationshipReadRouter
from relationship_account_store import RelationshipAccountStore
from unified_person_registry import UnifiedPersonRegistry


POLICY = "req041-v1"


def _identity(subject: str) -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1", "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user", "platform_subject_id": subject,
    }


class MigrationRelationshipReadRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        source = self.data_dir / "companions.json"
        source.write_text('{"users":{}}', encoding="utf-8")
        self.coordinator = MigrationCoordinator(self.data_dir)
        status = self.coordinator.start_or_resume(
            source_files=[source], policy_version=POLICY,
            source_schema_version="legacy", target_schema_version="req041-v1",
            companion_version="6.1.1", memory_version="1.7.2", reserve_bytes=0,
        )
        self.coordinator.capture_compatibility({})
        for phase in ("S3", "S4", "S5", "S6"):
            self.coordinator.transition(phase, checkpoint=f"test-{phase.lower()}")
        self.epoch = status["migration_epoch"]
        self.data: dict = {}
        self.registry = UnifiedPersonRegistry(self.data)
        created = self.registry.create_or_link(_identity("10001"), operation_id="create")
        self.person_id = created["person_id"]
        self.user = {
            "user_id": "10001", "unified_person_id": self.person_id,
            "relationship_role": "owner", "relationship_mode": "normal",
            "relationship_score": 777,
            "relationship_positive_stage_cap_key": "deeply_bonded",
        }
        self.coordinator.register_identity(self.person_id, assurance="verified")
        self.store = RelationshipAccountStore(
            self.data_dir / "relationships.db", active_migration_epoch=self.epoch,
        )
        resolution = self.registry.formal_namespace_for_person(
            self.person_id, policy_version=POLICY, migration_epoch=self.epoch,
            purpose="relationship_write",
        )
        self.context = build_namespace_context(resolution["context"])
        assert self.context is not None
        self.store.create_account(
            self.context, operation_id="shadow", actor="migration",
            relationship_role="owner", relationship_mode="normal", score=777,
            positive_stage_cap_key="deeply_bonded", legacy_snapshot=True,
        )
        self.router = MigrationRelationshipReadRouter(
            coordinator=self.coordinator, relationship_store=self.store,
            registry_resolver=lambda person_id: self.registry if person_id == self.person_id else None,
            migration_epoch=self.epoch, policy_version=POLICY,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stabilize(self, cycles: int = 2) -> None:
        for _ in range(cycles):
            self.coordinator.reconcile_identity(
                self.person_id, source_revision=2, target_revision=2,
                source_hash="a" * 64, target_hash="a" * 64, backlog=0,
            )

    def test_existing_chain_keeps_legacy_when_identity_switches_mid_chain(self) -> None:
        first = self.router.begin(self.user, event_ref="message-1")
        self._stabilize()
        self.coordinator.switch_identity_to_new_read(self.person_id, required_stable_cycles=2)
        same_chain = self.router.begin(self.user, event_ref="message-1")
        new_chain = self.router.begin(self.user, event_ref="message-2")
        self.assertEqual("legacy", first["generation"])
        self.assertEqual("legacy", same_chain["generation"])
        self.assertEqual("new", new_chain["generation"])
        self.assertEqual(777, new_chain["user"]["relationship_score"])
        self.assertEqual("owner", new_chain["user"]["relationship_role"])
        self.assertEqual(777, self.user["relationship_score"])
        self.assertTrue(self.router.finish(first["chain_id"]))
        self.assertTrue(self.router.finish(new_chain["chain_id"]))

    def test_group_view_contains_stage_but_not_precise_global_score(self) -> None:
        self._stabilize()
        self.coordinator.switch_identity_to_new_read(self.person_id, required_stable_cycles=2)
        result = self.router.begin(
            self.user, event_ref="group-message", kind="group_member", group_id="group-a",
        )
        self.assertEqual("new", result["generation"])
        self.assertEqual(0, result["user"]["relationship_score"])
        self.assertEqual("owner", result["user"]["relationship_role"])
        self.assertTrue(result["user"]["req041_relationship_stage_key"])
        self.assertNotIn("relationship_ledger", result["user"])

    def test_target_read_failure_rolls_back_only_this_identity(self) -> None:
        self._stabilize()
        self.coordinator.switch_identity_to_new_read(self.person_id, required_stable_cycles=2)
        empty_store = RelationshipAccountStore(
            self.data_dir / "empty.db", active_migration_epoch=self.epoch,
        )
        broken = MigrationRelationshipReadRouter(
            coordinator=self.coordinator, relationship_store=empty_store,
            registry_resolver=lambda _person_id: self.registry,
            migration_epoch=self.epoch, policy_version=POLICY,
        )
        result = broken.begin(self.user, event_ref="broken-target")
        self.assertEqual("legacy", result["generation"])
        self.assertTrue(result["rolled_back"])
        status = self.coordinator.identity_status(self.person_id)
        self.assertEqual("legacy_read", status["state"])
        self.assertEqual("legacy", status["read_generation"])

    def test_shadow_value_mismatch_fails_closed_and_rolls_back_identity(self) -> None:
        self._stabilize()
        self.coordinator.switch_identity_to_new_read(self.person_id, required_stable_cycles=2)
        stale = {**self.user, "relationship_score": 10}
        result = self.router.begin(stale, event_ref="shadow-mismatch")
        self.assertEqual("legacy", result["generation"])
        self.assertEqual("migration_read_shadow_mismatch", result["code"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual("legacy", self.coordinator.identity_status(self.person_id)["read_generation"])

    def test_corrupt_person_pointer_cannot_roll_back_another_identity(self) -> None:
        self._stabilize()
        self.coordinator.switch_identity_to_new_read(self.person_id, required_stable_cycles=2)
        corrupt = {**self.user, "user_id": "someone-else"}
        result = self.router.begin(corrupt, event_ref="corrupt-pointer")
        self.assertEqual("legacy", result["generation"])
        self.assertFalse(result["rolled_back"])
        self.assertEqual("new", self.coordinator.identity_status(self.person_id)["read_generation"])


if __name__ == "__main__":
    unittest.main()
