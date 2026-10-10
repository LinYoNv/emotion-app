# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from astrbot_plugin_private_companion.proactive import ProactiveMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class _RitualHarness(ProactiveMixin, ProactiveEngineMixin):
    allow_insomnia_night_message = True

    def __init__(self) -> None:
        self.data = {"important_dates": []}

    @staticmethod
    def _environment_fromtimestamp(value: float) -> datetime:
        return datetime.fromtimestamp(value)

    @staticmethod
    def _private_user_role(_user, _user_id="") -> str:
        return "owner"

    @staticmethod
    def _latest_private_user_activity_ts(user) -> float:
        return float(user.get("last_activity_at") or 0)

    @staticmethod
    def _photo_text_available(_user=None) -> bool:
        return False

    @staticmethod
    def _effective_user_daily_limit(user) -> int:
        return int(user.get("daily_limit", 2))

    @staticmethod
    def _effective_user_min_interval_minutes(_user) -> int:
        return 60

    def _has_active_insomnia_state(self) -> bool:
        return True

    @staticmethod
    def _quiet_hours_end_timestamp(at_ts: float) -> float:
        return at_ts + 7 * 3600


class MidnightRitualAndInsomniaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _RitualHarness()

    @staticmethod
    def _ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
        return datetime(year, month, day, hour, minute).timestamp()

    def test_birthday_eve_schedules_just_after_midnight(self) -> None:
        now = self._ts(2026, 8, 18, 22, 0)
        user = {
            "birthday_profile": {"month": 8, "day": 19, "calendar": "solar"},
            "birthday_event": {},
            "ignored_streak": 0,
            "last_activity_at": now - 3600,
        }

        with patch("astrbot_plugin_private_companion.proactive_engine.random.randint", side_effect=lambda low, _high: low):
            event = self.harness._pick_birthday_celebration_event(user, now)

        self.assertIsNotNone(event)
        self.assertEqual("birthday_celebration", event["reason"])
        self.assertEqual("2026-08-19", event["date"])
        self.assertEqual("midnight", event["context"]["delivery_timing"])
        self.assertEqual(2026, event["context"]["observance_year"])
        self.assertLess(event["_scheduled_ts"], self._ts(2026, 8, 19, 0, 8))

    def test_missed_birthday_midnight_falls_back_to_daytime(self) -> None:
        now = self._ts(2026, 8, 19, 1, 0)
        user = {
            "birthday_profile": {"month": 8, "day": 19, "calendar": "solar"},
            "birthday_event": {},
            "ignored_streak": 0,
        }

        with patch("astrbot_plugin_private_companion.proactive_engine.random.randint", side_effect=lambda low, _high: low):
            event = self.harness._pick_birthday_celebration_event(user, now)

        self.assertIsNotNone(event)
        self.assertEqual("daytime", event["context"]["delivery_timing"])
        self.assertGreaterEqual(event["_scheduled_ts"], self._ts(2026, 8, 19, 9, 30))

    def test_valentines_eve_midnight_ritual_bypasses_generic_quiet_shift(self) -> None:
        now = self._ts(2027, 2, 13, 22, 0)
        event = self.harness._pick_special_day_greeting_event({}, now=now)

        self.assertIsNotNone(event)
        self.assertEqual("情人节", event["context"]["observance_title"])
        prepared, reason = self.harness._prepare_proactive_candidate_window(
            event,
            reason="special_day_greeting",
            source="special_day_ritual",
            now=now,
        )

        self.assertEqual("", reason)
        self.assertIsNotNone(prepared)
        self.assertEqual(event["_scheduled_ts"], prepared["scheduled_ts"])
        self.assertNotIn("quiet_hours_adjusted", prepared)

    def test_normal_calendar_day_does_not_create_midnight_ritual(self) -> None:
        now = self._ts(2027, 2, 12, 22, 0)
        self.assertIsNone(self.harness._pick_special_day_greeting_event({}, now=now))

    def test_insomnia_has_one_reserved_slot_after_normal_quota(self) -> None:
        now = self._ts(2026, 8, 18, 23, 20)
        user = {"daily_limit": 2, "sent_today": 2, "last_sent": now - 3 * 3600}

        self.assertTrue(self.harness._can_send_insomnia_night_message(user, now=now))
        user["insomnia_night_sent_key"] = self.harness._insomnia_night_key(now)
        self.assertFalse(self.harness._can_send_insomnia_night_message(user, now=now))

    def test_zero_daily_limit_still_disables_insomnia_slot(self) -> None:
        now = self._ts(2026, 8, 18, 23, 20)
        user = {"daily_limit": 0, "sent_today": 0, "last_sent": 0}
        self.assertFalse(self.harness._can_send_insomnia_night_message(user, now=now))

    def test_cross_midnight_hours_share_one_insomnia_receipt_key(self) -> None:
        before_midnight = self._ts(2026, 8, 18, 23, 40)
        after_midnight = self._ts(2026, 8, 19, 2, 10)
        self.assertEqual(
            self.harness._insomnia_night_key(before_midnight),
            self.harness._insomnia_night_key(after_midnight),
        )


if __name__ == "__main__":
    unittest.main()
