# -*- coding: utf-8 -*-
"""P0-4 判据：LLM 钩子（记忆注入的入口）。

🔴 本文件守的核心风险：**插件对 `req` 的修改必须真的生效**。
   真实现把**同一个 `ProviderRequest` 实例**传给所有钩子，之后直接拿它请求模型。
   如果我们传的是副本（或忘了把改过的对象传下去），插件的记忆/人设注入会
   **静默丢失** —— 表现是"模型好像没记住我说的话"，
   而全链路不报任何错。这是最难排查的一类 bug。

其余判据覆盖：优先级顺序、终止语义、异常不中断、`plugins_name` 过滤。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


@pytest.fixture
def clean_registry():
    """每个用例独享一个干净的注册表（避免跨用例污染）。"""
    from astrbot_host.registry import star_handlers_registry

    star_handlers_registry.clear()
    try:
        yield star_handlers_registry
    finally:
        star_handlers_registry.clear()


def _event(text: str = "你好"):
    from astrbot_host.umo import build_inbound_event

    return build_inbound_event(text, "3412642082")


def _req(system_prompt: str = "原始系统提示"):
    from astrbot_host.compat.core.provider.entities import ProviderRequest

    return ProviderRequest(prompt="用户说的话", system_prompt=system_prompt)


def _register(reg, event_type, name, fn, priority=0, module="probe.main"):
    from astrbot_host.registry import StarHandlerMetadata

    reg.append(
        StarHandlerMetadata(
            event_type=event_type,
            handler_full_name=f"{module}_{name}",
            handler_name=name,
            handler_module_path=module,
            handler=fn,
            extras_configs={"priority": priority},
        )
    )


# ---------------------------------------------------------------------------
# 一、核心：插件改了 req，必须生效
# ---------------------------------------------------------------------------
def test_plugin_modification_of_req_persists(clean_registry):
    """🔴🔴 **最重要的判据**：钩子对 `req` 的修改必须能被调用方看到。

    这就是"记忆注入"在实现层面的全部含义 —— 插件往 `req.system_prompt`
    里追加一段记忆，调用方拿到的 `req` 里就必须有那段。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks

    async def inject_memory(event, req):
        req.system_prompt += "\n【记忆】你上次说喜欢猫。"

    from astrbot_host.registry import EventType as ET

    _register(clean_registry, ET.OnLLMRequestEvent, "inject_memory", inject_memory)

    ev = _event()
    req = _req()
    same, cancelled = asyncio.run(run_llm_request_hooks(ev, req))

    assert same is req, "必须返回**同一个** req 对象（传副本会让注入丢失）"
    assert "【记忆】" in req.system_prompt, (
        f"插件注入的记忆没生效：{req.system_prompt!r} —— "
        "这就是「记忆注入静默丢失」的形态"
    )
    assert cancelled is False


def test_extra_user_content_parts_injection(clean_registry):
    """另一种注入方式：往 `req.extra_user_content_parts` 追加内容块。

    真实现字段（`entities.py:100-101`）：用于在**用户消息之后**追加内容块
    （如系统提醒、指令）。插件用它做"隐式追加"，与改 system_prompt 不同。
    """
    from astrbot_host.registry import EventType as ET
    from astrbot_host.llm_hooks import run_llm_request_hooks

    async def inject_part(event, req):
        req.extra_user_content_parts.append({"type": "text", "text": "【提醒】该喝水了"})

    _register(clean_registry, ET.OnLLMRequestEvent, "inject_part", inject_part)

    req = _req()
    asyncio.run(run_llm_request_hooks(_event(), req))
    assert req.extra_user_content_parts, "追加的内容块丢了"
    assert req.extra_user_content_parts[0]["text"] == "【提醒】该喝水了"


