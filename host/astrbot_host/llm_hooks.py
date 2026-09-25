# -*- coding: utf-8 -*-
"""LLM 钩子派发（P0-4）—— 让插件能在**调用模型前后**介入。

⭐⭐ **这是"记忆注入/人设增强"的入口**。陪伴系插件的核心机制全靠它：

| 钩子 | 实测数量 | 插件用它做什么 |
|---|---:|---|
| `OnLLMRequestEvent` | **24** | **改 `req`**：注入记忆/人设/关系状态/分段指令 |
| `OnLLMResponseEvent` | **12** | 读/改模型回复（后处理、抽取、状态结算） |
| `OnWaitingLLMRequestEvent` | **2** | 拿到锁之前的通知（可终止本次请求） |

真实现的调用点（**逐个核对过**）：
  * `pipeline/process_stage/method/agent_sub_stages/internal.py:269`
    → `if await call_event_hook(event, EventType.OnLLMRequestEvent, req): return`
    **返回值 = 是否终止**（True 就不发这次请求了）
  * `internal.py:217` → `if await call_event_hook(event, OnWaitingLLMRequestEvent): return`
  * `core/astr_agent_hooks.py:31-35` → `call_event_hook(event, OnLLMResponseEvent, llm_response)`

🔴 **关键语义：钩子拿到的是 `req` 对象本身，改它就是"生效"**
   （真实现把同一个 `ProviderRequest` 实例传给所有钩子，之后直接拿它去请求模型）。
   所以我们的 `run_llm_request_hooks()` **必须传同一个对象、并返回它** ——
   如果传副本，插件的注入会**静默丢失**（最贵的一类 bug）。

⚠️ 与 `dispatch.py` 的关系：那里管**消息事件**（`AdapterMessageEvent`），
   这里管 **LLM 钩子**。两者共用 `call_event_hook` 的错误处理约定：
   钩子抛错 → **记 ERROR 但不中断整条链**（真实现 `context_utils.py:102-103` 就是
   `except BaseException: logger.error(...)` 然后继续），
   但被 `stop_event()` 终止时要**立刻停止后续钩子**并返回 True。
"""
from __future__ import annotations

import logging
from typing import Any

from astrbot_host.registry import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)

logger = logging.getLogger("astrbot")


async def call_event_hook(
    event: Any,
    hook_type: EventType,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """调用某类事件钩子（照真实现 `pipeline/context_utils.py:78-112`）。

    **返回值语义照真实现**：
      * `True` = 事件被终止（某个钩子调了 `event.stop_event()`）→ 调用方应停止后续流程；
      * `False` = 继续。

    🔴 三处照抄真实现的细节：
      1. **按事件的 `plugins_name` 过滤**（`:93`）—— 支持按会话启用/禁用插件；
      2. **钩子抛错只记 ERROR，不中断**（`:102-103` 是 `except BaseException`）——
         一个坏钩子不该让整条 LLM 链路挂掉；
      3. **每个钩子后检查 `is_stopped()`**（`:105-110`），
         终止时**打 info 并立刻 return True**（不是继续跑完剩下的）。

    ⚠️ 真实现用 `assert inspect.iscoroutinefunction(...)` 卡非协程钩子；
       我们**改成记 ERROR 并跳过**（`assert` 在生产会被 `-O` 掉，
       而且响亮失败比静默跳过好，但**不能**让整链炸）。
    """
    handlers = star_handlers_registry.get_handlers_by_event_type(
        hook_type,
        plugins_name=getattr(event, "plugins_name", None),
    )
    for handler in handlers:
        try:
            result = handler.handler(event, *args, **kwargs)
            # 支持协程与异步生成器两种（真实现的 `call_handler` 也兼容生成器；
            # 钩子通常是协程，但插件可能写成生成器 —— 不消费会静默不执行）
            if hasattr(result, "__await__"):
                await result
            elif hasattr(result, "__aiter__"):
                async for _ in result:
                    pass
        except Exception:  # noqa: BLE001
            logger.error(
                "LLM 钩子执行失败：%s（%s）",
                handler.handler_full_name, hook_type.name,
                exc_info=True,
            )

        if _is_stopped(event):
            logger.info(
                "钩子 %s 终止了事件传播（%s）",
                handler.handler_full_name, hook_type.name,
            )
            return True

    return _is_stopped(event)


def _is_stopped(event: Any) -> bool:
    """安全取 `is_stopped()`（真实现是方法；这里兼容两种写法）。"""
    fn = getattr(event, "is_stopped", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return bool(fn)


async def run_llm_request_hooks(event: Any, req: Any) -> tuple[Any, bool]:
    """在**请求模型之前**跑钩子（照真实现 `internal.py:269`）。

    🔴 **传给插件的是 `req` 本身**（同一个对象），插件改 `req.system_prompt`
       或 `req.contexts` / `req.extra_user_content_parts` 就是注入。
       我们**原样返回它** —— 传副本会让注入静默丢失。

    Args:
        event: `AstrMessageEvent`
        req: `ProviderRequest`（**会被插件就地修改**）

    Returns:
        `(req, cancelled)`：
          * `req` 同一个对象（便于链式使用）；
          * `cancelled=True` 表示插件终止了本次请求，**不该再调模型**
            （真实现：`if await call_event_hook(...): return`）。
    """
    cancelled = await call_event_hook(event, EventType.OnLLMRequestEvent, req)
    if cancelled:
        logger.info("LLM 请求被插件钩子终止（umo=%s）", getattr(event, "unified_msg_origin", ""))
    return req, cancelled


async def run_waiting_llm_request_hooks(event: Any) -> bool:
    """在**拿到会话锁之前**的等待钩子（照真实现 `internal.py:217`）。

    语义：这是一个**通知型**钩子，让插件知道"马上要请求模型了"
    （实测 `meme_manager` 用它标记请求来源）。
    返回 True = 被终止。
    """
    return await call_event_hook(event, EventType.OnWaitingLLMRequestEvent)


async def run_llm_response_hooks(event: Any, response: Any) -> bool:
    """在**模型返回之后**跑钩子（照真实现 `astr_agent_hooks.py:31-35`）。

    ⚠️ 与 request 钩子不同，真实现**不使用**它的返回值来决定流程
       （那里是裸 `await call_event_hook(...)`）；
       但我们仍**如实返回**是否被终止，供调用方自行决定。

    Args:
        response: `LLMResponse` —— 插件可能改 `completion_text` / `result_chain`。
            ⚠️ 注意 `completion_text` 是 **property**（读时优先 `result_chain`），
               见 `compat/core/provider/entities.py` 的说明。
    """
    return await call_event_hook(event, EventType.OnLLMResponseEvent, response)


def hook_counts() -> dict[str, int]:
    """诊断：当前注册了多少个 LLM 相关钩子（宿主自检用）。"""
    out: dict[str, int] = {}
    for et in (
        EventType.OnLLMRequestEvent,
        EventType.OnLLMResponseEvent,
        EventType.OnWaitingLLMRequestEvent,
    ):
        out[et.name] = len(
            star_handlers_registry.get_handlers_by_event_type(et, only_activated=False)
        )
    return out


__all__ = [
    "call_event_hook",
    "hook_counts",
    "run_llm_request_hooks",
    "run_llm_response_hooks",
    "run_waiting_llm_request_hooks",
]
