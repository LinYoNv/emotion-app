# -*- coding: utf-8 -*-
"""可替换接口（ports）与实现（backends）的测试。

🔴 这组测试守的是**情绪那条要求**：「最终还是要以陪伴为核心，**接口别写死了**」。
   所以重点不是"当前实现能跑"，而是：
     1. 领域层**只依赖 Protocol**，不依赖具体实现；
     2. **换实现不改领域层**（用假后端验证）；
     3. 工具下发方式**可切换**（function calling ⇄ 文本标签）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from emotion_app.domain.backends import (
    DISPATCH_BACKENDS,
    FunctionCallDispatch,
    OpenAIChatBackend,
    OpenAIImageBackend,
    TextTagDispatch,
    build_dispatch,
)
from emotion_app.domain.image_gen import GeneratedImage, ImageTarget
from emotion_app.domain.image_tool import build_spec
from emotion_app.domain.message import Message
from emotion_app.domain.ports import ChatBackend, ImageBackend, ToolDispatch
from emotion_app.domain.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec


# ===========================================================================
# Protocol 一致性（结构化类型）
# ===========================================================================


def test_openai_backend_satisfies_protocol():
    """⭐ 实现满足 Protocol —— **不需要继承**（鸭子类型，便于对接别人的框架）。"""
    assert isinstance(OpenAIChatBackend(), ChatBackend)


def test_dispatch_impls_satisfy_protocol():
    assert isinstance(FunctionCallDispatch(), ToolDispatch)
    assert isinstance(TextTagDispatch(), ToolDispatch)


def test_image_backend_satisfies_protocol():
    from emotion_app.domain.image_gen import ImageClient

    backend = OpenAIImageBackend(
        ImageClient(), ImageTarget(base_url="https://x/v1", api_key="k", model="m")
    )
    assert isinstance(backend, ImageBackend)


# ===========================================================================
# ⭐ 换实现不改领域层（关键！）
# ===========================================================================


class FakeChatBackend:
    """一个**完全不依赖 OpenAI** 的假后端。

    它不继承任何东西，只实现 `ChatBackend` 的两个方法。
    用来证明：领域层换后端**不需要改代码**。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def chat(self, messages, **kw):  # type: ignore[no-untyped-def]
        from emotion_app.domain.upstream import ChatResult

        self.calls.append(kw)
        return ChatResult(content="来自假后端", model=kw.get("model", ""))

    async def chat_stream(self, messages, **kw):  # type: ignore[no-untyped-def]
        from emotion_app.domain.upstream import StreamChunk

        self.calls.append(kw)
        yield StreamChunk(delta="假", done=False)
        yield StreamChunk(delta="流式", done=False)
        yield StreamChunk(done=True)


async def test_chatservice_accepts_any_backend():
    """⭐ 领域层用假后端也能跑 —— 证明接口没写死。"""
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.upstream.model = "whatever"
    fake = FakeChatBackend()

    svc = ChatService(cfg, fake)
    req = ChatRequest(messages=[Message(role="user", content="你好")])
    result = await svc.complete(req)

    assert result.content == "来自假后端"
    assert fake.calls, "假后端应被调用"


async def test_chatservice_stream_with_fake_backend():
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.infra.config import Config

    cfg = Config()
    fake = FakeChatBackend()
    svc = ChatService(cfg, fake)

    got = []
    async for chunk in svc.stream(ChatRequest(messages=[Message(role="user", content="x")])):
        if chunk.delta:
            got.append(chunk.delta)
    assert "".join(got) == "假流式"


