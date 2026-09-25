# -*- coding: utf-8 -*-
"""`astrbot.core.star.star_handler` 兼容层。

⭐ 依据：真 AstrBot `astrbot/core/star/star_handler.py`（实测 16 个文件 import 它）。
真正实现委托给 `astrbot_host.registry`（宿主自己的事件总线），
这里只做**名字与签名的等价映射**。
"""
from __future__ import annotations

from astrbot_host.registry import (
    SUPPORTED_TYPES,
    EventType,
    StarHandlerMetadata,
    StarHandlerRegistry,
    star_handlers_registry,
)

__all__ = [
    "EventType",
    "StarHandlerMetadata",
    "StarHandlerRegistry",
    "star_handlers_registry",
    "SUPPORTED_TYPES",
]
