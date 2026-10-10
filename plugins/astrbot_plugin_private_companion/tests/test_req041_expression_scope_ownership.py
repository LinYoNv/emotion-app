from __future__ import annotations

from dataclasses import dataclass
import unittest

from expression_scope_ownership import (
    ExpressionScopeError,
    bind_expression_item,
    bind_expression_profile,
    runtime_binding_is_approved,
    validate_expression_scope_binding,
)


@dataclass(frozen=True)
class Context:
    kind: str
    persona_id: str = "persona-a"
    identity_id: str = ""
    group_id: str = ""
    assurance: str = "verified"
    profile_status: str = "active"
    policy_version: str = "req041-v1"
    migration_epoch: str = "epoch-a"

    def errors(self):
        return []


class ExpressionScopeOwnershipTests(unittest.TestCase):
    def private(self, identity: str = "person-a", **changes):
        values = dict(kind="private", identity_id=identity)
        values.update(changes)
        return Context(**values)

    def group(self, group: str = "group-a", **changes):
        values = dict(kind="group_shared", group_id=group)
        values.update(changes)
        return Context(**values)

    def test_binding_uses_opaque_owner_and_namespace(self):
        item = bind_expression_item(
            {"id": "rule-a", "platform_subject_id": "raw-user-must-remain-content-only"},
            self.private(), approval_state="approved", approved_by="administrator",
        )
        binding = item["scope_binding"]
        self.assertTrue(runtime_binding_is_approved(binding))
        self.assertNotIn("person-a", str(binding))
        self.assertNotIn("persona-a", str(binding))
        self.assertNotIn("epoch-a", str(binding))

    def test_private_group_persona_and_epoch_are_distinct(self):
        contexts = [
            self.private("person-a"), self.private("person-b"),
            self.group("group-a"), self.group("group-b"),
            self.private("person-a", persona_id="persona-b"),
            self.private("person-a", migration_epoch="epoch-b"),
        ]
        namespaces = {
            bind_expression_item({}, context, approval_state="pending")["scope_binding"]["source_namespace"]
            for context in contexts
        }
        self.assertEqual(len(contexts), len(namespaces))

    def test_existing_binding_cannot_be_reassigned(self):
        original = bind_expression_item({}, self.private("person-a"), approval_state="pending")
        with self.assertRaisesRegex(ExpressionScopeError, "expression_scope_binding_mismatch"):
            bind_expression_item(original, self.private("person-b"), approval_state="pending")

    def test_approval_and_content_change_advance_item_revision(self):
        pending = bind_expression_item({"id": "a"}, self.private(), approval_state="pending")
        approved = bind_expression_item(
            pending, self.private(), approval_state="approved", approved_by="administrator", bump_revision=True,
        )
        self.assertEqual(1, pending["scope_binding"]["revision"])
        self.assertEqual(2, approved["scope_binding"]["revision"])
        self.assertTrue(runtime_binding_is_approved(approved["scope_binding"]))

    def test_projection_cannot_change_approval_without_revision_bump(self):
        pending = bind_expression_item({"id": "a"}, self.private(), approval_state="pending")
        with self.assertRaisesRegex(ExpressionScopeError, "expression_scope_approval_mismatch"):
            bind_expression_item(
                pending, self.private(), approval_state="approved", approved_by="administrator",
            )

    def test_profile_revision_is_monotonic_and_scope_bound(self):
        profile = bind_expression_profile({"learned_rules": []}, self.group())
        changed = bind_expression_profile(profile, self.group(), bump_revision=True)
        self.assertEqual(1, profile["scope_revision"])
        self.assertEqual(2, changed["scope_revision"])
        with self.assertRaisesRegex(ExpressionScopeError, "expression_profile_scope_mismatch"):
            bind_expression_profile(changed, self.group("group-b"), bump_revision=True)

    def test_profile_revision_cannot_be_repaired_from_conflicting_fields(self):
        profile = bind_expression_profile({}, self.private())
        profile["scope_revision"] = 99
        with self.assertRaisesRegex(ExpressionScopeError, "expression_profile_revision_mismatch"):
            bind_expression_profile(profile, self.private(), bump_revision=True)

    def test_pending_cannot_claim_approver_and_approved_requires_one(self):
        with self.assertRaisesRegex(ExpressionScopeError, "expression_pending_approved_by_invalid"):
            bind_expression_item({}, self.private(), approval_state="pending", approved_by="client")
        with self.assertRaisesRegex(ExpressionScopeError, "expression_approved_by_required"):
            bind_expression_item({}, self.private(), approval_state="approved")

    def test_unverified_and_member_scopes_fail_closed(self):
        for context in (
            self.private(assurance="unverified"),
            Context(kind="group_member", identity_id="person-a", group_id="group-a"),
        ):
            with self.assertRaises(ExpressionScopeError):
                bind_expression_item({}, context, approval_state="pending")

    def test_forged_extra_field_is_rejected(self):
        item = bind_expression_item({}, self.private(), approval_state="pending")
        item["scope_binding"]["owner_raw"] = "person-a"
        with self.assertRaisesRegex(ExpressionScopeError, "expression_scope_binding_fields_invalid"):
            validate_expression_scope_binding(item["scope_binding"], self.private())


if __name__ == "__main__":
    unittest.main()
