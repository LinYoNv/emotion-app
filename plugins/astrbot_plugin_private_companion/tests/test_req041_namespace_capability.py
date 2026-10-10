from __future__ import annotations

import unittest

from namespace_capability import (
    API_METHODS,
    namespace_capability_descriptor,
    negotiate_namespace_capability,
    validate_namespace_capability,
)
from tests.test_c1_livingmemory_decoupling import MemoryCompanionAdapterMixin
from identity_namespace import NamespaceContext


class _AdapterHost(MemoryCompanionAdapterMixin):
    pass


def _context(**changes: str) -> NamespaceContext:
    values = {
        "kind": "private",
        "identity_id": "person-a",
        "group_id": "",
        "assurance": "verified",
        "profile_status": "active",
        "policy_version": "req041-v1",
        "migration_epoch": "req041-20260810-001",
    }
    values.update(changes)
    return NamespaceContext(**values)


class NamespaceCapabilityTests(unittest.TestCase):
    def test_complete_descriptor_negotiates_ready(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        self.assertEqual([], validate_namespace_capability(descriptor))
        result = negotiate_namespace_capability(descriptor)
        self.assertTrue(result["available"])
        self.assertEqual("namespace_capability_ready", result["code"])

    def test_unbound_remote_is_well_formed_but_fails_closed(self) -> None:
        descriptor = namespace_capability_descriptor()
        self.assertEqual([], validate_namespace_capability(descriptor, require_available=False))
        result = negotiate_namespace_capability(descriptor)
        self.assertFalse(result["available"])
        self.assertEqual("namespace_capability_unavailable", result["code"])

    def test_missing_extra_and_mismatched_contract_are_rejected(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        descriptor.pop("context_fields")
        descriptor["unexpected"] = True
        descriptor["namespace_contract_fingerprint"] = "wrong"
        errors = validate_namespace_capability(descriptor)
        self.assertIn("namespace_capability_fields_invalid", errors)
        self.assertIn("namespace_contract_fingerprint_mismatch", errors)

    def test_available_requires_every_scoped_method(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS[:-1])
        self.assertFalse(descriptor["available"])
        self.assertIn("namespace_capability_unavailable", validate_namespace_capability(descriptor))

    def test_adapter_probe_handles_missing_exception_unavailable_and_ready(self) -> None:
        host = _AdapterHost()
        missing = host._memory_companion_probe_namespace_capabilities(object())
        self.assertEqual("namespace_capability_probe_missing", missing["code"])

        class Broken:
            @staticmethod
            def probe_namespace_context_capabilities():
                raise RuntimeError("remote failure must not escape")

        broken = host._memory_companion_probe_namespace_capabilities(Broken())
        self.assertEqual("namespace_capability_probe_exception", broken["code"])

        class Unbound:
            @staticmethod
            def probe_namespace_context_capabilities():
                return namespace_capability_descriptor()

        unavailable = host._memory_companion_probe_namespace_capabilities(Unbound())
        self.assertEqual("namespace_capability_unavailable", unavailable["code"])

        class Ready:
            @staticmethod
            def probe_namespace_context_capabilities():
                return namespace_capability_descriptor(available=True, methods=API_METHODS)

        ready = host._memory_companion_probe_namespace_capabilities(Ready())
        self.assertTrue(ready["available"])
        self.assertEqual("namespace_capability_ready", ready["code"])

    def test_adapter_bind_and_scoped_calls_preserve_fail_closed_order(self) -> None:
        class Bridge:
            def __init__(self) -> None:
                self.bound = False
                self.calls = []
                self.capability = object()

            def register_emotion_producer(self, producer):
                self.calls.append(("register", producer))
                return self.capability

            def probe_namespace_context_capabilities(self):
                return namespace_capability_descriptor(
                    available=self.bound,
                    methods=API_METHODS if self.bound else (),
                    error_code="" if self.bound else "namespace_scoped_api_not_bound",
                )

            def bind_namespace_migration_epoch(self, capability, **kwargs):
                self.calls.append(("bind", capability, kwargs))
                self.bound = True
                return {"ok": True, "state": "ready", "code": "bound"}

            def upsert_scoped_record(self, capability, namespace, **kwargs):
                self.calls.append(("upsert", capability, namespace, kwargs))
                return {"ok": True, "state": "ready", "code": "created"}

        host = _AdapterHost()
        bridge = Bridge()
        unavailable = host._memory_companion_upsert_scoped_record(
            bridge, _context(), record_kind="memory", record_id="m1", revision=1,
            payload={"value": 1}, event_id="event-1",
        )
        self.assertEqual("namespace_capability_unavailable", unavailable["code"])
        self.assertFalse(any(call[0] == "upsert" for call in bridge.calls))

        rejected = host._memory_companion_bind_namespace_epoch(
            bridge, operation_id="bad operation", migration_epoch="", policy_version="req041-v1"
        )
        self.assertEqual("namespace_epoch_binding_invalid", rejected["code"])
        self.assertFalse(any(call[0] == "bind" for call in bridge.calls))

        bound = host._memory_companion_bind_namespace_epoch(
            bridge, operation_id="bind-1", migration_epoch="req041-20260810-001",
            policy_version="req041-v1",
        )
        self.assertEqual("bound", bound["code"])
        bind_call = next(call for call in bridge.calls if call[0] == "bind")
        self.assertIs(bridge.capability, bind_call[1])
        self.assertEqual("req041-20260810-001", bind_call[2]["migration_epoch"])
        self.assertEqual("req041-v1", bind_call[2]["policy_version"])

        created = host._memory_companion_upsert_scoped_record(
            bridge, _context(), record_kind="memory", record_id="m1", revision=1,
            payload={"value": 1}, event_id="event-1",
        )
        self.assertEqual("created", created["code"])
        upsert_call = next(call for call in bridge.calls if call[0] == "upsert")
        self.assertEqual(_context().to_dict(), upsert_call[2])

    def test_adapter_scoped_remote_exception_never_falls_back(self) -> None:
        class Bridge:
            @staticmethod
            def register_emotion_producer(_producer):
                return object()

            @staticmethod
            def probe_namespace_context_capabilities():
                return namespace_capability_descriptor(available=True, methods=API_METHODS)

            @staticmethod
            def read_scoped_record(*_args, **_kwargs):
                raise RuntimeError("remote unavailable")

        result = _AdapterHost()._memory_companion_read_scoped_record(
            Bridge(), _context(), record_kind="memory", record_id="m1"
        )
        self.assertEqual("namespace_scoped_call_exception", result["code"])
        self.assertNotIn("record", result)


if __name__ == "__main__":
    unittest.main()
