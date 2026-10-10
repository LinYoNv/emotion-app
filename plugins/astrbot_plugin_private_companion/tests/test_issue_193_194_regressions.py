from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_private_companion.persona_config import load_scope_manifest


def test_creative_writing_missing_config_defaults_to_disabled() -> None:
    manifest = load_scope_manifest()
    assert manifest["enable_creative_writing"]["default"] is False


def test_primary_persona_change_keeps_a_recoverable_ownership_audit(tmp_path: Path) -> None:
    from tests.test_multi_persona_isolation import _plugin_harness

    plugin = _plugin_harness(str(tmp_path))
    plugin._data_default["users"] = {"u": {"name": "历史"}}
    warning = plugin._record_primary_persona_change("main", "alt")

    assert warning["code"] == "primary_store_owner_mismatch"
    backup = Path(warning["backup_path"])
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["users"] == {"u": {"name": "历史"}}
    ownership = plugin._data_default["primary_store_ownership"]
    assert ownership["owner_persona_id"] == "main"
    assert ownership["active_persona_id"] == "alt"
    assert ownership["status"] == "pending_review"
    assert ownership["history"][-1]["from_persona_id"] == "main"
    assert ownership["history"][-1]["to_persona_id"] == "alt"
