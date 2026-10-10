# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from astrbot_plugin_private_companion.chronotype import ChronotypeMixin
from astrbot_plugin_private_companion.planning import evaluate_daily_plan_quality
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class _ChronotypeHost(ChronotypeMixin):
    def _environment_fromtimestamp(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _now_ts(self) -> float:
        return 2_000_000.0


class ChronotypeProfileTests(unittest.TestCase):
    def setUp(self):
        self.host = _ChronotypeHost()

    def test_default_profile_has_no_shift(self):
        user = {}
        profile = self.host._user_chronotype(user)
        self.assertEqual("default", profile["source"])
        self.assertEqual(0, profile["shift_minutes"])
        self.assertEqual(0, self.host._chronotype_reason_shift(user))

    def test_explicit_tell_overrides_default(self):
        user = {}
        noted = self.host._note_user_chronotype_tell(user, "我一般凌晨两点睡", now=2_000_000.0)
        self.assertTrue(noted)
        profile = self.host._user_chronotype(user)
        self.assertEqual("explicit", profile["source"])
        self.assertEqual(2 * 60, profile["sleep_minute"])

    def test_explicit_wake_tell_drives_shift(self):
        user = {}
        self.assertTrue(self.host._note_user_chronotype_tell(user, "我通常早上十点起", now=2_000_000.0))
        profile = self.host._user_chronotype(user)
        self.assertEqual(10 * 60, profile["wake_minute"])
        self.assertEqual(150, profile["shift_minutes"])

    def test_one_off_statement_is_not_a_tell(self):
        user = {}
        noted = self.host._note_user_chronotype_tell(user, "我今天两点才睡", now=2_000_000.0)
        self.assertFalse(noted)

    def test_histogram_learns_nocturnal_anchor(self):
        user = {}
        base = int(datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc).timestamp())
        # 六天，活跃集中在 14:00–02:00，凌晨 3:00–13:00 是睡眠段。
        for day in range(6):
            for hour in (14, 16, 18, 20, 21, 22, 23, 0, 1):
                ts = base + day * 86400 + hour * 3600
                self.host._note_user_chronotype_activity(user, ts)
        store = user["chronotype_profile"]
        self.assertGreaterEqual(store["learned_wake_minute"], 0)
        wake_hour = store["learned_wake_minute"] // 60
        self.assertIn(wake_hour, {13, 14}, "夜型用户的起床锚点应落在午后")
        profile = self.host._user_chronotype(user)
        self.assertEqual("learned", profile["source"])
        self.assertGreater(profile["shift_minutes"], 0)

    def test_reason_windows_shift_with_profile(self):
        user = {}
        self.host._note_user_chronotype_tell(user, "我通常上午十点起", now=2_000_000.0)
        def _make_harness():
            namespace = SimpleNamespace(
                _normalize_legacy_proactive_text=lambda value, limit=40: value,
                _morning_greeting_window=lambda: (465, 620),
                _chronotype_reason_shift=lambda _user: self.host._chronotype_reason_shift(user),
                _shift_reason_windows=lambda windows, shift: ChronotypeMixin._shift_reason_windows(windows, shift),
            )
            namespace._apply_chronotype_shift_to_windows = (
                lambda windows, target, _ns=namespace: ProactiveEngineMixin._apply_chronotype_shift_to_windows(_ns, windows, target)
            )
            return namespace

        shifted = ProactiveEngineMixin._reason_windows(_make_harness(), "noon_greeting", user)
        base = ProactiveEngineMixin._reason_windows(_make_harness(), "noon_greeting")
        self.assertEqual((725 + 150) % 1440, shifted[0][0])
        self.assertNotEqual(base[0][0], shifted[0][0])

    def test_midnight_span_shift_splits_correctly(self):
        # 20:00-24:00 前移 3 小时 → 跨零点拆两段。
        shifted = ChronotypeMixin._shift_reason_windows([(20 * 60, 24 * 60)], 3 * 60)
        self.assertEqual([(0, 3 * 60), (23 * 60, 24 * 60)], shifted)
        # 常规窗前移不拆分。
        plain = ChronotypeMixin._shift_reason_windows([(12 * 60 + 5, 13 * 60 + 35)], 150)
        self.assertEqual([(14 * 60 + 35, 16 * 60 + 5)], plain)

    def test_hour_weights_blend_with_histogram(self):
        user = {}
        self.host._note_user_chronotype_tell(user, "我一般凌晨两点睡", now=2_000_000.0)
        store = user["chronotype_profile"]
        store["hour_histogram"] = [10] * 24
        store["hour_histogram"][15] = 500
        base = [1.0] * 24
        blended = self.host._chronotype_hour_weights(user, base)
        self.assertGreater(blended[15], blended[0])
        self.assertGreater(blended[15], 1.0)
        self.assertLess(blended[0], 1.0)

    def test_hour_weights_cold_start_returns_base(self):
        user = {}
        blended = self.host._chronotype_hour_weights(user, [0.5] * 24)
        self.assertEqual([0.5] * 24, blended)


