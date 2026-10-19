# -*- coding: utf-8 -*-
"""`Context.get_db()` 的兼容实现 —— 让插件能读 AstrBot 会话表的**一个子集**。

⭐⭐ 设计取舍（2026-10-19，陪 7.0.0 需要）

## 为什么以前**不**实现，现在**必须**实现

旧版（≤6.7.0）的插件不走这条路，所以 `Context.get_db()` 当时故意抛
`NotImplementedError`，理由是「宿主不向插件暴露数据库句柄」。那个理由本身是对的。

但 7.0.0 的 `astrbot_host/native.py:84` 这样做：

```python
async with database.get_db() as session:
    result = await session.execute(
        select(ConversationV2.persona_id).where(
            ConversationV2.conversation_id == conversation_id))
```

⇒ 不上它，**「这个会话用哪个人格」永远解析不出来**，而那是陪伴把人设映射到
V2 角色的关键一步。

## 我们到底给了它什么（**最小暴露面，不伪造**）

真 AstrBot 的 `context._db` 是一个 SQLite 库，`conversations` 表里有
`conversation_id / persona_id / content / ...`。

**我们不建那张表**（宿主的数据模型是 `emotion_app/domain/session_repo.py`
的 `Session`，字段与它一一对得上，但存储是 JSON 而非 SQL）。
所以这里返回一个**只支持"按 conversation_id 查 persona_id"的会话对象**：

* `async with database.get_db() as session:` —— 支持
* `await session.execute(stmt)` —— 解析 `select(ConversationV2.persona_id).where(...)`，
  从语句里取出 `conversation_id` 参数，去宿主的 `conversation_manager` 查
* 结果对象支持 `.one_or_none()` / `.scalars()` / `.all()`（插件只用到第一种）

🔴 **不是**一个能跑任意 SQL 的东西。它**只认这一种语句形状**，
别的语句**显式抛 `NotImplementedError` 并说明原因** ——
假装修一个能跑 SQL 的会话，会让插件以为它能读 `content`（对话正文），
那才是真的越界。这也符合项目铁律：「能力缺失要显式返回 unavailable」。

## 与真实现在此处的差异（如实记录）

| 维度 | 真 AstrBot | 这里 |
|---|---|---|
| 底层 | SQLAlchemy ORM → SQLite | 宿主 `conversation_manager`（JSON） |
| 支持语句 | 任意 SQL | **仅** `select(ConversationV2.persona_id).where(conversation_id==X)` |
| 写入 | 支持 | **不支持**（显式报错） |
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")

__all__ = ["HostDbAdapter", "ScalarResult"]


class ScalarResult:
    """`session.execute()` 的返回（只实现插件用到的那几个方法）。

    ⚠️ 插件点的是 `.one_or_none()`（`native.py:86`）。`scalars()` / `all()`
    也补上，因为它们是最常见的 SQLAlchemy 读法，缺了会让"看起来能用"
    的代码在运行时炸。
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def one_or_none(self) -> Any:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            # 真 SQLAlchemy 在这里抛 MultipleResultsFound —— 照做，
            # 否则"取第一条"会让一条错误查询永久静默
            raise ValueError(
                f"one_or_none() 期望 0 或 1 行，实际 {len(self._rows)} 行"
            )
        row = self._rows[0]
        # `select(单列)` 的 SQLAlchemy 结果是 Row，插件取 [0] 或直接解包
        return row

    def scalars(self) -> "ScalarResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Session:
    """一次 `async with` 的会话（见模块 docstring）。"""

    def __init__(self, adapter: "HostDbAdapter") -> None:
        self._adapter = adapter

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, statement: Any) -> ScalarResult:
        return await self._adapter._execute(statement)

    # 写入一律不支持（见模块 docstring 的"最小暴露面"）
    async def commit(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "宿主不通过 get_db() 接受写入：插件改数据请走领域端口"
            "（`ConversationManagerPort` / 宿主 API）。"
        )

    def add(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "宿主不通过 get_db() 接受写入：插件改数据请走领域端口。"
        )