def test_multiple_hooks_see_each_others_changes(clean_registry):
    """**多个钩子串行、且后一个能看到前一个的修改**。

    ⚠️ 这条是"优先级顺序"之外的另一半语义：真实现把同一个 req 依次传给每个钩子，
       所以钩子是**链式叠加**的（陪伴插件有 24 个 request 钩子，全都这么做）。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    seen: list[str] = []

    async def first(event, req):
        req.system_prompt += "|A"
        seen.append("first")

    async def second(event, req):
        # 必须能看到 first 改过的内容
        seen.append(f"second:{'A' in req.system_prompt}")
        req.system_prompt += "|B"

    _register(clean_registry, ET.OnLLMRequestEvent, "first", first, priority=10)
    _register(clean_registry, ET.OnLLMRequestEvent, "second", second, priority=5)

    req = _req()
    asyncio.run(run_llm_request_hooks(_event(), req))

    assert seen == ["first", "second:True"], (
        f"钩子顺序或传递有问题：{seen}（应为 first 先跑，且 second 能看到 A）"
    )
    assert req.system_prompt.endswith("|A|B"), req.system_prompt


def test_priority_order_is_honored(clean_registry):
    """**优先级越大越先执行**（照注册表 `append` 的 `-priority` 排序）。

    实测插件大量使用 priority（陪伴有 110000 / -253000 这种极端值），
    顺序错了会让"先注入的记忆"变成"后注入"，语义完全不同。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    order: list[str] = []

    async def high(event, req):
        order.append("high")

    async def low(event, req):
        order.append("low")

    _register(clean_registry, ET.OnLLMRequestEvent, "low", low, priority=-100)
    _register(clean_registry, ET.OnLLMRequestEvent, "high", high, priority=99999)

    asyncio.run(run_llm_request_hooks(_event(), _req()))
    assert order == ["high", "low"], f"优先级顺序不对：{order}"


# ---------------------------------------------------------------------------
# 二、终止语义
# ---------------------------------------------------------------------------
def test_stop_event_cancels_request_and_skips_rest(clean_registry):
    """钩子 `stop_event()` → 返回 `cancelled=True`，且**后续钩子不再执行**。

    真实现（`internal.py:269`）：`if await call_event_hook(...): return`
    —— 终止意味着**这次不调模型了**。
    ⚠️ 注意"不再执行后续钩子"也是照真实现（`context_utils.py:105-110`
       命中 stopped 就 `return True`，不是继续跑完）。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    ran: list[str] = []

    async def stopper(event, req):
        ran.append("stopper")
        event.stop_event()

    async def later(event, req):
        ran.append("later")

    _register(clean_registry, ET.OnLLMRequestEvent, "stopper", stopper, priority=100)
    _register(clean_registry, ET.OnLLMRequestEvent, "later", later, priority=1)

    _, cancelled = asyncio.run(run_llm_request_hooks(_event(), _req()))
    assert cancelled is True, "stop_event 后应返回 cancelled=True（调用方不该再调模型）"
    assert ran == ["stopper"], f"被终止后不该继续跑后续钩子：{ran}"


def test_no_stop_means_not_cancelled(clean_registry):
    """没有钩子终止 → `cancelled=False`（正常请求照发）。"""
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    async def normal(event, req):
        req.system_prompt += "|x"

    _register(clean_registry, ET.OnLLMRequestEvent, "normal", normal)
    _, cancelled = asyncio.run(run_llm_request_hooks(_event(), _req()))
    assert cancelled is False


# ---------------------------------------------------------------------------
# 三、健壮性：坏钩子不许拖垮整链
# ---------------------------------------------------------------------------
def test_hook_exception_does_not_break_chain(clean_registry):
    """一个钩子抛错 → **记 ERROR 但继续跑后续钩子**（照真实现 `except BaseException`）。

    ⚠️ 为什么不能中断：陪伴插件有 24 个 request 钩子，
       任何一个因为配置问题抛错都不该让"记忆注入"整个失效。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    ran: list[str] = []

    async def broken(event, req):
        ran.append("broken")
        raise RuntimeError("钩子内部炸了")

    async def healthy(event, req):
        ran.append("healthy")
        req.system_prompt += "|survived"

    _register(clean_registry, ET.OnLLMRequestEvent, "broken", broken, priority=100)
    _register(clean_registry, ET.OnLLMRequestEvent, "healthy", healthy, priority=1)

    req = _req()
    _, cancelled = asyncio.run(run_llm_request_hooks(_event(), req))

    assert ran == ["broken", "healthy"], f"坏钩子不该中断后续：{ran}"
    assert req.system_prompt.endswith("|survived"), "后续钩子的注入应生效"
    assert cancelled is False


