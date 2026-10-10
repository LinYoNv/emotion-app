# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenRouterConfigUiTests(unittest.TestCase):
    def test_page_setting_normalizer_accepts_openrouter_aliases(self) -> None:
        source = (ROOT / "page_api_settings.py").read_text(encoding="utf-8")
        alias_block = source.split('if key in {"external_image_api_platform", "backup_external_image_api_platform"}', 1)[1].split(
            "return _SETTING_UNHANDLED", 1
        )[0]
        for alias in ("openrouter", "open-router", "open_router", "openrouter.ai"):
            with self.subTest(alias=alias):
                self.assertIn(f'"{alias}": "openrouter"', alias_block)
        self.assertIn('"openrouter"', alias_block)

    def test_manual_command_platform_choices_include_openrouter(self) -> None:
        source = (ROOT / "command_handlers.py").read_text(encoding="utf-8")
        for key in ("external_image_api_platform", "backup_external_image_api_platform"):
            spec = source.split(f'"{key}": {{', 1)[1].split(
                '"backup_external_image_api_timeout_seconds"', 1
            )[0]
            with self.subTest(key=key):
                self.assertIn('"openrouter"', spec)
                for alias in ("open-router", "open_router", "openrouter.ai"):
                    self.assertIn(f'"{alias}": "openrouter"', spec)

    def test_runtime_endpoint_summary_uses_openrouter_label(self) -> None:
        source = (ROOT / "page_api.py").read_text(encoding="utf-8")
        labels = source.split("platform_labels = {", 1)[1].split("}", 1)[0]
        self.assertIn('"openrouter": "OpenRouter"', labels)

    def test_404_is_classified_as_endpoint_mismatch_before_network(self) -> None:
        source = (ROOT / "page_api.py").read_text(encoding="utf-8")
        rules = source.split("rules = [", 1)[1]
        endpoint_rule = rules.index('"endpoint_mismatch"')
        network_rule = rules.index('"network"')
        self.assertLess(endpoint_rule, network_rule)
        endpoint_block = rules[endpoint_rule:network_rule]
        for message, needle in (
            ("HTTP 404: endpoint not found", "http 404"),
            ("未找到生图接口", "未找到生图接口"),
            ("端点不匹配：请检查 URL", "端点不匹配"),
        ):
            with self.subTest(message=message):
                self.assertIn(needle.lower(), rules.lower())
        self.assertIn('"端点不匹配"', endpoint_block)
        self.assertIn("False", endpoint_block)

    def test_both_panel_variants_expose_openrouter_options_and_hints(self) -> None:
        chinese = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        ascii_panel = (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(chinese, ascii_panel)
        for script in (chinese, ascii_panel):
            self.assertIn('["openrouter", "OpenRouter"]', script)
            self.assertIn('"open-router": "openrouter"', script)
            self.assertIn('"open_router": "openrouter"', script)
            self.assertIn('"openrouter.ai": "openrouter"', script)
            self.assertIn("input_references", script)

    def test_schema_and_runtime_whitelist_document_openrouter(self) -> None:
        schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        page_api = (ROOT / "page_api.py").read_text(encoding="utf-8")
        self.assertIn("auto、openai、openrouter", schema)
        self.assertIn('"openrouter": "OpenRouter"', page_api)
        self.assertIn('"auto", "openai", "openrouter"', page_api)


if __name__ == "__main__":
    unittest.main()
