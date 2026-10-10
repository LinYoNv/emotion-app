# -*- coding: utf-8 -*-
"""内部 ``personality_sync`` 元数据清理的回归测试。

测试目的：
    确保人格同步注释、完整同步块及被截断的同步块不会出现在用户可见的
    出站聊天内容中，同时保留同步块前后的正常回复正文。

使用方法：
    在插件目录的上一级执行：

    python -m pytest -q \
        astrbot_plugin_private_companion/tests/test_personality_sync_cleanup.py

测试原理：
    每项测试都会将具有代表性的模型输出传入内部历史和出站消息管线实际
    使用的两个清理函数，再把处理结果与用户应当看到的正文进行精确比较。
    如果后续改动导致内部同步数据再次泄漏，测试套件会在发布前直接失败。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest

try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ModuleNotFoundError:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("personality-sync-cleanup-test")
    astrbot_module.api = astrbot_api_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

from astrbot_plugin_private_companion.helpers import (
    _strip_internal_message_blocks,
    _strip_outbound_control_blocks,
)


class PersonalitySyncCleanupTests(unittest.TestCase):
    def test_complete_block_and_comment_are_removed(self) -> None:
        raw = """算啦，原谅你了。
<!-- private_companion_personality_sync_v1 -->
<personality_sync>
{"last_interaction_mood": "warm_tease"}
</personality_sync>"""

        self.assertEqual("算啦，原谅你了。", _strip_internal_message_blocks(raw))
        self.assertEqual("算啦，原谅你了。", _strip_outbound_control_blocks(raw))

    def test_truncated_block_is_removed(self) -> None:
        raw = (
            "我还记得呢。\n"
            "<personality_sync>\n"
            '{"last_interaction_mood": "warm_tease"'
        )

        self.assertEqual("我还记得呢。", _strip_internal_message_blocks(raw))
        self.assertEqual("我还记得呢。", _strip_outbound_control_blocks(raw))

    def test_case_and_spacing_variants_are_removed(self) -> None:
        raw = "可见正文< PERSONALITY_SYNC >secret< / PERSONALITY_SYNC >"

        self.assertEqual("可见正文", _strip_outbound_control_blocks(raw))

    def test_future_marker_and_orphan_closing_tag_are_removed(self) -> None:
        raw = (
            "前半句<!-- private_companion_personality_sync_v2 -->"
            '<personality_sync mode="delta">{"focus":"reply"}</personality_sync>'
            "后半句</ PERSONALITY_SYNC >"
        )

        self.assertEqual("前半句后半句", _strip_internal_message_blocks(raw))
        self.assertEqual("前半句后半句", _strip_outbound_control_blocks(raw))

    def test_truncated_comment_is_removed_without_leaking_metadata(self) -> None:
        raw = "可见正文\n<!-- private_companion_personality_sync_v1\n{\"focus\":\"reply\"}"

        self.assertEqual("可见正文", _strip_internal_message_blocks(raw))
        self.assertEqual("可见正文", _strip_outbound_control_blocks(raw))


if __name__ == "__main__":
    unittest.main()
