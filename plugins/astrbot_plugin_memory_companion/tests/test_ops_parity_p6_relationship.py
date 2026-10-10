from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


ROOT = bootstrap_package()

from astrbot_plugin_memory_companion.core.bridge import MemoryCompanionBridge
from astrbot_plugin_memory_companion.core.coordination_status import build_coordination_status, project_p6_status, project_runtime_health
from astrbot_plugin_memory_companion.core.p6_four_package_manifest import (
    FOUR_PACKAGE_IDS,
    FOUR_PACKAGE_MANIFEST_SCHEMA,
    verify_four_package_manifests,
)
from astrbot_plugin_memory_companion.core.p6_readonly_projection import (
    P6_READONLY_STATUS_FINGERPRINT,
    P6_READONLY_STATUS_SCHEMA,
    build_p6_readonly_status,
)


def _manifest_set() -> dict[str, dict[str, str]]:
    return {
        package_id: {
            "schema": FOUR_PACKAGE_MANIFEST_SCHEMA,
            "package_id": package_id,
            "manifest_version": "1.0",
            "package_fingerprint": "a" * 64,
            "compatibility_fingerprint": "b" * 64,
        }
        for package_id in FOUR_PACKAGE_IDS
    }


def _p6_raw(**changes):
    result = {
        "schema_version": P6_READONLY_STATUS_SCHEMA,
        "source_plugin": "private_companion",
        "contract_fingerprint": P6_READONLY_STATUS_FINGERPRINT,
        "health": "ready",
        "reason_code": "",
        "counts": {"profiles": 1, "identity_links": 2, "audit_events": 3, "operations": 4},
    }
    result.update(changes)
    return result


