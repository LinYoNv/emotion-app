# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptSection,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.daily_state import DailyStateMixin


class _WeatherQueryHarness(DailyStateMixin):
    enable_weather_context = True
    weather_source = "qweather"

    def __init__(self, results: list[dict] | None = None) -> None:
        self.results = list(results or [])
        self.ensure_calls: list[bool] = []
        self.recorded: list[dict] = []

    @staticmethod
    def _private_user_role(user=None, *_args) -> str:
        return str((user or {}).get("relationship_role") or "friend")

    @staticmethod
    def _request_has_managed_prompt_marker(req, marker: str) -> bool:
        return marker in str(getattr(req, "system_prompt", "") or "")

    @staticmethod
    def _append_turn_prompt_fragment_by_position(
        req,
        marker: str,
        section: PromptSection,
        **_kwargs,
    ) -> bool:
        req.prompt = (
            f"{req.prompt}\n\n{marker}\n{render_prompt_sections([section])}".strip()
        )
        return True

    async def _record_request_prompt_fragment(self, _event, **kwargs) -> None:
        self.recorded.append(kwargs)

    async def _ensure_weather_context(self, force: bool = False) -> dict:
        self.ensure_calls.append(force)
        if self.results:
            return self.results.pop(0)
        return {
            "prompt": "暂无天气信息",
            "source": "none",
            "fetched_ts": time.time(),
        }


def _request(text: str):
    return SimpleNamespace(system_prompt="人格", prompt=text)


def _event(text: str):
    return SimpleNamespace(
        message_str=text,
        unified_msg_origin="default:FriendMessage:10001",
    )


class PassiveWeatherQueryContextTests(unittest.IsolatedAsyncioTestCase):
    def test_current_weather_intent_recognizes_natural_queries(self) -> None:
        for text in (
            "今天天气怎么样",
            "现在多少度",
            "外面下雨吗",
            "气温呢",
            "你那边冷不冷",
            "出门需要带伞吗",
        ):
            with self.subTest(text=text):
                self.assertTrue(_WeatherQueryHarness._user_asks_current_weather(text))

    def test_meta_and_forecast_questions_are_not_misclassified_as_current_weather(self) -> None:
        for text in (
            "和风天气 API 怎么配置",
            "天气插件为什么报错",
            "天气接口如何接入",
            "明天天气怎么样",
            "未来一周会下雨吗",
            "你喜欢什么天气",
            "天气变化的原因是什么",
            "我这边现在多少度",
        ):
            with self.subTest(text=text):
                self.assertFalse(_WeatherQueryHarness._user_asks_current_weather(text))

    async def test_successful_query_injects_configured_weather_and_stops_redundant_tools(self) -> None:
        harness = _WeatherQueryHarness(
            [
                {
                    "prompt": "当前天气 晴，约 28°C，体感 30°C，湿度 65%。",
                    "source": "qweather",
                    "location_label": "北京市朝阳区",
                    "fetched_ts": time.time(),
                }
            ]
        )
        req = _request("今天天气怎么样")

        handled = await harness._append_weather_query_context_to_request(
            _event("今天天气怎么样"),
            req,
            current_user={"relationship_role": "owner"},
        )

        self.assertTrue(handled)
        self.assertEqual(harness.ensure_calls, [False])
        self.assertIn("北京市朝阳区", req.prompt)
        self.assertIn("和风天气", req.prompt)
        self.assertIn("不要再调用搜索、浏览器、地图、记忆或其他天气工具", req.prompt)
        self.assertEqual(harness.recorded[0]["metadata"]["获取成功"], True)

    async def test_friend_and_group_weather_context_do_not_expose_configured_location(self) -> None:
        result = {
            "prompt": "当前天气 多云，约 25°C。",
            "source": "qweather",
            "location_label": "主人住所附近",
            "fetched_ts": time.time(),
        }
        for current_user in ({"relationship_role": "friend"}, None):
            with self.subTest(current_user=current_user):
                harness = _WeatherQueryHarness([dict(result)])
                req = _request("天气呢")
                await harness._append_weather_query_context_to_request(
                    _event("天气呢"),
                    req,
                    current_user=current_user,
                )
                self.assertIn("当前天气 多云", req.prompt)
                self.assertNotIn("主人住所附近", req.prompt)

    async def test_stale_failed_cache_retries_once_and_failure_prompt_limits_tool_sprawl(self) -> None:
        harness = _WeatherQueryHarness(
            [
                {
                    "prompt": "暂无天气信息",
                    "source": "none",
                    "fetched_ts": time.time() - 120,
                },
                {
                    "prompt": "暂无天气信息",
                    "source": "none",
                    "fetched_ts": time.time(),
                },
            ]
        )
        req = _request("现在天气怎么样")

        handled = await harness._append_weather_query_context_to_request(
            _event("现在天气怎么样"),
            req,
            current_user={"relationship_role": "owner"},
        )

        self.assertTrue(handled)
        self.assertEqual(harness.ensure_calls, [False, True])
        self.assertIn("本轮没有取得有效实况", req.prompt)
        self.assertIn("最多尝试一次", req.prompt)
        self.assertIn("不要调用搜索、浏览器、地图、记忆或多个工具反复查找", req.prompt)

    async def test_stale_fallback_cache_retries_configured_primary_weather_source(self) -> None:
        harness = _WeatherQueryHarness(
            [
                {
                    "prompt": "当前天气 多云，约 30°C。",
                    "source": "screen_companion",
                    "fetched_ts": time.time() - 120,
                },
                {
                    "prompt": "当前天气 阴，约 29°C。",
                    "source": "qweather",
                    "fetched_ts": time.time(),
                },
            ]
        )
        req = _request("天气怎么样")

        await harness._append_weather_query_context_to_request(
            _event("天气怎么样"),
            req,
            current_user={"relationship_role": "owner"},
        )

        self.assertEqual(harness.ensure_calls, [False, True])
        self.assertIn("当前天气 阴，约 29°C", req.prompt)
        self.assertIn("来源：和风天气", req.prompt)

    async def test_normal_conversation_does_not_fetch_or_inject_weather(self) -> None:
        harness = _WeatherQueryHarness()
        req = _request("今天过得怎么样")

        handled = await harness._append_weather_query_context_to_request(
            _event("今天过得怎么样"),
            req,
            current_user={"relationship_role": "owner"},
        )

        self.assertFalse(handled)
        self.assertEqual(harness.ensure_calls, [])
        self.assertNotIn("private_companion_weather_query", req.prompt)

    async def test_disabled_weather_context_leaves_other_weather_capabilities_available(self) -> None:
        harness = _WeatherQueryHarness()
        harness.enable_weather_context = False
        req = _request("今天天气怎么样")

        handled = await harness._append_weather_query_context_to_request(
            _event("今天天气怎么样"),
            req,
            current_user={"relationship_role": "owner"},
        )

        self.assertFalse(handled)
        self.assertEqual(harness.ensure_calls, [])


if __name__ == "__main__":
    unittest.main()
