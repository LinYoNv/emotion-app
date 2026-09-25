# -*- coding: utf-8 -*-
"""`astrbot.core.platform.message_type` 兼容层 —— 消息类型枚举。

⭐ 依据：真 AstrBot `core/platform/message_type.py`（实测 7 行）。

⚠️ **没有省略任何东西**（真实现全量 7 行照搬）。这是个纯数据模块，
   单独成文件的原因就是插件会 `from astrbot.core.platform.message_type import MessageType`
   单独 import（实测 `image_runtime.py:78`、`event_dispatch.py:52`）。

⚠️ **`Enum` 不是 `Flag`**：值就是字符串（`"GroupMessage"` 等），
   配合 `MessageSession.__str__` 与 `MessageSession.from_str` 做**跨进程持久化**
   （umo 字符串）。成员名与**字符串值**都必须逐字照抄 ——
   插件里 `MessageType("GroupMessage")` 这种按值构造是常见写法，
   改了值等于把已落盘的 umo 全部读不出来。
   注意区别于同目录的 `astrbot.core.star.filter.event_message_type.EventMessageType`
   （那个是 `Flag` 且成员名不同：`GROUP_MESSAGE` / `PRIVATE_MESSAGE`）——
   **两者不是一回事，不要互相替换**。
"""
from __future__ import annotations

from enum import Enum


class MessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"  # 群组形式的消息
    FRIEND_MESSAGE = "FriendMessage"  # 私聊、好友等单聊消息
    OTHER_MESSAGE = "OtherMessage"  # 其他类型的消息，如系统消息等


__all__ = ["MessageType"]
