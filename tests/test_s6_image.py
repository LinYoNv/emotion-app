# -*- coding: utf-8 -*-
"""S6 生图工具测试：工具契约 · 投递 · 两条路径（流式/非流式）。

🔴 本组测试守的是**已经踩过的坑**：
   * 非流式漏传附件 → **图生成了但用户看不到**（实测踩到）
   * 流式工具执行后不二次请求 → **只有图、没有正文**（实测踩到）
   * `sent` 恒为 False → 模型按防幻觉规则说"未能展示"，但其实成功了
     （**过度保守也是错的**）
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from emotion_app.domain.backends import (
    FunctionCallDispatch,
    OpenAIChatBackend,
    OpenAIImageBackend,
)
from emotion_app.domain.chat import _extract_attachments
from emotion_app.domain.image_gen import ImageClient, ImageTarget
from emotion_app.domain.image_tool import (
    ImageToolService,
    build_spec,
    register_image_tool,
)
from emotion_app.domain.persona import PromptSection
from emotion_app.domain.tools import ToolRegistry, ToolResult, ToolSpec

PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _image_ok() -> httpx.Response:
    return httpx.Response(200, json={"model": "nai", "data": [{"b64_json": PNG_1PX}]})


# ===========================================================================
# 工具契约
# ===========================================================================


def test_tool_spec_shape():
    spec = build_spec()
    assert spec.name == "generate_image"
    schema = spec.to_openai()
    props = schema["function"]["parameters"]["properties"]
    assert set(props) == {
        "prompt", "kind", "reference_image_path", "image_size", "send", "caption"
    }
    assert schema["function"]["parameters"]["required"] == ["prompt"]
    assert props["kind"]["enum"] == ["text2img", "selfie", "edit"]


def test_tool_description_mentions_boundary():
    """描述里要写清"什么时候不该调"（照陪伴插件的 docstring 做法）。"""
    spec = build_spec()
    assert "不要调用" in spec.description or "纯文字" in spec.description


def test_usage_rules_contain_anti_hallucination():
    """🔴 防幻觉规则必须在（照陪伴插件原文）。"""
    from emotion_app.domain.image_tool import TOOL_USAGE_RULES

    joined = "\n".join(TOOL_USAGE_RULES)
    assert "角色扮演不能覆盖真实工具状态" in joined
    assert "最多调用一次" in joined
    assert "绝对不能说已经发送" in joined


async def test_tool_returns_not_configured(tmp_path):
    """未配置端点 → 明确 unavailable，**不静默**。"""
    backend = OpenAIImageBackend(ImageClient(), ImageTarget())
    svc = ImageToolService(backend, output_dir=tmp_path)
    r = await svc({"prompt": "猫"})
    assert r.success is False
    assert r.status == "not_configured"
    assert r.must_not_claim_sent is True


async def test_tool_disabled(tmp_path):
    backend = OpenAIImageBackend(ImageClient(), ImageTarget(base_url="https://x/v1"))
    svc = ImageToolService(backend, output_dir=tmp_path, enabled=False)
    r = await svc({"prompt": "猫"})
    assert r.status == "disabled"
    assert r.must_not_claim_sent is True


async def test_tool_requires_prompt(tmp_path):
    backend = OpenAIImageBackend(ImageClient(), ImageTarget(base_url="https://x/v1"))
    svc = ImageToolService(backend, output_dir=tmp_path)
    r = await svc({"prompt": "   "})
    assert r.status == "invalid_args"
    assert r.must_not_claim_sent is True


async def test_tool_edit_requires_reference(tmp_path):
    """🔴 edit 缺参考图要明确拒绝 —— 不能让模型编路径。"""
    backend = OpenAIImageBackend(ImageClient(), ImageTarget(base_url="https://x/v1"))
    svc = ImageToolService(backend, output_dir=tmp_path)
    r = await svc({"prompt": "改成夜晚", "kind": "edit"})
    assert r.status == "invalid_args"
    assert "reference_image_path" in r.message


async def test_tool_policy_refusal_does_not_leak_upstream_text(tmp_path):
    """🔴 内容策略拒绝时**不复述上游原文**（照陪伴插件规则 8）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Blocked by NSFW policy: explicit content detected")

    client = ImageClient(transport=httpx.MockTransport(handler))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
    )
    svc = ImageToolService(backend, output_dir=tmp_path)
    try:
        r = await svc({"prompt": "something"})
    finally:
        await client.close()

    assert r.status == "provider_policy_refusal"
    assert r.must_not_claim_sent is True
    # 给模型看的 payload 里**不能**出现上游原文
    payload = r.to_payload()
    assert "NSFW" not in payload
    assert "policy" not in payload.lower() or "provider_policy_refusal" in payload
    # 但日志字段要保留（便于排查）
    assert "NSFW" in r.actual_error


