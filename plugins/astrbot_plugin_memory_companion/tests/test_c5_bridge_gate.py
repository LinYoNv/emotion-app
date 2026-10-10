from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest

from core.config import ConfigView
from core.provenance import (
    ATTESTATION_SCHEMA_VERSION,
    COMPANION_ATTESTATION_ISSUER,
    P3_CONTRACT_FINGERPRINT,
    P3_CONTRACT_NAME,
    P3_CONTRACT_VERSION,
    PROVENANCE_CONTRACT_NAME,
    PROVENANCE_CONTRACT_VERSION,
    contract_fingerprint,
)
from core.provenance_store import ProvenanceLedger
from core.service import MemoryCompanionService


def run(coro):
    return asyncio.run(coro)


def make_service(*, bridge_gate: bool = True, recall_gate: bool = True):
    service = object.__new__(MemoryCompanionService)
    service.config = ConfigView(
        {
            "private_companion_bridge": {
                "enable_p5_b1_bridge_gate": bridge_gate,
                "enable_p5_b1_recall_gate": recall_gate,
            }
        }
    )
    service._last_p5_gate_status = {}
    return service


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mint(*, source_kind="current_user_intent", trust="T2", disposition="allow", sink="bridge_serialization"):
    """Provide a self-contained opaque, one-shot attestation consumer.

    The Memory side must only consume an opaque handle and inspect the returned
    redacted snapshot.  This fixture exercises that boundary without depending
    on a sibling plugin path in the repository test environment.
    """

    handle = object()
    consumed = False
    snapshot = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "contract_fingerprint": contract_fingerprint(),
        "issuer": COMPANION_ATTESTATION_ISSUER,
        "issuer_epoch": _hash("issuer-epoch"),
        "p3_contract_name": P3_CONTRACT_NAME,
        "p3_contract_version": P3_CONTRACT_VERSION,
        "p3_contract_fingerprint": P3_CONTRACT_FINGERPRINT,
        "source_kind": source_kind,
        "source_trust": trust,
        "firewall_status": "allowed",
        "disposition": disposition,
        "reason_codes": ("evidence_only_nonexecuting",) if disposition == "allow" else ("untrusted_source_shadowed", "p5_nonexecuting"),
        "source_event_ref_hash": _hash("source-event"),
        "authority_attestation_ref_hash": _hash("authority-attestation"),
        "request_hash": _hash("request"),
        "session_hash": _hash("session"),
        "derived_from_ref_hash": "",
        "provenance_state": "observed",
        "sink": sink,
    }

    def consume(candidate, requested_sink):
        nonlocal consumed
        if candidate is not handle or consumed or requested_sink != sink:
            return None
        consumed = True
        return dict(snapshot)

    return handle, consume


class C5BridgeGateTests(unittest.TestCase):
    def test_bridge_gate_accepts_current_user_attestation_and_rejects_replay(self):
        service = make_service()
        handle, consumer = mint()
        allowed = run(service._p5_gate(sink="bridge_serialization", attestation=handle, consumer=consumer))
        replay = run(service._p5_gate(sink="bridge_serialization", attestation=handle, consumer=consumer))
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["state"], "allowed")
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["error_code"], "p5_attestation_invalid")

    def test_shadow_source_is_not_allowed_to_expand_recall(self):
        service = make_service()
        handle, consumer = mint(
            source_kind="forwarded_text",
            trust="T3",
            disposition="shadow_quarantine",
        )
        result = run(service._p5_gate(sink="bridge_serialization", attestation=handle, consumer=consumer))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "shadow")

    def test_disabled_gate_preserves_legacy_behavior(self):
        service = make_service(bridge_gate=False, recall_gate=False)
        result = run(service._p5_gate(sink="memory_recall"))
        self.assertEqual(result, {"ok": True, "state": "legacy", "legacy": True, "enabled": False})

    def test_ledger_preview_apply_backup_and_cas_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ProvenanceLedger(Path(directory) / "provenance_ledger.json")
            preview = ledger.preview_legacy(
                [{"memory_id": "memory-1", "record_revision": 0}],
                operation_ref_hash="d" * 64,
            )
            self.assertTrue(preview["readonly"])
            self.assertEqual(preview["write_count"], 0)
            operation = preview["operations"][0]
            applied = ledger.apply(operation)
            self.assertTrue(applied["ok"])
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(ledger.backup()["created"])
            self.assertEqual(ledger.snapshot()["records"]["memory-1"]["provenance_state"], "legacy_unresolved")
            conflict = ledger.rollback({**operation, "after_record_digest": "e" * 64})
            self.assertFalse(conflict["ok"])
            rolled_back = ledger.rollback(operation)
            self.assertTrue(rolled_back["ok"])
            self.assertEqual(rolled_back["status"], "rolled_back")

    def test_dual_plugin_snapshot_is_recorded_without_raw_payload(self):
        service = make_service(bridge_gate=True, recall_gate=False)
        with tempfile.TemporaryDirectory() as directory:
            service.provenance_ledger = ProvenanceLedger(Path(directory) / "provenance_ledger.json")
            handle, consumer = mint()
            gate = run(service._p5_gate(sink="bridge_serialization", attestation=handle, consumer=consumer))
            result = run(service._p5_record_observed(["memory-1"], gate["snapshot"]))
            snapshot = service.provenance_snapshot()
            self.assertTrue(result["ok"])
            self.assertEqual(snapshot["records"]["memory-1"]["provenance_state"], "observed")
            self.assertNotIn("content", str(snapshot))
            self.assertNotIn("prompt", str(snapshot))


if __name__ == "__main__":
    unittest.main()