class _PlanStub:
    """evaluate_daily_plan_quality 所需的最小宿主。"""

    @staticmethod
    def _parse_hhmm_to_minutes(value):
        text = str(value or "")
        try:
            hour, minute = text.split(":")
            return int(hour) * 60 + int(minute)
        except Exception:
            return None

    @staticmethod
    def _is_sleepy_plan_item(item):
        return "睡" in str(item.get("activity") or "")

    @staticmethod
    def _schedule_text_is_single_meal_action(_text):
        return False

    @staticmethod
    def _plan_has_excess_micro_segments(_items):
        return False

    @staticmethod
    def _plan_has_excess_abstract_segments(_items):
        return False

    @staticmethod
    def _plan_conflicts_with_calendar(_items):
        return False

    @staticmethod
    def _plan_is_too_repetitive(_items):
        return False


def _item(start: str, end: str, activity: str) -> dict:
    return {"time": start, "end": end, "activity": activity}


class PlanQualityChronotypeTests(unittest.TestCase):
    def test_early_sleeper_plan_not_penalized_for_ending_before_dusk(self):
        items = [
            _item("04:30", "06:00", "起床洗漱"),
            _item("06:00", "09:00", "晨间家务"),
            _item("09:00", "12:00", "打理院子"),
            _item("12:00", "14:00", "午餐与午休"),
            _item("14:00", "16:30", "手作"),
            _item("16:30", "18:00", "准备晚饭"),
            _item("18:00", "03:00", "睡觉"),
        ]
        report = evaluate_daily_plan_quality(_PlanStub(), items)
        self.assertNotIn("日程在傍晚前结束，没有覆盖晚间生活", report["issues"])

    def test_standard_plan_still_requires_evening_coverage(self):
        items = [
            _item("07:30", "09:00", "起床洗漱"),
            _item("09:00", "12:00", "上课"),
            _item("12:00", "14:00", "午餐"),
            _item("14:00", "16:00", "自习"),
            _item("16:00", "17:00", "运动"),
        ]
        report = evaluate_daily_plan_quality(_PlanStub(), items)
        self.assertIn("日程在傍晚前结束，没有覆盖晚间生活", report["issues"])


class SleepPhaseGateTests(unittest.TestCase):
    @staticmethod
    def _host(phase: str, enabled: bool = True):
        return SimpleNamespace(
            enable_rest_reply_simulation=enabled,
            _sleep_runtime_state=lambda: {"phase": phase},
            _normalize_legacy_proactive_text=lambda value, limit=40: value,
            _environment_now=lambda: datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc),
            _proactive_sleep_phase_block_reason=ProactiveEngineMixin._proactive_sleep_phase_block_reason,
            _PROACTIVE_SLEEP_BLOCK_PHASES=ProactiveEngineMixin._PROACTIVE_SLEEP_BLOCK_PHASES,
            _PROACTIVE_SLEEP_EXEMPT_REASONS=ProactiveEngineMixin._PROACTIVE_SLEEP_EXEMPT_REASONS,
        )

    def test_sleeping_phase_blocks_casual_reason(self):
        self.assertEqual(
            "sleep_phase:light_sleep",
            ProactiveEngineMixin._proactive_sleep_phase_block_reason(self._host("light_sleep"), "check_in"),
        )

    def test_safety_reasons_exempt_from_sleep_gate(self):
        for reason in ("weather_alert", "health_alert", "memo_note_reminder"):
            self.assertEqual(
                "",
                ProactiveEngineMixin._proactive_sleep_phase_block_reason(self._host("light_sleep"), reason),
            )

    def test_staying_up_and_awake_pass(self):
        for phase in ("awake", "staying_up", "woken", "natural_wake"):
            self.assertEqual(
                "",
                ProactiveEngineMixin._proactive_sleep_phase_block_reason(self._host(phase), "check_in"),
            )

    def test_disabled_rest_simulation_disables_gate(self):
        self.assertEqual(
            "",
            ProactiveEngineMixin._proactive_sleep_phase_block_reason(
                self._host("light_sleep", enabled=False), "check_in"
            ),
        )


if __name__ == "__main__":
    unittest.main()
