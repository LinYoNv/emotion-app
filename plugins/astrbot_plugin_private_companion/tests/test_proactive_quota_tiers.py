# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.proactive import ProactiveMixin


class _QuotaHarness(ProactiveMixin):
    max_daily_messages = 8
    idle_minutes = 60
    min_interval_minutes = 120
    greeting_idle_minutes = 30
    proactive_unanswered_slowdown_start = 1
    proactive_unanswered_max_interval_multiplier = 2.2
    friend_unanswered_max_cooldown_hours = 60.0
    enable_custom_relationship_stage_policy = False

    def __init__(self) -> None:
        self.config = {}
        self.data = {"daily_state": {"energy": 70, "conditions": []}}

    @staticmethod
    def _private_user_role(_user, _user_id: str = "") -> str:
        return "friend"

    @staticmethod
    def _get_relevant_important_dates() -> list[dict]:
        return []

    @staticmethod
    def _proactive_intensity_effect(_key: str, default):
        return default


class ProactiveQuotaTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _QuotaHarness()

    def test_quota_values_map_to_five_tiers_and_25_plus_stays_at_tier_five(self) -> None:
        cases = {
            0: 0,
            1: 1,
            3: 1,
            4: 2,
            7: 2,
            8: 3,
            12: 3,
            13: 4,
            18: 4,
            19: 5,
            25: 5,
            30: 5,
            100: 5,
        }
        for quota, expected_tier in cases.items():
            with self.subTest(quota=quota):
                self.assertEqual(expected_tier, self.harness._proactive_quota_tier_for_limit(quota))

    def test_explicit_user_quota_can_exceed_global_default_without_new_strategy_tier(self) -> None:
        user = {"proactive_daily_limit": 30, "ignored_streak": 0}

        self.assertEqual(30, self.harness._effective_user_daily_limit(user))
        policy = self.harness._proactive_quota_policy(user)
        self.assertEqual(5, policy["tier"])
        self.assertEqual(30, policy["quota"])
        self.assertEqual(30.0, self.harness._soft_daily_target(user))

    def test_quota_tier_caps_default_interval_but_preserves_user_override(self) -> None:
        self.assertEqual(120, self.harness._effective_user_min_interval_minutes({"proactive_daily_limit": 3}))
        self.assertEqual(90, self.harness._effective_user_min_interval_minutes({"proactive_daily_limit": 10}))
        self.assertEqual(55, self.harness._effective_user_min_interval_minutes({"proactive_daily_limit": 16}))
        self.assertEqual(35, self.harness._effective_user_min_interval_minutes({"proactive_daily_limit": 25}))
        self.assertEqual(
            70,
            self.harness._effective_user_min_interval_minutes(
                {"proactive_daily_limit": 25, "proactive_min_interval_minutes": 70}
            ),
        )

    def test_highest_tier_does_not_slow_frequency_for_unanswered_messages(self) -> None:
        user = {"proactive_daily_limit": 25, "ignored_streak": 8}

        self.assertEqual(1.0, self.harness._unanswered_interval_multiplier(user))
        self.assertEqual(
            35 * 60,
            self.harness._effective_min_interval_seconds(user, kind="relational"),
        )

    def test_message_purpose_routes_are_independent_from_delivery_modality(self) -> None:
        cases = (
            ({"source": "timer", "reason": "reminder"}, "transactional"),
            ({"source": "pending_followup", "reason": "check_in"}, "continuation"),
            ({"source": "daily_greeting", "reason": "morning_greeting"}, "ritual"),
            ({"source": "habit", "reason": "quiet_care"}, "relational"),
            ({"source": "state", "reason": "state_share"}, "self_life"),
            ({"source": "news_share", "reason": "news_share"}, "content_share"),
            ({"source": "weather_alert", "reason": "weather_alert"}, "safety_event"),
        )
        for values, expected_kind in cases:
            with self.subTest(values=values):
                self.assertEqual(expected_kind, self.harness._proactive_message_kind(**values))


if __name__ == "__main__":
    unittest.main()
