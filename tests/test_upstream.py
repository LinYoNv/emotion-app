# -*- coding: utf-8 -*-
"""上游客户端测试：请求体构造 · 解析 · 拒答重试 · 流式。

**全部用 mock transport，不碰真实网络** —— 这样测试快且可重复。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from emotion_app.domain.message import Message
from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.domain.upstream import (
    DEFAULT_REFUSAL_KEYWORDS,
    ChatResult,
    StreamChunk,
    UpstreamError,
    UpstreamTarget,
    apply_nested_params,
    build_body,
    looks_like_refusal,
    parse_chat_response,
    parse_sse_line,
)

TARGET = UpstreamTarget(base_url="https://mock.test/v1", api_key="sk-mock", model="m1")


# ---------------------------------------------------------------------------
# URL / headers
# ---------------------------------------------------------------------------


def test_target_url_joining():
    assert TARGET.url() == "https://mock.test/v1/chat/completions"
    assert TARGET.url("/models") == "https://mock.test/v1/models"
    trailing = UpstreamTarget(base_url="https://x/v1/", api_key="", model="m")
    assert trailing.url() == "https://x/v1/chat/completions"


def test_target_headers_include_bearer():
    assert TARGET.headers()["Authorization"] == "Bearer sk-mock"


def test_target_headers_omit_auth_when_no_key():
    t = UpstreamTarget(base_url="https://x/v1", api_key="", model="m")
    assert "Authorization" not in t.headers()


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


def test_build_body_shape():
    body = build_body([Message(role="user", content="hi")], model="m", stream=False)
    assert body["model"] == "m"
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_build_body_filters_reasoning_role():
    body = build_body(
        [Message(role="user", content="a"), Message(role="reasoning", content="b")],
        model="m", stream=False,
    )
    assert len(body["messages"]) == 1


def test_nested_params():
    assert apply_nested_params({}, {"a.b": 1}) == {"a": {"b": 1}}
    assert apply_nested_params({}, {"a.b.c": 2}) == {"a": {"b": {"c": 2}}}
    assert apply_nested_params({}, {"top": 1}) == {"top": 1}
    # 已存在的中间层不会被覆盖掉
    assert apply_nested_params({"a": {"keep": 9}}, {"a.new": 1}) == {"a": {"keep": 9, "new": 1}}
    # 非 dict 的中间层要被替换成 dict（而不是报错）
    assert apply_nested_params({"a": 5}, {"a.b": 1}) == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_chat_response():
    r = parse_chat_response({
        "model": "m1",
        "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 5},
    })
    assert r.content == "你好"
    assert r.finish_reason == "stop"
    assert r.model == "m1"
    assert r.usage["total_tokens"] == 5


def test_parse_chat_response_with_reasoning():
    r = parse_chat_response({
        "choices": [{"message": {"content": "答", "reasoning_content": "想"}}]
    })
    assert r.reasoning_content == "想"


def test_parse_chat_response_rejects_no_choices():
    with pytest.raises(UpstreamError, match="choices"):
        parse_chat_response({})


def test_parse_sse_line_data():
    c = parse_sse_line('data: {"choices":[{"delta":{"content":"你"}}]}')
    assert c is not None
    assert c.delta == "你"
    assert c.done is False


def test_parse_sse_line_done():
    c = parse_sse_line("data: [DONE]")
    assert c is not None and c.done is True


def test_parse_sse_line_ignores_non_data():
    assert parse_sse_line("") is None
    assert parse_sse_line(": comment") is None
    assert parse_sse_line("event: ping") is None


def test_parse_sse_line_survives_bad_json():
    """坏行不该炸掉整个流 —— 跳过即可。"""
    assert parse_sse_line("data: {not json") is None


def test_parse_sse_line_finish_reason():
    c = parse_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    assert c is not None and c.finish_reason == "stop"


def test_parse_sse_line_reasoning_delta():
    c = parse_sse_line('data: {"choices":[{"delta":{"reasoning_content":"想"}}]}')
    assert c is not None and c.reasoning_delta == "想"


# ---------------------------------------------------------------------------
# 拒答检测（借鉴陪伴插件 model_routing.py）
# ---------------------------------------------------------------------------


def test_looks_like_refusal():
    for phrase in DEFAULT_REFUSAL_KEYWORDS[:3]:
        assert looks_like_refusal(f"{phrase}这样做"), phrase
    assert looks_like_refusal("THE PROMPT COULD NOT BE SUBMITTED")  # 大小写无关
    assert not looks_like_refusal("正常回复")
    assert not looks_like_refusal("")


# ---------------------------------------------------------------------------
# Mock transport 驱动的客户端测试
# ---------------------------------------------------------------------------


def make_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_client_chat_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is False
        assert request.headers["Authorization"] == "Bearer sk-mock"
        return httpx.Response(200, json={
            "model": "m1",
            "choices": [{"message": {"content": "回复"}, "finish_reason": "stop"}],
        })

    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        r = await c.chat([Message(role="user", content="hi")],
                         model=TARGET.model, base_url=TARGET.base_url, api_key=TARGET.api_key)
    finally:
        await c.close()
    assert r.content == "回复"


async def test_backend_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        with pytest.raises(UpstreamError) as ei:
            await c.chat([Message(role="user", content="hi")],
                         model=TARGET.model, base_url=TARGET.base_url, api_key=TARGET.api_key)
    finally:
        await c.close()
    assert ei.value.status == 401


async def test_backend_raises_on_non_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        with pytest.raises(UpstreamError, match="JSON"):
            await c.chat([Message(role="user", content="hi")],
                         model=TARGET.model, base_url=TARGET.base_url, api_key=TARGET.api_key)
    finally:
        await c.close()


async def test_backend_stream_parses_sse():
    sse = (
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    chunks: list[StreamChunk] = []
    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        async for chunk in c.chat_stream(
            [Message(role="user", content="hi")],
            model=TARGET.model, base_url=TARGET.base_url, api_key=TARGET.api_key,
        ):
            chunks.append(chunk)
    finally:
        await c.close()

    assert "".join(x.delta for x in chunks) == "你好"
    assert any(x.done for x in chunks)
    assert any(x.finish_reason == "stop" for x in chunks)


async def test_backend_stream_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        with pytest.raises(UpstreamError) as ei:
            async for _ in c.chat_stream(
                [Message(role="user", content="hi")],
                model=TARGET.model, base_url=TARGET.base_url, api_key=TARGET.api_key,
            ):
                pass
    finally:
        await c.close()
    assert ei.value.status == 500


async def test_backend_requires_open():
    c = OpenAIChatBackend()
    with pytest.raises(RuntimeError, match="open"):
        await c.chat([Message(role="user", content="x")],
                     model="m", base_url="https://x/v1")


# ---------------------------------------------------------------------------
# ⭐ function calling（S6，实测上游支持）
# ---------------------------------------------------------------------------


async def test_backend_sends_tools():
    """工具定义要出现在请求体里。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "generate_image", "arguments": '{"prompt":"猫"}'}}
            ]}, "finish_reason": "tool_calls"}],
        })

    tools = [{"type": "function", "function": {"name": "generate_image", "parameters": {}}}]
    c = OpenAIChatBackend(transport=make_transport(handler))
    await c.open()
    try:
        r = await c.chat([Message(role="user", content="画猫")],
                         model="m", base_url="https://x/v1", api_key="k", tools=tools)
    finally:
        await c.close()

    assert captured["tools"] == tools
    assert r.wants_tools is True
    assert r.tool_calls[0]["function"]["name"] == "generate_image"


