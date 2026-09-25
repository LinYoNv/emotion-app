# -*- coding: utf-8 -*-
"""`context.llm_generate` 的契约测试（宿主侧）。

## 为什么单开这个文件

它**此前零覆盖** —— 而实测生产日志里有 **38 条**：

```
WARNING astrbot: [Token预算] LLM 调用失败: task=daily_plan
        provider=fast/gemini-3.5-flash error=宿主未接入模型调用能力（`HostPorts.llm` 为空）。
```

即：陪伴插件的**记忆抽取 / 每日计划 / 日记 / 每日回顾**四个机制
**全部跑不动**，而它们正是"陪伴感"的主体。

## 被测契约（照真实现 `astrbot/core/star/context.py:174-213`）

真实现的 `llm_generate` **不是**把请求直接丢给某个端口，而是三段：

```
chat_provider_id  →  provider_manager.get_provider_by_id(id)  →  prov.text_chat(...)
                                                                        ↓
                                                                   LLMResponse
```

🔴 **两个必须成立的形状**（否则插件会**静默**拿到空字符串）：

1. 返回**必须是 `LLMResponse`**（有 `completion_text`），不是我们的 `ChatResult`。
   插件读的是 `getattr(response, "completion_text", "")`
   （`token_budget.py:1556`）—— 属性不存在时 `getattr` 返回**默认空串**，
   **不报错**，于是"模型答了但解析不出内容" → 静默失败。
2. `max_tokens` 等 OpenAI 兼容参数**必须真的进上游请求体**。
   插件用它控制成本（`token_budget.py:1530`）；
   静默丢掉会让"预算"形同虚设，而且**没有任何报错**。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from astrbot_host.compat.core.provider.entities import LLMResponse  # noqa: E402
from astrbot_host.compat.core.star.context import Context, HostPorts  # noqa: E402
from astrbot_host.provider import Provider, ProviderManager  # noqa: E402


class _RecordingLLM:
    """记录 `generate()` 收到的参数，并返回一个**领域层形状**的结果。

    ⚠️ 刻意返回 `ChatResult`（只有 `.content`、**没有** `.completion_text`）——
       这正是 `emotion_app.domain.upstream.ChatResult` 的真实形状，
       所以这个桩能真实复现"插件读不到文本"的缺陷。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        from emotion_app.domain.upstream import ChatResult

        return ChatResult(
            content="模型答复文本",
            model="test-model",
            usage={"prompt_tokens": 3, "completion_tokens": 5},
        )


def _ctx(llm: Any = None, providers: list[tuple[str, str]] | None = None) -> Context:
    """造一个带供应商管理器的 Context。"""
    pm = ProviderManager()
    for pid, model in providers or [("fast/gemini-3.5-flash", "gemini-3.5-flash")]:
        pm.register(
            Provider(provider_id=pid, model=model, llm=llm),
        )
    pm.set_default(providers[0][0] if providers else "")
    return Context(ports=HostPorts(llm=llm, provider_manager=pm))


# ---------------------------------------------------------------------------
# ① 返回形状：必须是 LLMResponse（有 completion_text）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_generate_returns_llm_response_not_chat_result():
    """🔴 核心判据：返回对象必须有可读的 `completion_text`。

    注入 bug 方式：把 `llm_generate` 改回 `return await llm.generate(**kwargs)`
    （即返回裸 `ChatResult`）→ 本判据必须挂。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm)

    resp = await ctx.llm_generate(
        chat_provider_id="fast/gemini-3.5-flash",
        prompt="你好",
    )

    assert isinstance(resp, LLMResponse), (
        f"返回的不是 LLMResponse 而是 {type(resp).__name__} —— "
        "插件会 getattr(response,'completion_text','') 拿到**空串**（静默失败）"
    )
    assert resp.completion_text == "模型答复文本", (
        f"completion_text 读不到内容：{resp.completion_text!r}"
    )


@pytest.mark.asyncio
async def test_llm_generate_text_is_survivable_by_plugin_read_pattern():
    """照插件**逐字**的读法再验一次（`token_budget.py:1556`）。

    插件写的是 `str(getattr(response, "completion_text", "") or "").strip()`。
    这里用同一行代码取值 —— 若返回形状不对，它会**安静地**得到空串。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm)

    response = await ctx.llm_generate(
        chat_provider_id="fast/gemini-3.5-flash", prompt="你好"
    )
    completion = str(getattr(response, "completion_text", "") or "").strip()

    assert completion == "模型答复文本", (
        f"照插件的读法拿到 {completion!r}（应为模型答复）—— "
        "形状不对时插件**不会报错**，只会以为模型没说话"
    )


