# -*- coding: utf-8 -*-
"""`astrbot.api.event` —— 事件相关导出面。

⭐ 依据：真 AstrBot `api/event/__init__.py`。
   插件按 `from astrbot.api.event import AstrMessageEvent, MessageChain, filter` import。

⚠️ `filter` 必须是**子模块对象**（不是函数）——
   插件的用法是 `@filter.llm_tool(...)`，即访问子模块的属性。
"""
from astrbot_host.compat.core.platform.astr_message_event import (
    AstrMessageEvent,
    MessageChain,
)

from astrbot_host.compat.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)

from . import filter as filter  # noqa: PLC0414  (`filter` 子模块本身要能 import)

__all__ = ["AstrMessageEvent", "MessageChain", "MessageEventResult",
           "ResultContentType", "filter"]
