# -*- coding: utf-8 -*-
"""S3 测试：上下文策略 · 会话仓库。"""
from __future__ import annotations

import pytest

from emotion_app.domain.context import (
    MODE_RECENT_ONLY,
    MODE_ROLLING_SUMMARY,
    MODE_SEMANTIC,
    ContextError,
    ContextPolicy,
    build_context,
    build_summary_messages,
    needs_summary,
    to_summarize,
)
from emotion_app.domain.message import Message
from emotion_app.domain.session_repo import (
    MAX_MESSAGES_PER_SESSION,
    SessionNotFound,
    SessionRepository,
)
from emotion_app.storage.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(tmp_path / "s3.db")
    await s.open()
    yield s
    await s.close()


def dialog(n: int) -> list[Message]:
    """造 n 条交替的 user/assistant 消息。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(role=role, content=f"m{i}"))
    return out


# ===========================================================================
# ContextPolicy
# ===========================================================================


def test_policy_defaults_are_sane():
    p = ContextPolicy()
    assert p.mode == MODE_RECENT_ONLY
    assert p.protected_tail > 0
    p.validate()


def test_policy_rejects_bad_mode():
    with pytest.raises(ContextError, match="mode"):
        ContextPolicy(mode="hacker").validate()


def test_policy_rejects_bad_numbers():
    for kw in ({"protected_tail": -1}, {"summary_trigger": 0},
               {"summary_max_chars": 0}, {"budget_chars": 0}):
        with pytest.raises(ContextError):
            ContextPolicy(**kw).validate()


def test_policy_roundtrip():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=7, summary_trigger=9)
    p2 = ContextPolicy.from_dict(p.to_dict())
    assert p2.mode == MODE_SEMANTIC
    assert p2.protected_tail == 7
    assert p2.summary_trigger == 9


def test_policy_from_empty_dict_is_recent_only():
    assert ContextPolicy.from_dict({}).mode == MODE_RECENT_ONLY


# ===========================================================================
# build_context
# ===========================================================================


def test_recent_only_keeps_window():
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=4)
    r = build_context(dialog(10), p)
    assert len(r.history) == 4
    assert r.history[-1].content == "m9"
    assert r.dropped == 6
    assert r.summary == ""


def test_recent_only_ignores_summary():
    """滚动模式不用摘要 —— 传了也不该生效（避免误解）。"""
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=4)
    r = build_context(dialog(10), p, summary="旧摘要")
    assert r.summary == ""


def test_rolling_summary_includes_summary():
    p = ContextPolicy(mode=MODE_ROLLING_SUMMARY, protected_tail=4)
    r = build_context(dialog(10), p, summary="用户喜欢咖啡")
    assert r.summary == "用户喜欢咖啡"
    assert len(r.history) == 4
    assert r.mode == MODE_ROLLING_SUMMARY


def test_semantic_is_like_rolling():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4)
    r = build_context(dialog(10), p, summary="摘要")
    assert r.summary == "摘要"
    assert len(r.history) == 4


def test_summary_is_capped():
    """摘要本身也要受预算限制。"""
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=2, summary_max_chars=10)
    r = build_context(dialog(10), p, summary="x" * 999)
    assert len(r.summary) == 10


def test_fewer_messages_than_window():
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=20)
    r = build_context(dialog(3), p)
    assert len(r.history) == 3
    assert r.dropped == 0


def test_empty_messages():
    r = build_context([], ContextPolicy())
    assert r.history == []
    assert r.dropped == 0


def test_system_messages_excluded_from_window():
    """system 不该占保护窗口的名额（它们不是"对话"）。"""
    msgs = [Message(role="system", content="sys")] + dialog(10)
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=4)
    r = build_context(msgs, p)
    assert len(r.history) == 4
    assert all(m.role in ("user", "assistant") for m in r.history)


def test_budget_drops_oldest_first():
    """预算超了就**从最旧的开始丢**。"""
    msgs = [Message(role="user", content="x" * 100) for _ in range(10)]
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=10, budget_chars=350)
    r = build_context(msgs, p)
    assert len(r.history) < 10
    assert r.history[-1].content == "x" * 100     # 最后一条必须留着
    assert r.dropped > 0


def test_budget_never_drops_last_message():
    """🔴 宁可丢历史，也不能让"当前这句话"发不出去。"""
    msgs = [
        Message(role="user", content="x" * 5000),
        Message(role="assistant", content="y" * 5000),
        Message(role="user", content="当前问题"),
    ]
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=10, budget_chars=100)
    r = build_context(msgs, p)
    assert len(r.history) >= 1
    assert r.history[-1].content == "当前问题"


def test_context_result_to_dict():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=2)
    d = build_context(dialog(6), p, summary="s").to_dict()
    assert d["mode"] == MODE_SEMANTIC
    assert d["history_count"] == 2
    assert d["has_summary"] is True


# ===========================================================================
# 总结触发
# ===========================================================================


def test_needs_summary_false_for_recent_only():
    p = ContextPolicy(mode=MODE_RECENT_ONLY, protected_tail=2, summary_trigger=2)
    assert needs_summary(dialog(100), p) is False


def test_needs_summary_threshold():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4, summary_trigger=6)
    assert needs_summary(dialog(10), p) is False   # 10 <= 4+6
    assert needs_summary(dialog(11), p) is True    # 11 > 4+6


def test_to_summarize_picks_overflow():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=4)
    picked = to_summarize(dialog(10), p)
    assert len(picked) == 6
    assert picked[0].content == "m0"
    assert picked[-1].content == "m5"


def test_to_summarize_empty_when_within_window():
    p = ContextPolicy(mode=MODE_SEMANTIC, protected_tail=20)
    assert to_summarize(dialog(5), p) == []


def test_summary_prompt_has_no_hardcoded_junk():
    """🔴 摘要提示词不能带爱语那些硬编码（严重健忘症等）。"""
    text = build_summary_messages(dialog(2))[0].content
    assert "严重健忘症" not in text
    assert "每条消息" not in text
    from emotion_app.domain.context import SUMMARY_PROMPT

    assert "严重健忘症" not in SUMMARY_PROMPT


def test_build_summary_messages_shape():
    msgs = build_summary_messages(dialog(4), previous_summary="旧摘要")
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert "旧摘要" in msgs[1].content
    assert "用户：" in msgs[1].content


# ===========================================================================
# SessionRepository
# ===========================================================================


async def test_session_create_and_get(store):
    repo = SessionRepository(store)
    s = await repo.create(title="测试会话")
    got = await repo.get(s.id)
    assert got.title == "测试会话"
    assert got.message_count == 0


async def test_session_get_missing(store):
    repo = SessionRepository(store)
    with pytest.raises(SessionNotFound):
        await repo.get("nope")


async def test_session_ensure_creates_once(store):
    repo = SessionRepository(store)
    a = await repo.ensure("fixed-id")
    b = await repo.ensure("fixed-id")
    assert a.id == b.id == "fixed-id"
    assert len(await repo.list()) == 1


async def test_session_append_messages(store):
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.append(s.id, [
        Message(role="user", content="你好"),
        Message(role="assistant", content="你好呀"),
    ])
    msgs = await repo.messages(s.id)
    assert len(msgs) == 2
    assert msgs[0].content == "你好"
    assert (await repo.get(s.id)).message_count == 2


async def test_session_title_auto_from_first_user_message(store):
    """首条用户消息自动做标题（省得用户手填）。"""
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.append(s.id, [Message(role="user", content="帮我写一首诗好吗")])
    assert (await repo.get(s.id)).title == "帮我写一首诗好吗"


async def test_session_explicit_title_not_overwritten(store):
    repo = SessionRepository(store)
    s = await repo.create(title="我起的名")
    await repo.append(s.id, [Message(role="user", content="随便说点什么")])
    assert (await repo.get(s.id)).title == "我起的名"


async def test_session_persists_across_reload(store):
    repo = SessionRepository(store)
    s = await repo.create(title="持久")
    await repo.append(s.id, [Message(role="user", content="hi")])

    repo2 = SessionRepository(store)
    got = await repo2.get(s.id)
    assert got.title == "持久"
    assert got.message_count == 1
    assert len(await repo2.messages(s.id)) == 1


async def test_session_update_policy(store):
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.update(s.id, {"policy": {"mode": MODE_SEMANTIC, "protected_tail": 8}})
    got = await repo.get(s.id)
    assert got.policy.mode == MODE_SEMANTIC
    assert got.policy.protected_tail == 8


async def test_session_update_rejects_bad_policy(store):
    repo = SessionRepository(store)
    s = await repo.create()
    with pytest.raises(Exception):
        await repo.update(s.id, {"policy": {"mode": "hacker"}})


async def test_session_delete(store):
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.append(s.id, [Message(role="user", content="x")])
    await repo.delete(s.id)

    assert await repo.list() == []
    with pytest.raises(SessionNotFound):
        await repo.messages(s.id)


async def test_session_clear_messages_keeps_session(store):
    """清消息但保留会话（含策略）。"""
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.update(s.id, {"policy": {"mode": MODE_SEMANTIC}})
    await repo.append(s.id, [Message(role="user", content="x")])

    await repo.clear_messages(s.id)
    got = await repo.get(s.id)
    assert got.message_count == 0
    assert got.policy.mode == MODE_SEMANTIC     # 策略没丢
    assert got.summary == ""
    assert await repo.messages(s.id) == []


async def test_session_list_sorted_by_updated(store):
    repo = SessionRepository(store)
    a = await repo.create(title="A")
    b = await repo.create(title="B")
    await repo.append(a.id, [Message(role="user", content="touch")])
    items = await repo.list()
    assert items[0].id == a.id


async def test_session_message_cap(store):
    """超上限时丢最旧的，并平移总结游标。"""
    repo = SessionRepository(store)
    s = await repo.create()
    await repo.update(s.id, {"summarized_upto": 100})

    big = [Message(role="user", content=f"m{i}") for i in range(MAX_MESSAGES_PER_SESSION + 10)]
    await repo.append(s.id, big)

    msgs = await repo.messages(s.id)
    assert len(msgs) == MAX_MESSAGES_PER_SESSION
    assert msgs[-1].content == f"m{MAX_MESSAGES_PER_SESSION + 9}"
    assert (await repo.get(s.id)).summarized_upto == 90   # 100 - 10


async def test_session_policy_isolated_per_session(store):
    """每个会话的上下文策略互不影响。"""
    repo = SessionRepository(store)
    a = await repo.create()
    b = await repo.create()
    await repo.update(a.id, {"policy": {"mode": MODE_SEMANTIC}})
    assert (await repo.get(a.id)).policy.mode == MODE_SEMANTIC
    assert (await repo.get(b.id)).policy.mode == MODE_RECENT_ONLY
