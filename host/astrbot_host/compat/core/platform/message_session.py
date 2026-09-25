# -*- coding: utf-8 -*-
"""`astrbot.core.platform.message_session` 兼容层 —— 会话唯一标识。

⭐ 依据：真 AstrBot `core/platform/message_session.py`（实测 30 行）。

⚠️ **相对真实现省略了什么**：只把 `MessageType` 的 import 换成
   `astrbot_host.compat.` 绝对前缀（项目约定：内部统一写全前缀，更清晰）。
   逻辑**零删减**，包括三个容易"顺手改掉"但必须保留的点：

   * `platform_id` 是 `field(init=False)` 且**在 `__post_init__` 里被赋成
     `platform_name`** —— 所以构造函数**只收 3 个参数**
     （`platform_name` / `message_type` / `session_id`）。
     ⚠️ 千万别为了"支持 platform_id 入参"把它改成可传参：
     真实现的 `from_str` 就是按 3 个位置参数回填的，一改就两边不一致。
   * `MessageSesion = MessageSession` 这个**拼写错误的别名必须留**：
     它是真实现里公开的向后兼容名（老插件按错拼写 import），删掉=老插件崩。
   * `from_str` 用 `split(":", 2)` **切 2 刀**（不是 `split(":")`）——
     这样 `session_id` 里含冒号（如 `a:b:c`）也能正确还原；
     换成不带 maxsplit 会让 umo 解析静默错位。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from astrbot_host.compat.core.platform.message_type import MessageType


@dataclass
class MessageSession:
    """描述一条消息在 AstrBot 中对应的会话的唯一标识。
    如果您需要实例化 MessageSession，请不要给 platform_id 赋值（或者同时给 platform_name 和 platform_id 赋值相同值）。它会在 __post_init__ 中自动设置为 platform_name 的值。
    """

    platform_name: str
    """平台适配器实例的唯一标识符。自 AstrBot v4.0.0 起，该字段实际为 platform_id。"""
    message_type: MessageType
    session_id: str
    platform_id: str = field(init=False)

    def __str__(self) -> str:
        return f"{self.platform_id}:{self.message_type.value}:{self.session_id}"

    def __post_init__(self):
        self.platform_id = self.platform_name

    @staticmethod
    def from_str(session_str: str):
        platform_id, message_type, session_id = session_str.split(":", 2)
        return MessageSession(platform_id, MessageType(message_type), session_id)


MessageSesion = MessageSession  # back compatibility


__all__ = ["MessageSession", "MessageSesion"]
