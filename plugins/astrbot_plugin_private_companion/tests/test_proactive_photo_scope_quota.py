# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest
from typing import Any

from astrbot_plugin_private_companion.helpers import _today_key
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _AvailabilityHarness(ProactiveEngineMixin):
    def __init__(self) -> None:
        self.enable_photo_text_action = True
        self.photo_generation_backend = "external"
        self.scope_limit = -1
        self.scope_used = 0
        self.legacy_limit = 2
        self.scope_calls: list[dict[str, Any]] = []

    @staticmethod
    def _private_user_role(_user: dict[str, Any]) -> str:
        return "owner"

    @staticmethod
    def _daily_token_soft_limit_should_defer(_kind: str) -> bool:
        return False

    @staticmethod
    def _image_companion_available() -> bool:
        return True

    @staticmethod
    def _image_companion_status() -> dict[str, Any]:
        return {
            "available": True,
            "selected_backend": "external",
            "backends": {"external": True},
        }

    def _effective_user_photo_daily_limit(self, _user: dict[str, Any] | None = None) -> int:
        return self.legacy_limit

    def _photo_generation_scope_quota_left(self, **kwargs: Any) -> int | None:
        self.scope_calls.append(dict(kwargs))
        if self.scope_limit < 0:
            return None
        return max(0, self.scope_limit - self.scope_used)


class _PhotoActionHarness(ProactiveMessageMixin):
    def __init__(self) -> None:
        self.enable_photo_text_action = True
        self._data_lock = asyncio.Lock()
        self.scope_allowed = True
        self.load_defer_note = ""
        self.user_available = True
        self.backend_available = True
        self.image_path = "C:/generated/proactive.png"
        self.workflow_note = "ok"
        self.generate_calls = 0
        self.proactive_attempts: list[str] = []
        self.scope_attempts: list[dict[str, Any]] = []
        self.save_calls = 0

    def _photo_generation_scope_allowed(self, **_kwargs: Any) -> bool:
        return self.scope_allowed

    def _photo_text_load_defer_note(self, *_args: Any, **_kwargs: Any) -> str:
        return self.load_defer_note

    def _photo_text_available(self, user: dict[str, Any] | None = None) -> bool:
        return self.user_available if isinstance(user, dict) else self.backend_available

    async def _build_photo_scene_prompt(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "kind": "text2img",
            "prompt": "a cup on a desk beside a sunlit window",
            "caption": "窗边桌上的一杯饮料。",
            "subject_owner": "scene",
            "use_persona_reference": False,
        }

    @staticmethod
    def _compose_photo_continuity_key(session_key: str, user_id: str) -> str:
        return f"{session_key}:{user_id}"

    async def _generate_photo_image(self, **_kwargs: Any) -> tuple[str, str, str]:
        self.generate_calls += 1
        return "test-backend", self.image_path, self.workflow_note

    def _note_photo_generation_attempt(self, user_id: str, image_path: str = "") -> None:
        self.proactive_attempts.append(f"{user_id}:{image_path}")

    def _note_photo_generation_scope_attempt(self, **kwargs: Any) -> None:
        self.scope_attempts.append(dict(kwargs))

    def _save_data_sync(self, **_kwargs: Any) -> None:
        self.save_calls += 1


