from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_adapter():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    package_name = "c5_adapter_link"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.memory_companion_adapter").MemoryCompanionAdapterMixin


class C5AdapterLinkTests(unittest.TestCase):
    def test_adapter_hands_bridge_a_fresh_one_shot_attestation(self):
        from p5_attestation import P5AttestationRegistry

        mixin = load_adapter()
        registry = P5AttestationRegistry()
        request, event, p3 = object(), types.SimpleNamespace(), object()

        class Companion(mixin):
            enable_p5_source_observer = True

            def _p5_issue_attestation_for_event(self, *, event, request, sink):
                handle = registry.mint(
                    request or event,
                    event,
                    p3,
                    request_hash="a" * 64,
                    session_hash="b" * 64,
                    source_kind="current_user_intent",
                    source_trust="T2",
                    firewall_status="allowed",
                    disposition="allow",
                    reason_codes=("evidence_only_nonexecuting",),
                    source_event_ref_hash="c" * 64,
                    sinks=(sink,),
                )

                def consume(candidate, requested_sink=sink):
                    return registry.consume(candidate, request or event, event, p3, requested_sink)

                return handle, consume

        companion = Companion()
        values = companion._memory_companion_p5_gate_kwargs(event=event, sink="bridge_serialization")
        self.assertEqual(set(values), {"p5_attestation", "p5_attestation_consumer"})
        snapshot = values["p5_attestation_consumer"](values["p5_attestation"], "bridge_serialization")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.sink, "bridge_serialization")
        self.assertIsNone(values["p5_attestation_consumer"](values["p5_attestation"], "bridge_serialization"))
