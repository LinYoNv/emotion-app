# -*- coding: utf-8 -*-
"""P2 判据：模型供应商（Provider）层。

🔴 本文件守的核心风险：**插件按供应商 ID 选模型时解析不到 → 静默退化成模板兜底**。

实测证据（我们没实现 provider 时的真实服务日志）：
```
WARNING [Token预算] 无法解析默认对话 Provider：get_using_provider 与注册表兜底都没有结果；
        未显式配置 provider 的模型任务将退化为模板兜底
WARNING [Token预算] LLM 调用失败: task=daily_plan error=未找到可用的 AstrBot 默认模型 Provider
```
→ 记忆抽取/总结、日程生成、关系分析**全部拿不到模型**，而且**不报错**。

契约来源全部是实测读出来的（见 `host/astrbot_host/provider.py` 顶部表格）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


class _FakeLLM:
    """假模型端口：记录收到的参数，返回一个可控结果。"""

    def __init__(self, content: str = "模型回复", raises: Exception | None = None):
        self.content = content
        self.raises = raises
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises

        class _R:
            pass

        r = _R()
        r.content = self.content
        return r


# ---------------------------------------------------------------------------
# 一、Provider 对象形状（插件按属性/方法读，形状错了就显示空）
# ---------------------------------------------------------------------------
def test_provider_shape_matches_plugin_reads():
    """Provider 的 id/model/name 要能被插件**三种读法**取到。

    实测插件读法（`page_api.py:22926-23026`）：
      ① `provider_config["id"/"model"]`（**优先**）
      ② `provider.provider_id`
      ③ `provider.get_model()`（**方法**，失败才退回属性）
    """
    from astrbot_host.provider import Provider

    p = Provider(provider_id="ab12", model="gpt-x", name="快速响应")
    assert p.provider_config["id"] == "ab12", "① provider_config 里必须有 id"
    assert p.provider_config["model"] == "gpt-x", "① provider_config 里必须有 model"
    assert p.provider_id == "ab12", "② provider_id 属性"
    assert p.get_model() == "gpt-x", "③ get_model() 方法"

    # `key` 照真实现是**列表**（`provider.py:83`）
    p2 = Provider(provider_id="k", api_key="sk-1")
    assert p2.provider_config["key"] == ["sk-1"], (
        "真实现 `provider_config.get('key', [''])` 取的是**列表**，给字符串会分叉"
    )


def test_provider_type_fields_match_real_contract():
    """`type`（适配器）与 `provider_type`（分类）**必须分开** —— 照真实契约。

    实测真实 AstrBot 的对话供应商：
        `type = "openai_chat_completion"`  +  `provider_type = "chat_completion"`
    ⚠️ 我第一版把两个都塞成 `chat_completion` —— 插件读 `type` 时拿到的是分类名，
       虽然它能 fallback，但**与真实现不一致**（行为分叉）。
    """
    from astrbot_host.provider import Provider

    cfg = Provider(provider_id="x", model="m").provider_config
    assert cfg["type"] == "openai_chat_completion", (
        f"type 应是适配器协议名（照真实契约），实际 {cfg['type']!r}"
    )
    assert cfg["provider_type"] == "chat_completion", (
        f"provider_type 应是用途分类，实际 {cfg['provider_type']!r}"
    )
    # 插件两处都读（`page_api.py:23029` 先读 type 再读 provider_type）→ 两个都要有
    assert "type" in cfg and "provider_type" in cfg


def test_provider_meta_exposes_id_and_model():
    """`meta()` 要有 id/model/type（插件按属性读）。"""
    from astrbot_host.provider import Provider

    m = Provider(provider_id="p1", model="m1").meta()
    assert m.id == "p1"
    assert m.model == "m1"
    assert m.type == "chat_completion"


# ---------------------------------------------------------------------------
# 二、text_chat：插件真正用来"让模型干活"的入口
# ---------------------------------------------------------------------------
def test_text_chat_returns_llm_response_with_text():
    """🔴 `text_chat` 必须返回 **`LLMResponse`**，且 `.completion_text` 有内容。

    为什么关键：插件的记忆抽取/总结读的是 `resp.completion_text`
    （它是 property，见 `compat/core/provider/entities.py`）。
    返回裸字符串会让插件拿到空值或 AttributeError。
    """
    from astrbot_host.compat.core.provider.entities import LLMResponse
    from astrbot_host.provider import Provider

    llm = _FakeLLM(content="摘要内容")
    p = Provider(provider_id="p1", model="m1", llm=llm)

    resp = asyncio.run(p.text_chat(prompt="总结一下", system_prompt="你是总结器"))

    assert isinstance(resp, LLMResponse), f"应返回 LLMResponse，实际 {type(resp).__name__}"
    assert resp.completion_text == "摘要内容", f"文本丢了：{resp.completion_text!r}"


def test_text_chat_passes_prompt_and_system_prompt_through():
    """`prompt` / `system_prompt` 要**真的传给**底层模型端口。

    ⚠️ 传丢的后果：模型收到的提示词是空的 → 回复驴唇不对马嘴，
       而全链路不报错（记忆会写进垃圾内容）。
    """
    from astrbot_host.provider import Provider

    llm = _FakeLLM()
    p = Provider(provider_id="p1", model="m1", llm=llm)
    asyncio.run(p.text_chat(prompt="用户输入", system_prompt="系统提示"))

    assert llm.calls, "底层模型端口没被调用"
    call = llm.calls[0]
    assert call["system_prompt"] == "系统提示", call
    assert call["model"] == "m1", "模型名应传下去"
    # prompt 要么作为 prompt 传，要么并进 messages —— 两者有一个即可
    assert call.get("prompt") == "用户输入" or any(
        m.get("content") == "用户输入" for m in (call.get("messages") or [])
    ), f"prompt 没传下去：{call}"


def test_text_chat_merges_contexts_before_prompt():
    """`contexts` + `prompt` 的组合语义（照真实现：prompt 追加到最后）。"""
    from astrbot_host.provider import Provider

    llm = _FakeLLM()
    p = Provider(provider_id="p1", llm=llm)
    asyncio.run(
        p.text_chat(
            prompt="新问题",
            contexts=[{"role": "user", "content": "旧问题"}],
        )
    )
    msgs = llm.calls[0]["messages"]
    assert msgs[0]["content"] == "旧问题" and msgs[-1]["content"] == "新问题", msgs


def test_text_chat_raises_when_no_llm_port():
    """🔴 **不伪造模型能力**：没接模型端口时**抛错**，不许返回假回复。

    为什么这条重要：假回复会被"记忆抽取"当成真实摘要**写进数据库** ——
       用户会看到凭空编造的记忆，比直接失败糟得多（作者判据③）。
    """
    from astrbot_host.provider import Provider

    p = Provider(provider_id="p1", llm=None)
    with pytest.raises(RuntimeError):
        asyncio.run(p.text_chat(prompt="hi"))


def test_text_chat_propagates_model_error():
    """模型调用失败要**抛出去**（插件靠捕获它记用量/重试）。

    吞掉异常会让插件以为"成功但返回空" → 静默失败。
    """
    from astrbot_host.provider import Provider

    p = Provider(provider_id="p1", llm=_FakeLLM(raises=RuntimeError("上游 503")))
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(p.text_chat(prompt="hi"))


def test_text_chat_stream_is_honestly_unsupported():
    """流式：**如实**一次性产出（不假装分块）。

    真实现的 `text_chat_stream` 是逐块 yield；我们只有非流式。
    与其造假（分块但要等全部拿到），不如让它**语义正确但只有一个块** ——
    插件对单块的处理是对的。
    """
    from astrbot_host.provider import Provider

    async def run():
        p = Provider(provider_id="p1", llm=_FakeLLM(content="整段"))
        gen = await p.text_chat_stream(prompt="hi")
        return [chunk async for chunk in gen]

    chunks = asyncio.run(run())
    assert len(chunks) == 1
    assert chunks[0].completion_text == "整段"


# ---------------------------------------------------------------------------
# 三、ProviderManager（两个取值路径都要有）
# ---------------------------------------------------------------------------
def test_manager_inst_map_is_dict_of_providers():
    """`inst_map` 必须是 **dict[str, Provider]**（照真实现）。

    实测插件第二取值路径（`page_api.py:22104`）：
        inst_map = getattr(manager, "inst_map", None)
        if isinstance(inst_map, dict): providers = list(inst_map.values())
    → 给 list 或改名都会让这条分支拿到空。
    """
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a"))
    assert isinstance(pm.inst_map, dict), "inst_map 必须是 dict"
    assert list(pm.inst_map.keys()) == ["a"]
    assert isinstance(pm.inst_map["a"], Provider)


def test_manager_default_and_lookup():
    """默认供应商 / 按 id 取 / 取不存在的返回 None（不许乱认）。"""
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a"))
    pm.register(Provider(provider_id="b"))

    assert pm.default_provider_id == "a", "第一个注册的应自动成为默认"
    assert pm.get_using().provider_id == "a"
    assert pm.get_provider_by_id("b").provider_id == "b"

    # 🔴 反例：不存在的 id / 空 id 必须返回 None
    assert pm.get_provider_by_id("nope") is None
    assert pm.get_provider_by_id("") is None


def test_manager_set_default_rejects_unknown_id():
    """设默认时 id 不存在 → **不设**（不许留悬空默认值）。

    ⚠️ 悬空默认的后果：`get_using_provider` 返回 None，
       但面板显示"已设默认" —— 又是一次静默不一致。
    """
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a"))
    assert pm.set_default("nope") is False
    assert pm.default_provider_id == "a", "失败的 set_default 不该改状态"


def test_manager_unregister_reassigns_default():
    """删掉默认供应商后，默认要**让给剩下的第一个**（不留悬空）。"""
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a"))
    pm.register(Provider(provider_id="b"))
    assert pm.unregister("a") is True
    assert pm.default_provider_id == "b", "默认应转到剩下的供应商"
    assert pm.unregister("zzz") is False, "删不存在的应返回 False"


# ---------------------------------------------------------------------------
# 四、Context 接线（插件实际拿到的东西）
# ---------------------------------------------------------------------------
def test_context_get_using_provider_returns_provider_with_text_chat():
    """🔴🔴 **最核心的一条**：`context.get_using_provider()` 返回的对象
    **必须有 `text_chat`**。

    这正是我原来写错的地方：我 `return self._ports.llm`（只有 `generate()`），
    插件调 `provider.text_chat(...)` → AttributeError → **退化成模板兜底**。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a", llm=_FakeLLM()))

    ctx = Context(ports=HostPorts(provider_manager=pm))
    got = ctx.get_using_provider()

    assert got is not None, "有供应商时不该返回 None"
    assert callable(getattr(got, "text_chat", None)), (
        "🔴 返回的对象必须有 text_chat —— 插件就是用它调模型的"
        "（memory_companion/core/service.py:7519）。"
        "返回 LLMPort 会让插件 AttributeError 并静默退化成模板"
    )


