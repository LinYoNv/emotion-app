# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class _ShouldSendHarness(ProactiveEngineMixin):
    def __init__(self) -> None:
        self.enable_proactive_message_review = False
        self.enable_creative_writing = True
        self.now = 1_000_000.0
        self.data = {"proactive_candidate_pool": []}
        self.impulse_value = 0.8
        self.window_phase = "best"
        self.timeliness = "routine"
        self.recent_activity_at = 0.0
        self.idle_minutes = 60
        self.semantic = {
            "score": 0.8,
            "pressure": 0.2,
            "risk": 0.0,
            "blocker": False,
            "note": "具体生活片段",
        }
        self.persona = {"score": 0.8, "blocker": False, "note": "符合人格"}
        self.inner = {"score": 0.8, "detail": "自然想分享"}
        self.repeated = False
        self.daypart_cap = False
        self.defer_calls: list[dict] = []
        self.scheduled: list[tuple[float, tuple[float, float] | None]] = []

    @staticmethod
    def _recover_stale_proactive_sending(_user) -> None:
        return None

    @staticmethod
    def _user_enabled_for_proactive(_user_id, _user) -> bool:
        return True

    @staticmethod
    def _proactive_generation_disabled(_user=None) -> bool:
        return False

    @staticmethod
    def _effective_user_daily_limit(_user) -> int:
        return 8

    @staticmethod
    def _simulation_active(_user) -> bool:
        return False

    @staticmethod
    def _has_due_llm_timer(_user, *, now) -> bool:
        return False

    def _planned_proactive_timeliness_level(self, _user) -> str:
        return self.timeliness

    @staticmethod
    def _proactive_rest_block_until(*_args, **_kwargs) -> float:
        return 0.0

    @staticmethod
    def _is_quiet_time() -> bool:
        return False

    @staticmethod
    def _current_relationship_gate_mode(_user, *, now) -> str:
        return ""

    @staticmethod
    def _current_emotion_gate_mode(_user, *, now) -> str:
        return ""

    def _planned_impulse_value(self, _user, *, now=None) -> float:
        return self.impulse_value

    def _planned_impulse_window_phase(self, _user, *, now=None) -> tuple[str, str]:
        return self.window_phase, "test window"

    @staticmethod
    def _promote_earlier_daily_greeting_event(_user, *, now) -> bool:
        return False

    @staticmethod
    def _in_llm_timer_silence_window(_user, *, now) -> bool:
        return False

    def _ensure_planned_proactive_delivery_state(self, _user, *, now=None) -> dict:
        return {
            "freshness": "contextual",
            "best_until_at": self.now + 3600,
        }

    @staticmethod
    def _is_proactive_plan_stale(_user, *, now) -> bool:
        return False

    def _proactive_inner_readiness(self, _user, *, now=None) -> dict:
        return dict(self.inner)

    @staticmethod
    def _unverified_social_relay_plan_reason(*_args, **_kwargs) -> str:
        return ""

    @staticmethod
    def _reset_daily_counter_if_needed(_user) -> None:
        return None

    @staticmethod
    def _proactive_daily_limit_is_unlimited(_value) -> bool:
        return False

    def _effective_user_idle_minutes(self, _user) -> int:
        return self.idle_minutes

    def _latest_private_user_activity_ts(self, _user) -> float:
        return self.recent_activity_at

    @staticmethod
    def _post_goodnight_group_activity_is_fresh(_user, *, now) -> bool:
        return False

    @staticmethod
    def _effective_min_interval_seconds(_user) -> int:
        return 0

    @staticmethod
    def _private_user_role(_user, _user_id="") -> str:
        return "owner"

    @staticmethod
    def _friend_can_receive_proactive_reason(_user, _reason, _action="") -> bool:
        return True

    def _planned_proactive_semantics(self, _user) -> dict:
        return dict(self.semantic)

    def _planned_proactive_persona_alignment(self, _user, *, now=None) -> dict:
        return dict(self.persona)

    @staticmethod
    def _is_reason_allowed_now(_reason, user=None) -> bool:
        return True

    @staticmethod
    def _action_is_available(_action, _user=None) -> bool:
        return True

    def _planned_proactive_recently_repeated(self, _user) -> bool:
        return self.repeated

    def _planned_event_exceeds_daypart_cap(self, _user, _reason, _scheduled_at) -> bool:
        return self.daypart_cap

    @staticmethod
    def _proactive_daypart_bucket_for_timestamp(_timestamp) -> str:
        return "daytime"

    @staticmethod
    def _friend_proactive_spread_delay_hours(_user, *, now):
        return None

    def _defer_or_replace_planned_impulse(self, _user, **kwargs) -> bool:
        self.defer_calls.append(dict(kwargs))
        return False

    def _schedule_next_proactive(self, _user, *, now, delay_hours=None) -> None:
        self.scheduled.append((now, delay_hours))


class _RenderBudgetHarness(ProactiveEngineMixin):
    default_nickname = "用户"
    enable_proactive_message_review = False

    def __init__(self) -> None:
        self._execute_proactive_action = AsyncMock(
            side_effect=AssertionError("token hard limit must stop before executing the action")
        )

    @staticmethod
    def _has_due_llm_timer(_user) -> bool:
        return False

    @staticmethod
    def _is_reason_allowed_now(_reason, user=None) -> bool:
        return True

    @staticmethod
    def _should_use_name_only_opener(*_args, **_kwargs) -> bool:
        return False

    @staticmethod
    def _llm_daily_budget_remaining() -> int:
        return 0