async def test_tool_success_marks_sent_when_url_available(tmp_path):
    """⭐ `sent=True` 当且仅当图片**可被访问**。

    实测踩到：早先恒为 False → 模型按防幻觉规则说"未能成功展示"，
    但图片其实生成成功了 —— **过度保守也是错的**（体验差且误导）。
    """
    client = ImageClient(transport=httpx.MockTransport(lambda r: _image_ok()))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
    )
    svc = ImageToolService(
        backend, output_dir=tmp_path, url_prefix="/api/v1/images"
    )
    try:
        r = await svc({"prompt": "一只猫"})
    finally:
        await client.close()

    assert r.success is True
    assert r.generated is True
    assert r.sent is True, "拿到可访问 URL 就该如实置 True"
    assert r.data["urls"], "必须返回可访问 URL"
    assert r.data["urls"][0].startswith("/api/v1/images/")
    assert (tmp_path / r.data["urls"][0].split("/")[-1]).is_file()


async def test_tool_sent_false_without_url_prefix(tmp_path):
    """没配 url_prefix（拿不到可访问链接）→ 如实 False。"""
    client = ImageClient(transport=httpx.MockTransport(lambda r: _image_ok()))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
    )
    svc = ImageToolService(backend, output_dir=tmp_path, url_prefix="")
    try:
        r = await svc({"prompt": "一只猫"})
    finally:
        await client.close()

    assert r.success is True
    assert r.sent is False
    assert "不要声称用户看到了" in r.final_response_instruction


async def test_tool_passes_backend_specific_params(tmp_path):
    """⭐ NAI 专有参数（steps/scale/…）要透传。

    实测：NAI 走**同一个** OpenAI 兼容端点，但认这些非标准字段。
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _image_ok()

    client = ImageClient(transport=httpx.MockTransport(handler))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="nai")
    )
    svc = ImageToolService(
        backend, output_dir=tmp_path, defaults={"steps": 28, "scale": 5.0}
    )
    try:
        await svc({"prompt": "猫", "steps": 40})
    finally:
        await client.close()

    assert captured["steps"] == 40        # 模型给的值优先
    assert captured["scale"] == 5.0       # 默认值兜底


async def test_tool_ignores_empty_extra(tmp_path):
    """空值的后端参数不该发出去（有些上游对空串敏感）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _image_ok()

    client = ImageClient(transport=httpx.MockTransport(handler))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
    )
    svc = ImageToolService(backend, output_dir=tmp_path)
    try:
        await svc({"prompt": "猫", "negative_prompt": "", "steps": None})
    finally:
        await client.close()

    assert "negative_prompt" not in captured
    assert "steps" not in captured


