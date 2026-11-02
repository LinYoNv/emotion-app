"""together 的默认值必须三处一致：schema、defaults、运行时兜底。

schema 里写 true、defaults 里写 false 时，面板显示的默认值和真实行为相反，
「房间打不开」就会被误当成配置没保存。
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "astrbot_host" / "native_extensions" / "together"


def load(name):
    return json.loads((EXTENSION / name).read_text(encoding="utf-8"))


class TogetherDefaultConsistencyTests(unittest.TestCase):
    def test_defaults_never_disagree_with_the_schema(self):
        schema, defaults = load("settings-schema.json"), load("settings-defaults.json")
        drift = []
        for section, values in defaults.items():
            items = schema.get(section, {}).get("items", {})
            for key, value in values.items():
                declared = items.get(key, {}).get("default")
                if declared is not None and declared != value:
                    drift.append(f"{section}.{key}: schema={declared!r} defaults={value!r}")
        self.assertEqual(drift, [], "settings-defaults.json 与 settings-schema.json 默认值不一致")

    def test_room_server_is_enabled_by_default_like_v1(self):
        # 旧版插件默认就起房间服务（只监听回环）；关掉默认值会让「一起看电影」
        # 直接报房间服务未启用。
        self.assertIs(load("settings-defaults.json")["server"]["enabled"], True)
        self.assertIs(load("settings-schema.json")["server"]["items"]["enabled"]["default"], True)

    def test_runtime_fallback_matches_the_declared_default(self):
        source = (EXTENSION / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("_cfg_bool('server.enabled', True)", source,
                      "运行时对 server.enabled 的兜底值必须与 schema/defaults 一致")


if __name__ == "__main__":
    unittest.main()