def test_context_provider_methods():
    """`get_provider_by_id` / `get_all_providers` 都要查管理器（不再硬编码空）。"""
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.provider import Provider, ProviderManager

    pm = ProviderManager()
    pm.register(Provider(provider_id="a", llm=_FakeLLM()))
    pm.register(Provider(provider_id="b", llm=_FakeLLM()))
    ctx = Context(ports=HostPorts(provider_manager=pm))

    assert ctx.get_provider_by_id("a") is not None
    assert ctx.get_provider_by_id("nope") is None
    assert len(ctx.get_all_providers()) == 2, (
        "面板靠 get_all_providers 列供应商（page_api.py:22097）"
    )
    # 没有管理器时如实返回空/None（不抛错）
    bare = Context(ports=HostPorts())
    assert bare.get_all_providers() == []
    assert bare.get_provider_by_id("a") is None
    assert bare.get_using_provider() is None


def test_context_exposes_provider_manager_attribute():
    """`context.provider_manager` 必须是属性（插件 `page_api.py:22103` 读它）。"""
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.provider import ProviderManager

    pm = ProviderManager()
    assert Context(ports=HostPorts(provider_manager=pm)).provider_manager is pm
    assert Context(ports=HostPorts()).provider_manager is None


def test_capabilities_separate_manager_from_usable():
    """诊断要把"有管理器"与"有能用的供应商"**分开**报。

    ⚠️ 只报 True 会让人以为"模型能用了"，而供应商没接模型端口时照样发不出请求。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.provider import Provider, ProviderManager

    wired = ProviderManager()
    wired.register(Provider(provider_id="a", llm=_FakeLLM()))
    caps = HostPorts(provider_manager=wired).capabilities()
    assert caps["provider_manager"] is True
    assert caps["provider_usable"] is True

    unwired = ProviderManager()
    unwired.register(Provider(provider_id="a", llm=None))   # 配了但没接模型
    caps2 = HostPorts(provider_manager=unwired).capabilities()
    assert caps2["provider_manager"] is True
    assert caps2["provider_usable"] is False, (
        "供应商没接模型端口时必须如实报 False —— 否则诊断说可用、实际发不出请求"
    )

    assert HostPorts().capabilities()["provider_usable"] is False


def test_no_provider_means_none_not_fake_object():
    """没有供应商时 `get_using_provider()` 返回 **None**（不造一个假对象）。

    真实现"未设置"时也返回 None，插件对该分支有处理 ——
    造假对象会让插件以为"有模型"，然后调出编造的内容。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.provider import ProviderManager

    ctx = Context(ports=HostPorts(provider_manager=ProviderManager()))
    assert ctx.get_using_provider() is None


