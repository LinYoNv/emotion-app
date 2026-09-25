# -*- coding: utf-8 -*-
"""S3 HTTP 测试：会话 CRUD · 上下文策略 · 聊天持久化与多轮记忆。"""
from __future__ import annotations

import json as _json

import httpx
import pytest

from emotion_app.domain.chat import ChatService
from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.domain.session_repo import SessionRepository
from emotion_app.domain.settings import SettingsService
from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 20


def _reply(text: str = "收到") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    })


def _sse(text: str = "流式回复") -> httpx.Response:
    body = (
        f'data: {{"choices":[{{"delta":{{"role":"assistant","content":""}}}}]}}\n\n'
        f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.fixture
async def ctx(tmp_path):
    """返回 (client, captured, store)。captured 会记录最后一次上游请求体。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        captured.clear()
        captured.update(body)
        return _sse() if body.get("stream") else _reply()

    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "s3http.db")
    cfg.upstream.model = "m1"

    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    settings = SettingsService(store, cfg)
    await settings.load()
    personas = PersonaRepository(store)
    sessions = SessionRepository(store)
    upstream = OpenAIChatBackend(transport=httpx.MockTransport(handler))
    await upstream.open()
    chat = ChatService(cfg, upstream, settings=settings, personas=personas)
    chat.bind_sessions(sessions)

    app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat,
                     personas=personas, settings=settings, sessions=sessions)
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    yield client, captured, store
    await store.close()


# ===========================================================================
# 会话 CRUD
# ===========================================================================


async def test_session_crud(ctx):
    client, _, _ = ctx

    r = await client.post("/api/v1/sessions", json={"title": "测试"})
    assert r.status_code == 201
    sid = (await r.get_json())["id"]

    r = await client.get("/api/v1/sessions")
    assert len((await r.get_json())["data"]) == 1

    r = await client.get(f"/api/v1/sessions/{sid}")
    assert (await r.get_json())["title"] == "测试"

    r = await client.delete(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200
    assert (await (await client.get("/api/v1/sessions")).get_json())["data"] == []


async def test_session_requires_auth(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "na.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    c = app.test_client()
    assert (await c.get("/api/v1/sessions")).status_code == 401
    await store.close()


async def test_session_404(ctx):
    client, _, _ = ctx
    for method, path in (("get", "/api/v1/sessions/nope"),
                         ("delete", "/api/v1/sessions/nope"),
                         ("get", "/api/v1/sessions/nope/messages")):
        r = await getattr(client, method)(path)
        assert r.status_code == 404, path


async def test_session_messages_endpoint(ctx):
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "第一句"}],
    })

    r = await client.get(f"/api/v1/sessions/{sid}/messages")
    data = (await r.get_json())["data"]
    assert len(data) == 2
    assert data[0]["content"] == "第一句"
    assert data[0]["role"] == "user"
    assert data[1]["role"] == "assistant"


async def test_session_messages_limit(ctx):
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    for i in range(3):
        await client.post("/v1/chat/completions", json={
            "session_id": sid, "messages": [{"role": "user", "content": f"m{i}"}],
        })

    r = await client.get(f"/api/v1/sessions/{sid}/messages?limit=2")
    body = await r.get_json()
    assert len(body["data"]) == 2
    # ⚠️ 消息是 user/assistant 交替的：最近的两条 = [m2, 助手回复]
    assert body["data"][0]["content"] == "m2"
    assert body["data"][0]["role"] == "user"
    assert body["data"][-1]["role"] == "assistant"
    # ⚠️ total 是**切之前**的总数（3 轮 × 2 条 = 6）
    assert body["total"] == 6
    assert body["returned"] == 2


async def test_session_clear_keeps_session(ctx):
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "x"}],
    })

    r = await client.post(f"/api/v1/sessions/{sid}/clear")
    assert r.status_code == 200
    assert (await (await client.get(f"/api/v1/sessions/{sid}/messages")).get_json())["data"] == []
    assert (await client.get(f"/api/v1/sessions/{sid}")).status_code == 200


# ===========================================================================
# 上下文策略
# ===========================================================================


async def test_context_modes_listed(ctx):
    client, _, _ = ctx
    d = await (await client.get("/api/v1/context-modes")).get_json()
    ids = [m["id"] for m in d["modes"]]
    assert ids == ["recent_only", "rolling_summary", "semantic"]


async def test_update_session_policy(ctx):
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    r = await client.patch(f"/api/v1/sessions/{sid}",
                           json={"policy": {"mode": "semantic", "protected_tail": 5}})
    assert r.status_code == 200
    p = (await r.get_json())["policy"]
    assert p["mode"] == "semantic"
    assert p["protected_tail"] == 5


async def test_update_session_rejects_bad_mode(ctx):
    """🔴 非法 mode 要明确 400，不能静默改成默认值。"""
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    r = await client.patch(f"/api/v1/sessions/{sid}", json={"policy": {"mode": "hacker"}})
    assert r.status_code == 400
    assert "mode" in (await r.get_json())["error"]["message"]


async def test_create_session_with_policy(ctx):
    client, _, _ = ctx
    r = await client.post("/api/v1/sessions", json={"policy": {"mode": "rolling_summary"}})
    assert (await r.get_json())["policy"]["mode"] == "rolling_summary"


async def test_create_session_rejects_bad_policy(ctx):
    client, _, _ = ctx
    r = await client.post("/api/v1/sessions", json={"policy": {"mode": "bad"}})
    assert r.status_code == 400


# ===========================================================================
# ⭐ 多轮记忆（S3 的核心价值）
# ===========================================================================


async def test_session_carries_history_to_upstream(ctx):
    """⭐ 第二轮请求必须带上第一轮的历史。"""
    client, captured, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "我叫小明"}],
    })

    # 第二轮
    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "我叫什么？"}],
    })

    contents = [m["content"] for m in captured["messages"]]
    assert any("我叫小明" in c for c in contents), contents
    assert any("我叫什么" in c for c in contents)
    # 且顺序是"历史在前、当前在后"
    assert contents.index([c for c in contents if "我叫小明" in c][0]) < \
           contents.index([c for c in contents if "我叫什么" in c][0])


async def test_streaming_also_persists(ctx):
    """⭐ 流式也要存历史（否则流式模式永远失忆）。

    ⚠️ 必须**完整读完**响应体：持久化发生在流结束之后
       （`chat.stream()` 的收尾逻辑）。test_client 不自动消费，
       不读就相当于"客户端提前断开"，服务端生成器不会被推到结尾。
    """
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    r = await client.post("/v1/chat/completions", json={
        "session_id": sid, "stream": True,
        "messages": [{"role": "user", "content": "流式问题"}],
    })
    await r.get_data()          # ← 关键：读完整条流

    data = (await (await client.get(f"/api/v1/sessions/{sid}/messages")).get_json())["data"]
    assert [m["content"] for m in data] == ["流式问题", "流式回复"]


async def test_streaming_generator_is_fully_consumed(ctx):
    """🔴 回归：`/v1/chat/completions` 的 SSE 生成器**必须被完整消费**。

    实测踩到的真 bug：`app.py` 里原本写
        `async for chunk in svc.stream(req): if chunk.done: break`
    → 生成器被提前抛弃 → `ChatService.stream()` 尾部的**持久化代码永不执行**
    → **流式聊天永远不存历史**（而流式正是面板默认用法）。

    这条测试盯的是"结果"：完整读完流之后，历史必须落盘。
    """
    client, _, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    r = await client.post("/v1/chat/completions", json={
        "session_id": sid, "stream": True,
        "messages": [{"role": "user", "content": "完整消费"}],
    })
    raw = await r.get_data()
    assert b"[DONE]" in raw

    body = await (await client.get(f"/api/v1/sessions/{sid}/messages")).get_json()
    assert len(body["data"]) == 2, "流式回复必须落盘（否则流式模式永远失忆）"


async def test_stateless_without_session_id(ctx):
    """不传 session_id → 不持久化，行为与 S1 一致（向后兼容）。"""
    client, captured, _ = ctx
    await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "无会话"}],
    })
    assert (await (await client.get("/api/v1/sessions")).get_json())["data"] == []


async def test_sessions_are_isolated(ctx):
    """不同会话的历史不能串。"""
    client, captured, _ = ctx
    a = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    b = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    await client.post("/v1/chat/completions", json={
        "session_id": a, "messages": [{"role": "user", "content": "A的私密内容"}],
    })
    await client.post("/v1/chat/completions", json={
        "session_id": b, "messages": [{"role": "user", "content": "B的问题"}],
    })

    contents = [m["content"] for m in captured["messages"]]
    assert not any("A的私密内容" in c for c in contents), "B 不该看到 A 的历史"


async def test_history_respects_protected_tail(ctx):
    """窗口大小生效：历史被裁剪到 protected_tail。"""
    client, captured, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    await client.patch(f"/api/v1/sessions/{sid}",
                       json={"policy": {"mode": "recent_only", "protected_tail": 2}})

    for i in range(6):
        await client.post("/v1/chat/completions", json={
            "session_id": sid, "messages": [{"role": "user", "content": f"m{i}"}],
        })

    # 最后一次请求里：历史最多 2 条 + 当前 1 条
    dialog = [m for m in captured["messages"] if m["role"] in ("user", "assistant")]
    assert len(dialog) <= 3, dialog


async def test_system_prefix_stable_across_turns(ctx):
    """⭐ 前缀稳定性：多轮之后 system 仍是第一条，且内容只依赖人设。"""
    client, captured, _ = ctx
    pid = (await (await client.post("/api/v1/personas",
                                    json={"name": "小爱", "description": "固定人设"})).get_json())["id"]
    await client.post(f"/api/v1/personas/{pid}/activate")

    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "第一轮"}],
    })
    first_system = captured["messages"][0]["content"]

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "第二轮"}],
    })
    second_system = captured["messages"][0]["content"]

    assert captured["messages"][0]["role"] == "system"
    assert "固定人设" in second_system
    # 去掉时间片段后必须一致（时间在最后一行，会变）
    strip = lambda t: "\n".join(l for l in t.split("\n") if "现在的时间是" not in l)
    assert strip(first_system) == strip(second_system)


async def test_persona_applies_to_session(ctx):
    client, captured, _ = ctx
    pid = (await (await client.post("/api/v1/personas",
                                    json={"name": "诗人", "description": "必须押韵"})).get_json())["id"]
    await client.post(f"/api/v1/personas/{pid}/activate")

    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "你好"}],
    })
    assert "必须押韵" in captured["messages"][0]["content"]


async def test_summary_injected_as_system_message(ctx):
    """⭐ 摘要作为**独立的 system 消息**注入（不污染人设前缀）。"""
    client, captured, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    await client.patch(f"/api/v1/sessions/{sid}", json={
        "policy": {"mode": "semantic", "protected_tail": 2},
        "summary": "用户叫小明，喜欢咖啡",
    })

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "继续"}],
    })

    msgs = captured["messages"]
    summary_msgs = [m for m in msgs if "此前对话摘要" in m.get("content", "")]
    assert len(summary_msgs) == 1
    assert "小明" in summary_msgs[0]["content"]
    # 摘要必须是独立的 system 消息，且是第一条（人设之后）
    assert summary_msgs[0]["role"] == "system"


async def test_recent_only_ignores_summary(ctx):
    """滚动模式不注入摘要。"""
    client, captured, _ = ctx
    sid = (await (await client.post("/api/v1/sessions", json={})).get_json())["id"]
    await client.patch(f"/api/v1/sessions/{sid}",
                       json={"summary": "不该出现的摘要"})

    await client.post("/v1/chat/completions", json={
        "session_id": sid, "messages": [{"role": "user", "content": "hi"}],
    })
    assert not any("不该出现的摘要" in m.get("content", "") for m in captured["messages"])
