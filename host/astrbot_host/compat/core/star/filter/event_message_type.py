# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.event_message_type` 兼容层。

⭐ 依据：真 AstrBot `core/star/filter/event_message_type.py`。

🔴 **两处必须照抄的细节**（实测真源码，容易写错）：
   ① `EventMessageType` 是 `enum.Flag`，且 **`ALL = GROUP | PRIVATE | OTHER`**
      —— 它是**组合值**（位值 7），**不是独立的 `enum.auto()`**。
      写成独立位会让 `&` 比较结果不同（1 vs 7），插件的类型判断会静默错。
   ② `ALL` 因为是组合值，**不出现在 `list(EventMessageType)` 里**（Flag 特性）。
      依赖迭代枚举的代码要注意这一点。
"""
from __future__ import annotations

import enum
from typing import Any

from . import HandlerFilter


class EventMessageType(enum.Flag):
    """消息类型（照真实现 —— `Flag` 且 `ALL` 是组合值）。"""

    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    # ⚠️ 组合值，**不是** enum.auto()（真实现 `event_message_type.py:11`）
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


def _message_type_map() -> dict[Any, EventMessageType]:
    """`MessageType` → `EventMessageType` 映射（照真实现 `MESSAGE_TYPE_2_EVENT_MESSAGE_TYPE`）。

    ⚠️ 延迟构造：避免与本模块的 `MessageType` 产生循环 import。
    """
    from astrbot_host.compat.core.platform.message_type import MessageType

    return {
        MessageType.GROUP_MESSAGE: EventMessageType.GROUP_MESSAGE,
        MessageType.FRIEND_MESSAGE: EventMessageType.PRIVATE_MESSAGE,
        MessageType.OTHER_MESSAGE: EventMessageType.OTHER_MESSAGE,
    }


class EventMessageTypeFilter(HandlerFilter):
    """按消息类型过滤（照真实现）。"""

    def __init__(self, event_message_type: EventMessageType) -> None:
        self.event_message_type = event_message_type

    def filter(self, event: Any, cfg: Any) -> bool:
        """判断事件是否匹配。

        照真实现：用 `event.get_message_type()` 取类型再查映射表比对。
        ⚠️ 取不到类型时**放行**（返回 True）—— 与真实现一致：
           宁可多跑一个 handler，也不要因宿主缺字段而静默丢消息。
        """
        get_type = getattr(event, "get_message_type", None)
        if not callable(get_type):
            return True
        try:
            message_type = get_type()
        except Exception:  # noqa: BLE001
            return True
        mapped = _message_type_map().get(message_type)
        if mapped is None:
            return True
        return bool(self.event_message_type & mapped)


__all__ = ["EventMessageType", "EventMessageTypeFilter"]
