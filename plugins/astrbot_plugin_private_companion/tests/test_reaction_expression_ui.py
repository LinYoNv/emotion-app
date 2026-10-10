# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReactionExpressionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        page_root = ROOT / "pages" / "陪伴面板"
        cls.html = (page_root / "index.html").read_text(encoding="utf-8")
        cls.script = (page_root / "app.js").read_text(encoding="utf-8")
        cls.css = (page_root / "app.css").read_text(encoding="utf-8")
        cls.schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_experiment_is_opt_in_and_keeps_group_scope_off(self) -> None:
        items = self.schema["experimental_motivation_config"]["items"]

        self.assertFalse(items["enable_reaction_expression_experiment"]["default"])
        self.assertTrue(items["reaction_expression_private_enabled"]["default"])
        self.assertTrue(items["reaction_expression_proactive_enabled"]["default"])
        self.assertFalse(items["reaction_expression_group_enabled"]["default"])
        for key in (
            "reaction_expression_private_enabled",
            "reaction_expression_proactive_enabled",
            "reaction_expression_group_enabled",
            "reaction_expression_delivery_mode",
            "reaction_expression_image_format",
            "reaction_expression_trigger_probability",
            "reaction_expression_cooldown_seconds",
            "reaction_expression_semantic_trigger_enabled",
            "reaction_expression_low_latency_mode",
            "reaction_expression_candidate_limit",
        ):
            self.assertEqual(
                {"enable_reaction_expression_experiment": True},
                items[key]["condition"],
                key,
            )

    def test_grouped_settings_keep_hidden_flat_compatibility_entries(self) -> None:
        items = self.schema["experimental_motivation_config"]["items"]
        for key in (
            "enable_reaction_expression_experiment",
            "reaction_expression_private_enabled",
            "reaction_expression_proactive_enabled",
            "reaction_expression_group_enabled",
            "reaction_expression_delivery_mode",
            "reaction_expression_image_format",
            "reaction_expression_trigger_probability",
            "reaction_expression_cooldown_seconds",
            "reaction_expression_semantic_trigger_enabled",
            "reaction_expression_low_latency_mode",
            "reaction_expression_candidate_limit",
        ):
            self.assertIn(key, self.schema)
            self.assertTrue(self.schema[key]["invisible"], key)
            self.assertEqual(items[key]["type"], self.schema[key]["type"], key)
            self.assertEqual(items[key]["default"], self.schema[key]["default"], key)

    def test_latency_and_frequency_defaults_are_bounded(self) -> None:
        items = self.schema["experimental_motivation_config"]["items"]

        self.assertTrue(items["reaction_expression_low_latency_mode"]["default"])
        self.assertEqual(
            "separate_after",
            items["reaction_expression_delivery_mode"]["default"],
        )
        self.assertEqual(
            ["separate_after", "same_message", "separate_before"],
            items["reaction_expression_delivery_mode"]["options"],
        )
        self.assertEqual(
            ["正文后单独发送（推荐）", "与正文同一消息链", "正文前单独发送"],
            items["reaction_expression_delivery_mode"]["labels"],
        )
        self.assertEqual("image", items["reaction_expression_image_format"]["default"])
        self.assertEqual(
            ["image", "qq_emoji"],
            items["reaction_expression_image_format"]["options"],
        )
        self.assertEqual(0.2, items["reaction_expression_trigger_probability"]["default"])
        self.assertEqual({"min": 0, "max": 1, "step": 0.01}, items["reaction_expression_trigger_probability"]["slider"])
        self.assertEqual(180, items["reaction_expression_cooldown_seconds"]["default"])
        self.assertEqual(6, items["reaction_expression_candidate_limit"]["default"])
        self.assertEqual({"min": 1, "max": 16, "step": 1}, items["reaction_expression_candidate_limit"]["slider"])

    def test_panel_reuses_existing_experimental_navigation(self) -> None:
        self.assertIn('data-tab="experimental"', self.html)
        self.assertNotIn('data-tab="reaction-expression"', self.html)
        self.assertIn('"enable_reaction_expression_experiment",', self.script)
        self.assertIn('label: "表情表达实验"', self.script)
        self.assertIn('title: "适用会话"', self.script)
        self.assertIn('title: "发送方式"', self.script)
        self.assertIn('title: "触发节奏"', self.script)
        self.assertIn('title: "性能策略"', self.script)
        self.assertIn('theme: "expression"', self.script)
        self.assertIn(".exp-card-visual.expression", self.css)
        self.assertIn(".exp-research-hero.expression", self.css)

    def test_panel_exposes_compact_performance_controls(self) -> None:
        for key in (
            "reaction_expression_private_enabled",
            "reaction_expression_proactive_enabled",
            "reaction_expression_group_enabled",
            "reaction_expression_delivery_mode",
            "reaction_expression_image_format",
            "reaction_expression_trigger_probability",
            "reaction_expression_cooldown_seconds",
            "reaction_expression_semantic_trigger_enabled",
            "reaction_expression_low_latency_mode",
            "reaction_expression_candidate_limit",
        ):
            self.assertIn(key, self.script)
        self.assertIn('reaction_expression_trigger_probability: { type: "number", min: 0, max: 100, step: 1 }', self.script)
        self.assertIn(
            'reaction_expression_delivery_mode: { type: "select", options: [["separate_after", "正文后单独发送（推荐）"], ["same_message", "与正文同一消息链"], ["separate_before", "正文前单独发送"]] }',
            self.script,
        )
        self.assertIn(
            'reaction_expression_image_format: { type: "select", options: [["image", "普通图片（兼容）"], ["qq_emoji", "QQ 表情格式（OneBot）"]] }',
            self.script,
        )
        self.assertIn('reaction_expression_candidate_limit: { type: "number", min: 1, max: 16, step: 1 }', self.script)
        self.assertIn(
            'const deliveryMode = String(settings.reaction_expression_delivery_mode || "separate_after");',
            self.script,
        )
        self.assertIn("function featureSettingAccessibility(key, prefix", self.script)
        self.assertIn('aria-labelledby="${escapeHtml(labelId)}"', self.script)
        self.assertIn('aria-describedby="${escapeHtml(descriptionId)}"', self.script)
        self.assertIn(
            "featureSettingInput(name, value, accessibility)",
            self.script,
        )
        self.assertIn('["发送方式", deliveryModeLabels[deliveryMode] || deliveryModeLabels.separate_after]', self.script)
        self.assertIn("低延迟模式不调用额外选图模型", self.script)
        self.assertIn("插件仍只执行一次图库检索", self.script)
        self.assertIn("overview?.reaction_expression", self.script)
        self.assertIn("缓存命中", self.script)
        self.assertIn("最近检索", self.script)
        self.assertIn("model_omissions", self.script)
        self.assertIn("模型未采用", self.script)
        self.assertIn("local_fallbacks", self.script)
        self.assertIn("本地兜底", self.script)
        self.assertIn("高置信时优先", self.script)
        self.assertIn("没有足够合适的候选时保持纯文字", self.script)
        self.assertIn('embeddingEnabled ? "主回复 1 次 + 查询向量 1 次" : "仅主回复 1 次"', self.script)
        self.assertIn("本地语义 + 关键词", self.script)
        self.assertIn("绝不会用图片替代正文", self.script)
        self.assertNotIn("只把合适图片追加在文字后", self.script)
        self.assertNotIn(
            "只把合适图片追加在文字后",
            self.schema["experimental_motivation_config"]["items"]
            ["enable_reaction_expression_experiment"]["hint"],
        )

    def test_panel_exposes_optional_embedding_provider_with_local_fallback(self) -> None:
        items = self.schema["experimental_motivation_config"]["items"]
        model_items = self.schema["model_assignment_config"]["items"]

        self.assertFalse(items["reaction_expression_embedding_enabled"]["default"])
        self.assertEqual("select_provider", items["REACTION_EXPRESSION_EMBEDDING_PROVIDER_ID"]["_special"])
        self.assertEqual("select_provider", model_items["EMBEDDING_PROVIDER_ID"]["_special"])
        self.assertIn("群内黑话", model_items["EMBEDDING_PROVIDER_ID"]["hint"])
        self.assertEqual(0.42, items["reaction_expression_embedding_score_threshold"]["default"])
        self.assertTrue(items["reaction_expression_embedding_backfill_enabled"]["default"])
        for key in (
            "REACTION_EXPRESSION_EMBEDDING_PROVIDER_ID",
            "reaction_expression_embedding_timeout_ms",
            "reaction_expression_embedding_candidate_limit",
            "reaction_expression_embedding_score_threshold",
            "reaction_expression_embedding_weight",
        ):
            self.assertIn(key, self.script)
        self.assertIn("availableEmbeddingProviders", self.script)
        self.assertIn("通用语义嵌入模型", self.script)
        self.assertIn('keys: ["EMBEDDING_PROVIDER_ID"]', self.script)
        self.assertIn("自动回退关键词检索", self.script)

    def test_runtime_panel_initializes_trigger_mode_summary_locally(self) -> None:
        runtime_source = self.script.split(
            "function renderExperimentalRuntime(key)", 1
        )[1].split("\nfunction ", 1)[0]

        declaration = "const triggerModes = runtime.trigger_modes || {};"
        self.assertIn(declaration, runtime_source)
        self.assertLess(
            runtime_source.index(declaration),
            runtime_source.index("Object.entries(triggerModes)"),
        )

    def test_panel_exposes_complete_owned_asset_library(self) -> None:
        for endpoint in (
            "/reaction_library/list",
            "/reaction_library/import",
            "/reaction_library/analyze",
            "/reaction_library/update",
            "/reaction_library/delete",
            "/reaction_library/rescan",
        ):
            self.assertIn(endpoint, self.script)
        for text in (
            "表情包素材库",
            "选择图片或 ZIP",
            "选择文件夹",
            "默认情绪",
            "沟通用途",
            "私聊 + 群聊",
            "重建索引",
            "批量导入",
            "上传后自动识别",
            "重新识别",
        ):
            self.assertIn(text, self.script)
        for selector in (
            ".reaction-library-workspace",
            ".reaction-asset-grid",
            ".reaction-library-editor",
            ".reaction-import-dialog",
            ".reaction-library-pager",
            ".reaction-analysis-badge",
            ".reaction-import-dropzone",
        ):
            self.assertIn(selector, self.css)
        self.assertIn('key === "enable_reaction_expression_experiment" ? renderReactionLibraryWorkspace()', self.script)


if __name__ == "__main__":
    unittest.main()
