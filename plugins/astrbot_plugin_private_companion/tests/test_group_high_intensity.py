"""The opt-in group closeout is a pure, replay-safe window calculation."""

from datetime import datetime, timedelta, timezone
import unittest

from companion_v2.conversation import group_high_intensity


AT = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class GroupHighIntensityTests(unittest.TestCase):
    def test_threshold_burst_closes_only_non_direct_candidates(self):
        rounds = (AT - timedelta(seconds=40), AT - timedelta(seconds=20), AT)
        state = group_high_intensity.state(
            rounds, AT, window_seconds=60, threshold=3, cooldown_seconds=150)

        self.assertTrue(state["active"])
        self.assertEqual(state["recent_wakeups"], 3)
        self.assertFalse(group_high_intensity.allows("interest", state))
        self.assertFalse(group_high_intensity.allows("cold_group", state))
        self.assertTrue(group_high_intensity.allows("mention", state))
        self.assertTrue(group_high_intensity.allows("quoted_reply", state))

    def test_cooldown_expires_and_missing_rounds_stay_open(self):
        rounds = (AT - timedelta(seconds=40), AT - timedelta(seconds=20), AT)
        self.assertFalse(group_high_intensity.state(
            rounds, AT + timedelta(seconds=151), window_seconds=60, threshold=3,
            cooldown_seconds=150)["active"])
        self.assertFalse(group_high_intensity.state(
            (), AT, window_seconds=60, threshold=3, cooldown_seconds=150)["active"])

    def test_a_burst_outside_the_window_does_not_trigger(self):
        rounds = (AT - timedelta(seconds=100), AT - timedelta(seconds=80), AT)
        self.assertFalse(group_high_intensity.state(
            rounds, AT, window_seconds=60, threshold=3, cooldown_seconds=150)["active"])

    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            group_high_intensity.state((), AT, window_seconds=0, threshold=3, cooldown_seconds=1)
        with self.assertRaises(ValueError):
            group_high_intensity.state((), AT, window_seconds=60, threshold=1, cooldown_seconds=1)


if __name__ == "__main__":
    unittest.main()