async def test_fake_backend_receives_connection_params():
    """连接参数（base_url/api_key/model）通过**调用参数**传，而非构造时绑定。

    ⚠️ 这样 per-persona / 运行时改配置才能生效。
    """
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.domain.persona import Persona
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.upstream.base_url = "https://global/v1"
    cfg.upstream.api_key = "sk-global"
    cfg.upstream.model = "global-model"

    fake = FakeChatBackend()
    svc = ChatService(cfg, fake)
    p = Persona(name="x", api_url="https://persona/v1", api_key="sk-p", model="pm")

    await svc.complete(ChatRequest(messages=[Message(role="user", content="q")], persona=p))

    call = fake.calls[0]
    assert call["base_url"] == "https://persona/v1"
    assert call["api_key"] == "sk-p"
    assert call["model"] == "pm"


# ===========================================================================
# 工具下发方式可切换
# ===========================================================================


def _specs() -> list[ToolSpec]:
    return [build_spec()]


def test_function_call_dispatch_builds_tools_field():
    d = FunctionCallDispatch()
    payload = d.build_payload(_specs())
    assert "tools" in payload
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "generate_image"


def test_function_call_dispatch_no_sections():
    """function calling 不需要把工具说明塞进提示词（走 schema）。"""
    assert FunctionCallDispatch().prompt_sections(_specs()) == []


def test_function_call_dispatch_renders_tool_role():
    """结果回填走 `role="tool"`。"""
    msg = FunctionCallDispatch().render_result(ToolResult(message="ok"), call_id="c1")
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1"


def test_text_tag_dispatch_builds_sections():
    """⭐ 文本标签方式必须把工具说明写进提示词（退路的关键）。"""
    d = TextTagDispatch()
    assert d.build_payload(_specs()) == {}       # 不改请求体
    sections = d.prompt_sections(_specs())
    assert len(sections) == 1
    assert "generate_image" in sections[0].content


def test_text_tag_dispatch_parses_inline_tag():
    """⭐ 比爱语宽容：标签**混在句中**也能解析。

    爱语的正则 `^<generate_image:.+>$` 强制独占整行，模型稍一发挥就失配。
    """
    d = TextTagDispatch()
    calls = d.extract_calls(type("R", (), {"content": "好的！<generate_image:一只猫>马上"})())
    assert len(calls) == 1
    assert json.loads(calls[0].arguments)["prompt"] == "一只猫"


def test_text_tag_dispatch_ignores_other_tags():
    d = TextTagDispatch()
    calls = d.extract_calls(type("R", (), {"content": "<add:记忆内容><search:关键词>"})())
    assert calls == []


def test_text_tag_dispatch_truncated_tag_yields_nothing(caplog):
    """🔴 截断时**必须有日志**（爱语是静默失败的）。"""
    d = TextTagDispatch()
    with caplog.at_level("WARNING"):
        calls = d.extract_calls(type("R", (), {"content": "被截断的 <generate_image:一只"})())
    assert calls == []
    assert any("解析失败" in r.message or "解析失败" in str(r.msg) for r in caplog.records)


def test_text_tag_dispatch_renders_user_role():
    """标签方式没有 tool 角色 → 用 user 消息回填。"""
    msg = TextTagDispatch().render_result(ToolResult(message="ok"), call_id="t1")
    assert msg.role == "user"
    assert "工具" in msg.content


def test_text_tag_dispatch_stream_assembles_then_parses():
    d = TextTagDispatch()
    frags = [{"delta": "<generate_"}, {"delta": "image:一只猫>"}]
    calls = d.extract_stream_calls(frags)
    assert len(calls) == 1
    assert json.loads(calls[0].arguments)["prompt"] == "一只猫"


def test_build_dispatch_known_names():
    assert isinstance(build_dispatch("function_call"), FunctionCallDispatch)
    assert isinstance(build_dispatch("text_tag"), TextTagDispatch)
    assert isinstance(build_dispatch("FUNCTION_CALL"), FunctionCallDispatch)  # 大小写无关


def test_build_dispatch_rejects_unknown():
    """🔴 未知名字要**明确报错**，不静默退回默认（否则"配了没生效"）。"""
    with pytest.raises(ValueError, match="未知工具下发方式"):
        build_dispatch("telepathy")


def test_dispatch_backends_registry():
    assert set(DISPATCH_BACKENDS) == {"function_call", "text_tag"}


