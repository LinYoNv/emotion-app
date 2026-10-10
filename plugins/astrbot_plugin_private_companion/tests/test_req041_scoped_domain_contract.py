from __future__ import annotations

import unittest

from identity_namespace import NamespaceContext
from scoped_domain_contract import (
    ScopedDomainContractError,
    build_scoped_domain_payload,
    validate_scoped_domain_payload,
)


def _context(kind: str, *, persona: str = "default", identity: str = "person-a", group: str = "") -> NamespaceContext:
    return NamespaceContext(
        kind=kind, persona_id=persona, identity_id=identity, group_id=group,
        assurance="verified", profile_status="active", policy_version="req041-v1",
        migration_epoch="epoch-1",
    )


class ScopedDomainContractTests(unittest.TestCase):
    def test_profile_memory_and_learning_match_namespace_and_kind(self) -> None:
        profile = build_scoped_domain_payload(domain="profile", source_kind="private", content={"nickname": "A"})
        validate_scoped_domain_payload(_context("private"), "profile_fact", profile)
        memory = build_scoped_domain_payload(domain="memory", source_kind="group_shared", content={"topic": "x"})
        validate_scoped_domain_payload(_context("group_shared", identity="", group="g"), "memory", memory)
        rule = build_scoped_domain_payload(
            domain="learning", source_kind="private", content={"style": "x"}, approval_state="pending"
        )
        validate_scoped_domain_payload(_context("private"), "rule", rule)

    def test_cross_domain_and_privilege_fields_are_denied(self) -> None:
        payload = build_scoped_domain_payload(domain="memory", source_kind="private", content={"value": "x"})
        with self.assertRaisesRegex(ScopedDomainContractError, "record_kind_mismatch"):
            validate_scoped_domain_payload(_context("private"), "profile_fact", payload)
        with self.assertRaisesRegex(ScopedDomainContractError, "privilege_field_denied"):
            build_scoped_domain_payload(
                domain="profile", source_kind="private", content={"relationship_role": "owner"}
            )

    def test_persona_global_rule_requires_explicit_admin_approval(self) -> None:
        pending = build_scoped_domain_payload(
            domain="learning", source_kind="persona_global", content={"style": "x"}, approval_state="pending"
        )
        with self.assertRaisesRegex(ScopedDomainContractError, "persona_global_rule_approval_required"):
            validate_scoped_domain_payload(_context("persona_global", identity=""), "rule", pending)
        approved = build_scoped_domain_payload(
            domain="learning", source_kind="persona_global", content={"style": "x"},
            approval_state="approved", approved_by="administrator",
        )
        validate_scoped_domain_payload(_context("persona_global", identity=""), "rule", approved)

    def test_rejected_and_revoked_rules_remain_auditable_but_require_admin_for_global(self) -> None:
        for state in ("rejected", "revoked"):
            private = build_scoped_domain_payload(
                domain="learning", source_kind="private", content={"rule_id": "opaque"},
                source_revision=3, approval_state=state, approved_by="administrator",
            )
            validate_scoped_domain_payload(_context("private"), "rule", private)
            global_rule = build_scoped_domain_payload(
                domain="learning", source_kind="persona_global", content={"rule_id": "opaque"},
                source_revision=3, approval_state=state, approved_by="administrator",
            )
            validate_scoped_domain_payload(_context("persona_global", identity=""), "rule", global_rule)


if __name__ == "__main__":
    unittest.main()
