from __future__ import annotations

import copy
import hashlib
import json
import unittest

from core.provenance import (
    ATTESTATION_SCHEMA_VERSION,
    COMPANION_ATTESTATION_ISSUER,
    P3_CONTRACT_FINGERPRINT,
    P3_CONTRACT_NAME,
    P3_CONTRACT_VERSION,
    PROVENANCE_CONTRACT_NAME,
    PROVENANCE_CONTRACT_VERSION,
    apply_planned_operation,
    contract_descriptor,
    contract_fingerprint,
    legacy_unresolved,
    observed_from_companion_snapshot,
    plan_legacy_migration,
    rollback_planned_operation,
    validate_provenance_record,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "issuer": COMPANION_ATTESTATION_ISSUER,
        "issuer_epoch": _hash("issuer-epoch"),
        "p3_contract_name": P3_CONTRACT_NAME,
        "p3_contract_version": P3_CONTRACT_VERSION,
        "p3_contract_fingerprint": P3_CONTRACT_FINGERPRINT,
        "source_kind": "forwarded_text",
        "source_trust": "T3",
        "firewall_status": "sanitized",
        "disposition": "shadow_quarantine",
        "reason_codes": ("p5b_attested",),
        "source_event_ref_hash": _hash("source-event"),
        "authority_attestation_ref_hash": _hash("authority-attestation"),
        "request_hash": _hash("request"),
        "session_hash": _hash("session"),
        "derived_from_ref_hash": "",
        "provenance_state": "observed",
        "sink": "memory_recall",
    }
    value.update(overrides)
    return value


class C5ProvenanceTests(unittest.TestCase):
    def test_descriptor_fingerprint_is_closed_and_stable(self) -> None:
        descriptor = contract_descriptor()
        canonical = json.dumps(descriptor, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.assertEqual(_hash(canonical), contract_fingerprint())
        self.assertEqual(13, len(descriptor["record_fields"]))
        json.dumps(descriptor, ensure_ascii=True)

    def test_legacy_unresolved_is_conservative_and_valid(self) -> None:
        record = legacy_unresolved("memory-42", record_revision=4)
        self.assertEqual("legacy_unresolved", record["provenance_state"])
        self.assertEqual(("unknown", "T4", "unknown"), (record["source_kind"], record["source_trust"], record["firewall_status"]))
        self.assertTrue(validate_provenance_record(record)["ok"])

    def test_valid_snapshot_becomes_observed_and_invalid_snapshot_is_safe(self) -> None:
        record = observed_from_companion_snapshot("memory-42", _snapshot(), record_revision=7)
        self.assertEqual("observed", record["provenance_state"])
        self.assertTrue(validate_provenance_record(record)["ok"])
        self.assertEqual(_hash("source-event"), record["source_event_ref_hash"])

        invalid = observed_from_companion_snapshot("memory-42", _snapshot(source_trust="T9"))
        self.assertEqual("invalid", invalid["provenance_state"])
        invalid = observed_from_companion_snapshot("memory-42", _snapshot(content="raw prose"))
        self.assertEqual("invalid", invalid["provenance_state"])

    def test_record_and_preview_reject_raw_fields_without_echoing_them(self) -> None:
        record = legacy_unresolved("memory-42")
        record["content"] = "do not retain this"
        result = validate_provenance_record(record)
        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertFalse(result["ok"])
        self.assertIn("forbidden_raw_field", result["error_codes"])
        self.assertNotIn("do not retain this", rendered)

        hostile = [{"memory_id": "memory-secret", "record_revision": 0, "prompt": "secret prompt"}]
        preview = plan_legacy_migration(hostile, operation_ref_hash=_hash("migration"))
        self.assertEqual([], preview["operations"])
        self.assertNotIn("secret prompt", json.dumps(preview, ensure_ascii=False))

    def test_preview_is_immutable_and_apply_returns_a_new_record(self) -> None:
        source = [{"memory_id": "memory-42", "record_revision": 4}]
        before = copy.deepcopy(source)
        preview = plan_legacy_migration(source, operation_ref_hash=_hash("migration-42"))
        self.assertEqual(before, source)
        self.assertEqual(("preview", True, 0), (preview["mode"], preview["readonly"], preview["write_count"]))
        operation = preview["operations"][0]
        applied = apply_planned_operation({"memory_id": "memory-42", "record_revision": 4}, operation)
        self.assertEqual((True, "applied", True), (applied["ok"], applied["status"], applied["changed"]))
        self.assertIsNot(applied["record"], operation["after_record"])

    def test_apply_is_idempotent_and_rollback_uses_cas(self) -> None:
        operation = plan_legacy_migration(
            [{"memory_id": "memory-42", "record_revision": 4}], operation_ref_hash=_hash("migration-42")
        )["operations"][0]
        applied = apply_planned_operation({"memory_id": "memory-42", "record_revision": 4}, operation)
        retry = apply_planned_operation(applied["record"], operation)
        self.assertEqual((True, "idempotent", False), (retry["ok"], retry["status"], retry["changed"]))

        concurrent = copy.deepcopy(applied["record"])
        concurrent["record_revision"] += 1
        conflict = rollback_planned_operation(concurrent, operation)
        self.assertEqual((False, "revision_conflict"), (conflict["ok"], conflict["status"]))

        rolled_back = rollback_planned_operation(applied["record"], operation)
        self.assertEqual((True, "rolled_back", True), (rolled_back["ok"], rolled_back["status"], rolled_back["removed"]))
        self.assertIsNone(rolled_back["record"])

    def test_owner_recovery_is_preview_only_and_requires_attested_snapshot(self) -> None:
        legacy = legacy_unresolved("memory-42", record_revision=2)
        from core.provenance import plan_owner_recovery

        planned = plan_owner_recovery(legacy, _snapshot(), recovery_operation_ref_hash=_hash("recovery"))
        self.assertTrue(planned["ok"])
        self.assertEqual("owner_recovered", planned["operation"]["after_record"]["provenance_state"])
        rejected = plan_owner_recovery(
            legacy, _snapshot(p3_contract_fingerprint=_hash("wrong-p3")), recovery_operation_ref_hash=_hash("recovery-2")
        )
        self.assertEqual(["companion_snapshot_invalid"], rejected["error_codes"])


if __name__ == "__main__":
    unittest.main()
