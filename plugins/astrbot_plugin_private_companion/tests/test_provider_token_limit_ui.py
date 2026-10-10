# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProviderTokenLimitUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.panel_dir = ROOT / "pages" / "companion-panel"
        cls.localized_dir = ROOT / "pages" / "陪伴面板"
        cls.app = (cls.panel_dir / "app.js").read_text(encoding="utf-8")
        cls.tree = (cls.panel_dir / "js" / "panels" / "provider-tree.js").read_text(encoding="utf-8")
        cls.styles = (cls.panel_dir / "app.css").read_text(encoding="utf-8")
        cls.html = (cls.panel_dir / "index.html").read_text(encoding="utf-8")

    def test_app_loads_saves_and_refreshes_token_limit_overrides(self) -> None:
        self.assertIn("providerTokenLimitDraft: {}", self.app)
        self.assertIn("function normalizeModelTokenLimitOverrides(raw)", self.app)
        self.assertIn("tokenLimit >= 256 && tokenLimit <= 2000000", self.app)
        self.assertIn("overview?.settings?.model_token_limit_overrides", self.app)
        self.assertIn("const tokenLimitOverrides = currentProviderTokenLimitValues();", self.app)
        self.assertIn("model_token_limit_overrides: tokenLimitOverrides", self.app)
        self.assertIn("state.providerTokenLimitDraft = { ...tokenLimitOverrides };", self.app)
        self.assertIn("state.overview.settings.model_token_limit_overrides = { ...tokenLimitOverrides };", self.app)

    def test_provider_save_only_submits_current_mode_provider_keys(self) -> None:
        self.assertIn(
            "if (visibleConfigKey(key) && providerAllowedInCurrentMode(key)) providers[key] = values[key] || \"\";",
            self.app,
        )

    def test_provider_cards_render_side_by_side_token_limit_control(self) -> None:
        self.assertIn("单次 Token 上限（预估）", self.tree)
        self.assertIn('data-provider-token-limit="${escapeHtml(key)}"', self.tree)
        self.assertIn('min="256" max="2000000" step="256"', self.tree)
        self.assertIn("function rememberProviderTokenLimitDraft(context, input)", self.tree)
        self.assertIn("currentProviderTokenLimitValues,", self.tree)
        self.assertIn(".provider-limit-grid {", self.styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", self.styles)
        self.assertIn(".provider-token-limit-control {", self.styles)

    def test_token_limit_collector_enforces_bounds_and_blank_deletion(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = f"""
global.window = {{}};
eval({json.dumps(self.tree, ensure_ascii=False)});
const inputs = [
  {{ value: "1024", dataset: {{ providerTokenLimit: "FAST_RESPONSE_PROVIDER_ID" }} }},
  {{ value: "", dataset: {{ providerTokenLimit: "COMPLEX_REASONING_PROVIDER_ID" }} }},
  {{ value: "2000000", dataset: {{ providerTokenLimit: "CREATIVE_MODEL_PROVIDER_ID" }} }},
  {{ value: "2000001", dataset: {{ providerTokenLimit: "PLUGIN_VISION_PROVIDER_ID" }} }},
];
const result = window.PrivateCompanionProviderTree.currentProviderTokenLimitValues({{
  document: {{ querySelectorAll: (selector) => selector === "[data-provider-token-limit]" ? inputs : [] }},
  state: {{
    overview: {{ settings: {{ model_token_limit_overrides: JSON.stringify({{
      FAST_RESPONSE_PROVIDER_ID: 512,
      COMPLEX_REASONING_PROVIDER_ID: 2048,
      PLUGIN_VISION_PROVIDER_ID: 128,
    }}) }} }},
    providerTokenLimitDraft: {{ FAST_RESPONSE_PROVIDER_ID: 768 }},
  }},
}});
process.stdout.write(JSON.stringify(result));
"""
        with tempfile.TemporaryDirectory(prefix="pc-token-limit-") as temp_dir:
            script_path = Path(temp_dir) / "token_limit_check.js"
            script_path.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [node, str(script_path)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "FAST_RESPONSE_PROVIDER_ID": 1024,
                "CREATIVE_MODEL_PROVIDER_ID": 2000000,
            },
        )

    def test_mirrored_assets_and_cache_busting_match(self) -> None:
        for relative in (
            "app.js",
            "app.css",
            "index.html",
            "js/panels/provider-tree.js",
        ):
            self.assertEqual(
                (self.panel_dir / relative).read_bytes(),
                (self.localized_dir / relative).read_bytes(),
                relative,
            )
        # Provider editor code is now loaded on demand; verify its cache marker
        # and classic-script loader instead of an eager script tag.
        self.assertIn("page=lazy-classic-loader-v1", self.html)
        self.assertNotIn('<script src="./js/panels/provider-tree.js', self.html)
        self.assertIn('loadOptionalClassicScript("./js/panels/provider-tree.js?', self.app)


if __name__ == "__main__":
    unittest.main()