# ---------------------------------------------------------------------------
# ② 参数传递：max_tokens 必须进上游
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_generate_forwards_max_tokens_to_upstream():
    """🔴 `max_tokens` 不能静默丢 —— 插件靠它控制成本。

    注入 bug：在 `llm_generate` 里把 `**kwargs` 丢掉 → 本判据必须挂。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm)

    await ctx.llm_generate(
        chat_provider_id="fast/gemini-3.5-flash",
        prompt="你好",
        max_tokens=512,
    )

    assert llm.calls, "上游根本没被调用"
    # `max_tokens` 可能在顶层 kwargs，也可能已被并进 extra
    got = llm.calls[0].get("max_tokens")
    if got is None:
        got = (llm.calls[0].get("extra") or {}).get("max_tokens")
    assert got == 512, (
        f"max_tokens 没传到上游（收到 {llm.calls[0]!r}）—— "
        "插件用它控成本，静默丢掉会让预算形同虚设且无任何报错"
    )


@pytest.mark.asyncio
async def test_llm_generate_forwards_system_prompt_and_contexts():
    """system_prompt 与 contexts 都要到上游（否则人设/上下文丢失）。"""
    llm = _RecordingLLM()
    ctx = _ctx(llm)

    await ctx.llm_generate(
        chat_provider_id="fast/gemini-3.5-flash",
        prompt="这一轮",
        system_prompt="你是助手",
        contexts=[{"role": "user", "content": "上一轮"}],
    )

    call = llm.calls[0]
    assert call.get("system_prompt") == "你是助手", f"system_prompt 丢了：{call!r}"
    msgs = call.get("messages") or []
    assert any("上一轮" in str(m.get("content", "")) for m in msgs), (
        f"contexts 丢了：{msgs!r}"
    )


# ---------------------------------------------------------------------------
# ③ 如实失败（不能静默降级）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_generate_without_port_raises_loudly():
    """没接模型端口 → **响亮抛错**，绝不返回空回复。

    理由（项目铁律）：返回假回复会让插件把编造内容写进记忆库，
    比失败糟得多。
    """
    ctx = Context(ports=HostPorts(llm=None))
    with pytest.raises(Exception) as ei:
        await ctx.llm_generate(chat_provider_id="x/y", prompt="hi")
    assert "模型" in str(ei.value) or "llm" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_llm_generate_unknown_provider_raises_loudly():
    """供应商 id 不存在 → 抛错（照真实现 `ProviderNotFoundError`）。

    ⚠️ 不能"退回到默认供应商"——那会让插件以为用的是 A，实际用了 B
       （记忆/日程会写进错误的模型配置下）。配置错误必须可见。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm)
    with pytest.raises(Exception):
        await ctx.llm_generate(chat_provider_id="不存在/模型", prompt="hi")
    assert not llm.calls, "供应商不存在却仍然调了上游"


@pytest.mark.asyncio
async def test_llm_generate_without_chat_provider_id_uses_default():
    """**不传** `chat_provider_id` 时退回默认供应商（真实现同款）。

    与上一条的区别：上一条是"给了一个不存在的 id"（配置错误，要抛）；
    这一条是"没给 id"（正常用法，用默认）。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm)

    resp = await ctx.llm_generate(prompt="你好")

    assert isinstance(resp, LLMResponse)
    assert resp.completion_text == "模型答复文本"
    assert llm.calls, "默认供应商没有被调用"


# ---------------------------------------------------------------------------
# ④ 兜底路径：接了 llm 端口但一个供应商都没注册
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_generate_falls_back_to_llm_port_when_no_provider_registered():
    """接上了 `HostPorts.llm`、但**一个供应商都没注册** → 走端口，仍返回 LLMResponse。

    🔴 为什么要专门验这条：一开始 38 条生产 ERROR 的教训是 ——
       「端口接上了」与「供应商配好了」是**两件事**，
       只做其中一件，插件就有一半调用点打不通。
       这条守住"只接端口"这个中间状态也能用。
    """
    llm = _RecordingLLM()
    # ⚠️ 刻意**不注册任何供应商**
    ctx = Context(ports=HostPorts(llm=llm, provider_manager=ProviderManager()))

    resp = await ctx.llm_generate(prompt="你好")

    assert isinstance(resp, LLMResponse), f"返回 {type(resp).__name__}，应为 LLMResponse"
    assert resp.completion_text == "模型答复文本"
    assert llm.calls, "端口没有被调用"


@pytest.mark.asyncio
async def test_llm_generate_fallback_still_forwards_max_tokens():
    """兜底路径也要透传 `max_tokens`（不能因为换了条路就丢参数）。"""
    llm = _RecordingLLM()
    ctx = Context(ports=HostPorts(llm=llm, provider_manager=ProviderManager()))

    await ctx.llm_generate(prompt="你好", max_tokens=77)

    call = llm.calls[0]
    got = call.get("max_tokens") or (call.get("extra") or {}).get("max_tokens")
    assert got == 77, f"兜底路径丢了 max_tokens：{call!r}"


@pytest.mark.asyncio
async def test_llm_generate_prefers_registered_provider_over_port():
    """有注册供应商时**优先走供应商**（它带着自己的 model/源，比裸端口更准）。

    注入 bug：把 `_resolve_chat_provider` 短路成总返回 None → 本判据必须挂。
    """
    llm = _RecordingLLM()
    ctx = _ctx(llm, providers=[("fast/gemini-3.5-flash", "gemini-3.5-flash")])

    await ctx.llm_generate(prompt="你好")

    call = llm.calls[0]
    assert call.get("model") == "gemini-3.5-flash", (
        f"没有用供应商自己的 model（收到 {call.get('model')!r}）—— "
        "说明走的是兜底端口而不是注册的供应商"
    )
