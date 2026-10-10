from __future__ import annotations

from types import SimpleNamespace
import unittest

from core.bridge import MemoryCompanionBridge
from core.namespace_capability import (
    API_METHODS,
    namespace_capability_descriptor,
    negotiate_namespace_capability,
    validate_namespace_capability,
)


class NamespaceCapabilityTests(unittest.TestCase):
    def test_complete_descriptor_negotiates_ready(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        self.assertEqual([], validate_namespace_capability(descriptor))
        self.assertTrue(negotiate_namespace_capability(descriptor)["available"])

    def test_bridge_is_honest_until_scoped_api_is_bound(self) -> None:
        bridge = MemoryCompanionBridge(SimpleNamespace())
        descriptor = bridge.probe_namespace_context_capabilities()
        self.assertFalse(descriptor["available"])
        self.assertEqual("namespace_scoped_api_not_bound", descriptor["error_code"])
        self.assertEqual([], validate_namespace_capability(descriptor, require_available=False))
        self.assertEqual("namespace_capability_unavailable", negotiate_namespace_capability(descriptor)["code"])

    def test_contract_mismatch_and_extra_field_fail_closed(self) -> None:
        descriptor = namespace_capability_descriptor(available=True, methods=API_METHODS)
        descriptor["namespace_contract_version"] = "2.0"
        descriptor["extra"] = "unsafe"
        errors = validate_namespace_capability(descriptor)
        self.assertIn("namespace_capability_fields_invalid", errors)
        self.assertIn("namespace_contract_version_mismatch", errors)


if __name__ == "__main__":
    unittest.main()
