# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from astrbot_plugin_private_companion.main import PrivateCompanionExtensionAPI
from astrbot_plugin_private_companion.conversation_prompt_section import PromptSection
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin
from astrbot_plugin_private_companion.scene_context import SceneContextMixin, infer_companion_scene_category


class _SceneHarness(SceneContextMixin):
    def __init__(self, outfit_path: str) -> None:
        self.data = {
            "daily_state": {
                "date": "2026-07-19",
                "energy": 62,
                "mood_bias": "放松",
                "location": "学校教室",
                "location_source": "daily_plan",
                "conditions": [{"label": "午后有点困"}],
            },
            "daily_plan": {
                "date": "2026-07-19",
                "items": [
                    {
                        "time": "15:00",
                        "end": "17:00",
                        "activity": "在教室整理今天的笔记",
                        "mood": "专注",
                        "message_seed": "终于整理完一小节",
                    }
                ],
            },
            "daily_weather": {
                "prompt": "傍晚多云，24 度",
                "source": "private_companion",
            },
            "daily_outfit_photo": {
                "date": "2026-07-19",
                "path": outfit_path,
                "outfit_profile": {
                    "top": "白色衬衫",
                    "bottom": "深灰百褶裙",
                    "palette": "白色和深灰",
                },
            },
            "users": {
                "10001": {
                    "nickname": "主人",
                    "relationship_role": "owner",
                    "style": "自然亲近",
                    "planned_proactive_topic": "整理完的笔记",
                    "planned_proactive_motive": "想分享刚刚完成的一小段",
                }
            },
        }

    @staticmethod
    def _get_current_plan_item(plan):
        return plan["items"][0]

    @staticmethod
    def _scene_context_now() -> datetime:
        return datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc)

    @staticmethod
    def _format_plan_item_for_prompt(item) -> str:
        return f"{item['time']}-{item['end']} {item['activity']}"

    @staticmethod
    def _current_location_state_text(state) -> str:
        return str(state.get("location") or "")

    @staticmethod
    def _coarse_roleplay_location_text(_location: str) -> str:
        return "学校"

    @staticmethod
    def _weather_summary_text(weather) -> str:
        return str(weather.get("prompt") or "")

    @staticmethod
    def _private_user_role(user, _user_id="") -> str:
        return str(user.get("relationship_role") or "owner")

    @staticmethod
    def _private_user_role_label(_role: str) -> str:
        return "主要用户"


