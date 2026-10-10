from __future__ import annotations

import unittest

from identity_namespace import validate_namespace_context
from unified_person_registry import UnifiedPersonRegistry


def _identity(subject_id: str) -> dict[str, str]:
    return {
        "companion_instance_id": "astrbot_plugin_private_companion",
        "bot_account_id": "onebot:bot-1",
        "adapter_instance_id": "onebot:default",
        "subject_namespace": "onebot:user",
        "platform_subject_id": subject_id,
    }


class IdentityAssuranceNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict = {}
        self.registry = UnifiedPersonRegistry(self.store)

    def _namespace(self, identity: dict[str, str], *, kind: str = "private", group_id: str = ""):
        return self.registry.namespace_context(
            identity,
            kind=kind,
            group_id=group_id,
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
            purpose="memory_read",
        )

    def test_unlinked_complete_identity_is_pending_and_denied(self) -> None:
        result = self._namespace(_identity("10001"))
        self.assertFalse(result["ok"])
        self.assertEqual("namespace_pending_denied", result["code"])
        self.assertEqual("pending", result["context"]["kind"])
        self.assertEqual("unverified", result["context"]["assurance"])
        self.assertEqual([], validate_namespace_context(result["context"]))

    def test_exact_active_link_maps_to_verified_without_mutating_legacy_assurance(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.assertTrue(created["ok"])
        self.assertEqual("observed", created["projection"]["identity_assurance"])
        result = self._namespace(_identity("10001"))
        self.assertTrue(result["ok"])
        self.assertEqual("verified", result["context"]["assurance"])
        self.assertEqual("observed", self.registry.read_projection(created["person_id"])["identity_assurance"])

    def test_explicit_secondary_link_preserves_explicit_assurance(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        linked = self.registry.link_identity(created["person_id"], _identity("10002"), operation_id="link-1")
        self.assertTrue(linked["ok"])
        result = self._namespace(_identity("10002"))
        self.assertTrue(result["ok"])
        self.assertEqual("explicit_linked", result["context"]["assurance"])

    def test_group_member_context_requires_exact_group_and_stays_isolated(self) -> None:
        self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        group_a = self._namespace(_identity("10001"), kind="group_member", group_id="group-a")
        group_b = self._namespace(_identity("10001"), kind="group_member", group_id="group-b")
        self.assertTrue(group_a["ok"])
        self.assertTrue(group_b["ok"])
        self.assertNotEqual(group_a["context"]["group_id"], group_b["context"]["group_id"])
        self.assertEqual(group_a["person_id"], group_b["person_id"])

    def test_same_subject_on_different_bot_does_not_merge(self) -> None:
        first = _identity("10001")
        second = {**first, "bot_account_id": "onebot:bot-2"}
        created = self.registry.create_or_link(first, operation_id="create-1")
        unresolved = self._namespace(second)
        self.assertEqual("pending", unresolved["context"]["kind"])
        self.assertNotEqual(created["person_id"], unresolved["person_id"])

    def test_person_wide_profile_facts_are_revisioned_idempotent_and_cas_guarded(self) -> None:
        created = self.registry.create_or_link(
            _identity("10001"),
            profile={"display_name": "Initial", "preferred_address": "I"},
            operation_id="create-profile",
        )
        person_id = created["person_id"]
        initial = self.registry.identity_profile_facts(person_id)
        self.assertEqual(1, initial["profile_fact_revision"])
        updated = self.registry.update_identity_profile_facts(
            person_id,
            {"display_name": "Alice", "preferred_address": "A", "style": "direct"},
            operation_id="profile-update-1",
            actor_id="page_administrator",
            expected_revision=1,
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(2, updated["profile_fact_revision"])
        self.assertEqual(updated, self.registry.update_identity_profile_facts(
            person_id,
            {"display_name": "Alice", "preferred_address": "A", "style": "direct"},
            operation_id="profile-update-1",
            actor_id="page_administrator",
            expected_revision=1,
        ))
        self.assertEqual("operation_id_conflict", self.registry.update_identity_profile_facts(
            person_id,
            {"preferred_address": "Other"},
            operation_id="profile-update-1",
            actor_id="page_administrator",
        )["code"])
        self.assertEqual("profile_fact_revision_conflict", self.registry.update_identity_profile_facts(
            person_id,
            {"preferred_address": "Stale"},
            operation_id="profile-update-stale",
            expected_revision=1,
        )["code"])
        self.assertEqual("profile_fact_fields_invalid", self.registry.update_identity_profile_facts(
            person_id,
            {"relationship_score": 999},
            operation_id="profile-update-privilege",
        )["code"])
        serialized = repr(self.registry.identity_profile_facts(person_id))
        self.assertNotIn("relationship_score", serialized)
        self.assertNotIn("capabilities", serialized)

    def test_secondary_identity_reads_same_person_profile_facts(self) -> None:
        created = self.registry.create_or_link(
            _identity("10001"), profile={"display_name": "Alice"}, operation_id="create-1",
        )
        self.registry.link_identity(created["person_id"], _identity("10002"), operation_id="link-2")
        self.registry.update_identity_profile_facts(
            created["person_id"], {"preferred_address": "SharedName"},
            operation_id="profile-shared",
        )
        self.assertEqual(
            "SharedName",
            self.registry.identity_profile_facts(created["person_id"])["facts"]["preferred_address"],
        )
        self.assertEqual(created["person_id"], self._namespace(_identity("10002"))["person_id"])

    def test_formal_namespace_for_person_revalidates_primary_exact_link(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        resolved = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertTrue(resolved["ok"])
        self.assertEqual("verified", resolved["context"]["assurance"])
        root = self.store["unified_person"]
        root["identity_links"][created["identity_key"]]["identity"]["platform_subject_id"] = "tampered"
        rejected = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual("identity_exact_link_invalid", rejected["code"])

    def test_formal_person_rejects_unlisted_or_corrupt_secondary_link(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.store["unified_person"]["profiles"][created["person_id"]]["identity_keys"].append("orphan-key")
        result = self.registry.formal_namespace_for_person(
            created["person_id"],
            policy_version="req041-v1",
            migration_epoch="shadow-20260810",
        )
        self.assertFalse(result["ok"])
        self.assertEqual("identity_exact_link_invalid", result["code"])

    def test_person_subject_match_accepts_exact_and_scoped_key_only(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.assertTrue(self.registry.matches_person_subject(created["person_id"], "10001"))
        self.assertTrue(self.registry.matches_person_subject(created["person_id"], "onebot:10001:0123456789abcdef"))
        self.assertFalse(self.registry.matches_person_subject(created["person_id"], "other-user"))

    def test_explicit_relink_retires_detached_state_and_operation_ids_are_request_bound(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        secondary = _identity("10002")
        linked = self.registry.link_identity(created["person_id"], secondary, operation_id="link-1")
        detached = self.registry.unlink_identity(
            created["person_id"], secondary, operation_id="unlink-1", dry_run=False,
        )
        self.assertTrue(linked["ok"])
        self.assertTrue(detached["ok"])
        automatic = self.registry.create_or_link(secondary, operation_id="automatic-recreate")
        self.assertEqual("identity_relink_required", automatic["code"])
        self.assertEqual(created["person_id"], automatic["person_id"])
        relinked = self.registry.link_identity(created["person_id"], secondary, operation_id="relink-1")
        self.assertEqual("identity_relinked", relinked["code"])
        self.assertNotIn(linked["identity_key"], self.store["unified_person"]["detached_identity_links"])
        self.assertEqual(relinked, self.registry.link_identity(
            created["person_id"], secondary, operation_id="relink-1"
        ))
        conflict = self.registry.link_identity(
            created["person_id"], _identity("10003"), operation_id="relink-1"
        )
        self.assertEqual("operation_id_conflict", conflict["code"])

    def test_person_archive_requires_remote_receipt_and_is_idempotent(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        linked = self.registry.link_identity(
            created["person_id"], _identity("10002"), operation_id="link-1",
        )
        prepared = self.registry.prepare_person_archive(
            created["person_id"], operation_id="archive-1", actor_id="page_administrator",
        )
        self.assertTrue(prepared["ok"])
        self.assertEqual(2, prepared["active_identity_count"])
        self.assertEqual(prepared, self.registry.prepare_person_archive(
            created["person_id"], operation_id="archive-1", actor_id="page_administrator",
        ))
        conflict = self.registry.prepare_person_archive(
            created["person_id"], operation_id="archive-1", actor_id="different_actor",
        )
        self.assertEqual("operation_id_conflict", conflict["code"])
        failed = self.registry.finalize_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
            {"ok": False, "code": "memory_unavailable"},
            {"code": "relationship_account_tombstoned", "last_revision": 1},
            {"code": "outbox_streams_retired", "stream_count": 2, "revisions": {}},
            actor_id="page_administrator",
        )
        self.assertEqual("archive_remote_not_confirmed", failed["code"])
        self.assertEqual("active", self.registry.read_projection(created["person_id"])["profile_status"])
        self.assertEqual([], self.registry.confirmed_person_archives())
        wrong_token = self.registry.finalize_person_archive(
            created["person_id"], "archive-1", "0" * 64,
            {"ok": True, "code": "identity_scopes_tombstoned", "count": 4, "namespace_count": 2},
            {"code": "relationship_account_tombstoned", "last_revision": 1},
            {"code": "outbox_streams_retired", "stream_count": 2, "revisions": {}},
            actor_id="page_administrator",
        )
        self.assertEqual("archive_confirmation_mismatch", wrong_token["code"])
        confirmed = self.registry.confirm_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
            actor_id="page_administrator",
        )
        self.assertEqual("person_archive_confirmed", confirmed["code"])
        self.assertEqual(1, len(self.registry.confirmed_person_archives()))
        archived = self.registry.finalize_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
            {"ok": True, "code": "identity_scopes_tombstoned", "count": 4, "namespace_count": 2},
            {"code": "relationship_account_tombstoned", "last_revision": 1},
            {"code": "outbox_streams_retired", "stream_count": 2, "revisions": {}},
            actor_id="page_administrator",
        )
        self.assertEqual("person_archived", archived["code"])
        self.assertEqual(2, archived["detached_identity_count"])
        self.assertEqual("deleted", self.registry.read_projection(created["person_id"])["profile_status"])
        self.assertFalse(self.registry.matches_person_subject(created["person_id"], "10001"))
        root = self.store["unified_person"]
        self.assertEqual({}, root["identity_links"])
        self.assertEqual({created["identity_key"], linked["identity_key"]}, set(root["detached_identity_links"]))
        self.assertEqual(
            {created["identity_key"], linked["identity_key"]}, set(root["identity_tombstones"]),
        )
        self.assertTrue(all("identity" not in tombstone for tombstone in root["identity_tombstones"].values()))
        self.assertEqual("identity_archived", self.registry.create_or_link(
            _identity("10001"), operation_id="recreate-primary",
        )["code"])
        self.assertEqual("identity_archived", self.registry.create_or_link(
            _identity("10002"), operation_id="recreate-secondary",
        )["code"])
        self.assertEqual([], self.registry.confirmed_person_archives())
        self.assertEqual(archived, self.registry.finalize_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
            {"ok": True, "code": "identity_scopes_already_empty", "count": 0, "namespace_count": 0},
            {"code": "relationship_account_already_empty", "last_revision": 0},
            {"code": "outbox_streams_retired", "stream_count": 2, "revisions": {}},
            actor_id="page_administrator",
        ))

    def test_archive_prepare_rejects_corrupt_or_inactive_people(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        self.store["unified_person"]["profiles"][created["person_id"]]["identity_keys"].append("orphan")
        corrupt = self.registry.prepare_person_archive(created["person_id"], operation_id="archive-corrupt")
        self.assertEqual("identity_exact_link_invalid", corrupt["code"])

    def test_archived_person_purge_respects_retention_and_preserves_hash_tombstones(self) -> None:
        created = self.registry.create_or_link(_identity("10001"), operation_id="create-1")
        prepared = self.registry.prepare_person_archive(created["person_id"], operation_id="archive-1")
        self.registry.confirm_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
        )
        archived = self.registry.finalize_person_archive(
            created["person_id"], "archive-1", prepared["confirmation_token"],
            {"ok": True, "code": "identity_scopes_already_empty", "count": 0, "namespace_count": 0},
            {"code": "relationship_account_already_empty", "last_revision": 0},
            {"code": "outbox_streams_retired", "stream_count": 2, "revisions": {}},
        )
        self.assertTrue(archived["ok"])
        waiting = self.registry.prepare_person_purge(created["person_id"], operation_id="purge-wait")
        self.assertEqual("archive_retention_active", waiting["code"])
        root = self.store["unified_person"]
        root["person_tombstones"][created["person_id"]]["created_at"] = "2020-01-01T00:00:00+00:00"
        purge = self.registry.prepare_person_purge(created["person_id"], operation_id="purge-1")
        self.assertEqual("person_purge_prepared", purge["code"])
        self.assertEqual(["10001"], self.registry.archived_identity_subjects(created["person_id"]))
        self.registry.confirm_person_purge(
            created["person_id"], "purge-1", purge["confirmation_token"],
        )
        completed = self.registry.finalize_person_purge(
            created["person_id"], "purge-1", purge["confirmation_token"],
            {"code": "outbox_retired_streams_purged", "stream_count": 2},
        )
        self.assertEqual("person_purged", completed["code"])
        self.assertIsNone(self.registry.read_projection(created["person_id"]))
        self.assertNotIn(created["identity_key"], root["detached_identity_links"])
        self.assertIn(created["identity_key"], root["identity_tombstones"])
        self.assertIn(created["person_id"], root["person_tombstones"])
        self.assertEqual("identity_archived", self.registry.create_or_link(
            _identity("10001"), operation_id="recreate-after-purge",
        )["code"])


if __name__ == "__main__":
    unittest.main()
