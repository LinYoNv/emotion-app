# -*- coding: utf-8 -*-
"""`/v1/*` HTTP 接口测试（用 mock 上游，不碰真实网络）。"""
from __future__ import annotations

import json

import httpx
import pytest

from emotion_app.domain.chat import ChatError, ChatRequest, ChatService, parse_chat_request
from emotion_app.domain.message import Message
from emotion_app.domain.persona import Persona
from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.domain.upstream import UpstreamError, UpstreamTarget
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 20


def _ok_response(content: str = "你好呀") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 3},
    })


def _sse_response() -> httpx.Response:
    sse = (
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})


@pytest.fixture
async def make_client(tmp_path):
    """工厂：可注入自定义上游 handler。"""
    created: list = []

    async def _make(handler, *, with_api_key: bool = True):
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(tmp_path / "chat.db")
        cfg.upstream.api_key = "sk-server" if with_api_key else ""
        cfg.upstream.model = "m1"

        store = SqliteStore(cfg.storage.sqlite_path)
        await store.open()
        created.append(store)

        upstream = OpenAIChatBackend(transport=httpx.MockTransport(handler))
        await upstream.open()
        chat = ChatService(cfg, upstream)
        app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat)
        return app.test_client()

    yield _make
    for s in created:
        await s.close()


# ---------------------------------------------------------------------------
# 请求解析（单元）
# ---------------------------------------------------------------------------