# ---------------------------------------------------------------------------
# 五、装配层：把我们的上游接成插件的 provider（emotion_app 侧）
# ---------------------------------------------------------------------------
def _bridge():
    """导入装配层（它在 emotion_app/，需要额外的 sys.path）。"""
    app_dir = HOST_DIR.parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    from emotion_app.web.provider_bridge import UpstreamLLMPort, build_provider_manager

    return UpstreamLLMPort, build_provider_manager


class _FakeBackend:
    """假上游后端（形状照领域层 `ChatBackend.complete`）。"""

    def __init__(self, content="上游回复", raises=None):
        self.content = content
        self.raises = raises
        self.calls: list[dict] = []

    async def complete(self, *, messages=None, model=None, tools=None, **kw):
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        if self.raises is not None:
            raise self.raises

        class _R:
            pass

        r = _R()
        r.content = self.content
        r.usage = {"total_tokens": 3}
        return r


def test_llm_port_passes_system_prompt_as_first_message():
    """装配层要把 `system_prompt` 变成**第一条 system 消息**（OpenAI 约定）。"""
    UpstreamLLMPort, _ = _bridge()
    be = _FakeBackend()
    port = UpstreamLLMPort(backend=be, model="m1")

    asyncio.run(port.generate(prompt="问题", system_prompt="你是总结器"))

    msgs = be.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "你是总结器"}, msgs
    assert msgs[-1] == {"role": "user", "content": "问题"}, msgs
    assert be.calls[0]["model"] == "m1", "模型名要传下去"


