from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from identity_namespace import NamespaceContext
from migration_backfill import MigrationBackfill, legacy_pending_reference
from migration_coordinator import MigrationCoordinator
from person_context_contract import build_identity_key
from relationship_account_store import RelationshipNotFound
from unified_person_registry import UnifiedPersonRegistry


def _identity(subject_id: str = "10001") -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject_id,
    }


class MigrationBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.source = self.data_dir / "companions.json"
        self.source.write_text('{"users":{}}', encoding="utf-8")
        self.coordinator = MigrationCoordinator(self.data_dir)
        status = self.coordinator.start_or_resume(
            source_files=[self.source],
            policy_version="req041-v1",
            source_schema_version="legacy-effective",
            target_schema_version="req041-v1",
            companion_version="6.1.1",
            memory_version="1.7.2",
            reserve_bytes=0,
        )
        self.coordinator.capture_compatibility({})
        self.coordinator.transition("S3", checkpoint="durable_outbox_active")
        self.epoch = status["migration_epoch"]
        self.backfill = MigrationBackfill(
            coordinator=self.coordinator,
            relationship_path=self.data_dir / "req041_relationship.db",
            migration_epoch=self.epoch,
            policy_version="req041-v1",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _snapshot(
        self,
        *,
        role: str = "friend",
        mode: str = "normal",
        score: object = 21,
        legacy_key: str = "10001",
    ) -> tuple[dict, str]:
        snapshot: dict = {}
        created = UnifiedPersonRegistry(snapshot).create_or_link(
            _identity(),
            profile={"display_name": "safe fixture"},
            operation_id="fixture-create",
        )
        person_id = created["person_id"]
        snapshot["users"] = {
            legacy_key: {
                "unified_person_id": person_id,
                "relationship_role": role,
                "relationship_mode": mode,
                "relationship_score": score,
                "relationship_positive_stage_cap_key": "close",
            }
        }
        return snapshot, person_id

    def _context(self, person_id: str, assurance: str = "verified") -> NamespaceContext:
        return NamespaceContext(
            kind="private",
            identity_id=person_id,
            group_id="",
            assurance=assurance,
            profile_status="active",
            policy_version="req041-v1",
            migration_epoch=self.epoch,
        )

    def test_exact_five_field_link_creates_legacy_snapshot_without_mutating_source(self) -> None:
        snapshot, person_id = self._snapshot()
        original = deepcopy(snapshot)
        result = self.backfill.run(snapshot)
        account = self.backfill.relationships.account(self._context(person_id))
        self.assertEqual("S4", result["phase"])
        self.assertEqual(1, result["migrated"])
        self.assertEqual(0, result["pending"])
        self.assertEqual(21, account["relationship_score"])
        self.assertEqual("friend", account["relationship_role"])
        self.assertEqual("normal", account["relationship_mode"])
        self.assertTrue(account["legacy_snapshot"])
        self.assertEqual([], account["relationship_ledger"])
        self.assertEqual(original, snapshot)
        identity_status = self.coordinator.identity_status(person_id)
        self.assertEqual("legacy_read", identity_status["state"])
        self.assertEqual("legacy", identity_status["read_generation"])

    def test_owner_modes_preserve_normal_and_exclusive_semantics(self) -> None:
        snapshot, person_id = self._snapshot(role="owner", mode="normal", score=100)
        self.backfill.run(snapshot)
        account = self.backfill.relationships.account(self._context(person_id))
        self.assertEqual("owner", account["relationship_role"])
        self.assertEqual("normal", account["relationship_mode"])

    def test_restart_replay_is_idempotent_and_does_not_add_ledger_events(self) -> None:
        snapshot, person_id = self._snapshot()
        first = self.backfill.run(snapshot)
        reopened = MigrationBackfill(
            coordinator=MigrationCoordinator(self.data_dir),
            relationship_path=self.data_dir / "req041_relationship.db",
            migration_epoch=self.epoch,
            policy_version="req041-v1",
        )
        second = reopened.run(deepcopy(snapshot))
        account = reopened.relationships.account(self._context(person_id))
        self.assertEqual(1, first["migrated"])
        self.assertEqual(1, second["idempotent"])
        self.assertEqual(1, account["revision"])
        self.assertEqual([], account["relationship_ledger"])

    def test_user_without_explicit_person_link_stays_pending_without_raw_id_storage(self) -> None:
        raw_id = "998877665544"
        result = self.backfill.run({"users": {raw_id: {"relationship_score": 9}}})
        summary = self.coordinator.pending_summary()
        self.assertEqual(1, result["pending"])
        self.assertEqual(1, summary["total"])
        self.assertEqual("identity_link_missing", summary["reasons"][0]["reason_code"])
        connection = sqlite3.connect(self.data_dir / "req041_migration_control.db")
        try:
            row = connection.execute(
                "SELECT legacy_ref_hash,source_kind,reason_code FROM migration_pending_records"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(64, len(row[0]))
        self.assertNotIn(raw_id, row)
        status = self.coordinator.pending_status(
            legacy_pending_reference(self.epoch, "default", raw_id)
        )
        self.assertTrue(status["found"])
        self.assertEqual("identity_link_missing", status["reason_code"])
        self.assertNotIn("legacy_ref_hash", status)
        self.assertNotIn(raw_id, json.dumps(status, ensure_ascii=False))

    def test_dismissed_pending_survives_backfill_and_can_be_restored_or_resolved(self) -> None:
        raw_id = "legacy-review-10001"
        snapshot = {"users": {raw_id: {"relationship_score": 9}}}
        self.backfill.run(deepcopy(snapshot))
        reference = legacy_pending_reference(self.epoch, "default", raw_id)

        self.assertTrue(self.coordinator.dismiss_pending(reference))
        self.assertEqual("dismissed", self.coordinator.pending_status(reference)["state"])
        self.assertEqual(0, self.coordinator.pending_summary()["total"])

        self.backfill.run(deepcopy(snapshot))
        self.assertEqual("dismissed", self.coordinator.pending_status(reference)["state"])
        self.assertEqual(0, self.coordinator.pending_summary()["total"])
        self.assertFalse(self.coordinator.dismiss_pending(reference))

        self.assertTrue(self.coordinator.restore_pending(reference))
        self.assertEqual("pending", self.coordinator.pending_status(reference)["state"])
        self.assertEqual(1, self.coordinator.pending_summary()["total"])
        self.assertFalse(self.coordinator.restore_pending(reference))

        self.assertTrue(self.coordinator.dismiss_pending(reference))
        self.assertTrue(self.coordinator.resolve_pending(reference))
        self.assertEqual("resolved", self.coordinator.pending_status(reference)["state"])

    def test_tampered_fingerprint_is_not_migrated(self) -> None:
        snapshot, person_id = self._snapshot()
        root = snapshot["unified_person"]
        key = build_identity_key(_identity())
        root["identity_links"][key]["identity_key"] = "chat-origin-v1:" + "0" * 64
        result = self.backfill.run(snapshot)
        self.assertEqual(1, result["pending"])
        with self.assertRaises(RelationshipNotFound):
            self.backfill.relationships.account(self._context(person_id))

    def test_two_legacy_records_for_one_person_are_quarantined_instead_of_merged(self) -> None:
        snapshot, person_id = self._snapshot()
        snapshot["users"]["duplicate-10001"] = deepcopy(snapshot["users"]["10001"])
        snapshot["users"]["duplicate-10001"]["relationship_score"] = 99
        result = self.backfill.run(snapshot)
        self.assertEqual(2, result["pending"])
        self.assertEqual(2, result["conflicts"])
        with self.assertRaises(RelationshipNotFound):
            self.backfill.relationships.account(self._context(person_id))

    def test_invalid_legacy_score_stays_pending(self) -> None:
        snapshot, person_id = self._snapshot(score="not-a-score")
        result = self.backfill.run(snapshot)
        self.assertEqual(1, result["pending"])
        with self.assertRaises(RelationshipNotFound):
            self.backfill.relationships.account(self._context(person_id))

    def test_user_pointing_at_other_subject_person_is_not_trusted(self) -> None:
        snapshot, person_id = self._snapshot(legacy_key="different-user")
        result = self.backfill.run(snapshot)
        self.assertEqual(1, result["pending"])
        self.assertEqual(
            "legacy_identity_subject_mismatch",
            self.coordinator.pending_summary()["reasons"][0]["reason_code"],
        )
        with self.assertRaises(RelationshipNotFound):
            self.backfill.relationships.account(self._context(person_id))

    def test_pending_record_resolves_after_exact_link_becomes_available(self) -> None:
        self.backfill.run({"users": {"10001": {"relationship_score": 5}}})
        self.assertEqual(1, self.coordinator.pending_summary()["total"])
        snapshot, _person_id = self._snapshot(score=5)
        result = self.backfill.run(snapshot)
        self.assertEqual(1, result["migrated"])
        self.assertEqual(0, self.coordinator.pending_summary()["total"])

    def test_same_legacy_key_in_two_persona_scopes_has_distinct_pending_records(self) -> None:
        snapshot = {"users": {"10001": {"relationship_score": 5}}}
        self.backfill.run(snapshot, source_scope="persona:aaa")
        self.backfill.run(snapshot, source_scope="persona:bbb")
        self.assertEqual(2, self.coordinator.pending_summary()["total"])

    def test_profile_identity_key_set_corruption_fails_closed(self) -> None:
        snapshot, person_id = self._snapshot()
        snapshot["unified_person"]["profiles"][person_id]["identity_keys"].append("unexpected-key")
        result = self.backfill.run(snapshot)
        self.assertEqual(1, result["pending"])
        with self.assertRaises(RelationshipNotFound):
            self.backfill.relationships.account(self._context(person_id))


if __name__ == "__main__":
    unittest.main()
