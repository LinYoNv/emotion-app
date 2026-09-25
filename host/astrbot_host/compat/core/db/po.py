# -*- coding: utf-8 -*-
"""`astrbot.core.db.po` 兼容层 —— 持久化对象（**只取纯数据类**）。

⭐⭐ 依据：真 AstrBot `core/db/po.py`（622 行）。

🔴 **为什么只搬一部分**：真 `po.py` 是一个 **SQLModel/SQLAlchemy ORM 表定义**文件
   （`sqlmodel` + `sqlalchemy` + `deprecated` 依赖）。
   本项目依赖刻意只有 quart/hypercorn/aiosqlite/httpx —— **不为兼容引入 ORM**。

   实测**插件只用到 `Conversation` 一个类**（10 个文件 import 它），
   而真实现里的 `Conversation` **恰好是纯 `@dataclass`**（`po.py:557-580`，
   与 ORM 表 `ConversationV2` 是**两个不同的类**）。
   → 所以这里**逐字照抄** `Conversation` / `Platform` / `Stats` 三个纯数据类。

⚠️ 真实现里 `Platform`/`Stats` 带 `@deprecated` 装饰器（来自 `deprecated` 包）；
   我们**不加装饰器**（不引入该依赖），但保留 docstring 里的弃用说明 ——
   语义影响仅限"不会打出弃用警告"，无功能差异。
"""
from __future__ import annotations

import uuid  # noqa: F401  (真实现模块级 import，保留导入面)
from dataclasses import dataclass, field


@dataclass
class Conversation:
    """LLM 对话（照真实现 `po.py:557-580` 逐字）。

    对于 WebChat，history 存储了包括指令、回复、图片等在内的所有消息。
    对于其他平台的聊天，不存储非 LLM 的回复。
    """

    platform_id: str = ""
    user_id: str = ""
    cid: str = ""
    """对话 ID，uuid 格式字符串"""
    history: str = ""
    """字符串格式的对话列表"""
    title: str | None = ""
    persona_id: str | None = ""
    created_at: int = 0
    updated_at: int = 0
    token_usage: int = 0
    """对话的总 token 数；可能为 0 表示未知"""

    def __repr__(self) -> str:
        # ⚠️ 不给 history 全量打印（可能很大），这也是真实现的使用习惯
        return (
            f"Conversation(cid={self.cid!r}, title={self.title!r}, "
            f"persona_id={self.persona_id!r}, history_len={len(self.history or '')})"
        )


@dataclass
class Platform:
    """平台使用统计数据。

    ⚠️ 真实现标记为废弃（4.0.0，改用 `PlatformStat`）——此处保留兼容。
    """

    name: str
    count: int
    timestamp: int


@dataclass
class Stats:
    """统计（真实现标记为废弃，改用 `get_platform_stats()`）。"""

    platform: list[Platform] = field(default_factory=list)


__all__ = ["Conversation", "Platform", "Stats"]
