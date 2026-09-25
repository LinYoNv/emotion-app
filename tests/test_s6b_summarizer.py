# -*- coding: utf-8 -*-
"""S6b 测试：总结自动化。

🔴 重点守的坑：
   * **不能阻塞回复**（照爱语的"静默总结"）—— 必须在后台跑
   * **失败不推进游标** —— 否则那段对话永远不会被总结
   * **幂等** —— 同一条消息不能被总结两次
   * **后台任务不能被 GC 回收**（asyncio 只持弱引用）
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.domain.chat import ChatRequest, ChatService
from emotion_app.domain.context import (
    MODE_RECENT_ONLY,
    MODE_SEMANTIC,
    ContextPolicy,
)
from emotion_app.domain.message import Message
from emotion_app.domain.session_repo import SessionRepository
from emotion_app.domain.summarizer import Summarizer, merge_summary
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(tmp_path / "s6b.db")
    await s.open()
    yield s
    await s.close()


def dialog(n: int) -> list[Message]:
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
        for i in range(n)
    ]


def _ok_summary(text: str = "用户叫小明，喜欢咖啡") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    })


class _OpenedBackend:
    """已 open 的测试后端（`Summarizer` 需要 open 后才能用）。

    ⚠️ 不想在每个测试里写 `await backend.open()`，
       所以包一层在**首次调用时**自动 open。
    """

    def __init__(self, handler):
        self._inner = OpenAIChatBackend(transport=httpx.MockTransport(handler))
        self._opened = False

    async def _ensure(self):
        if not self._opened:
            await self._inner.open()
            self._opened = True

    async def chat(self, messages, **kw):
        await self._ensure()
        return await self._inner.chat(messages, **kw)

    async def chat_stream(self, messages, **kw):
        await self._ensure()
        async for c in self._inner.chat_stream(messages, **kw):
            yield c

    async def open(self):
        await self._ensure()

    async def close(self):
        await self._inner.close()


def _backend(handler):
    return _OpenedBackend(handler)


# ===========================================================================
# 触发判定
# ===========================================================================


async def test_skips_when_below_threshold(store):
    """未到边界 → 不总结（省上游调用）。"""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_summary()

    s = Summarizer(_backend(handler))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(5),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="", cursor=0,
        base_url="https://x/v1", api_key="k",
    )
    assert out.ran is False
    assert "边界" in out.reason
    assert calls == [], "不该调上游"


async def test_recent_only_never_summarizes(store):
    """滚动模式不需要摘要 —— 不该调上游。"""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_summary()

    s = Summarizer(_backend(handler))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(100),
        policy=ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=2, summary_trigger=2),
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
    )
    assert out.ran is False
    assert calls == []


async def test_summarizes_when_boundary_reached(store):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_summary("小明喜欢咖啡")

    s = Summarizer(_backend(handler))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
    )

    assert out.ran is True
    assert out.ok is True
    assert out.summary == "小明喜欢咖啡"
    assert out.summarized == 16        # 20 - 4（保护窗口）
    assert out.cursor == 16
    # 只总结了滑出窗口的部分
    assert "m0" in json.dumps(captured["messages"], ensure_ascii=False)


async def test_only_summarizes_new_messages(store):
    """⭐ 游标生效：第二次只总结**新增**的部分（幂等）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(json.dumps(body["messages"], ensure_ascii=False))
        return _ok_summary("摘要")

    s = Summarizer(_backend(handler))
    policy = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6)

    # 第一次：20 条 → 总结 0..15
    out1 = await s.maybe_summarize(
        session_id="a", messages=dialog(20), policy=policy,
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
    )
    assert out1.cursor == 16

    # 再加 10 条，游标从 16 开始
    out2 = await s.maybe_summarize(
        session_id="a", messages=dialog(30), policy=policy,
        current_summary=out1.summary, cursor=out1.cursor,
        base_url="https://x/v1", api_key="k",
    )
    assert out2.cursor == 26
    # 第二次的输入**不该包含**已经总结过的 m0
    assert "m0" not in seen[1]
    assert "m16" in seen[1]


