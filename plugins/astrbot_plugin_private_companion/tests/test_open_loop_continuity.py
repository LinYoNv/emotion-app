# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import unittest

from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


class _OpenLoopHarness(UserMemoryMixin):
    enable_open_loop_tracking = True


class OpenLoopContinuityTests(unittest.TestCase):
    def setUp(self):
        self.harness = _OpenLoopHarness()
        self.now = time.time()

    def test_generic_acknowledgement_does_not_recall_old_topic(self):
        loops = [{
            "text": "提醒我面试结果",
            "status": "待自然延续",
            "created_ts": self.now - 3600,
        }]
        self.assertEqual([], self.harness._select_open_loops_for_prompt(loops, hint="不错呀"))
        self.assertEqual([], self.harness._select_open_loops_for_prompt(loops, hint="之前那个怎么样了"))

    def test_explicit_topic_return_can_recall_matching_loop(self):
        loops = [{
            "text": "提醒我面试结果",
            "status": "待自然延续",
            "created_ts": self.now - 3600,
        }]
        selected = self.harness._select_open_loops_for_prompt(
            loops,
            hint="之前说的面试后来怎么样了",
        )
        self.assertEqual(1, len(selected))
        self.assertEqual("提醒我面试结果", selected[0]["text"])

    def test_prompt_includes_open_loop_timestamp(self):
        loops = [{
            "text": "面试结果",
            "status": "待自然延续",
            "created_ts": self.now - 2 * 3600,
        }]
        user = {"open_loops": loops}
        rendered = self.harness._format_open_loops_for_prompt(user, hint="面试结果后来怎么样了")
        self.assertIn("记录于", rendered)
        self.assertIn("距今约 2 小时", rendered)
        self.assertIn("created_at", loops[0])

    def test_unrelated_completion_does_not_close_newest_loop(self):
        loops = [{
            "text": "面试结果",
            "status": "待自然延续",
            "created_ts": self.now - 3600,
        }]
        self.assertIsNone(self.harness._resolve_matching_open_loop(loops, "好了"))

    def test_explicit_open_loop_persists_readable_timestamp(self):
        user = {"open_loops": []}
        self.harness._update_open_loops_from_message(user, "提醒我面试结果")
        self.assertEqual(1, len(user["open_loops"]))
        item = user["open_loops"][0]
        self.assertGreater(item["created_ts"], 0)
        self.assertRegex(item["created_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
