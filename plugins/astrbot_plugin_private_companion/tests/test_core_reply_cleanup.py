"""回复正文只清理“整段一模一样”的自重复，正常语气重复要保持原样。"""

from __future__ import annotations

import unittest

from astrbot_host.core_reply_cleanup import clean_reply_text


class ReplyCleanupTests(unittest.TestCase):
    def test_whole_message_written_twice_becomes_one_copy(self):
        one = "又来了呀…都说了现在不好看嘛，等我收拾一下再考虑。"
        cleaned, removed = clean_reply_text(one + one)
        self.assertEqual(cleaned, one)
        self.assertEqual(removed, 1)

    def test_repeated_paragraph_is_merged_once(self):
        one = "今天有点累，想早点睡。"
        cleaned, removed = clean_reply_text(one + "\n\n" + one)
        self.assertEqual(cleaned, one)
        self.assertEqual(removed, 1)

    def test_trailing_self_reported_retry_line_is_dropped(self):
        cleaned, removed = clean_reply_text("本来想说的话。\n等等，格式错了。重来。")
        self.assertEqual(cleaned, "本来想说的话。")
        self.assertEqual(removed, 1)

    def test_inline_retry_wording_stays_because_it_can_be_dialogue(self):
        text = "等等，格式错了。重来。\n算了，我先说别的。"
        cleaned, removed = clean_reply_text(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(removed, 0)

    def test_natural_repetition_is_untouched(self):
        for text in ("嗯嗯。", "不要不要。", "好呀好呀。", "嘿嘿。嘿嘿。",
                     "今天有点累。\n\n你呢？要不要一起早点睡？"):
            self.assertEqual(clean_reply_text(text), (text, 0), text)

    def test_empty_and_non_text_are_safe(self):
        self.assertEqual(clean_reply_text(""), ("", 0))
        self.assertEqual(clean_reply_text(None), (None, 0))


if __name__ == "__main__":
    unittest.main()
