# -*- coding: utf-8 -*-
"""判据：**子代理委派**（平台不跑 agent 循环，只「把活派出去、等结果回收」）。

## 语义来源（情绪 2026-11-03 定，原话）

> 「我想要的是**平台本身不带 agent 能力**，是子代理。**她把任务发出去等回收就行**。
>  就像是我们是主人，把任务发给佣人，明白不。」

⇒ 平台的职责边界：
  * **主人** = 陪伴角色，决定"这活我派出去"
  * **佣人** = 用户自己接的 agent 平台（DSH / 任意 HTTP）
  * **传话的** = `astrbot_host/subagent.py`（本文件测的就是它 + `Context.tool_loop_agent`）

## 这个文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | `NullBackend` **抛异常**而不是返回 `""` | 空串会让模型以为"佣人说没事"→ **编造结果** |
| ② | 未注册的 agent 名被拒绝 | 别猜、别回退到默认那个 |
| ③ | `configure()` 过滤掉空名/禁用项 | 否则注册出主模型永远调不到的工具 |
| ④ | 后端真被调用、收到正确的请求 | 修复本身 |
| ⑤ | `Context.tool_loop_agent` **真的转发**到 registry | **装配判据**：模块能工作 ≠ 被接上了 |
| ⑥ | 只有一个 agent 时才允许按名推断 | 多个候选必须拒绝（对齐作者"唯一候选才认领"的原则） |
| ⑦ | 工具名必须是 `transfer_to_<name>` | 面板上要显示这个前缀，换掉用户对不上 |

⚠️ ⑤ 是必须的：本项目反复踩过「方法写好了但没接到入口」——
   只测 `SubagentRegistry.dispatch` 的话，`Context` 忘接也照样绿。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from astrbot_host.subagent import (  # noqa: E402
    NullBackend,
    SubagentBackend,
    SubagentOrchestrator,
    SubagentRegistry,
    SubagentRequest,
    SubagentResult,
    SubagentUnavailable,
)


class RecordingBackend:
    """记下收到的请求，返回固定结果。"""

    name = "recording"

    def __init__(self, text: str = "佣人交活回来了") -> None:
        self.text = text
        self.requests: list[SubagentRequest] = []

    async def run(self, request: SubagentRequest) -> SubagentResult:
        self.requests.append(request)
        return SubagentResult(text=self.text, backend=self.name)


AGENTS = [
    {"name": "coder", "system_prompt": "你写代码", "tools": ["shell"], "enabled": True},
    {"name": "writer", "system_prompt": "你写文案", "enabled": True},
]


# ---------------------------------------------------------------------------
# ① 🔴 最重要的那条：不允许用空串冒充结果
# ---------------------------------------------------------------------------


async def test_null_backend_raises_instead_of_returning_empty_string():
    """没接后端时**必须抛**，绝不能返回 `""`。

    🔴 为什么这条最重要：`HandoffTool` 把返回值原样当工具结果交给主模型。
       返回 `""` 等于告诉模型「佣人说：一切正常，没有要汇报的」，
       于是它**会编一个结果出来**（本项目最贵的那类静默 bug）。
    """
    backend = NullBackend()
    with pytest.raises(SubagentUnavailable):
        await backend.run(SubagentRequest(name="coder", prompt="写个排序"))


async def test_null_backend_message_is_actionable():
    """拒绝时的文案要让人知道下一步做什么，不是一句 `error`。"""
    backend = NullBackend()
    try:
        await backend.run(SubagentRequest(name="coder", prompt="x"))
    except SubagentUnavailable as error:
        text = str(error)
        assert "子代理后端" in text, f"要说明缺的是什么：{text}"
        assert "配置" in text, f"要给出可行动的方向：{text}"
    else:  # pragma: no cover
        pytest.fail("必须抛异常")


# ---------------------------------------------------------------------------
# ②③ 注册表：拒绝未知、过滤无效配置
# ---------------------------------------------------------------------------


async def test_unknown_agent_is_refused_not_defaulted():
    """没配过的名字必须拒绝 —— 不许"回退到唯一那个"。"""
    registry = SubagentRegistry(RecordingBackend())
    registry.configure(AGENTS)
    with pytest.raises(SubagentUnavailable, match="nobody"):
        await registry.dispatch("nobody", "随便干点什么")


def test_configure_skips_blank_names_and_disabled_agents():
    """空名、禁用项都不能变成工具（否则主模型永远调不到它）。"""
    registry = SubagentRegistry(RecordingBackend())
    registry.configure([
        {"name": "  ", "enabled": True},          # 空名
        {"name": "off", "enabled": False},        # 显式禁用
        {"name": "coder", "enabled": True},
        "not-a-dict",                              # 脏数据
    ])
    assert registry.agents == ("coder",)


# ---------------------------------------------------------------------------
# ④ 真的调到了后端，且请求内容对
# ---------------------------------------------------------------------------


async def test_dispatch_reaches_the_backend_with_the_configured_fields():
    backend = RecordingBackend("ok")
    registry = SubagentRegistry(backend)
    registry.configure(AGENTS)

    result = await registry.dispatch("coder", "帮我写个快排")

    assert result.text == "ok"
    assert len(backend.requests) == 1
    got = backend.requests[0]
    assert got.name == "coder"
    assert got.prompt == "帮我写个快排"
    # system_prompt / tools 必须来自**这个 agent 的配置**，不能串到别的 agent
    assert got.system_prompt == "你写代码"
    assert got.tools == ("shell",)


async def test_backend_override_is_used_when_given():
    """显式 provider_id 优先于配置里的（官方 `handoff.provider_id` 同义）。"""
    backend = RecordingBackend()
    registry = SubagentRegistry(backend)
    registry.configure([{"name": "coder", "provider_id": "配置里的"}])

    await registry.dispatch("coder", "x", provider_id="调用方指定的")

    assert backend.requests[0].provider_id == "调用方指定的"


# ---------------------------------------------------------------------------
# ⑤ 🔴 装配判据：Context 必须真的转发
# ---------------------------------------------------------------------------


def make_context(registry: Any) -> Any:
    """造一个带子代理端口的 `Context`。

    ⚠️ 真实形状是 `Context(ports=HostPorts(...))`，**不是**把能力当 kwargs 直接传 ——
       直接传会 `TypeError`（我第一次就写错了，判据当场抓到）。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts

    return Context(ports=HostPorts(subagents=registry))


