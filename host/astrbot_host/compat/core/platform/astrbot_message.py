# -*- coding: utf-8 -*-
"""`astrbot.core.platform.astrbot_message` 兼容层 —— 平台消息对象。

⭐⭐ **照真 AstrBot `core/platform/astrbot_message.py` 逐条移植**（该模块本身
   就是纯标准库 dataclass，没有重依赖 —— 这是**可以直接全量照抄**的好例子）。

实测依赖：`AstrBotMessage` 15 文件 / `MessageMember` 15 文件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from astrbot_host.compat.core.message.components import BaseMessageComponent
from astrbot_host.compat.core.platform.message_type import MessageType


@dataclass
class MessageMember:
    """消息发送者（照真实现）。"""

    user_id: str
    nickname: str | None = None

    def __str__(self) -> str:
        return (
            f"User ID: {self.user_id},"
            f"Nickname: {self.nickname if self.nickname else 'N/A'}"
        )


@dataclass
class Group:
    """群组（照真实现 —— 单用户场景通常为 None，但类必须存在）。"""

    group_id: str
    """群号"""
    group_name: str | None = None
    """群名称"""
    group_avatar: str | None = None
    """群头像"""
    group_owner: str | None = None
    """群主 id"""
    group_admins: list[str] | None = None
    """群管理员 id"""
    members: list[MessageMember] | None = None
    """所有群成员"""

    def __str__(self) -> str:
        return (
            f"Group ID: {self.group_id}\n"
            f"Name: {self.group_name if self.group_name else 'N/A'}\n"
            f"Avatar: {self.group_avatar if self.group_avatar else 'N/A'}\n"
            f"Owner ID: {self.group_owner if self.group_owner else 'N/A'}\n"
            f"Admin IDs: {self.group_admins if self.group_admins else 'N/A'}\n"
            f"Members Len: {len(self.members) if self.members else 0}\n"
            f"First Member: {self.members[0] if self.members else 'N/A'}\n"
        )


class AstrBotMessage:
    """一条平台消息（照真实现 —— 普通类，`__init__` 只设默认值）。

    ⚠️ 真实现**不声明** `type`/`self_id` 等为 dataclass 字段，
       而是在 `__init__` 里只设 `timestamp` 与 `group`，
       其余靠使用方赋值 —— 照抄这个行为（改成强制字段会让插件构造失败）。
    """

    type: MessageType
    self_id: str
    session_id: str
    message_id: str
    group: Group | None
    sender: MessageMember
    message: list[BaseMessageComponent]
    message_str: str
    raw_message: object
    timestamp: int

    def __init__(self) -> None:
        self.timestamp = int(time.time())
        self.group = None

    def __str__(self) -> str:
        return str(self.__dict__)

    @property
    def group_id(self) -> str:
        """群号（私聊为空，照真实现）。"""
        if self.group:
            return self.group.group_id
        return ""

    @group_id.setter
    def group_id(self, value: str | None) -> None:
        if value:
            if self.group:
                self.group.group_id = value
            else:
                self.group = Group(group_id=value)
        else:
            self.group = None


__all__ = ["AstrBotMessage", "Group", "MessageMember"]
