from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import pickle
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p5_attestation import (  # noqa: E402
    ATTESTATION_SCHEMA_VERSION,
    P5AttestationError,
    P5AttestationHandle,
    P5AttestationRegistry,
    PROVENANCE_CONTRACT_FINGERPRINT,
)


def _hash(char: str) -> str:
    return char * 64


class _Carrier:
    pass


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class AttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = P5AttestationRegistry()
        self.request = _Carrier()
        self.event = _Carrier()
        self.p3 = _Carrier()

    def _mint(self, **changes: object) -> P5AttestationHandle | None:
        values: dict[str, object] = {
            "request_hash": _hash("a"),
            "session_hash": _hash("b"),
            "source_kind": "forwarded_text",
            "source_trust": "T3",
            "firewall_status": "allowed",
            "disposition": "shadow_quarantine",
            "reason_codes": ("untrusted_source_shadowed",),
            "source_event_ref_hash": _hash("c"),
            "sinks": ("memory_recall",),
        }
        values.update(changes)
        return self.registry.mint(self.request, self.event, self.p3, **values)  # type: ignore[arg-type]

    def test_mint_consume_returns_immutable_metadata_snapshot(self) -> None:
        handle = self._mint()
        self.assertIsInstance(handle, P5AttestationHandle)
        assert handle is not None
        snapshot = self.registry.consume(handle, self.request, self.event, self.p3, "memory_recall")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.schema_version, ATTESTATION_SCHEMA_VERSION)
        self.assertEqual(snapshot.contract_fingerprint, PROVENANCE_CONTRACT_FINGERPRINT)
        self.assertEqual(snapshot.source_trust, "T3")
        self.assertEqual(snapshot.reason_codes, ("untrusted_source_shadowed",))
        with self.assertRaises(FrozenInstanceError):
            snapshot.sink = "tool_retrieval"  # type: ignore[misc]
        self.assertEqual(self.registry.pending_count(), 0)
        json.dumps(asdict(snapshot), sort_keys=True)

    def test_replay_sink_mismatch_and_carrier_replacement_are_one_shot_failures(self) -> None:
        handle = self._mint(sinks=("memory_recall", "bridge_serialization"))
        assert handle is not None
        self.assertIsNone(self.registry.consume(handle, self.request, self.event, self.p3, "tool_retrieval"))
        self.assertIsNone(self.registry.consume(handle, self.request, self.event, self.p3, "memory_recall"))

        handle = self._mint()
        assert handle is not None
        self.assertIsNone(self.registry.consume(handle, _Carrier(), self.event, self.p3, "memory_recall"))
        self.assertIsNone(self.registry.consume(handle, self.request, self.event, self.p3, "memory_recall"))

    def test_expiry_and_epoch_reset_invalidate_handles(self) -> None:
        clock = _Clock()
        registry = P5AttestationRegistry(clock=clock)
        handle = self._mint_registry(registry, ttl_seconds=1.0)
        assert handle is not None
        clock.value += 1.0
        self.assertEqual(registry.cleanup(), 1)
        self.assertIsNone(registry.consume(handle, self.request, self.event, self.p3, "memory_recall"))
        handle = self._mint_registry(registry)
        assert handle is not None
        registry.reset_epoch()
        self.assertIsNone(registry.consume(handle, self.request, self.event, self.p3, "memory_recall"))

    def _mint_registry(self, registry: P5AttestationRegistry, **changes: object) -> P5AttestationHandle | None:
        values: dict[str, object] = {
            "request_hash": _hash("a"),
            "session_hash": _hash("b"),
            "source_kind": "forwarded_text",
            "source_trust": "T3",
            "firewall_status": "allowed",
            "disposition": "shadow_quarantine",
            "reason_codes": ("untrusted_source_shadowed",),
            "source_event_ref_hash": _hash("c"),
            "sinks": ("memory_recall",),
        }
        values.update(changes)
        return registry.mint(self.request, self.event, self.p3, **values)  # type: ignore[arg-type]

    def test_pickle_json_and_forged_structural_handles_are_rejected(self) -> None:
        handle = self._mint()
        assert handle is not None
        with self.assertRaises(TypeError):
            pickle.dumps(handle)
        with self.assertRaises(TypeError):
            json.dumps(handle)
        with self.assertRaises(TypeError):
            P5AttestationHandle()
        forged = object.__new__(P5AttestationHandle)
        self.assertIsNone(self.registry.consume(forged, self.request, self.event, self.p3, "memory_recall"))
        self.assertIsNone(self.registry.consume({"sink": "memory_recall"}, self.request, self.event, self.p3, "memory_recall"))

    def test_raw_or_open_enum_metadata_is_rejected(self) -> None:
        with self.assertRaises(P5AttestationError):
            self._mint(request_hash="raw request id")
        with self.assertRaises(P5AttestationError):
            self._mint(reason_codes=("arbitrary prose",))
        with self.assertRaises(P5AttestationError):
            self._mint(source_kind="forwarded_text", source_trust="T2")
        with self.assertRaises(P5AttestationError):
            self._mint(disposition="allow")


if __name__ == "__main__":
    unittest.main()