async def test_context_tool_loop_agent_forwards_to_the_registry():
    """走 `Context.tool_loop_agent`（官方 HandoffTool 的真实入口）。

    ⚠️ 必须从这个入口进，不能直接调 `registry.dispatch` ——
       否则"Context 忘接"这种错永远测不出来。
    """
    backend = RecordingBackend("子代理干完了")
    registry = SubagentRegistry(backend)
    registry.configure(AGENTS)
    ctx = make_context(registry)

    # 官方 HandoffTool 通过 kwargs 把 agent 名带下来
    response = await ctx.tool_loop_agent(
        event=None,
        chat_provider_id="p1",
        prompt="写个快排",
        system_prompt="你写代码",
        tools=None,
        subagent_name="coder",
    )

    assert response.completion_text == "子代理干完了"
    assert backend.requests[-1].name == "coder"


async def test_context_without_registry_refuses_clearly():
    """端口没装配时要**说清是端口没装**，不是 `AttributeError`。"""
    ctx = make_context(None)
    with pytest.raises(SubagentUnavailable, match="未装配"):
        await ctx.tool_loop_agent(event=None, chat_provider_id="p", prompt="x", subagent_name="coder")


# ---------------------------------------------------------------------------
# ⑥ 名字推断：单候选才认领
# ---------------------------------------------------------------------------


async def test_single_agent_is_inferred_when_caller_omits_the_name():
    backend = RecordingBackend()
    registry = SubagentRegistry(backend)
    registry.configure([{"name": "only_one"}])
    ctx = make_context(registry)

    await ctx.tool_loop_agent(event=None, chat_provider_id="p", prompt="x")

    assert backend.requests[-1].name == "only_one"


async def test_multiple_agents_without_a_name_is_refused():
    """多候选时**必须拒绝**（猜错就是把活派给了错的佣人）。"""
    registry = SubagentRegistry(RecordingBackend())
    registry.configure(AGENTS)
    ctx = make_context(registry)

    with pytest.raises(SubagentUnavailable, match="无法确定"):
        await ctx.tool_loop_agent(event=None, chat_provider_id="p", prompt="x")


# ---------------------------------------------------------------------------
# ⑦ 工具名与参数形状（面板上要显示 `transfer_to_<name>`）
# ---------------------------------------------------------------------------


def test_handoff_tool_is_named_transfer_to_agent():
    registry = SubagentRegistry(RecordingBackend())
    orchestrator = SubagentOrchestrator(registry)

    handoffs = orchestrator.reload_from_config({"agents": AGENTS})

    assert [tool.name for tool in handoffs] == ["transfer_to_coder", "transfer_to_writer"]
    # 参数契约：主模型必须知道要传 input
    assert handoffs[0].parameters["required"] == ["input"]
    assert "input" in handoffs[0].parameters["properties"]


async def test_handoff_tool_call_dispatches_and_returns_text():
    """工具被调用 → 真的派活 → 返回佣人的话。"""
    backend = RecordingBackend("这是子代理的输出")
    registry = SubagentRegistry(backend)
    orchestrator = SubagentOrchestrator(registry)
    tool = orchestrator.reload_from_config({"agents": AGENTS})[0]

    text = await tool.call(context=None, input="帮我做点事")

    assert text == "这是子代理的输出"
    assert backend.requests[-1].prompt == "帮我做点事"


def test_orchestrator_rebuilds_instead_of_appending():
    """重复配置要**整体重建**，不能残留上一次的 agent。"""
    registry = SubagentRegistry(RecordingBackend())
    orchestrator = SubagentOrchestrator(registry)

    orchestrator.reload_from_config({"agents": AGENTS})
    handoffs = orchestrator.reload_from_config({"agents": [{"name": "only_one"}]})

    assert [t.name for t in handoffs] == ["transfer_to_only_one"]
    assert registry.agents == ("only_one",)


# ---------------------------------------------------------------------------
# ⑧ 诊断：能力必须可观测
# ---------------------------------------------------------------------------


def test_capabilities_report_subagents_unusable_by_default():
    """默认（无端口 / NullBackend）必须报**不可用**，别自欺。

    ⚠️ `capabilities()` 在 `HostPorts` 上，**不在 `Context` 上**
       （我第一次写成 `ctx.capabilities()` → AttributeError，判据当场抓到）。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts

    def caps(registry: Any) -> dict[str, bool]:
        ports = HostPorts(subagents=registry)
        return Context(ports=ports)._ports.capabilities()

    assert caps(None)["subagents"] is False
    assert caps(None)["subagents_usable"] is False

    null_registry = SubagentRegistry()  # 默认 NullBackend
    null_registry.configure(AGENTS)
    assert caps(null_registry)["subagents"] is True
    # 有注册表、有 agent，但后端是 Null ⇒ **不可用**（这条最容易自欺）
    assert caps(null_registry)["subagents_usable"] is False

    real = SubagentRegistry(RecordingBackend())
    real.configure(AGENTS)
    assert caps(real)["subagents_usable"] is True