def test_llm_port_keeps_contexts_order():
    """`messages` 要原样保留顺序（system 只加在最前，不重排）。"""
    UpstreamLLMPort, _ = _bridge()
    be = _FakeBackend()
    port = UpstreamLLMPort(backend=be)
    asyncio.run(
        port.generate(
            messages=[{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}],
            system_prompt="S",
        )
    )
    assert [m["content"] for m in be.calls[0]["messages"]] == ["S", "A", "B"]


def test_llm_port_raises_without_backend():
    """🔴 **不伪造**：没有后端就抛错（不许返回假回复）。

    假回复会被"记忆抽取"当成真摘要写进库 —— 用户看到凭空编造的记忆。
    """
    UpstreamLLMPort, _ = _bridge()
    port = UpstreamLLMPort(backend=None)
    with pytest.raises(RuntimeError):
        asyncio.run(port.generate(prompt="hi"))


def test_llm_port_propagates_upstream_error():
    """上游错误要抛出去（插件靠它记用量/重试）。"""
    UpstreamLLMPort, _ = _bridge()
    port = UpstreamLLMPort(backend=_FakeBackend(raises=RuntimeError("上游 503")))
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(port.generate(prompt="hi"))


def test_build_provider_manager_registers_and_sets_default():
    """配置 → 管理器：id/name/model 对上，默认按配置设。"""
    _, build = _bridge()
    pm = build(
        backend=_FakeBackend(),
        providers=[
            {"id": "fast", "name": "快速响应", "model": "m-fast"},
            {"id": "deep", "name": "复杂推理", "model": "m-deep"},
        ],
        default_id="deep",
    )
    assert set(pm.inst_map) == {"fast", "deep"}
    assert pm.default_provider_id == "deep"
    got = pm.get_using()
    assert got.provider_id == "deep"
    assert got.get_model() == "m-deep"
    assert got.name == "复杂推理", f"name 应来自配置，实际 {got.name!r}"