def test_parse_chat_request_basic():
    req = parse_chat_request({
        "model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    assert req.stream is True
    assert len(req.messages) == 1


def test_parse_chat_request_rejects_missing_messages():
    with pytest.raises(ChatError, match="messages"):
        parse_chat_request({"model": "m"})


def test_parse_chat_request_rejects_empty_list():
    with pytest.raises(ChatError):
        parse_chat_request({"messages": []})


def test_parse_chat_request_collects_custom_params():
    """非标准字段被当作自定义参数透传（爱语就是这么做的）。"""
    req = parse_chat_request({
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "top_p": 0.9,
    })
    assert req.custom_params == {"temperature": 0.7, "top_p": 0.9}


# ---------------------------------------------------------------------------
# ChatService（单元）
# ---------------------------------------------------------------------------


def _svc(tmp_path, handler=None, **cfg_over):
    cfg = Config()
    cfg.upstream.model = "m1"
    for k, v in cfg_over.items():
        setattr(cfg.upstream, k, v)
    upstream = OpenAIChatBackend(
        transport=httpx.MockTransport(handler or (lambda r: _ok_response()))
    )
    return ChatService(cfg, upstream)


def test_resolve_target_prefers_persona():
    """⭐ per-persona 覆盖（实测爱语支持）。"""
    svc = _svc(None)
    p = Persona(name="x", api_url="https://persona.test/v1", api_key="sk-p", model="pm")
    t = svc.resolve_target(p)
    assert t.base_url == "https://persona.test/v1"
    assert t.api_key == "sk-p"
    assert t.model == "pm"


def test_resolve_target_falls_back_to_global():
    svc = _svc(None, base_url="https://global.test/v1", api_key="sk-g")
    t = svc.resolve_target(None)
    assert t.base_url == "https://global.test/v1"
    assert t.api_key == "sk-g"


def test_resolve_target_model_override_wins():
    svc = _svc(None)
    assert svc.resolve_target(None, "explicit").model == "explicit"


def test_build_messages_prepends_system():
    svc = _svc(None)
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        persona=Persona(name="x", description="我是描述"),
    )
    msgs = svc.build_messages(req)
    assert msgs[0].role == "system"
    assert "我是描述" in msgs[0].content
    assert msgs[-1].content == "hi"


def test_build_messages_requires_user_or_assistant():
    svc = _svc(None)
    with pytest.raises(ChatError, match="user/assistant"):
        svc.build_messages(ChatRequest(messages=[Message(role="system", content="only")]))


# ---------------------------------------------------------------------------
# HTTP：鉴权
# ---------------------------------------------------------------------------


async def test_v1_requires_auth(make_client):
    """🔴 无会话无 Bearer → 401（不静默放行）。"""
    client = await make_client(lambda r: _ok_response())
    resp = await client.post("/v1/chat/completions", json={
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 401


async def test_v1_accepts_access_token_as_bearer(make_client):
    """⭐ Bearer 必须是**我们的访问令牌**（不再接受任意 Bearer）。

    实测踩到：旧实现把任何 Bearer 都放过 → 等于匿名可用。
    现在 Bearer 要么是访问令牌，要么就走会话 cookie。
    """
    client = await make_client(lambda r: _ok_response())
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200


async def test_v1_rejects_arbitrary_bearer(make_client):
    """🔴 反面：随便给个 Bearer **不能**放行。"""
    client = await make_client(lambda r: _ok_response())
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-anything-at-all"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


async def test_v1_accepts_session_cookie(make_client):
    client = await make_client(lambda r: _ok_response())
    await client.post("/api/v1/auth/login", json={"token": TOKEN})
    resp = await client.post("/v1/chat/completions", json={
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HTTP：非流式
# ---------------------------------------------------------------------------


async def test_chat_completions_shape(make_client):
    """返回体必须是标准 OpenAI 形状。"""
    client = await make_client(lambda r: _ok_response("测试回复"))
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = await resp.get_json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"]["content"] == "测试回复"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 3


async def test_chat_sends_system_prompt_with_markers(make_client):
    """⭐ 端到端：system 内容带 `<|SYSTEM_INJECT|>…<|END_INJECT|>` 包裹（爱语实测行为）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_response()

    client = await make_client(handler)
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )

    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("<|SYSTEM_INJECT|>")
    assert msgs[0]["content"].endswith("<|END_INJECT|>")
    assert msgs[-1] == {"role": "user", "content": "hi"}
    assert captured["stream"] is False


async def test_chat_stream_sends_stream_true(make_client):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _sse_response()

    client = await make_client(handler)
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert captured["stream"] is True


async def test_chat_rejects_bad_body(make_client):
    client = await make_client(lambda r: _ok_response())
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m"},  # 缺 messages
    )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"


async def test_chat_maps_upstream_error_to_502(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    client = await make_client(handler)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    body = await resp.get_json()
    assert body["error"]["type"] == "upstream_error"


async def test_custom_params_passed_through(make_client):
    """自定义参数（含 `.` 嵌套）要透传到上游。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_response()

    client = await make_client(handler)
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "extra.body.flag": True,
        },
    )
    assert captured["temperature"] == 0.5
    assert captured["extra"] == {"body": {"flag": True}}


# ---------------------------------------------------------------------------
# HTTP：流式
# ---------------------------------------------------------------------------


async def test_chat_stream_sse(make_client):
    """⭐ 流式：逐块 SSE，最后 [DONE]。"""
    client = await make_client(lambda r: _sse_response())
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["Content-Type"]
    assert resp.headers.get("X-Accel-Buffering") == "no"

    text = (await resp.get_data()).decode()
    assert text.rstrip().endswith("data: [DONE]")

    deltas = []
    for line in text.splitlines():
        if line.startswith("data: ") and "[DONE]" not in line:
            payload = json.loads(line[6:])
            if "choices" in payload:
                deltas.append(payload["choices"][0]["delta"].get("content", ""))
    assert "".join(deltas) == "你好"


async def test_chat_stream_reports_error_inline(make_client):
    """🔴 流中途出错要**明确告知**客户端，而不是静默结束。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = await make_client(handler)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    text = (await resp.get_data()).decode()
    assert "error" in text
    assert "upstream_error" in text
    assert text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


async def test_list_models(make_client):
    client = await make_client(lambda r: _ok_response())
    resp = await client.get("/v1/models", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "m1"
    assert body["data"][0]["owned_by"] == "emotion-app"


async def test_list_models_requires_auth(make_client):
    client = await make_client(lambda r: _ok_response())
    resp = await client.get("/v1/models")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /health 反映 chat 就绪
# ---------------------------------------------------------------------------


async def test_health_reports_chat_ready(make_client):
    client = await make_client(lambda r: _ok_response())
    resp = await client.get("/health")
    body = await resp.get_json()
    assert body["chat_ready"] is True
