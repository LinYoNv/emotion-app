"""The recall-query trigger stays narrow; mere mentions of the word do not ask."""

import unittest

from astrbot_host.core_reply_perception import _asks_recalled_messages


class RecallQueryDetectionTests(unittest.TestCase):
    def test_question_shapes_are_detected(self):
        for text in ("刚才撤回了什么", "撤回了哪条消息", "防撤回看看",
                     "刚才撤回的内容是什么", "你看到我撤了啥吗"):
            with self.subTest(text=text):
                self.assertTrue(_asks_recalled_messages(text))

    def test_bare_mentions_do_not_trigger_a_transcript(self):
        for text in ("撤回功能怎么关", "我要撤回这个设置", "防撤回开关在哪", ""):
            with self.subTest(text=text):
                self.assertFalse(_asks_recalled_messages(text))


if __name__ == "__main__":
    unittest.main()