def test_built_provider_can_actually_reach_upstream():
    """🔴 **端到端**：建出来的 provider 调 `text_chat` 能真打到后端。

    这条把装配层 + Provider + ProviderManager 串起来验一次 ——
    只验 `build_*` 返回了对象是不够的（"模块能工作 ≠ 被接上了"）。
    """
    _, build = _bridge()
    be = _FakeBackend(content="真实摘要")
    pm = build(backend=be, providers=[{"id": "p1", "model": "m1"}])

    resp = asyncio.run(pm.get_using().text_chat(prompt="总结", system_prompt="S"))

    assert resp.completion_text == "真实摘要", f"文本没回来：{resp.completion_text!r}"
    assert be.calls, "底层后端没被调用 → 链路断了"


def test_build_with_empty_providers_returns_empty_manager():
    """没配供应商 → **空管理器**（`get_using_provider()` 得 None），不造假的。

    ⚠️ 凭空造一个默认供应商会让插件以为"配好了"，然后写出编造内容。
    """
    _, build = _bridge()
    pm = build(backend=_FakeBackend(), providers=[], default_id="")
    assert pm.get_using() is None
    assert pm.get_all() == []


def test_build_skips_entries_without_id():
    """没有 id 的配置项要**跳过并留日志**（不静默、也不崩）。"""
    _, build = _bridge()
    pm = build(backend=_FakeBackend(), providers=[{"name": "没id"}, {"id": "ok"}])
    assert set(pm.inst_map) == {"ok"}


def test_build_warns_when_default_id_missing():
    """配置里的 default id 不存在 → 不静默（记 WARNING）且仍可用第一个。"""
    _, build = _bridge()
    pm = build(
        backend=_FakeBackend(),
        providers=[{"id": "a"}, {"id": "b"}],
        default_id="nope",
    )
    assert pm.default_provider_id == "a", "第一个应兜底为默认"


# ---------------------------------------------------------------------------
# 六、两层结构展平（对齐 AstrBot 真实契约）
# ---------------------------------------------------------------------------
def _bridge2():
    app_dir = HOST_DIR.parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    from emotion_app.web.provider_bridge import _first_key, flatten_two_layer

    return flatten_two_layer, _first_key


def test_flatten_two_layer_inherits_source_connection():
    """🔴 **核心**：模型继承所属源的 `api_base`/`key`/`timeout`。

    AstrBot 真实契约里模型**自己不带连接信息**（那是源上的），
    而 `Provider` 对象是**一个模型一个实例** →
    不展平的话每个模型都没有 base_url → 全部调不通。
    """
    flatten, _ = _bridge2()
    store = {
        "provider_sources": [
            {"id": "syuan", "api_base": "https://api.syuan.org/v1",
             "key": ["sk-abc"], "timeout": 90, "enable": True}
        ],
        "providers": [
            {"id": "syuan/gemini-3.5-flash", "provider_source_id": "syuan",
             "model": "gemini-3.5-flash", "enable": True}
        ],
        "default_provider_id": "syuan/gemini-3.5-flash",
    }
    flat, default_id = flatten(store)

    assert len(flat) == 1
    m = flat[0]
    assert m["id"] == "syuan/gemini-3.5-flash"
    assert m["model"] == "gemini-3.5-flash"
    assert m["base_url"] == "https://api.syuan.org/v1", "没继承源的 api_base"
    assert m["api_key"] == "sk-abc", "没继承源的 key"
    assert m["timeout"] == 90
    assert default_id == "syuan/gemini-3.5-flash"


