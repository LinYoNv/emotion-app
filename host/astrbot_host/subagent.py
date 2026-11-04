# -*- coding: utf-8 -*-
"""子代理委派 —— **我们只负责「把活派出去 + 收结果」，不自己跑 agent**。

## 为什么要这个模块（情绪 2026-11-03 定的语义）

> 「我想要的是**平台本身不带 agent 能力**，是子代理。**她把任务发出去等回收就行**。
>  就像是我们是主人，把任务发给佣人，明白不。」

这条把职责划清了：

| 角色 | 是谁 | 干什么 |
|---|---|---|
| **主人** | 陪伴插件的角色 | 判断「这活我自己不做」，派给某个佣人，拿到结果继续聊天 |
| **佣人** | 外部 agent（DSH / 别的服务） | 真正干活 |
| **传话的** | **本模块** | 把请求送出去、把结果拿回来 |

⇒ **平台不实现 agent 循环**。AstrBot 那套 `ToolLoopAgentRunner`（1568 行）+
`FunctionToolExecutor`（812 行）是「平台自己当 agent」的路子，我们不复制。

## 接线点只有一个

AstrBot 的 `HandoffTool` 执行时最终落到：

```python
llm_resp = await ctx.tool_loop_agent(
    event=..., chat_provider_id=..., prompt=..., system_prompt=...,
    tools=..., contexts=..., max_steps=..., ...)
```

⚠️ **官方那个 `tool_loop_agent` 是拿 runner 跑一个完整 agent 循环**。
   我们这里**只把它实现成「转调 backend」** —— 语义上是同一件事
   （"把这个任务做掉，给我结果"），但**执行者是外部**，不是我们自己。

⇒ 于是插件、子代理配置、handoff 工具**全部照官方工作**，
   而我们的平台**依然只是个聊天后端**。

## 后端可拔插（情绪选方案 C）

`SubagentBackend` 是协议；`NullBackend` 是默认实现 ——
**没配置时如实说「没接子代理」，不假装成功、不静默失败**
（本项目铁律：能力缺失必须显式返回 unavailable）。

将来接 DSH 只需要写一个实现了 `run()` 的类并注入，**不改这里的任何逻辑**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class SubagentUnavailable(RuntimeError):
    """没有可用的子代理后端。

    🔴 **必须是显式异常，不能返回空串**：调用方（HandoffTool）会把返回值
       原样交给主模型当工具结果。空串会让模型以为「佣人说没事」，
       从而**编造一个结果** —— 这正是本项目最贵的那类静默 bug。
    """


@dataclass(frozen=True)
class SubagentRequest:
    """一次委派请求。字段与 AstrBot `tool_loop_agent` 的入参对齐。"""

    #: 子代理的名字（配置里写什么就是什么），用于日志与结果标注
    name: str = ""
    #: 佣人这次要做的活（主模型给的 `input`）
    prompt: str = ""
    #: 佣人的人设/指令
    system_prompt: str = ""
    #: 允许佣人用的工具名（我们不解释它们，交给后端）
    tools: tuple[str, ...] = ()
    #: 供后端参考的调用方 provider id（官方有 per-subagent provider 覆盖）
    provider_id: str = ""
    #: 透传的其它参数（`max_steps` 之类），后端爱用不用
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentResult:
    """佣人交回来的结果。"""

    text: str
    #: 便于排查：哪个后端、花了多久
    backend: str = ""
    elapsed_ms: int = 0


@runtime_checkable
class SubagentBackend(Protocol):
    """子代理后端协议 —— **实现这一个方法就能接任何外部 agent**。

    实现方只需要回答一个问题：*把这个任务做掉，结果是什么？*

    ⚠️ 约定：
      * 拿不到结果要**抛异常**（`SubagentUnavailable` 或自己的异常），
        **不要返回 `""`** —— 见 `SubagentUnavailable` 的注释。
      * 不要在这里面解析/改写主模型的消息历史，那是别人的职责。
    """

    async def run(self, request: SubagentRequest) -> SubagentResult:  # pragma: no cover
        ...


class NullBackend:
    """默认后端：**如实表示「没接子代理」**。

    🔴 为什么要有它（而不是让 `backend=None`）：
       插件会在**配置了子代理**时注册 `transfer_to_*` 工具。如果用户配了却没接后端，
       我们要让模型收到一句**明确的失败**（"没有可用的子代理后端"），
       而不是抛一个看起来像代码 bug 的 `AttributeError`，
       也不是返回空串让它编造。

    ⚠️ 这条路径**不是静默的**：每次被调用都会打一条 warning。
    """

    name = "null"

    async def run(self, request: SubagentRequest) -> SubagentResult:
        logger.warning(
            "子代理未接入后端，已如实拒绝委派：name=%s prompt=%r",
            request.name or "(未命名)",
            (request.prompt or "")[:80],
        )
        raise SubagentUnavailable(
            "没有可用的子代理后端：请在宿主侧配置子代理服务后再试。"
            "这不是你的工具调用写错了，是平台还没接上干活的那个 agent。"
        )


class SubagentRegistry:
    """把「配置里的 agents」翻成可调用的委派入口。

    与 AstrBot `SubAgentOrchestrator` 的分工：
      * **它**负责把配置变成 `transfer_to_xxx` 这个 **LLM 工具**（给主模型看）；
      * **这里**负责工具**被调用之后**把请求送到后端去。

    两者由 `subagent_id`（也就是 agent 的 `name`）串起来。
    """

    def __init__(self, backend: SubagentBackend | None = None) -> None:
        self._backend: SubagentBackend = backend or NullBackend()
        #: agent name -> 配置（`system_prompt` / `tools` / `provider_id`）
        self._agents: dict[str, dict[str, Any]] = {}

    # ---- 配置 ----

    def configure(self, agents: list[dict[str, Any]] | None) -> None:
        """按官方 `SubAgentOrchestrator.reload_from_config` 的口径读 agents。

        ⚠️ 只收 `enabled` 不为 False 且 `name` 非空的项 —— 与官方一致，
           否则会注册出一个名字是空串的工具（主模型永远调不到）。
        """
        table: dict[str, dict[str, Any]] = {}
        for item in agents or []:
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            tools = item.get("tools") or []
            table[name] = {
                "system_prompt": str(item.get("system_prompt", "") or "").strip(),
                "tools": tuple(str(t).strip() for t in tools if str(t).strip()),
                "provider_id": str(item.get("provider_id", "") or "").strip(),
            }
        self._agents = table
        logger.info("子代理注册表已更新：%d 个（后端=%s）", len(table), self.backend_name)

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(self._agents)

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "name", type(self._backend).__name__))

    def set_backend(self, backend: SubagentBackend | None) -> None:
        self._backend = backend or NullBackend()

    # ---- 调用 ----

    async def dispatch(
        self, name: str, prompt: str, *, provider_id: str = "", extra: dict[str, Any] | None = None
    ) -> SubagentResult:
        """把一次委派送出去。**未注册的名字直接拒绝**（不猜、不回退到默认）。"""
        config = self._agents.get(name)
        if config is None:
            raise SubagentUnavailable(
                f"没有名为 {name!r} 的子代理：请检查配置里的 subagent_orchestrator.agents。"
            )
        request = SubagentRequest(
            name=name,
            prompt=str(prompt or ""),
            system_prompt=config["system_prompt"],
            tools=config["tools"],
            provider_id=provider_id or config["provider_id"],
            extra=dict(extra or {}),
        )
        started = _monotonic_ms()
        result = await self._backend.run(request)
        return SubagentResult(
            text=result.text,
            backend=result.backend or self.backend_name,
            elapsed_ms=result.elapsed_ms or (int(_monotonic_ms() - started)),
        )


def _monotonic_ms() -> float:
    import time

    return time.monotonic() * 1000.0


# ===========================================================================
# 以下是「把配置变成 transfer_to_* 工具」的那一层
# ===========================================================================


class SubagentHandoffTool:
    """`transfer_to_<name>` 工具 —— **主模型看到的入口**。

    🔴 为什么不直接继承官方的 `HandoffTool`（`astrbot/core/agent/handoff.py`）：
       官方那个的执行体在 `FunctionToolExecutor._execute_handoff()` 里，
       而那个类属于「平台自己跑 agent 循环」那套（812 行），我们不引入。
       这里只复刻**接口面**：`name` / `description` / `parameters` / `active`，
       以及它被调用时该做什么（交给 `SubagentRegistry.dispatch`）。

    ⚠️ 工具名必须是 `transfer_to_<agent 名>`（官方 `handoff.py:26` 就是这么拼的）——
       用户面板上会显示这个前缀，换掉会让人对不上。
    """

    def __init__(self, name: str, registry: SubagentRegistry, *, description: str = "") -> None:
        self._subagent = name
        self._registry = registry
        self.name = f"transfer_to_{name}"
        self.description = description or (
            f"把当前任务交给子代理「{name}」去做。适用于需要它独立完成、"
            "你不想自己在对话里一步步做的工作。调用后你会拿到它交回来的结果。"
        )
        self.active = True
        self.parameters = {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": (
                        "要交给子代理的完整任务描述。写得越清楚它做得越准 —— "
                        "它看不到你们当前的对话，只看得到这段文字。"
                    ),
                },
            },
            "required": ["input"],
        }

    async def call(self, context: Any = None, **kwargs: Any) -> Any:
        """执行一次委派（工具被调用时走这里）。"""
        prompt = str(kwargs.get("input") or "")
        provider_id = ""
        # 事件上的统一消息来源，供后端判断"这是谁的会话"
        umo = ""
        if context is not None:
            umo = str(getattr(context, "unified_msg_origin", "") or "")
        result = await self._registry.dispatch(
            self._subagent, prompt, provider_id=provider_id, extra={"event_umo": umo}
        )
        return result.text


class SubagentOrchestrator:
    """把 `subagent_orchestrator` 配置翻成一组 `transfer_to_*` 工具。

    与官方 `core/subagent_orchestrator.py`（104 行）的分工一致：
    它负责**声明**工具，`SubagentRegistry` 负责**执行**。

    ⚠️ 与官方的差别（有意的）：
      官方会去 `persona_mgr.get_persona_v3_by_id(pid)` 用 AstrBot 人设覆盖指令。
      我们**不做这个兜底** —— 那是"猜用户的意图"。配置里没写 `system_prompt`
      就用 agent 名作指令，并在日志里**如实说明**用了兜底。
    """

    def __init__(self, registry: SubagentRegistry) -> None:
        self._registry = registry
        self.handoffs: list[SubagentHandoffTool] = []

    def reload_from_config(self, cfg: dict[str, Any] | None) -> list[SubagentHandoffTool]:
        """按配置重建工具表。**每次都整体重建**（不做增量，避免残留旧 agent）。"""
        cfg = cfg or {}
        agents = cfg.get("agents", [])
        if not isinstance(agents, list):
            logger.warning("subagent_orchestrator.agents 必须是数组，已忽略：%r", type(agents).__name__)
            agents = []

        self._registry.configure(agents)

        handoffs: list[SubagentHandoffTool] = []
        for item in agents:
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            description = str(item.get("public_description", "") or "").strip()
            if not str(item.get("system_prompt", "") or "").strip():
                logger.info(
                    "子代理 %r 没配 system_prompt，后端将按名字兜底（不是静默）", name
                )
            handoffs.append(
                SubagentHandoffTool(name, self._registry, description=description)
            )

        self.handoffs = handoffs
        logger.info("子代理工具已注册：%d 个（%s）",
                    len(handoffs), ", ".join(t.name for t in handoffs) or "无")
        return handoffs