class _DailyOutfitHarness(ProactiveMessageMixin):
    def __init__(self) -> None:
        self.enable_photo_text_action = True
        self.proactive_limit = -1
        self.generate_calls = 0
        self.scope_attempts = 0
        self.recorded_error = ""

    def _photo_generation_scope_allowed(self, **_kwargs: Any) -> bool:
        return self.proactive_limit != 0

    @staticmethod
    def _photo_text_available(_user: dict[str, Any] | None = None) -> bool:
        return True

    @staticmethod
    def _daily_outfit_schedule_text() -> str:
        return ""

    @staticmethod
    def _format_weather_for_prompt() -> str:
        return ""

    @staticmethod
    def _select_daily_outfit_profile(**_kwargs: Any) -> dict[str, Any]:
        return {}

    @staticmethod
    def _build_daily_outfit_photo_prompt(
        _diary: dict[str, Any],
        **_kwargs: Any,
    ) -> str:
        return "daily outfit"

    @staticmethod
    def _build_daily_outfit_photo_prompt_sections(
        _diary: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[object, ...]:
        return ()

    async def _generate_photo_image(self, **_kwargs: Any) -> tuple[str, str, str]:
        self.generate_calls += 1
        return "test-backend", "C:/generated/outfit.png", "ok"

    async def _record_daily_outfit_photo_result(
        self,
        _date_key: str,
        image_path: str,
        error: str = "",
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.recorded_error = error
        return {"path": image_path, "error": error}

    def _note_photo_generation_scope_attempt(self, **_kwargs: Any) -> None:
        self.scope_attempts += 1


class ProactivePhotoAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _AvailabilityHarness()
        self.user = {
            "user_id": "owner",
            "photo_generated_day": _today_key(),
            "photo_generated_today": 0,
            "photo_sent_day": _today_key(),
            "photo_sent_today": 0,
        }

    def test_zero_and_exhausted_proactive_scope_block_planning(self) -> None:
        for scope_limit, scope_used in ((0, 0), (2, 2)):
            with self.subTest(scope_limit=scope_limit, scope_used=scope_used):
                self.harness.scope_limit = scope_limit
                self.harness.scope_used = scope_used
                self.assertFalse(self.harness._photo_text_available(self.user))
                self.assertFalse(self.harness._photo_text_planning_available(self.user))

        call = self.harness.scope_calls[-1]
        self.assertTrue(call["proactive"])
        self.assertIs(call["user"], self.user)
        self.assertEqual("owner", call["user_id"])

    def test_scope_and_existing_user_limit_take_the_stricter_result(self) -> None:
        self.harness.scope_limit = 2
        self.harness.scope_used = 1
        self.harness.legacy_limit = 2
        self.assertTrue(self.harness._photo_text_available(self.user))

        self.harness.legacy_limit = 0
        self.assertFalse(self.harness._photo_text_available(self.user))

        self.harness.legacy_limit = 1
        self.user["photo_generated_today"] = 1
        self.assertFalse(self.harness._photo_text_available(self.user))

        self.user["photo_generated_today"] = 0
        self.harness.scope_used = 2
        self.assertFalse(self.harness._photo_text_available(self.user))

    def test_negative_effective_user_limit_is_unlimited(self) -> None:
        self.harness.scope_limit = -1
        self.harness.legacy_limit = -1

        self.assertTrue(self.harness._photo_text_available(self.user))
        self.assertTrue(self.harness._photo_text_planning_available(self.user))


class ProactivePhotoAttemptTests(unittest.IsolatedAsyncioTestCase):
    user = {"user_id": "owner", "umo": "default:FriendMessage:owner"}

    async def test_success_records_both_counters_with_one_save(self) -> None:
        harness = _PhotoActionHarness()

        result = await harness._run_photo_text_action(self.user, "主人", "quiet_care")

        self.assertIn("生成真实图片", result)
        self.assertEqual(1, harness.generate_calls)
        self.assertEqual(["owner:C:/generated/proactive.png"], harness.proactive_attempts)
        self.assertEqual(1, len(harness.scope_attempts))
        self.assertEqual("proactive", harness.scope_attempts[0]["scope"])
        self.assertEqual("owner", harness.scope_attempts[0]["user_id"])
        self.assertEqual(1, harness.save_calls)

    async def test_countable_failure_records_both_counters_with_one_save(self) -> None:
        harness = _PhotoActionHarness()
        harness.image_path = ""
        harness.workflow_note = "HTTP 500 from image provider"

        result = await harness._run_photo_text_action(self.user, "主人", "quiet_care")

        self.assertIn("已计入今日生图尝试额度", result)
        self.assertEqual(["owner:"], harness.proactive_attempts)
        self.assertEqual(1, len(harness.scope_attempts))
        self.assertEqual("proactive", harness.scope_attempts[0]["scope"])
        self.assertEqual(1, harness.save_calls)

    async def test_non_countable_backend_failure_does_not_record_attempt(self) -> None:
        harness = _PhotoActionHarness()
        harness.image_path = ""
        harness.workflow_note = "后端不可用或未配置"

        result = await harness._run_photo_text_action(self.user, "主人", "quiet_care")

        self.assertIn("生图失败", result)
        self.assertEqual([], harness.proactive_attempts)
        self.assertEqual([], harness.scope_attempts)
        self.assertEqual(0, harness.save_calls)

    async def test_preflight_failures_do_not_generate_or_record_attempts(self) -> None:
        cases = (
            ("scope", {"scope_allowed": False}),
            ("load", {"load_defer_note": "电脑高负荷"}),
            ("user_quota", {"user_available": False}),
            ("backend", {"backend_available": False}),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                harness = _PhotoActionHarness()
                for key, value in overrides.items():
                    setattr(harness, key, value)

                await harness._run_photo_text_action(self.user, "主人", "quiet_care")

                self.assertEqual(0, harness.generate_calls)
                self.assertEqual([], harness.proactive_attempts)
                self.assertEqual([], harness.scope_attempts)
                self.assertEqual(0, harness.save_calls)


class DailyOutfitPhotoScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_proactive_scope_blocks_daily_outfit(self) -> None:
        harness = _DailyOutfitHarness()
        harness.proactive_limit = 0

        result = await harness._ensure_daily_outfit_photo_unlocked(today="2026-08-10")

        self.assertEqual(0, harness.generate_calls)
        self.assertIn("不在当前配置", result["error"])
        self.assertEqual(0, harness.scope_attempts)

    async def test_positive_scope_does_not_consume_user_scope_counter(self) -> None:
        harness = _DailyOutfitHarness()
        harness.proactive_limit = 2

        result = await harness._ensure_daily_outfit_photo_unlocked(today="2026-08-10")

        self.assertEqual("C:/generated/outfit.png", result["path"])
        self.assertEqual(1, harness.generate_calls)
        self.assertEqual(0, harness.scope_attempts)


if __name__ == "__main__":
    unittest.main()
