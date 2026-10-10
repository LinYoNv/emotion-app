from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


identity = _load_module("c1_plugin_identity", ROOT / "plugin_identity.py")
runtime = _load_module("c1_runtime_compat", ROOT / "runtime_compat.py")
PLUGIN_ID = identity.PLUGIN_ID


def test_plugin_identity_uses_complete_tokens():
    assert identity.is_exact_plugin_id(PLUGIN_ID)
    assert identity.is_exact_plugin_id(f"data.plugins.{PLUGIN_ID}.main")
    assert not identity.is_exact_plugin_id(f"{PLUGIN_ID}_ops")
    assert not identity.is_exact_plugin_id(f"{PLUGIN_ID}2")
    assert not identity.is_exact_plugin_id("astrbot_plugin_memory_companion")


def test_handler_package_boundary_rejects_similarly_named_package():
    assert identity.is_module_path_for_package("astrbot_plugin_private_companion.main", PLUGIN_ID)
    assert identity.is_module_path_for_package("astrbot_plugin_private_companion.pages.api", PLUGIN_ID)
    assert not identity.is_module_path_for_package("astrbot_plugin_private_companion_ops.main", PLUGIN_ID)


def test_identity_snapshot_freezes_public_name_and_data_key():
    snapshot = identity.plugin_identity_snapshot()
    assert snapshot["plugin_id"] == PLUGIN_ID
    assert snapshot["data_directory"] == PLUGIN_ID
    assert snapshot["match_rule"] == "exact_id_or_module_segment"


def test_identity_helpers_ignore_unstringifiable_optional_proxy():
    class _ExplodingIdentity:
        def __str__(self):
            raise ModuleNotFoundError("No module named 'torch'", name="torch")

    value = _ExplodingIdentity()
    assert identity._identity_text(value) == ""
    assert identity._identity_segments(value) == ()
    assert not identity.is_exact_plugin_id(value)
    assert not identity.is_module_path_for_package(value, PLUGIN_ID)


def test_runtime_probe_is_non_blocking_and_structured():
    status = runtime.probe_runtime_capabilities(context=SimpleNamespace(), event=None, plugin_name=PLUGIN_ID, plugin_version="5.10.6")
    payload = status.to_dict()
    assert payload["plugin_name"] == PLUGIN_ID
    assert payload["plugin_version"] == "5.10.6"
    assert payload["compatibility_level"] in {"full", "degraded", "unsupported"}
    assert isinstance(payload["capabilities"], dict)
    assert isinstance(payload["missing_required"], list)
    assert isinstance(payload["warnings"], list)
