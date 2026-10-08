# -*- coding: utf-8 -*-
"""工具执行的**请求级上下文**（2026-09-25）。

## 为什么需要它

工具 handler 的签名是 `Callable[[dict], Awaitable[ToolResult]]` —— **只有参数**，
没有"这一轮是哪个会话、哪个人设"的信息。而人设参考图必须知道**当前人设**
（人设是一对多的，切角色就得换脸）。

🛠 三条路，选这条的理由：

| 做法 | 为什么不选 |
|---|---|
| 在 `ImageToolService` 上存个 `self._current_persona` | 🔴 **并发下会串**：两个会话同时生图，后一个会改掉前一个的人设 → 画错脸。而且这种 bug **单测看不出来**（单测是串行的） |
| 改 `ToolHandler` 签名加 `persona=` | 要动 `ToolRegistry` / 所有注册点 / 所有测试；收益一样 |
| ✅ `ContextVar` | **天生按 `asyncio` 任务隔离**（每个请求一个 task）→ 无竞态；不用改协议；测试里可直接设 |

⚠️ **必须在工具执行前设置、执行后清理**（用 `tool_scope`）。没设 → 取到 `None`
→ 退回"只有全局兜底参考图"的行为，**与改动前完全一致**（向后兼容）。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

#: 当前请求的人设（`Persona` 或 `None`）。默认 `None` = 没设置。
current_persona: ContextVar[Any] = ContextVar("emotion_current_persona", default=None)


@contextmanager
def tool_scope(*, persona: Any = None) -> Iterator[None]:
    """在这一段里把"当前人设"设好，退出时**必定还原**（含异常路径）。

    ⚠️ 用 `try/finally` 而不是只在末尾 reset：工具执行抛异常时也必须还原，
       否则这个 task 之后的所有工具调用都会看到**上一个请求的人设**（串脸）。
    """
    token = current_persona.set(persona)
    try:
        yield
    finally:
        current_persona.reset(token)


def get_persona() -> Any:
    """取当前人设（没设置时 `None`）。"""
    return current_persona.get()
