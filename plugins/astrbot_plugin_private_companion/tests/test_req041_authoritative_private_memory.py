from __future__ import annotations

from copy import deepcopy
import json
import unittest

from authoritative_private_memory import (
    AuthoritativePrivateMemoryError,
    AuthoritativePrivateMemoryStore,
    apply_private_memory_content,
    private_memory_content,
)


class AuthoritativePrivateMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot: dict = {}
        self.store = AuthoritativePrivateMemoryStore(self.snapshot, clock=lambda: 123.0)

    def test_create_read_update_and_restart_are_revisioned(self) -> None:
        created = self.store.commit(
            "person-a",
            {"companion_memory": {"items": [{"text": "apple"}]}},
            expected_revision=0,
            operation_id="create-a",
        )
        self.assertEqual("created", created["code"])
        self.assertEqual(1, created["revision"])
        reopened = AuthoritativePrivateMemoryStore(self.snapshot)
        self.assertEqual("apple", reopened.read("person-a")["record"]["content"]["companion_memory"]["items"][0]["text"])
        updated = reopened.commit(
            "person-a",
            {"companion_memory": {"items": [{"text": "banana"}]}},
            expected_revision=1,
            operation_id="update-a",
        )
        self.assertEqual(2, updated["revision"])

    def test_cas_idempotency_and_operation_conflict_fail_closed(self) -> None:
        first = self.store.commit(
            "person-a", {"open_loops": [{"text": "one"}]},
            expected_revision=0, operation_id="same-operation",
        )
        replay = self.store.commit(
            "person-a", {"open_loops": [{"text": "one"}]},
            expected_revision=0, operation_id="same-operation",
        )
        self.assertEqual("idempotent", replay["code"])
        conflict = self.store.commit(
            "person-a", {"open_loops": [{"text": "two"}]},
            expected_revision=1, operation_id="same-operation",
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual("operation_id_conflict", conflict["code"])
        stale = self.store.commit(
            "person-a", {"open_loops": []},
            expected_revision=0, operation_id="stale-operation",
        )
        self.assertEqual("private_memory_revision_conflict", stale["code"])
        self.assertEqual(first["record"], self.store.read("person-a")["record"])

    def test_unchanged_content_does_not_advance_revision(self) -> None:
        self.store.commit(
            "person-a", {"behavior_habits": {"patterns": []}},
            expected_revision=0, operation_id="create",
        )
        unchanged = self.store.commit(
            "person-a", {"behavior_habits": {"patterns": []}},
            expected_revision=1, operation_id="another-event",
        )
        self.assertEqual("unchanged", unchanged["code"])
        self.assertEqual(1, unchanged["revision"])

    def test_only_memory_fields_are_accepted_and_operation_id_is_not_persisted(self) -> None:
        with self.assertRaisesRegex(AuthoritativePrivateMemoryError, "private_memory_fields_invalid"):
            self.store.commit(
                "person-a", {"relationship_score": 999},
                expected_revision=0, operation_id="secret-operation-value",
            )
        self.store.commit(
            "person-a", {"intent_profile": {"mood": "calm"}},
            expected_revision=0, operation_id="secret-operation-value",
        )
        serialized = json.dumps(self.snapshot, ensure_ascii=False)
        self.assertNotIn("secret-operation-value", serialized)
        self.assertNotIn("relationship_score", serialized)

    def test_content_helpers_replace_stale_identity_local_fields(self) -> None:
        first = {
            "companion_memory": {"items": [{"text": "canonical"}]},
            "open_loops": [{"text": "stale"}],
            "relationship_score": 9,
        }
        content = private_memory_content(first)
        second = {
            "companion_memory": {"items": [{"text": "other"}]},
            "open_loops": [{"text": "other"}],
            "relationship_score": 88,
        }
        content.pop("open_loops")
        apply_private_memory_content(second, deepcopy(content))
        self.assertEqual("canonical", second["companion_memory"]["items"][0]["text"])
        self.assertNotIn("open_loops", second)
        self.assertEqual(88, second["relationship_score"])


if __name__ == "__main__":
    unittest.main()
