from __future__ import annotations

import unittest

from identity_namespace import (
    AssurancePolicy,
    CONTRACT_FINGERPRINT,
    NamespaceContext,
    build_namespace_context,
    contract_self_check,
    validate_namespace_context,
)


def _context(kind: str = "private", **changes: str) -> NamespaceContext:
    values = {
        "kind": kind,
        "identity_id": "person_aaaaaaaaaaaaaaaaaaaaaaaa",
        "group_id": "",
        "assurance": "verified",
        "profile_status": "active",
        "policy_version": "req041-v1",
        "migration_epoch": "shadow-20260810",
    }
    if kind == "group_member":
        values["group_id"] = "group-100"
    elif kind == "group_shared":
        values["identity_id"] = ""
        values["group_id"] = "group-100"
    elif kind == "persona_global":
        values["identity_id"] = ""
    values.update(changes)
    return NamespaceContext(**values)


class NamespaceContractTests(unittest.TestCase):
    def test_valid_private_context_round_trips_and_has_redacted_cache_scope(self) -> None:
        context = _context()
        payload = context.to_dict()
        self.assertEqual([], validate_namespace_context(payload))
        self.assertEqual(context, build_namespace_context(payload))
        self.assertNotIn(context.identity_id, context.cache_scope())
        self.assertEqual([], contract_self_check())
        self.assertEqual(16, len(CONTRACT_FINGERPRINT))

    def test_missing_or_extra_fields_fail_closed(self) -> None:
        self.assertEqual(["namespace_context_missing"], validate_namespace_context(None))
        payload = _context().to_dict()
        payload["raw_text"] = "must never cross"
        self.assertIn("namespace_context_fields_invalid", validate_namespace_context(payload))

    def test_kind_field_combinations_are_strict(self) -> None:
        self.assertIn("namespace_identity_required", _context(identity_id="").errors())
        self.assertIn("namespace_group_required", _context("group_member", group_id="").errors())
        self.assertIn("namespace_identity_forbidden", _context("group_shared", identity_id="person_x").errors())
        self.assertIn("namespace_group_forbidden", _context(group_id="group-100").errors())

    def test_assurance_policy_rejects_pending_unverified_and_inactive(self) -> None:
        self.assertFalse(AssurancePolicy.authorize(None, "memory_read").allowed)
        self.assertEqual(
            "identity_assurance_insufficient",
            AssurancePolicy.authorize(_context(assurance="observed"), "memory_read").code,
        )
        self.assertEqual(
            "namespace_pending_denied",
            AssurancePolicy.authorize(_context("pending", assurance="unverified"), "memory_read").code,
        )
        self.assertEqual(
            "profile_quarantined",
            AssurancePolicy.authorize(_context(profile_status="quarantined"), "memory_read").code,
        )
        self.assertEqual(
            "identity_assurance_insufficient",
            AssurancePolicy.authorize(
                _context("persona_global", assurance="unverified"), "rule_read"
            ).code,
        )

    def test_scope_specific_purposes_are_enforced(self) -> None:
        self.assertTrue(AssurancePolicy.authorize(_context(), "relationship_read").allowed)
        self.assertEqual(
            "group_shared_subject_access_denied",
            AssurancePolicy.authorize(_context("group_shared"), "profile_read").code,
        )
        self.assertTrue(AssurancePolicy.authorize(_context("group_shared"), "memory_read").allowed)
        self.assertEqual(
            "persona_global_purpose_denied",
            AssurancePolicy.authorize(_context("persona_global"), "memory_read").code,
        )
        self.assertTrue(AssurancePolicy.authorize(_context("persona_global"), "rule_read").allowed)


if __name__ == "__main__":
    unittest.main()