class _PhotoPromptHarness(_SceneHarness, ProactiveMessageMixin):
    def __init__(self, outfit_path: str) -> None:
        super().__init__(outfit_path)
        self.captured_prompt = ""
        self.photo_prompt_provider_id = ""
        self.mai_style_provider_id = ""
        self.photo_generation_prompt_format = "traditional"

    @staticmethod
    def _get_default_persona_prompt() -> str:
        return "安静但愿意分享生活片段。"

    @staticmethod
    def _get_photo_style_instruction() -> tuple[str, str]:
        return "真实", "自然手机摄影风格"

    @staticmethod
    def _format_content_choice_options_for_prompt(_action: str) -> str:
        return "笔记本 / 教室窗边 / 随手自拍"

    def _photo_generation_prompt_format_instruction(self) -> str:
        return f"提示词格式：{self.photo_generation_prompt_format}。"

    @staticmethod
    def _deferred_immediate_share_tense_hint(*_args, **_kwargs) -> str:
        return ""

    @staticmethod
    def _task_provider(*_args) -> str:
        return ""

    async def _llm_call(self, prompt: str, **_kwargs) -> str:
        self.captured_prompt = prompt
        return json.dumps(
            {
                "kind": "text2img",
                "use_persona_reference": False,
                "prompt": "A notebook on a classroom desk under cloudy evening light.",
                "caption": "刚整理完这一页笔记。",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _extract_json_payload(text: str):
        return json.loads(text)


class SceneContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.outfit_path = Path(self.temp_dir.name) / "outfit  reference.png"
        self.outfit_path.write_bytes(b"image")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_combines_current_life_context(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        user = harness.data["users"]["10001"]

        snapshot = harness._build_companion_scene_snapshot(
            {**user, "user_id": "10001"},
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(3, snapshot["version"])
        self.assertEqual("下午", snapshot["daypart"])
        self.assertIn("整理今天的笔记", snapshot["schedule"]["text"])
        self.assertEqual("学校", snapshot["location"]["text"])
        self.assertEqual("outdoor", snapshot["location"]["category"])
        self.assertEqual("外出", snapshot["location"]["category_label"])
        self.assertIn("多云", snapshot["weather"]["text"])
        self.assertTrue(snapshot["outfit"]["available"])
        self.assertEqual(str(self.outfit_path), snapshot["outfit"]["reference_path"])
        self.assertIn("白色衬衫", snapshot["outfit"]["description"])
        self.assertEqual("主要用户", snapshot["relationship"]["role_label"])
        self.assertTrue(snapshot["visual"]["shareable"])

        formatted = harness._format_companion_scene_snapshot(
            snapshot,
            purpose="proactive_photo",
        )
        section = harness._format_companion_scene_snapshot_prompt_section(
            snapshot,
            purpose="proactive_photo",
        )
        self.assertIsInstance(section, PromptSection)
        self.assertEqual("scene.snapshot", section.key)
        self.assertEqual("scene_context", section.source)
        self.assertEqual(formatted, section.content)
        self.assertIn("当前日程", formatted)
        self.assertIn("当前位置：学校", formatted)
        self.assertIn("当前场景：外出", formatted)
        self.assertIn("当天基础穿搭", formatted)
        self.assertIn("分享对象：主人（主要用户）", formatted)

    def test_scene_category_inference_covers_home_outdoor_and_ambiguous_context(self) -> None:
        self.assertEqual(("home", "居家室内"), infer_companion_scene_category("看账号运营内容", "家里"))
        self.assertEqual(("home", "居家室内"), infer_companion_scene_category("在宿舍整理照片", ""))
        self.assertEqual(("outdoor", "外出"), infer_companion_scene_category("去图书馆看书", ""))
        self.assertEqual(("", ""), infer_companion_scene_category("看账号运营内容", ""))

    def test_scene_snapshot_exposes_tentative_schedule_interruption_hint(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        harness._agenda_current_interruption_context = lambda *, now=None: {
            "active": True,
            "confidence": "low",
            "plan_title": "整理今天的笔记",
            "activity_summary": "和用户持续聊天",
        }
        snapshot = harness._build_companion_scene_snapshot(
            harness.data["users"]["10001"],
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )
        assert snapshot["schedule"]["interruption"]["plan_title"] == "整理今天的笔记"
        formatted = harness._format_companion_scene_snapshot(snapshot)
        assert "日程打断线索" in formatted
        assert "不代表计划已完成" in formatted

    def test_scene_snapshot_exposes_effective_calendar_constraints(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        harness._agenda_calendar_snapshot = lambda date_key="", now=None: {
            "date": date_key or "2026-07-19",
            "events": [
                {
                    "title": "暑假",
                    "kind": "period",
                    "occurrence_date": "2026-07-19",
                    "calendar_effective": True,
                },
                {
                    "title": "工作日上学",
                    "kind": "recurrence",
                    "occurrence_date": "2026-07-19",
                    "calendar_effective": False,
                    "overridden_by": "summer",
                },
            ],
            "effective_events": [
                {
                    "title": "暑假",
                    "kind": "period",
                    "occurrence_date": "2026-07-19",
                    "calendar_effective": True,
                }
            ],
            "conflicts": [],
            "applied_exceptions": [],
        }

        snapshot = harness._build_companion_scene_snapshot(
            harness.data["users"]["10001"],
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )

        self.assertEqual("暑假", snapshot["calendar"]["events"][0]["title"])
        formatted = harness._format_companion_scene_snapshot(snapshot)
        self.assertIn("今天日历上的记录：暑假（已确认约束）", formatted)
        self.assertIn("工作日上学（当天不生效）", formatted)

    def test_active_shared_activity_overrides_fixed_schedule(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        harness._external_realtime_activities = {
            "together:date": {
                "user_id": "10001",
                "kind": "shared_call",
                "label": "正在和主要用户约会并保持通话",
                "expires_at": 4102444800,
            }
        }
        harness._external_realtime_continuity = {
            "10001": {
                "user_id": "10001",
                "summary": "小星：我想吃汤圆；主要用户：到店再打给你",
                "public_summary": "正在和主要用户约会",
                "expires_at": 4102444800,
            }
        }
        snapshot = harness._build_companion_scene_snapshot(
            {**harness.data["users"]["10001"], "user_id": "10001"},
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )
        formatted = harness._format_companion_scene_snapshot(snapshot)

        self.assertTrue(snapshot["schedule"]["overridden_by_realtime_activity"])
        self.assertIn("实时共同活动（最高优先级事实）", formatted)
        self.assertIn("原定日程（仅作被打断的背景）", formatted)
        self.assertIn("小星：我想吃汤圆", formatted)

    def test_extension_api_exposes_structured_snapshot(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        api = PrivateCompanionExtensionAPI(harness)

        snapshot = api.get_scene_context("10001")

        self.assertEqual("10001", snapshot["relationship"]["user_id"])
        self.assertEqual("整理完的笔记", snapshot["visual"]["topic"])

    async def test_proactive_photo_prompt_uses_unified_snapshot(self) -> None:
        harness = _PhotoPromptHarness(str(self.outfit_path))
        user = harness.data["users"]["10001"]

        result = await harness._build_photo_scene_prompt(
            user,
            "主人",
            "activity_share",
        )

        self.assertIn("【当前统一情境快照】", harness.captured_prompt)
        self.assertIn("当前位置：学校", harness.captured_prompt)
        self.assertIn("当前场景：外出", harness.captured_prompt)
        self.assertIn("白色衬衫", harness.captured_prompt)
        self.assertIn("只帮助选择自然画面", harness.captured_prompt)
        self.assertIn("当前日程", result["scene_context"])

    async def test_proactive_photo_prompt_keeps_format_snapshot_during_llm_wait(self) -> None:
        harness = _PhotoPromptHarness(str(self.outfit_path))
        harness.photo_generation_prompt_format = "nai"
        original_llm_call = harness._llm_call

        async def switch_format(prompt: str, **kwargs) -> str:
            self.assertIn("提示词格式：nai。", prompt)
            harness.photo_generation_prompt_format = "natural_language"
            return await original_llm_call(prompt, **kwargs)

        harness._llm_call = switch_format
        result = await harness._build_photo_scene_prompt(
            harness.data["users"]["10001"],
            "主人",
            "activity_share",
        )

        self.assertEqual(result["prompt_format"], "nai")
        self.assertEqual(harness.photo_generation_prompt_format, "natural_language")

    async def test_proactive_photo_prompt_injects_enabled_relationship_cards(self) -> None:
        harness = _PhotoPromptHarness(str(self.outfit_path))
        harness.enable_bot_relationship_network = True
        harness.bot_relationship_cards = [
            "小林 || 高中同学兼死党 || 齐肩短发，戴黑框眼镜，常穿灰色连帽卫衣",
            "小林 || 重复卡片 || 不应重复注入",
        ]

        await harness._build_photo_scene_prompt(
            harness.data["users"]["10001"],
            "主人",
            "activity_share",
        )

        self.assertIn("【Bot 关系网】", harness.captured_prompt)
        self.assertIn("角色：小林；与Bot的关系：高中同学兼死党", harness.captured_prompt)
        self.assertIn("齐肩短发", harness.captured_prompt)
        self.assertIn("不能替代人物参考图", harness.captured_prompt)
        self.assertIn("不得让关系卡人物本人", harness.captured_prompt)
        self.assertIn("禁止合影、合照、双人/多人同框", harness.captured_prompt)
        self.assertNotIn("最多挑选一位入镜", harness.captured_prompt)

    def test_dialogue_outfit_overrides_daily_baseline_for_owner_only(self) -> None:
        harness = _SceneHarness(str(self.outfit_path))
        harness.data["dialogue_outfit_override"] = {
            "source_user_id": "10001",
            "instruction": "换一套JK出门",
        }

        def override_getter(*, user_id="", **_kwargs):
            snapshot = harness.data["dialogue_outfit_override"]
            return dict(snapshot) if user_id == snapshot["source_user_id"] else {}

        harness._current_dialogue_outfit_override = override_getter
        owner_snapshot = harness._build_companion_scene_snapshot(
            {**harness.data["users"]["10001"], "user_id": "10001"},
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )
        self.assertEqual("dialogue_override", owner_snapshot["outfit"]["source"])
        self.assertFalse(owner_snapshot["outfit"]["available"])
        self.assertIn("JK", owner_snapshot["outfit"]["description"])
        formatted = harness._format_companion_scene_snapshot(owner_snapshot, purpose="selfie_scene")
        self.assertIn("对话最新服装：换一套JK出门", formatted)
        self.assertNotIn("今日穿搭：", formatted)

        friend_snapshot = harness._build_companion_scene_snapshot(
            {"user_id": "20002", "relationship_role": "friend", "nickname": "访客"},
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )
        self.assertEqual("daily_baseline", friend_snapshot["outfit"]["source"])
        self.assertNotIn("JK", friend_snapshot["outfit"]["description"])

        group_snapshot = harness._build_companion_scene_snapshot(
            {**harness.data["users"]["10001"], "user_id": "10001"},
            include_dialogue_outfit=False,
            now=datetime(2026, 7, 19, 16, 20, tzinfo=timezone.utc),
        )
        self.assertEqual("daily_baseline", group_snapshot["outfit"]["source"])
        self.assertNotIn("JK", group_snapshot["outfit"]["description"])


if __name__ == "__main__":
    unittest.main()