class HostDbAdapter:
    """`context._db` 的替身（照真实现暴露 `get_db()`）。

    Args:
        conversation_manager: 宿主的会话管理器端口（`get_conversation`）。
    """

    def __init__(self, conversation_manager: Any = None) -> None:
        self._manager = conversation_manager

    #: 真实现有 `inited`（插件在慢查询日志里读它，`native.py:97`）
    @property
    def inited(self) -> bool:
        return self._manager is not None

    def get_db(self) -> _Session:
        """照真实现的同步方法形态 —— 返回**异步上下文管理器**。"""
        return _Session(self)

    async def _execute(self, statement: Any) -> ScalarResult:
        """只解析"查 ConversationV2.persona_id"这一种语句（见模块 docstring）。"""
        columns, params = _parse_statement(statement)
        if columns != ("persona_id",):
            raise NotImplementedError(
                "宿主只支持 `select(ConversationV2.persona_id)` 这一种查询；"
                f"实际请求的列：{columns or '（无法识别）'}。"
                "其它数据请走领域端口。"
            )
        conversation_id = params.get("conversation_id")
        if not conversation_id:
            raise NotImplementedError(
                "宿主会话查询必须带 `ConversationV2.conversation_id == <值>` 条件。"
            )
        if self._manager is None:
            # 没有会话端口 = 宿主没装配 → **显式报**，不返回空结果假装"没找到"
            raise RuntimeError(
                "宿主未装配 `conversation_manager`，无法解析会话人格。"
                "请在 `HostPorts(conversation_manager=...)` 里注入它。"
            )
        umo = params.get("umo") or ""
        conv = None
        try:
            # 真实现的查询是**全局按 conversation_id**（不带 umo）。
            # 宿主的端口要求 umo，所以先按 umo 查；拿不到再**扫描所有 umo**。
            if umo:
                conv = await self._manager.get_conversation(umo, conversation_id)
            if conv is None:
                conv = await self._find_anywhere(conversation_id)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "读取会话人格失败：conversation_id=%s error_type=%s %s",
                conversation_id, type(error).__name__, error,
            )
            raise
        if conv is None:
            return ScalarResult([])
        persona_id = getattr(conv, "persona_id", None)
        # ⚠️ `select(单列)` 的 SQLAlchemy 返回是 Row；插件 `row` 之后会
        #    按 `row[0]` 或属性读。这里返回**元组**，两种读法都支持。
        return ScalarResult([(persona_id,)])

    async def _find_anywhere(self, conversation_id: str) -> Any:
        """在所有 umo 下找这个 conversation（宿主的端口按 umo 查）。"""
        manager = self._manager
        finder = getattr(manager, "find_conversation", None)
        if callable(finder):
            return await finder(conversation_id)
        # 没有专用方法 → 用宿主的"当前 umo"记录逐个试（见 conversation_manager）
        umos = getattr(manager, "known_umos", None)
        if callable(umos):
            for umo in (await umos()) or ():
                conv = await manager.get_conversation(umo, conversation_id)
                if conv is not None:
                    return conv
        return None


def _parse_statement(statement: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    """从 SQLModel/SQLAlchemy 的 select 对象里抽出【列名】与【参数】。

    ⚠️ 这是本适配器**唯一的解析点** —— 因此它对语句形状的假设集中在这里，
    改的时候只需看这一个函数。
    """
    columns: tuple[str, ...] = ()
    params: dict[str, Any] = {}

    # 列：`Select` 的 `_raw_columns`（sqlmodel 0.0.48 / SQLAlchemy 2.x）
    raw_columns = getattr(statement, "_raw_columns", None) or ()
    names: list[str] = []
    for col in raw_columns:
        key = getattr(col, "key", None) or getattr(col, "name", None)
        if isinstance(key, str):
            names.append(key)
    columns = tuple(names)

    # 参数：编译后的语句里带 `where` 的绑定值
    # ⚠️ 用 `compile().params` 而不是自己遍历 AST —— SQLAlchemy 的表达式树
    #    内部结构不稳定，编译产物才是公开契约。
    try:
        compiled = statement.compile(compile_kwargs={"literal_binds": False})
        raw_params = dict(compiled.params)
    except Exception:  # noqa: BLE001 — 编译失败就走下面的文本兜底
        raw_params = {}

    # `sqlmodel`/SQLAlchemy 会用 `conversation_id_1` 这种名字（带后缀），
    # 所以按前缀匹配。
    for key, value in raw_params.items():
        for wanted in ("conversation_id", "umo", "persona_id", "platform_id"):
            if key == wanted or key.startswith(f"{wanted}_"):
                params.setdefault(wanted, value)
                break

    return columns, params
