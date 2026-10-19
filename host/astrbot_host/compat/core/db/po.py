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


# ======================================================================
# 2026-10-19 补：`ConversationV2` / `ProviderStat`（陪伴 7.0.0 需要）
#
# 🔴 与上面的 `Conversation` **不是同一个类**（真实现里也是两个）：
#    `Conversation`    = 纯 dataclass，老 API 用
#    `ConversationV2`  = SQLModel 表，新 API 用（陪伴读它）
#
# 陪伴 `astrbot_host/native.py:21` 顶层导入 `ConversationV2`，
# 并在 `load_persona_snapshot_selection()` 里执行：
#
#     select(ConversationV2.persona_id).where(ConversationV2.conversation_id == cid)
#
# ⇒ **只用到 `conversation_id` 与 `persona_id` 两列**。我们照真实字段定义，
#    但**没有引入 SQLAlchemy 建表逻辑**（宿主不建这张表，见 `get_db()` 适配器）。
#
# ⚠️ sqlmodel 现在是硬依赖了（`astrbot_host/main.py` 链上 import 它）。
#    pyproject 的 dependencies 已加（见同批改动）。
# ======================================================================

try:  # pragma: no cover —— 环境缺 sqlmodel 时给出**可读**的错，不是裸 ImportError
    from sqlmodel import Field, SQLModel
except ImportError as _sqlmodel_error:  # noqa: N816
    raise ImportError(
        "`astrbot.core.db.po` 需要 `sqlmodel`（陪伴 7.0.0 的 ConversationV2 依赖它）。"
        "请执行：.venv/bin/pip install sqlmodel"
    ) from _sqlmodel_error

from datetime import datetime, timezone


class TimestampMixin(SQLModel):
    """创建/更新时间（照真实现 `po.py:12-18`）。"""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationV2(TimestampMixin, SQLModel, table=True):
    """会话表（照真实现 `po.py:67-107` 的字段与表名）。

    ⚠️ **只定义字段，不负责建表** —— 宿主的作用域见 `Context.get_db()`。
    """

    __tablename__: str = "conversations"  # type: ignore[misc]

    inner_conversation_id: int | None = Field(
        default=None, primary_key=True, sa_column_kwargs={"autoincrement": True}
    )
    conversation_id: str = Field(max_length=36, nullable=False, unique=True)
    platform_id: str = Field(nullable=False, default="")
    user_id: str = Field(nullable=False, default="")
    title: str | None = Field(default=None, max_length=255)
    persona_id: str | None = Field(default=None)
    token_usage: int = Field(default=0, nullable=False)


class ProviderStat(TimestampMixin, SQLModel, table=True):
    """Provider 用量统计（照真实现 `po.py:43-64`）。"""

    __tablename__: str = "provider_stats"  # type: ignore[misc]

    id: int | None = Field(
        default=None, primary_key=True, sa_column_kwargs={"autoincrement": True}
    )
    agent_type: str = Field(default="internal", nullable=False)
    status: str = Field(default="completed", nullable=False)
    umo: str = Field(nullable=False, default="")
    conversation_id: str | None = Field(default=None)
    provider_id: str = Field(nullable=False, default="")
    provider_model: str | None = Field(default=None)
    token_input_other: int = Field(default=0, nullable=False)
    token_input_cached: int = Field(default=0, nullable=False)
    token_output: int = Field(default=0, nullable=False)
    start_time: float = Field(default=0.0, nullable=False)
    end_time: float = Field(default=0.0, nullable=False)