def test_async_generator_hook_is_consumed(clean_registry):
    """写成**异步生成器**的钩子也要被完整消费。

    ⚠️ 不消费生成器 = 钩子体**根本不执行**（本项目踩过同类坑：
       流式 `break` 掉生成器导致持久化永不执行）。
       真实现的 `call_handler` 也兼容生成器。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    async def gen_hook(event, req):
        req.system_prompt += "|from-generator"
        yield None

    _register(clean_registry, ET.OnLLMRequestEvent, "gen_hook", gen_hook)

    req = _req()
    asyncio.run(run_llm_request_hooks(_event(), req))
    assert req.system_prompt.endswith("|from-generator"), (
        f"生成器型钩子没被执行：{req.system_prompt!r}"
    )


def test_plugins_name_filter(clean_registry):
    """`event.plugins_name` 能过滤钩子（照真实现 `context_utils.py:93`）。

    语义：`None` = 全部启用；给列表 = **只跑这些插件的**钩子。
    ⚠️ 真实现的过滤是**模块路径顶层包名**匹配。
    """
    from astrbot_host.llm_hooks import run_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    ran: list[str] = []

    async def from_a(event, req):
        ran.append("a")

    async def from_b(event, req):
        ran.append("b")

    _register(clean_registry, ET.OnLLMRequestEvent, "from_a", from_a, module="pkg_a.main")
    _register(clean_registry, ET.OnLLMRequestEvent, "from_b", from_b, module="pkg_b.main")

    # 只启用 pkg_a
    ev = _event()
    ev.plugins_name = ["pkg_a"]
    asyncio.run(run_llm_request_hooks(ev, _req()))
    assert ran == ["a"], f"plugins_name 过滤失效：{ran}"

    # None = 全部
    ran.clear()
    ev2 = _event()
    ev2.plugins_name = None
    asyncio.run(run_llm_request_hooks(ev2, _req()))
    assert sorted(ran) == ["a", "b"], f"None 应放开全部：{ran}"


# ---------------------------------------------------------------------------
# 四、response / waiting 钩子
# ---------------------------------------------------------------------------
def test_response_hook_receives_response_object(clean_registry):
    """`OnLLMResponseEvent` 钩子拿到的是 `LLMResponse`，改它要生效。"""
    from astrbot_host.compat.core.provider.entities import LLMResponse
    from astrbot_host.llm_hooks import run_llm_response_hooks
    from astrbot_host.registry import EventType as ET

    async def post(event, resp):
        resp.completion_text = (resp.completion_text or "") + "【后处理】"

    _register(clean_registry, ET.OnLLMResponseEvent, "post", post)

    resp = LLMResponse(completion_text="模型原话")
    asyncio.run(run_llm_response_hooks(_event(), resp))
    assert "【后处理】" in (resp.completion_text or ""), (
        f"response 钩子的修改没生效：{resp.completion_text!r}"
    )


def test_waiting_hook_can_cancel(clean_registry):
    """`OnWaitingLLMRequestEvent` 也能终止（照真实现 `internal.py:217`）。"""
    from astrbot_host.llm_hooks import run_waiting_llm_request_hooks
    from astrbot_host.registry import EventType as ET

    async def waiting(event):
        event.stop_event()

    _register(clean_registry, ET.OnWaitingLLMRequestEvent, "waiting", waiting)
    assert asyncio.run(run_waiting_llm_request_hooks(_event())) is True


def test_hook_counts_reports_registered(clean_registry):
    """诊断函数能如实报出各钩子数量（宿主自检用）。"""
    from astrbot_host.llm_hooks import hook_counts
    from astrbot_host.registry import EventType as ET

    async def h(event, req=None):
        return None

    _register(clean_registry, ET.OnLLMRequestEvent, "h1", h)
    _register(clean_registry, ET.OnLLMRequestEvent, "h2", h)
    _register(clean_registry, ET.OnLLMResponseEvent, "h3", h)

    counts = hook_counts()
    assert counts["OnLLMRequestEvent"] == 2
    assert counts["OnLLMResponseEvent"] == 1
    assert counts["OnWaitingLLMRequestEvent"] == 0