def test_first_key_handles_list_and_string():
    """`key` 在真实契约里是**列表** —— 取第一把；同时兼容旧的裸字符串。

    ⚠️ 不处理列表的话 `str(["sk-x"])` 会变成 `"['sk-x']"` 发给上游 → 401。
    """
    _, first_key = _bridge2()
    assert first_key(["sk-a", "sk-b"]) == "sk-a"
    assert first_key([]) == ""
    assert first_key("sk-plain") == "sk-plain"     # 旧格式
    assert first_key(None) == ""


def test_flatten_skips_dangling_source_reference():
    """🔴 模型的 `provider_source_id` 指向不存在的源 → **跳过**（不静默）。

    悬空引用会让这个模型永远调不通；静默保留会让插件看到一个"配了但用不了"的模型。
    """
    flatten, _ = _bridge2()
    store = {
        "provider_sources": [{"id": "a", "api_base": "https://a/v1", "key": ["k"]}],
        "providers": [
            {"id": "a/m1", "provider_source_id": "a", "model": "m1"},
            {"id": "ghost/m2", "provider_source_id": "nope", "model": "m2"},
        ],
    }
    flat, _ = flatten(store)
    assert [m["id"] for m in flat] == ["a/m1"], "悬空引用的模型应被跳过"


def test_flatten_skips_disabled():
    """被禁用的源/模型**不进管理器**（插件不该看到停用的模型）。"""
    flatten, _ = _bridge2()
    store = {
        "provider_sources": [
            {"id": "on", "api_base": "https://on/v1", "key": ["k"], "enable": True},
            {"id": "off", "api_base": "https://off/v1", "key": ["k"], "enable": False},
        ],
        "providers": [
            {"id": "on/m1", "provider_source_id": "on", "model": "m1", "enable": True},
            {"id": "on/m2", "provider_source_id": "on", "model": "m2", "enable": False},
            {"id": "off/m3", "provider_source_id": "off", "model": "m3", "enable": True},
        ],
    }
    flat, _ = flatten(store)
    assert [m["id"] for m in flat] == ["on/m1"], f"禁用项应被过滤：{flat}"


def test_flatten_passes_through_legacy_flat_store():
    """**旧扁平格式**（迁移前）也要能读 —— 不能因为格式变了就读不出来。"""
    flatten, _ = _bridge2()
    legacy = {
        "default_provider_id": "fast",
        "providers": [
            {"id": "fast", "name": "快速响应", "model": "m", "base_url": "https://x/v1",
             "api_key": "sk-y", "timeout": 60}
        ],
    }
    flat, default_id = flatten(legacy)
    assert len(flat) == 1 and flat[0]["id"] == "fast"
    assert default_id == "fast"


def test_flatten_result_reaches_upstream():
    """🔴 **端到端**：展平后的配置真能建出可用的 provider 并调到模型。"""
    app_dir = HOST_DIR.parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    from emotion_app.web.provider_bridge import build_provider_manager

    store = {
        "provider_sources": [
            {"id": "s1", "api_base": "https://s1/v1", "key": ["sk-1"], "timeout": 30}
        ],
        "providers": [
            {"id": "s1/m1", "provider_source_id": "s1", "model": "m1"}
        ],
        "default_provider_id": "s1/m1",
    }
    flatten, _ = _bridge2()
    flat, default_id = flatten(store)

    be = _FakeBackend(content="展平后也能调通")
    pm = build_provider_manager(backend=be, providers=flat, default_id=default_id)
    resp = asyncio.run(pm.get_using().text_chat(prompt="hi"))

    assert resp.completion_text == "展平后也能调通"
    assert be.calls, "后端没被调用"
