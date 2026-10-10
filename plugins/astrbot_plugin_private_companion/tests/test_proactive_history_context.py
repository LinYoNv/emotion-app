# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_private_companion.daily_state import DailyStateMixin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


ROOT = Path(__file__).resolve().parents[1]


class _HistoryHarness(ProactiveMessageMixin, DailyStateMixin):
    bot_name = "陪伴者"

    def __init__(self, items: list[dict] | None = None) -> None:
        self._conversation = SimpleNamespace(
            history=json.dumps(items or [], ensure_ascii=False)
        )
        self.persona_values: dict[str, object] = {}

    def persona_setting(self, key: str, default: object = None) -> object:
        return self.persona_values.get(key, getattr(self, key, default))

    async def _get_current_conversation_safely(self, _umo: str, *, label: str = ""):
        return self._conversation


class ProactiveHistoryContextTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _prompt_builder_harness(*, custom_template: str = "") -> _HistoryHarness:
        harness = _HistoryHarness()
        harness.data = {"daily_state": {}, "daily_plan": {}}
        harness.persona_values["proactive_prompt_template"] = custom_template
        harness._format_action_prompt_context = lambda *_args: "当前动作上下文"
        harness._format_proactive_relationship_fact = lambda *_args: "当前关系平稳"
        harness._format_schedule_context_for_prompt = lambda: "当前日程片段"
        harness._format_state_for_framework_prompt = lambda *_args, **_kwargs: "当前状态平稳"
        harness._sanitize_owner_environment_context_for_private_user = lambda value, _user: value
        harness._sanitize_schedule_context_for_private_user = lambda value, _user: value
        harness._proactive_time_guard_hint = lambda *_args: "当前时段适合轻量开口"
        harness._format_recent_proactive_topics_hint = lambda _user: "最近话题"
        harness._environment_now = lambda: datetime(2026, 9, 4, 12, 30)
        harness._private_user_role = lambda _user: "owner"

        async def resolve_persona(_user):
            return "测试人格"

        harness._resolve_proactive_persona_prompt = resolve_persona
        return harness

    def test_history_limits_are_configurable_with_safe_legacy_defaults(self):
        harness = _HistoryHarness()

        self.assertEqual(20, harness._proactive_history_limit("generation"))
        self.assertEqual(30, harness._proactive_history_limit("review"))

        harness.proactive_generation_history_limit = 42
        harness.proactive_review_history_limit = 64
        self.assertEqual(42, harness._proactive_history_limit("generation"))
        self.assertEqual(64, harness._proactive_history_limit("review"))

        harness.persona_values.update(
            {
                "proactive_generation_history_limit": 17,
                "proactive_review_history_limit": 23,
            }
        )
        self.assertEqual(17, harness._proactive_history_limit("generation"))
        self.assertEqual(23, harness._proactive_history_limit("review"))

    async def test_compact_mode_keeps_recent_raw_and_includes_older_facts(self):
        items = [
            {"role": "user", "content": "明天考试改到下午，这件事后面仍然有效"},
            *[
                {"role": "assistant" if index % 2 else "user", "content": f"中间对话 {index}"}
                for index in range(8)
            ],
            {"role": "user", "content": "最新问题保持完整"},
            {"role": "assistant", "content": "最新回答也保持完整"},
        ]
        harness = _HistoryHarness(items)
        harness.proactive_history_context_mode = "compact"
        harness.proactive_history_recent_raw_count = 2
        harness.proactive_history_max_chars = 3000

        context = await harness._recent_private_conversation_for_proactive_review(
            {"umo": "session-a"},
            limit=20,
        )

        self.assertIn("【较早对话（已压缩）】", context)
        self.assertIn("明天考试改到下午，这件事后面仍然有效", context)
        self.assertIn("【最近对话（保留原文）】", context)
        self.assertIn("用户: 最新问题保持完整", context)
        self.assertIn("陪伴者(Bot回复): 最新回答也保持完整", context)
        self.assertLess(context.index("明天考试改到下午"), context.index("最新问题保持完整"))

    def test_compact_history_wire_keeps_legacy_byte_layout(self):
        harness = _HistoryHarness()
        harness.proactive_history_context_mode = "compact"
        harness.proactive_history_recent_raw_count = 1
        harness.proactive_history_max_chars = 3000

        context = harness._format_proactive_history_context(["较早原文", "最新原文"])

        self.assertEqual(
            "【较早对话（已压缩）】\n较早原文\n【最近对话（保留原文）】\n最新原文",
            context,
        )

    async def test_recent_only_mode_uses_configured_raw_count(self):
        items = [{"role": "user", "content": f"消息 {index}"} for index in range(6)]
        harness = _HistoryHarness(items)
        harness.proactive_history_context_mode = "recent_only"
        harness.proactive_history_recent_raw_count = 2

        context = await harness._recent_private_conversation_for_proactive_review(
            {"umo": "session-b"},
            limit=6,
        )

        self.assertNotIn("消息 3", context)
        self.assertIn("消息 4", context)
        self.assertIn("消息 5", context)

    async def test_context_respects_character_limit_and_keeps_newest_message(self):
        items = [
            {"role": "user", "content": f"历史 {index} " + ("内容" * 100)}
            for index in range(20)
        ]
        items.append({"role": "assistant", "content": "LATEST-MARKER"})
        harness = _HistoryHarness(items)
        harness.proactive_history_context_mode = "expanded"
        harness.proactive_history_max_chars = 500

        context = await harness._recent_private_conversation_for_proactive_review(
            {"umo": "session-c"},
            limit=50,
        )

        self.assertLessEqual(len(context), 500)
        self.assertIn("LATEST-MARKER", context)

    def test_generation_and_review_call_sites_use_stage_specific_limits(self):
        generation_source = inspect.getsource(ProactiveMessageMixin._build_framework_proactive_prompt)
        review_source = inspect.getsource(ProactiveMessageMixin._review_proactive_message_send_decision)

        self.assertIn('_proactive_history_limit("generation")', generation_source)
        self.assertIn('_proactive_history_limit("review")', review_source)
        self.assertNotIn("limit=5", generation_source)
        self.assertNotIn("limit=10", review_source)

    def test_future_schedule_hint_is_limited_to_relevant_routes(self):
        harness = ProactiveMessageMixin()
        harness._agenda_disclosure_view = lambda *_args, **_kwargs: {
            "entries": [
                {
                    "temporal_phase": "future",
                    "start_at": "2026-07-30T22:30:00+08:00",
                    "end_at": "2026-07-30T23:00:00+08:00",
                    "title": "整理桌面",
                }
            ]
        }
        hint = harness._format_proactive_future_schedule_hint(reason="background_schedule")
        assert "整理桌面" in hint
        assert "不是已经发生的事实" in hint
        assert harness._format_proactive_future_schedule_hint(reason="news_share") == ""

    def test_calendar_constraints_are_injected_into_generation_and_review_context(self):
        generation_source = inspect.getsource(ProactiveMessageMixin._build_framework_proactive_prompt)
        runtime_source = inspect.getsource(ProactiveMessageMixin._format_proactive_review_runtime_context)
        section_source = inspect.getsource(
            ProactiveMessageMixin._format_proactive_calendar_constraint_hint_section
        )
        self.assertIn("_format_proactive_calendar_constraint_hint_section", generation_source)
        self.assertIn('key="proactive.calendar_context"', section_source)
        self.assertIn('title="生活时间线参考"', section_source)
        self.assertIn("_format_proactive_calendar_constraint_hint", runtime_source)

    def test_external_material_has_one_generation_entry_and_prompt_section(self):
        generation_source = inspect.getsource(ProactiveMessageMixin._build_framework_proactive_prompt)

        self.assertEqual(1, generation_source.count("_external_schedule_material_context("))
        self.assertEqual(1, generation_source.count('key="proactive.external_material"'))
        self.assertEqual(
            1,
            generation_source.count('title="外部插件提供的今日实况（仅作生活素材'),
        )

    def test_default_template_is_authored_as_sections_but_keeps_legacy_wire(self):
        harness = _HistoryHarness()

        document = harness._default_proactive_prompt_document()
        rendered = harness._default_proactive_prompt_template()

        self.assertEqual(
            [
                "proactive.template.introduction",
                "proactive.template.clues",
                "proactive.template.selection",
                "proactive.template.composition",
                "proactive.template.output",
            ],
            [section.key for section in document.user],
        )
        self.assertIn("【这次可以使用的线索】", rendered)
        self.assertIn("【先判断，再开口】", rendered)
        self.assertIn("【成文方式】", rendered)
        self.assertNotIn("【主动私聊任务】", rendered)
        self.assertNotIn("【主动私聊输出要求】", rendered)
        self.assertNotIn("【", inspect.getsource(harness._default_proactive_prompt_document))

    def test_generation_builder_deduplicates_by_section_key(self):
        generation_source = inspect.getsource(ProactiveMessageMixin._build_framework_proactive_prompt)

        self.assertIn("included_keys", generation_source)
        self.assertIn("section.key in included_keys", generation_source)
        self.assertNotIn("not in prompt", generation_source)

    async def test_generation_builder_keeps_default_legacy_wire(self):
        harness = self._prompt_builder_harness()

        prompt = await harness._build_framework_proactive_prompt(
            user={"user_id": "QQ:112233", "umo": ""},
            name="测试用户",
            reason="check_in",
            action="message",
            action_context="",
            motive="想聊一句",
        )

        self.assertTrue(prompt.startswith("你正在给 测试用户 发一条主动私聊。"))
        self.assertEqual(1, prompt.count("【这次可以使用的线索】"))
        self.assertEqual(1, prompt.count("【主动消息的表达形状】"))
        self.assertEqual(1, prompt.count("【主动生成工具边界】"))
        self.assertEqual(1, prompt.count("【当前主动消息必须遵循的人格】"))

    async def test_custom_template_placeholder_covers_matching_section_key(self):
        harness = self._prompt_builder_harness(
            custom_template="自定义开头\n\n{{expression_shape_hint}}\n\n自定义结尾"
        )

        prompt = await harness._build_framework_proactive_prompt(
            user={"user_id": "QQ:112233", "umo": ""},
            name="测试用户",
            reason="check_in",
            action="message",
            action_context="",
            motive="想聊一句",
        )

        self.assertTrue(prompt.startswith("自定义开头"))
        self.assertEqual(1, prompt.count("【主动消息的表达形状】"))
        self.assertIn("自定义结尾", prompt)

    def test_generation_tool_boundary_allows_only_proactive_photo_tool(self):
        generation_source = inspect.getsource(ProactiveMessageMixin._build_framework_proactive_prompt)

        self.assertIn("允许调用一次 `pc_generate_photo`", generation_source)
        self.assertIn("除 `pc_generate_photo` 以外的其他 Private Companion 工具", generation_source)
        self.assertIn("`final_response_instruction`", generation_source)
        self.assertNotIn("或生图发送工具", generation_source)

    def test_schema_exposes_all_history_context_settings(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        generation_items = schema["proactive_generation_config"]["items"]
        review_items = schema["emotion_relationship_config"]["items"]

        for key in (
            "proactive_generation_history_limit",
            "proactive_history_context_mode",
            "proactive_history_recent_raw_count",
            "proactive_history_max_chars",
        ):
            self.assertIn(key, generation_items)
            self.assertFalse(generation_items[key].get("invisible", False))
        self.assertIn("proactive_review_history_limit", review_items)
        self.assertFalse(review_items["proactive_review_history_limit"].get("invisible", False))


if __name__ == "__main__":
    unittest.main()
