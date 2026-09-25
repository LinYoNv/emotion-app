# -*- coding: utf-8 -*-
"""`astrbot.core.platform` —— 平台抽象入口。

⭐ 依据：真 AstrBot `core/platform/__init__.py`。
"""
from .astr_message_event import AstrMessageEvent, MessageChain
from .message_session import MessageSession
from .message_type import MessageType

__all__ = ["AstrMessageEvent", "MessageChain", "MessageSession", "MessageType"]