def _due_user(now: float, **updates) -> dict:
    user = {
        "user_id": "10001",
        "enabled": True,
        "umo": "default:FriendMessage:10001",
        "next_proactive_at": now - 1,
        "planned_proactive_reason": "state_share",
        "planned_proactive_action": "message",
        "planned_proactive_source": "random",
        "planned_proactive_topic": "桌边刚收好的几支笔",
        "planned_proactive_motive": "顺手分享眼前的小事",
        "planned_proactive_impulse_id": "impulse-1",
        "planned_proactive_window_start_at": now - 300,
        "planned_proactive_best_until_at": now + 1800,
        "planned_proactive_expire_at": now + 3600,
        "sent_today": 0,
        "last_sent": 0,
        "ignored_streak": 0,
    }
    user.update(updates)
    return user


class ProactiveGateDecouplingTests(unittest.IsolatedAsyncioTestCase):
    def test_soft_quality_signals_do_not_block_before_disabled_final_review(self) -> None:
        harness = _ShouldSendHarness()
        harness.impulse_value = 0.2
        harness.inner = {"score": 0.1, "detail": "表达温度偏低"}
        harness.semantic = {
            "score": 0.2,
            "pressure": 0.9,
            "risk": 0.1,
            "blocker": False,
            "note": "由头偏弱",
        }
        harness.persona = {"score": 0.1, "blocker": False, "note": "贴合度一般"}
        user = _due_user(harness.now, ignored_streak=3)

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(user)

        self.assertTrue(allowed)
        self.assertEqual("ok", reason)
        self.assertEqual([], harness.defer_calls)

    def test_low_value_tail_window_is_replaced_before_final_review(self) -> None:
        harness = _ShouldSendHarness()
        harness.impulse_value = 0.2
        harness.window_phase = "tail"

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertIn("低价值念头已过最佳窗口", reason)
        self.assertTrue(harness.defer_calls[0]["block_current"])

    def test_recent_activity_defers_high_value_impulse_without_blocking_it(self) -> None:
        harness = _ShouldSendHarness()
        harness.recent_activity_at = harness.now - 60
        harness.impulse_value = 0.8

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertEqual("用户刚活跃过", reason)
        self.assertFalse(harness.defer_calls[0]["block_current"])

    def test_recent_activity_replaces_low_value_impulse(self) -> None:
        harness = _ShouldSendHarness()
        harness.recent_activity_at = harness.now - 60
        harness.impulse_value = 0.4

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertIn("用户刚活跃过", reason)
        self.assertTrue(harness.defer_calls[0]["block_current"])

    def test_semantic_hard_risk_remains_blocked_when_final_review_is_disabled(self) -> None:
        harness = _ShouldSendHarness()
        harness.semantic = {
            "score": 0.9,
            "pressure": 0.2,
            "risk": 0.8,
            "blocker": True,
            "note": "明确隐私边界冲突",
        }

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertIn("候选语义不够自然", reason)
        self.assertTrue(harness.defer_calls[0]["block_current"])

    def test_persona_blocker_remains_blocked_when_final_review_is_disabled(self) -> None:
        harness = _ShouldSendHarness()
        harness.persona = {
            "score": 0.9,
            "blocker": True,
            "note": "明确世界观冲突",
        }

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertIn("人格/世界观贴合度不足", reason)
        self.assertTrue(harness.defer_calls[0]["block_current"])

    def test_real_duplicate_remains_blocked_when_final_review_is_disabled(self) -> None:
        harness = _ShouldSendHarness()
        harness.repeated = True

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertEqual("近期主动主题过于相似", reason)
        self.assertTrue(harness.defer_calls[0]["block_current"])

    def test_daily_cap_remains_a_hard_gate_when_final_review_is_disabled(self) -> None:
        harness = _ShouldSendHarness()

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now, sent_today=8))

        self.assertFalse(allowed)
        self.assertEqual("已达每日上限", reason)
        self.assertEqual([], harness.defer_calls)

    def test_insomnia_reserved_slot_can_cross_normal_daily_cap_once(self) -> None:
        harness = _ShouldSendHarness()
        harness._can_send_insomnia_night_message = lambda _user, **_kwargs: True
        user = _due_user(
            harness.now,
            sent_today=8,
            planned_proactive_reason="insomnia_night",
            planned_proactive_source="night_care",
        )

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(user)

        self.assertTrue(allowed)
        self.assertEqual("ok", reason)

    def test_daypart_cap_defers_without_blocking_current_impulse(self) -> None:
        harness = _ShouldSendHarness()
        harness.daypart_cap = True

        with patch("astrbot_plugin_private_companion.proactive_engine._now_ts", return_value=harness.now):
            allowed, reason = harness._should_send(_due_user(harness.now))

        self.assertFalse(allowed)
        self.assertIn("当前时段主动已足够", reason)
        self.assertFalse(harness.defer_calls[0]["block_current"])

    async def test_token_hard_limit_stops_render_before_action_or_final_review(self) -> None:
        harness = _RenderBudgetHarness()
        user = {
            "nickname": "阿青",
            "planned_proactive_reason": "state_share",
            "planned_proactive_action": "message",
            "planned_proactive_source": "random",
            "planned_proactive_motive": "顺手分享眼前的小事",
        }

        reason, text, image_path, components, summary, action = await harness._render_message(user)

        self.assertEqual("state_share", reason)
        self.assertEqual("", text)
        self.assertEqual("", image_path)
        self.assertEqual([], components)
        self.assertEqual("Token 硬限额已耗尽", summary)
        self.assertEqual("message", action)
        self.assertIn("Token 硬限额已耗尽", user["_proactive_render_failure_stage"])
        harness._execute_proactive_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