# ===========================================================================
# 工具表 × 下发方式（端到端组合）
# ===========================================================================


async def test_registry_max_calls_per_turn():
    """🔴 每轮最多一次 —— 实测我们的上游**会重复调用**，这条是必需防护。"""
    calls: list[dict] = []

    async def handler(args: dict) -> ToolResult:
        calls.append(args)
        return ToolResult(message="ok", generated=True)

    reg = ToolRegistry(max_calls_per_turn=1)
    reg.register(ToolSpec(name="t", description="d"), handler)

    counts: dict[str, int] = {}
    first = await reg.execute(ToolCall(id="1", name="t", arguments="{}"), call_counts=counts)
    second = await reg.execute(ToolCall(id="2", name="t", arguments="{}"), call_counts=counts)

    assert first.success is True
    assert second.status == "already_called"
    assert second.must_not_claim_sent is True
    assert len(calls) == 1, "第二次不该真的执行"


async def test_registry_unknown_tool_is_explicit():
    reg = ToolRegistry()
    r = await reg.execute(ToolCall(id="1", name="nope", arguments="{}"))
    assert r.success is False
    assert r.status == "unknown_tool"
    assert r.must_not_claim_sent is True


async def test_registry_handler_exception_becomes_result():
    async def boom(args: dict) -> ToolResult:
        raise RuntimeError("炸了")

    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d"), boom)
    r = await reg.execute(ToolCall(id="1", name="t", arguments="{}"))
    assert r.success is False
    assert r.status == "error"
    assert r.must_not_claim_sent is True
    assert "炸了" in r.actual_error


async def test_registry_bad_json_arguments():
    """参数不是合法 JSON → 按空参数处理，**不炸**。"""
    seen: list[dict] = []

    async def handler(args: dict) -> ToolResult:
        seen.append(args)
        return ToolResult()

    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d"), handler)
    await reg.execute(ToolCall(id="1", name="t", arguments="{broken"))
    assert seen == [{}]


def test_registry_rejects_duplicate_name():
    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d"), lambda a: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="重名"):
        reg.register(ToolSpec(name="t", description="d"), lambda a: None)  # type: ignore[arg-type]


# ===========================================================================
# 生图后端可替换
# ===========================================================================


async def test_image_backend_generates():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["prompt"] == "一只猫"
        assert body["size"] == "1024x1024"
        return httpx.Response(200, json={
            "model": "dall-e-3",
            "data": [{"b64_json": "aGVsbG8="}],       # "hello"
        })

    from emotion_app.domain.image_gen import ImageClient

    client = ImageClient(transport=httpx.MockTransport(handler))
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="dall-e-3")
    )
    await backend.open()
    try:
        result = await backend.generate(prompt="一只猫")
    finally:
        await backend.close()

    assert result.count == 1
    assert result.images[0].b64 == "aGVsbG8="


async def test_image_backend_per_call_override():
    """⭐ 端点可按次覆盖（人设级生图后端 / 用户自定义）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"b64_json": "aGk="}]})

    from emotion_app.domain.image_gen import ImageClient

    client = ImageClient(transport=httpx.MockTransport(handler))
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://default/v1", api_key="k1", model="m1")
    )
    await backend.open()
    try:
        await backend.generate(prompt="x", base_url="https://override/v1", api_key="k2")
    finally:
        await backend.close()

    assert "override" in seen["url"]
    assert seen["auth"] == "Bearer k2"


async def test_image_backend_reports_unconfigured():
    from emotion_app.domain.image_gen import ImageClient

    backend = OpenAIImageBackend(ImageClient(), ImageTarget())
    assert backend.is_configured() is False


def test_image_backend_name_is_stable():
    """后端名用于日志与诊断 —— 换了后端要能看出来。"""
    from emotion_app.domain.image_gen import ImageClient

    assert OpenAIImageBackend(ImageClient(), ImageTarget()).name == "openai-images"