async def test_no_new_messages_is_noop(store):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok_summary()

    s = Summarizer(_backend(handler))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="旧摘要", cursor=16, base_url="https://x/v1", api_key="k",
    )
    assert out.ran is False
    # 游标已在边界 → 没有可总结的段落（两种 reason 都表示"无事可做"）
    assert "没有新消息" in out.reason or "窗口内" in out.reason
    assert out.cursor == 16, "游标不该后退"
    assert calls == [], "不该调上游"


# ===========================================================================
# ⭐ 失败处理（最重要）
# ===========================================================================


async def test_failure_does_not_advance_cursor(store):
    """🔴 总结失败 → **游标不动**（否则那段对话永远不会被总结）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    s = Summarizer(_backend(handler))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="旧摘要", cursor=5, base_url="https://x/v1", api_key="k",
    )

    assert out.ran is False
    assert out.ok is False
    assert out.cursor == 5, "失败必须保持原游标"
    assert "失败" in out.reason


async def test_empty_summary_still_advances_cursor(store):
    """模型说"不值得总结"（空串）→ **仍推进游标**，否则会反复问同一段。"""
    s = Summarizer(_backend(lambda r: _ok_summary("")))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="旧摘要", cursor=0, base_url="https://x/v1", api_key="k",
    )
    assert out.ran is True
    assert out.ok is True
    assert out.cursor == 16, "空摘要也要推进游标"
    assert out.summary == "旧摘要", "旧摘要不能被清空"


async def test_summary_is_capped(store):
    """摘要本身也受预算限制（防它自己膨胀）。"""
    s = Summarizer(_backend(lambda r: _ok_summary("x" * 9999)))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
    )
    assert out.summary_chars <= ContextPolicy().summary_max_chars


async def test_force_bypasses_threshold(store):
    """`force=True` 忽略阈值（手动触发/测试用）。"""
    s = Summarizer(_backend(lambda r: _ok_summary("强制摘要")))
    out = await s.maybe_summarize(
        session_id="a", messages=dialog(4),      # 远未到边界
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=2, summary_trigger=99),
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
        force=True,
    )
    assert out.ran is True
    assert out.summary == "强制摘要"


async def test_no_summary_contains_hardcoded_junk(store):
    """🔴 摘要提示词不能带爱语的硬编码（严重健忘症等）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_summary("s")

    s = Summarizer(_backend(handler))
    await s.maybe_summarize(
        session_id="a", messages=dialog(20),
        policy=ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6),
        current_summary="", cursor=0, base_url="https://x/v1", api_key="k",
    )
    prompt = captured["messages"][0]["content"]
    for junk in ("严重健忘症", "每条消息", "<add:"):
        assert junk not in prompt


# ===========================================================================
# 后台调度（⭐ 不能阻塞回复）
# ===========================================================================