async def test_backend_parses_tool_calls():
    r = parse_chat_response({
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "generate_image", "arguments": '{"kind":"selfie"}'}}
        ]}, "finish_reason": "tool_calls"}],
    })
    assert r.wants_tools
    assert r.content == ""
    assert r.tool_calls[0]["id"] == "c1"


def test_assemble_tool_calls_from_fragments():
    """⭐ 流式下 tool_calls 的分片拼装（实测上游行为）。

    首个 chunk 带 id/name，后续只有 arguments 增量。
    """
    from emotion_app.domain.upstream import assemble_tool_calls

    frags = [
        {"index": 0, "id": "call_x", "type": "function",
         "function": {"name": "generate_image", "arguments": ""}},
        {"index": 0, "type": None, "function": {"arguments": '{"kind":'}},
        {"index": 0, "type": None, "function": {"arguments": '"selfie",'}},
        {"index": 0, "type": None, "function": {"arguments": '"prompt":"a cat"}'}},
    ]
    calls = assemble_tool_calls(frags)
    assert len(calls) == 1
    assert calls[0]["id"] == "call_x"
    assert calls[0]["function"]["name"] == "generate_image"
    assert json.loads(calls[0]["function"]["arguments"]) == {"kind": "selfie", "prompt": "a cat"}


def test_assemble_tool_calls_handles_multiple_indices():
    from emotion_app.domain.upstream import assemble_tool_calls

    frags = [
        {"index": 0, "id": "a", "function": {"name": "t1", "arguments": "{}"}},
        {"index": 1, "id": "b", "function": {"name": "t2", "arguments": "{}"}},
    ]
    calls = assemble_tool_calls(frags)
    assert [c["function"]["name"] for c in calls] == ["t1", "t2"]


def test_assemble_tool_calls_skips_nameless():
    """没有 name 的分片无法构成调用 —— 跳过，不产生半成品。"""
    from emotion_app.domain.upstream import assemble_tool_calls

    assert assemble_tool_calls([{"index": 0, "function": {"arguments": "{}"}}]) == []


def test_parse_sse_line_extracts_tool_call_deltas():
    c = parse_sse_line(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"f","arguments":""}}]}}]}'
    )
    assert c is not None
    assert len(c.tool_call_deltas) == 1
    assert c.tool_call_deltas[0]["function"]["name"] == "f"