class OpsParityP6RelationshipTests(unittest.TestCase):
    class _HashCollisionKey:
        def __init__(self, field: str) -> None:
            self._field = field
            self.hash_calls = 0
            self.eq_calls = 0
            self.str_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            return hash(self._field)

        def __eq__(self, other: object) -> bool:
            self.eq_calls += 1
            raise AssertionError("hostile key was compared")

        def __str__(self) -> str:
            self.str_calls += 1
            raise AssertionError("hostile key was stringified")

    class _HostileValue:
        def __init__(self) -> None:
            self.hash_calls = 0
            self.eq_calls = 0
            self.str_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            raise AssertionError("hostile value was hashed")

        def __eq__(self, other: object) -> bool:
            self.eq_calls += 1
            raise AssertionError("hostile value was compared")

        def __str__(self) -> str:
            self.str_calls += 1
            raise AssertionError("hostile value was stringified")

    def test_p6_manifest_and_readonly_projection_reject_extensions_and_conflicts(self):
        self.assertEqual("verified", verify_four_package_manifests(_manifest_set())["status"])
        extended = _manifest_set()
        extended["memory"]["identity"] = "must-not-project"
        self.assertEqual("unverifiable", verify_four_package_manifests(extended)["status"])

        conflict = _manifest_set()
        conflict["peiban"]["compatibility_fingerprint"] = "c" * 64
        self.assertEqual("compatibility_fingerprint_mismatch", verify_four_package_manifests(conflict)["reason_code"])

        self.assertEqual("ready", project_p6_status(_p6_raw())["health"])
        self.assertEqual("unverifiable", project_p6_status(_p6_raw(extra="must-not-project"))["health"])
        self.assertEqual("unverifiable", project_p6_status(_p6_raw(contract_fingerprint="x" * 64))["health"])

    def test_p6_enum_checks_reject_hostile_values_before_hooks(self):
        for field in ("schema_version", "source_plugin", "contract_fingerprint", "health", "reason_code"):
            raw = _p6_raw(**{field: self._HostileValue()})
            self.assertEqual("unverifiable", project_p6_status(raw)["health"])
        hostile = self._HostileValue()
        self.assertEqual("unverifiable", build_p6_readonly_status({}, health=hostile)["health"])
        self.assertEqual("invalid_reason_code", build_p6_readonly_status({}, reason_code=hostile)["reason_code"])

    def test_p6_and_bridge_reject_hash_collision_hostile_keys_before_hooks(self):
        raw_key = self._HashCollisionKey("schema_version")
        counts_key = self._HashCollisionKey("profiles")
        bridge_key = self._HashCollisionKey("health")
        self.assertEqual("unverifiable", project_p6_status({raw_key: "not-reached"})["health"])
        self.assertEqual("unverifiable", project_p6_status({**_p6_raw(), "counts": {counts_key: 1}})["health"])
        status = build_coordination_status(
            config=None,
            runtime={"compatibility_level": "full"},
            bridge={bridge_key: "not-reached"},
            p6_raw=_p6_raw(),
        )
        self.assertEqual({"health": "unverifiable", "reason_code": "bridge_status_unavailable"}, status["bridge"])

    def test_p6_projection_rejects_hash_collision_hostile_keys_before_hooks(self):
        hostile = self._HashCollisionKey("profiles")
        status = {hostile: 1}
        hash_calls_before_projection = hostile.hash_calls

        projection = build_p6_readonly_status(status)

        self.assertEqual("unverifiable", projection["health"])
        self.assertEqual("registry_status_unavailable", projection["reason_code"])
        self.assertEqual(
            {"profiles": 0, "identity_links": 0, "audit_events": 0, "operations": 0},
            projection["counts"],
        )
        self.assertEqual(hash_calls_before_projection, hostile.hash_calls)
        self.assertEqual(0, hostile.eq_calls)
        self.assertEqual(0, hostile.str_calls)

    def test_relationship_peek_fails_closed_and_filters_untrusted_fields(self):
        class Broken:
            def _peek_relationship_phase(self, _ctx):
                raise RuntimeError("broken")

        class Extended:
            def _peek_relationship_phase(self, _ctx):
                return {
                    "observed": True,
                    "phase": "close",
                    "momentum_band": "rising",
                    "touch_count": 7,
                    "momentum": 0.9,
                    "person_id": "must-not-project",
                }

        fallback = {"observed": False, "phase": "unknown", "momentum_band": "unknown"}
        self.assertEqual(fallback, MemoryCompanionBridge(Broken()).peek_relationship_phase(session_id="s"))
        projection = MemoryCompanionBridge(Extended()).peek_relationship_phase(session_id="s")
        self.assertEqual({"observed": True, "phase": "close", "momentum_band": "rising", "touch_count": 7}, projection)
        self.assertNotIn("momentum", projection)
        self.assertNotIn("person_id", projection)

        hostile_key = self._HashCollisionKey("observed")
        hostile_value = self._HostileValue()
        hostile_result = {hostile_key: hostile_value}
        hash_calls_before_peek = hostile_key.hash_calls
        projection = MemoryCompanionBridge(
            types.SimpleNamespace(_peek_relationship_phase=lambda _ctx: hostile_result)
        ).peek_relationship_phase(session_id="s")
        self.assertEqual(fallback, projection)
        self.assertEqual(hash_calls_before_peek, hostile_key.hash_calls)
        self.assertEqual(0, hostile_key.eq_calls)
        self.assertEqual(0, hostile_key.str_calls)
        self.assertEqual(0, hostile_value.hash_calls)
        self.assertEqual(0, hostile_value.eq_calls)
        self.assertEqual(0, hostile_value.str_calls)
        self.assertNotIn("hostile", repr(projection))

    def test_runtime_health_rejects_hostile_dict_keys_and_values_before_hooks(self):
        self.assertEqual(
            {"health": "unverifiable", "reason_code": "runtime_unsupported"},
            project_runtime_health({"compatibility_level": "full", "extra": "must-not-project"}),
        )

        hostile_key = self._HashCollisionKey("compatibility_level")
        hostile_runtime = {hostile_key: "full"}
        hash_calls_before_projection = hostile_key.hash_calls
        self.assertEqual(
            {"health": "unverifiable", "reason_code": "runtime_unsupported"},
            project_runtime_health(hostile_runtime),
        )
        self.assertEqual(hash_calls_before_projection, hostile_key.hash_calls)
        self.assertEqual(0, hostile_key.eq_calls)
        self.assertEqual(0, hostile_key.str_calls)

        hostile_value = self._HostileValue()
        self.assertEqual(
            {"health": "unverifiable", "reason_code": "runtime_unsupported"},
            project_runtime_health({"compatibility_level": hostile_value}),
        )
        self.assertEqual(0, hostile_value.hash_calls)
        self.assertEqual(0, hostile_value.eq_calls)
        self.assertEqual(0, hostile_value.str_calls)

    def test_coordination_status_degrades_for_invalid_bridge_and_p6_without_leaking_fields(self):
        status = build_coordination_status(
            config=None,
            runtime={"compatibility_level": "full"},
            bridge={"health": "ready", "reason_code": "companion_bridge_available", "session_id": "must-not-project"},
            p6_raw=_p6_raw(extra="must-not-project"),
        )
        self.assertEqual("unverifiable", status["health"])
        self.assertEqual({"health": "unverifiable", "reason_code": "bridge_status_unavailable"}, status["bridge"])
        self.assertEqual("unverifiable", status["p6"]["health"])
        self.assertNotIn("must-not-project", repr(status))