async def test_schedule_summarize_is_non_blocking(store):
    """⭐ 触发后**立刻返回**，不等上游 —— 回复不该被总结拖慢。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_chat(messages, **kw):
        started.set()
        await release.wait()                 # 卡住，模拟慢上游
        from emotion_app.domain.upstream import ChatResult

        return ChatResult(content="摘要")

    class SlowBackend:
        async def open(self): ...
        async def close(self): ...
        async def chat(self, messages, **kw):
            return await slow_chat(messages, **kw)
        async def chat_stream(self, messages, **kw):
            if False:
                yield

    cfg = Config()
    cfg.upstream.base_url = "https://x/v1"
    cfg.upstream.api_key = "k"
    cfg.upstream.model = "m"

    sessions = SessionRepository(store)
    sid = (await sessions.create()).id
    await sessions.update(sid, {
        "policy": {"mode": MODE_SEMANTIC, "protected_tail": 2, "summary_trigger": 2},
    })
    await sessions.append(sid, dialog(20))

    svc = ChatService(cfg, SlowBackend())          # type: ignore[arg-type]
    svc.bind_sessions(sessions)
    svc.bind_summarizer(Summarizer(SlowBackend()))  # type: ignore[arg-type]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    svc.schedule_summarize(sid)                    # ← 不该 await
    elapsed = loop.time() - t0

    assert elapsed < 0.05, "schedule_summarize 必须立刻返回"
    await asyncio.wait_for(started.wait(), timeout=1)   # 后台确实在跑
    release.set()
    await asyncio.sleep(0.05)


async def test_background_task_is_retained(store):
    """🔴 后台任务必须**被强引用持有** —— asyncio 只持弱引用，否则会被 GC 掉。

    实测踩到过这个模式：`create_task` 后不保存引用 → 任务静默消失。
    """
    cfg = Config()
    cfg.upstream.base_url = "https://x/v1"
    cfg.upstream.api_key = "k"

    svc = ChatService(cfg, _backend(lambda r: _ok_summary()))
    svc.bind_sessions(SessionRepository(store))
    svc.bind_summarizer(Summarizer(_backend(lambda r: _ok_summary())))

    sid = (await svc._sessions.create()).id
    svc.schedule_summarize(sid)
    assert len(svc._bg_tasks) == 1, "任务必须被持有"

    await asyncio.sleep(0.15)
    assert len(svc._bg_tasks) == 0, "完成后应被清理（防内存泄漏）"


async def test_background_failure_does_not_raise(store):
    """后台总结失败 → **只记日志，绝不冒泡**（不能影响用户）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    cfg = Config()
    cfg.upstream.base_url = "https://x/v1"
    cfg.upstream.api_key = "k"

    sessions = SessionRepository(store)
    sid = (await sessions.create()).id
    await sessions.update(sid, {
        "policy": {"mode": MODE_SEMANTIC, "protected_tail": 2, "summary_trigger": 2},
    })
    await sessions.append(sid, dialog(20))

    svc = ChatService(cfg, _backend(handler))
    svc.bind_sessions(sessions)
    svc.bind_summarizer(Summarizer(_backend(handler)))

    svc.schedule_summarize(sid)
    await asyncio.sleep(0.2)          # 不该抛异常

    # 失败后摘要与游标都没变
    s = await sessions.get(sid)
    assert s.summary == ""
    assert s.summarized_upto == 0


async def test_complete_triggers_background_summarize(store):
    """端到端：聊天后自动排后台总结（不阻塞）。"""
    summary_calls: list = []

    def chat_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        msgs = body.get("messages") or []
        # 摘要请求的 system 里含"压缩成简洁的摘要"
        if any("压缩成简洁的摘要" in str(m.get("content", "")) for m in msgs):
            summary_calls.append(1)
            return _ok_summary("自动摘要")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "回复"}, "finish_reason": "stop"}],
        })

    cfg = Config()
    cfg.upstream.base_url = "https://x/v1"
    cfg.upstream.api_key = "k"
    cfg.upstream.model = "m"

    sessions = SessionRepository(store)
    sid = (await sessions.create()).id
    # 造足够多的历史（超过边界）
    await sessions.update(sid, {
        "policy": {"mode": MODE_SEMANTIC, "protected_tail": 2, "summary_trigger": 2},
    })
    await sessions.append(sid, dialog(20))

    svc = ChatService(cfg, _backend(chat_handler))
    svc.bind_sessions(sessions)
    svc.bind_summarizer(Summarizer(_backend(chat_handler)))

    await svc.complete(ChatRequest(
        messages=[Message(role="user", content="新消息")], session_id=sid,
    ))
    await asyncio.sleep(0.2)          # 给后台任务时间

    assert summary_calls, "聊天后应自动触发总结"
    assert (await sessions.get(sid)).summary == "自动摘要"


# ===========================================================================
# merge_summary
# ===========================================================================


def test_merge_summary_uses_new():
    assert merge_summary("旧", "新") == "新"


def test_merge_summary_keeps_old_when_new_empty():
    assert merge_summary("旧", "") == "旧"
    assert merge_summary("旧", "   ") == "旧"


def test_merge_summary_caps():
    assert len(merge_summary("", "x" * 9999, max_chars=100)) == 100
