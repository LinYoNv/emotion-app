from __future__ import annotations

import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APP_JS = (PLUGIN_ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
APP_CSS = (PLUGIN_ROOT / "pages" / "陪伴面板" / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (PLUGIN_ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(
        rf"function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)(?=\nfunction |\Z)",
        APP_JS,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing JavaScript function: {name}")
    return match.group("body")


class BodyMonitorWebUiPlacementTests(unittest.TestCase):
    def test_health_event_link_is_not_mounted_on_proactive_page(self) -> None:
        self.assertNotIn('id="bodyMonitorIntegrationCard"', INDEX_HTML)
        self.assertNotIn(
            "renderBodyMonitorIntegration();",
            _function_source("renderProactiveCandidates"),
        )

    def test_health_event_link_uses_standard_long_term_feature_row(self) -> None:
        feature_groups = APP_JS.split("const featureGroups = [", 1)[1].split(
            "const embeddedFeatureParentByKey", 1
        )[0]
        long_term_group = re.search(
            r'title: "长线主动".*?keys: \[(?P<keys>.*?)\]',
            feature_groups,
            re.DOTALL,
        )

        self.assertIsNotNone(long_term_group)
        self.assertIn(
            '"enable_body_monitor_integration"',
            long_term_group.group("keys"),
        )
        self.assertNotIn("bodyMonitorIntegrationCard", APP_JS)
        self.assertNotIn("body-monitor-", APP_CSS)
        self.assertIn(
            'visibleKeys.map((key) => featureSwitchItem(key)).join("")',
            _function_source("renderFeatureSwitches"),
        )

    def test_health_event_link_uses_the_shared_feature_draft(self) -> None:
        source = _function_source("featureDraftFromOverview")

        self.assertIn('"enable_body_monitor_integration"', source)

    def test_health_event_link_keeps_runtime_diagnostics_in_standard_detail(self) -> None:
        source = _function_source("bodyMonitorFeatureDetailCard")

        self.assertIn("body_monitor_integration", source)
        self.assertIn('class="feature-detail-card"', source)
        for label in ("联动状态", "最近拉取", "接口版本", "最近批次", "错误"):
            self.assertIn(label, source)
        self.assertIn(
            'key === "enable_body_monitor_integration" ? bodyMonitorFeatureDetailCard()',
            _function_source("featureDetailPage"),
        )

    def test_health_event_link_keeps_search_aliases(self) -> None:
        aliases = APP_JS.split("const featureSearchAliases = {", 1)[1].split("};", 1)[0]

        self.assertIn("身体状态联动 body monitor", aliases)
        self.assertIn("featureSearchAliases[key]", _function_source("featureSearchText"))


if __name__ == "__main__":
    unittest.main()
