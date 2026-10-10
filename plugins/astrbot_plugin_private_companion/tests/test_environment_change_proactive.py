# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
import unittest
from copy import deepcopy

from astrbot_plugin_private_companion.daily_state import DailyStateMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class EnvironmentChangeHarness(DailyStateMixin):
    enable_weather_context = True
    enable_environment_change_proactive = True
    environment_change_check_minutes = 10
    environment_change_cooldown_minutes = 90

    def __init__(self) -> None:
        self._data_lock = asyncio.Lock()
        self.data = {
            "daily_weather": {
                "date": "2026-07-12",
                "prompt": "当前天气 晴，约 28°C。",
                "source": "test",
                "fetched_ts": 1,
            },
            "environment_change_awareness": {},
        }
        self.weather_results: list[dict] = []
        self.offered: list[dict] = []

    async def _ensure_weather_context(self, force: bool = False) -> dict:
        self.assert_force = force
        result = deepcopy(self.weather_results.pop(0))
        self.data["daily_weather"] = result
        return result

    def _queue_environment_change_candidates_locked(self, change: dict, *, now: float) -> int:
        self.offered.append(deepcopy(change))
        return 1

    def _schedule_data_save(self, **_kwargs) -> None:
        return None

    def _save_data_sync(self, **_kwargs) -> None:
        return None


class EnvironmentSemanticHarness(ProactiveEngineMixin):
    enable_maslow_motivation_experiment = False

    @staticmethod
    def _private_user_role(_user) -> str:
        return "owner"

    @staticmethod
    def _is_vague_seek_user_motive(*_args, **_kwargs) -> bool:
        return False

    @staticmethod
    def _unverified_social_relay_plan_reason(*_args, **_kwargs) -> bool:
        return False

    @staticmethod
    def _friend_can_receive_proactive_reason(*_args, **_kwargs) -> bool:
        return True

    @staticmethod
    def _proactive_text_is_intimate(*_args, **_kwargs) -> bool:
        return False


class EnvironmentChangeProactiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = EnvironmentChangeHarness()

    @staticmethod
    def _weather(text: str, fetched_ts: float = 2) -> dict:
        return {
            "date": "2026-07-12",
            "prompt": text,
            "source": "test",
            "fetched_ts": fetched_ts,
        }

    async def test_first_refresh_only_builds_baseline_then_rain_is_immediate_event(self) -> None:
        self.harness.weather_results = [
            self._weather("当前天气 晴，约 28°C。", 2),
            self._weather("当前天气 雷阵雨，约 24°C。", 3),
        ]

        await self.harness._maybe_refresh_environment_change()
        self.assertTrue(self.harness.assert_force)
        self.assertEqual(self.harness.offered, [])

        self.harness.data["environment_change_awareness"]["next_check_at"] = 0
        await self.harness._maybe_refresh_environment_change()

        self.assertEqual(len(self.harness.offered), 1)
        self.assertEqual(self.harness.offered[0]["kind"], "weather_to_thunder")
        self.assertGreaterEqual(self.harness.offered[0]["score"], 90)

    async def test_unchanged_weather_does_not_offer_candidate(self) -> None:
        self.harness.data["environment_change_awareness"] = {"initialized": True, "next_check_at": 0}
        self.harness.weather_results = [self._weather("当前天气 晴，约 28°C。", 3)]

        await self.harness._maybe_refresh_environment_change()

        self.assertEqual(self.harness.offered, [])

    async def test_temperature_jump_is_detected_without_category_change(self) -> None:
        change = self.harness._detect_environment_weather_change(
            self._weather("当前天气 多云，约 18°C。"),
            self._weather("当前天气 多云，约 25°C。"),
        )

        self.assertEqual(change["kind"], "temperature_jump")
        self.assertEqual(change["temperature_delta"], 7.0)
        self.assertIn("升高", change["topic"])

    async def test_rain_stopping_is_detected(self) -> None:
        change = self.harness._detect_environment_weather_change(
            self._weather("当前天气 小雨，约 20°C。"),
            self._weather("当前天气 多云，约 20°C。"),
        )

        self.assertEqual(change["kind"], "precipitation_stopped")
        self.assertIn("停", change["topic"])

    async def test_different_change_within_cooldown_is_blocked_but_urgent_escapes(self) -> None:
        # 回归：旧代码 cooldown 只对同指纹去重，不同变化（雨停/下雨/雨势变大）指纹不同，
        # 30-40 分钟就 offer 一条（实测 08-14 从 11:53 到 16:13 连发 6 条），360 分钟设置
        # 形同虚设。修复后 cooldown 成为距上一次提示的通用间隔：非紧急（score<90）变化在
        # 冷却内被挡；score>=90 的极端变化保留逃生口。
        harness = self.harness
        harness.weather_results = [self._weather("当前天气 晴，约 28°C。", 2)]
        await harness._maybe_refresh_environment_change()
        state = harness.data["environment_change_awareness"]
        # 模拟 34 分钟前刚提示过一次环境变化：超过旧代码写死的 20 分钟窗口（旧代码会放行）、
        # 但在 90 分钟冷却内（修复后应被挡）——正是实测「30-40 分钟连发」的场景。
        prompted_at = time.time() - 34 * 60
        state["last_prompted_at"] = prompted_at
        state["next_check_at"] = 0

        # 非紧急变化：晴→小雨（weather_to_rain，score 86 < 90）→ 冷却内被挡
        harness.weather_results = [self._weather("当前天气 小雨，约 20°C。", 3)]
        await harness._maybe_refresh_environment_change()
        self.assertEqual(harness.offered, [])
        self.assertEqual(state["last_prompted_at"], prompted_at)

        # 紧急变化：小雨→大雨（weather_to_heavy_rain，score 92 >= 90）→ 突破冷却
        state["next_check_at"] = 0
        harness.weather_results = [self._weather("当前天气 大雨，约 20°C。", 4)]
        await harness._maybe_refresh_environment_change()
        self.assertEqual(len(harness.offered), 1)
        self.assertEqual(harness.offered[0]["kind"], "weather_to_heavy_rain")

    async def test_environment_change_is_observation_not_external_share(self) -> None:
        semantics = EnvironmentSemanticHarness()._proactive_candidate_semantics(
            {},
            reason="environment_change",
            action="message",
            motive="刚注意到外界环境发生了明显变化",
            topic="外面开始下雨",
            source="environment_change",
            context={"current": {"text": "当前天气 小雨，约 27°C。"}},
        )

        self.assertEqual(semantics["kind"], "observation")
        self.assertEqual(semantics["anchor_type"], "environment")

    def test_environment_change_prompt_is_canonically_authored(self) -> None:
        user = {
            "planned_environment_change_context": {
                "previous": {"text": "晴，28°C"},
                "current": {"text": "雷阵雨，24°C"},
                "topic": "外面开始下雨",
            }
        }

        section = self.harness._format_environment_change_prompt_section(
            user,
            reason="environment_change",
        )

        self.assertEqual("environment.change", section.key)
        self.assertEqual("刚发生的环境变化", section.title)
        self.assertEqual("daily_state", section.source)
        self.assertNotIn("【刚发生的环境变化】", section.content)
        self.assertTrue(
            self.harness._format_environment_change_prompt(
                user,
                reason="environment_change",
            ).startswith("【刚发生的环境变化】\n")
        )


if __name__ == "__main__":
    unittest.main()