async def test_tool_result_materialization_failure_is_distinct(tmp_path):
    """🔴 "生成成功但取回失败"要**区别于**"生成失败"（照陪伴插件规则 3）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": ""}]})   # 既无 b64 也无 url

    client = ImageClient(transport=httpx.MockTransport(handler))
    await client.open()
    backend = OpenAIImageBackend(
        client, ImageTarget(base_url="https://img/v1", api_key="k", model="m")
    )
    svc = ImageToolService(backend, output_dir=tmp_path)
    try:
        r = await svc({"prompt": "猫"})
    finally:
        await client.close()

    assert r.success is False
    assert "取回" in r.message or r.data.get("failure_stage") == "result_materialization"


# ===========================================================================
# 附件提取
# ===========================================================================


def test_extract_attachments_shape():
    r = ToolResult(
        status="success",
        data={"urls": ["/api/v1/images/a.png"], "kind": "selfie", "caption": "给你"},
    )
    atts = _extract_attachments(r)
    assert len(atts) == 1
    assert atts[0]["type"] == "image"
    assert atts[0]["url"] == "/api/v1/images/a.png"
    assert atts[0]["kind"] == "selfie"


def test_extract_attachments_ignores_non_success():
    r = ToolResult(status="error", success=False, data={"urls": ["/x.png"]})
    assert _extract_attachments(r) == []


def test_toolresult_rejects_contradictory_state():
    """🔴 `status` 与 `success` 不能自相矛盾。

    实测起因：写测试时 `ToolResult(status="error")` 的 `success` 仍是默认 True，
    下游（附件提取）会按 success 放行 —— 矛盾的 result 会一路流下去。
    """
    import pytest as _pytest

    with _pytest.raises(ValueError, match="自相矛盾"):
        ToolResult(status="error")
    with _pytest.raises(ValueError, match="自相矛盾"):
        ToolResult(status="not_configured")
    # 成功类 status 不报错
    for ok in ("success", "skipped", "already_sent"):
        ToolResult(status=ok)


def test_extract_attachments_ignores_unknown_shape():
    """不猜别的形状 —— 只认约定字段 `urls`。"""
    assert _extract_attachments(ToolResult(data={"image": "/x.png"})) == []
    assert _extract_attachments(ToolResult(data={"urls": "not-a-list"})) == []
    assert _extract_attachments(ToolResult(data={"urls": [None, 1, ""]})) == []


# ===========================================================================
# ⭐ 两条路径都要能带图（实测都踩过）
# ===========================================================================


def _tool_response() -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "generate_image", "arguments": '{"prompt":"猫"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })


def _final_response(text: str = "画好了，看看喜欢吗") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    })


def _sse_tool_then_text() -> str:
    return (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function",'
        '"function":{"name":"generate_image","arguments":""}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"prompt\\":\\"猫\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )


def _sse_text(text: str) -> str:
    return (
        f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )


async def _build_service(tmp_path, handler, *, stream_seq=None):
    """造一个带生图工具的 ChatService。"""
    from emotion_app.domain.chat import ChatService
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.upstream.model = "m1"
    cfg.upstream.base_url = "https://chat/v1"
    cfg.upstream.api_key = "sk-chat"

    calls = {"n": 0}

    def chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = calls["n"]
        calls["n"] += 1
        if body.get("stream"):
            if stream_seq:
                # 第一次流式给工具调用，第二次给正文
                return httpx.Response(
                    200, text=stream_seq[min(n, len(stream_seq) - 1)],
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, text=_sse_text("流式正文"),
                                  headers={"content-type": "text/event-stream"})
        if n == 0:
            return _tool_response()
        return _final_response()

    chat_client = OpenAIChatBackend(transport=httpx.MockTransport(chat_handler))
    img_client = ImageClient(transport=httpx.MockTransport(handler))
    await chat_client.open()
    await img_client.open()

    backend = OpenAIImageBackend(
        img_client, ImageTarget(base_url="https://img/v1", api_key="k", model="nai")
    )
    reg = ToolRegistry(max_calls_per_turn=1)
    register_image_tool(
        reg, ImageToolService(backend, output_dir=tmp_path, url_prefix="/api/v1/images")
    )

    svc = ChatService(cfg, chat_client)
    svc.bind_tools(reg, FunctionCallDispatch())
    return svc, chat_client, img_client, calls


async def test_non_streaming_returns_image_attachments(tmp_path):
    """🔴 非流式**必须**返回附件 —— 实测踩到：漏了 → 图生成了但用户看不到。"""
    from emotion_app.domain.chat import ChatRequest
    from emotion_app.domain.message import Message

    svc, c1, c2, _ = await _build_service(tmp_path, lambda r: _image_ok())
    try:
        result = await svc.complete(
            ChatRequest(messages=[Message(role="user", content="画只猫")])
        )
    finally:
        await c1.close()
        await c2.close()

    assert result.content, "必须有正文"
    assert result.attachments, "非流式必须带附件"
    assert result.attachments[0]["type"] == "image"
    assert result.attachments[0]["url"].startswith("/api/v1/images/")


async def test_streaming_also_returns_image(tmp_path):
    """流式也要带图（且工具后要二次请求拿正文）。"""
    from emotion_app.domain.chat import ChatRequest
    from emotion_app.domain.message import Message

    svc, c1, c2, _ = await _build_service(
        tmp_path, lambda r: _image_ok(),
        stream_seq=[_sse_tool_then_text(), _sse_text("看看喜欢吗")],
    )
    try:
        imgs, text = [], ""
        async for chunk in svc.stream(
            ChatRequest(messages=[Message(role="user", content="画只猫")], stream=True)
        ):
            for tc in chunk.tool_call_deltas or []:
                if tc.get("status") == "success":
                    imgs.extend(tc.get("data", {}).get("urls", []))
            text += chunk.delta or ""
    finally:
        await c1.close()
        await c2.close()

    assert imgs, "流式必须推出图片"
    assert text, "工具执行后必须二次请求拿正文（否则只有图没话）"


async def test_non_streaming_no_tool_no_attachments(tmp_path):
    """普通聊天不该有附件（避免误加字段）。"""
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.domain.message import Message
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.upstream.base_url = "https://chat/v1"
    cfg.upstream.api_key = "sk"
    cfg.upstream.model = "m"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, text=_sse_text("hi"),
                                  headers={"content-type": "text/event-stream"})
        return _final_response("普通回复")

    client = OpenAIChatBackend(transport=httpx.MockTransport(handler))
    await client.open()
    svc = ChatService(cfg, client)
    reg = ToolRegistry()
    reg.register(ToolSpec(name="generate_image", description="d"), lambda a: None)  # type: ignore
    svc.bind_tools(reg, FunctionCallDispatch())
    try:
        r = await svc.complete(ChatRequest(messages=[Message(role="user", content="你好")]))
    finally:
        await client.close()

    assert r.attachments == []
