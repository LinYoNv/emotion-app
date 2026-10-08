# -*- coding: utf-8 -*-
"""投递端口（`SessionDelivery`）的契约测试。

## 为什么需要

在此之前 `HostPorts.delivery` **一直是 None**（"故意传 None，等主动消息落地"），
导致插件的两条投递路径全断：

| 路径 | 插件的调用 | 原来的结局 |
|---|---|---|
| A | `context.send_message(umo, chain)` | `NotImplementedError` |
| B | `platform.send_by_session(session, chain)` | 返回 `False` |

→ 陪伴的主动消息**一条也发不出来**。

## 本文件守的三条硬约束

1. **真的写进会话**（不是"打了个日志就算发了"）；
2. **不伪造送达** —— 写不进去必须返回 `False` / 抛错（作者判据③）；
3. **降级可见** —— 非文本组件被降级成标记时要留 WARNING
   （本项目最贵的 bug 类型是"静默丢弃用户内容"）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.web.delivery import (  # noqa: E402
    PROACTIVE_SESSION_PREFIX,
    SessionDelivery,
    chain_to_text,
    session_id_for_umo,
)


class _FakeSessions:
    """最简会话仓库（记录写入，形状照 `SessionRepository`）。"""

    def __init__(
        self, *, fail_on_append: bool = False, known: dict[str, str] | None = None
    ) -> None:
        self.appended: list[tuple[str, list[Any]]] = []
        self.ensured: list[tuple[str, dict]] = []
        #: 未读增量 —— 交付层会传 `unread_delta=1`（= 未读 +1，见 `Session.unread`）。
        #: ⚠️ 夹具必须收这个关键字：不收的话测的就不是**真契约**
        #:    （真 `SessionRepository.append` 已经收它了）。
        self.unread_deltas: list[int] = []
        #: `user_id` → 会话 id（模拟"该 umo 已经绑在用户自己的会话上"那条路径）
        self._known = dict(known or {})
        self._fail = fail_on_append

    async def by_user_id(self, user_id: str) -> Any:
        sid = self._known.get(user_id)
        return None if sid is None else type("S", (), {"id": sid})()

    async def ensure(self, session_id: str, **kwargs: Any) -> Any:
        self.ensured.append((session_id, kwargs))
        return type("S", (), {"id": session_id})()

    async def append(
        self, session_id: str, new_messages: list[Any], *, unread_delta: int = 0
    ) -> Any:
        if self._fail:
            raise RuntimeError("磁盘满了（模拟写入失败）")
        self.appended.append((session_id, new_messages))
        self.unread_deltas.append(unread_delta)
        return type("S", (), {"id": session_id})()


class _Chain:
    """最小消息链（形状照 `MessageChain`）。"""

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        return "".join(self._texts)


# ---------------------------------------------------------------------------
# ① 真的写进会话
# ---------------------------------------------------------------------------


async def test_send_writes_message_into_session():
    """🔴 核心判据：投递**真的把消息写进了会话**。

    注入 bug：把 `send()` 的 `append` 那行删掉（只留日志）→ 本判据必须挂。
    """
    sessions = _FakeSessions()
    d = SessionDelivery(sessions=sessions)

    ok = await d.send("11:FriendMessage:default", _Chain("我想你了"))

    assert ok is True, "投递返回了非 True"
    assert sessions.appended, "消息**没有被写进任何会话**（只打了个日志？）"
    sid, msgs = sessions.appended[0]
    assert sid.startswith(PROACTIVE_SESSION_PREFIX), sid
    assert len(msgs) == 1
    assert msgs[0].content == "我想你了"
    assert msgs[0].role == "assistant", (
        f"主动消息的角色应为 assistant，实际 {msgs[0].role!r}"
    )


async def test_send_creates_session_when_missing():
    """会话不存在时**先建再写**（主动消息可能是第一句话）。"""
    sessions = _FakeSessions()
    d = SessionDelivery(sessions=sessions)

    await d.send("11:FriendMessage:u1", _Chain("在吗"))

    assert sessions.ensured, "没有先 ensure 会话（会话不存在时会写失败）"
    assert sessions.appended, "没有写入"


async def test_same_umo_always_maps_to_same_session():
    """🔴 同一个 umo **永远映射到同一个会话**（重启后不能漂）。

    为什么关键：插件只知道 umo，若会话 id 用随机 uuid，
    重启后主动消息会散落在无数个新会话里，用户永远找不到上下文。
    """
    a = session_id_for_umo("11:FriendMessage:default")
    b = session_id_for_umo("11:FriendMessage:default")
    c = session_id_for_umo("11:FriendMessage:other")

    assert a == b, "同一个 umo 映射出了不同会话 id（重启后会漂）"
    assert a != c, "不同 umo 映射到了同一个会话"


async def test_delivery_to_two_users_lands_in_two_sessions():
    """两个 umo → 两个独立会话（不能串台）。"""
    sessions = _FakeSessions()
    d = SessionDelivery(sessions=sessions)

    await d.send("11:FriendMessage:a", _Chain("给A"))
    await d.send("11:FriendMessage:b", _Chain("给B"))

    ids = [sid for sid, _ in sessions.appended]
    assert len(set(ids)) == 2, f"两个用户的消息写进了同一个会话：{ids}"


# ---------------------------------------------------------------------------
# ② 不伪造送达
# ---------------------------------------------------------------------------


async def test_send_returns_false_when_write_fails():
    """🔴 **写失败必须返回 False**，绝不假装成功（作者判据③）。

    注入 bug：把 `except` 里的 `return False` 改成 `return True`
    → 本判据必须挂。
    """
    sessions = _FakeSessions(fail_on_append=True)
    d = SessionDelivery(sessions=sessions)

    ok = await d.send("11:FriendMessage:default", _Chain("你好"))

    assert ok is False, (
        "写失败却返回了 True —— 这是**伪造送达**，"
        "插件会以为消息发出去了而不再重试"
    )


async def test_send_returns_false_without_repository():
    """没有会话仓库 → 如实返回 False（不抛错、也不假装成功）。"""
    d = SessionDelivery(sessions=None)
    assert await d.send("11:FriendMessage:default", _Chain("你好")) is False


async def test_send_returns_false_on_empty_chain():
    """空消息链 → 返回 False（不当成功）。"""
    sessions = _FakeSessions()
    d = SessionDelivery(sessions=sessions)

    assert await d.send("11:FriendMessage:default", _Chain("")) is False
    assert not sessions.appended, "空消息不该写进会话"


# ---------------------------------------------------------------------------
# ②·补 🔴 **失败必须可见**（2026-09-29 补，W2）
#
# 上面 ①②③ 那一组的判据都只断言**返回值**。实测注入验证（三种静默失败同时注入）：
#   把 `send()` 里三处失败日志全删掉、只留 `return False`
#   → **27 条判据全绿，一条都没抓住**。
#
# 🔴 后果为什么最重（本项目头号铁律「静默失败比报错贵」）：
#   `SessionDelivery.send` 是插件【主动消息】唯一的落点。
#   它失败且不记日志时，用户看到的是"伴侣一直没找我"，
#   而日志里**连一行线索都没有** —— 与 aitag「参考图被静默丢弃」同型事故。
#   ⚠️ 而且返回值是对的（False）⇒ 插件那边也认为"失败了"，
#      但**没有任何人、任何地方**能回答"为什么失败"。
#
# ⚠️ 判据写法：断言日志里含 **umo**（定位是哪条会话）与 **异常类型/原因**
#    （定位为什么失败）—— 只断言"有 ERROR"是不够的：
#    一条 `logger.error("failed")` 也能过，那种日志对排查没有价值。
# ---------------------------------------------------------------------------
async def test_write_failure_must_be_logged(caplog):
    """🔴 写会话失败 → 必须记 **ERROR**，且日志能定位到 umo 与失败原因。

    （注入验证：删掉这句 logger.error 后，本文件其余判据全绿 ⇒ 这条是必要的）
    """
    sessions = _FakeSessions(fail_on_append=True)
    d = SessionDelivery(sessions=sessions)

    with caplog.at_level("ERROR"):
        ok = await d.send("11:FriendMessage:default", _Chain("你好"))

    assert ok is False
    errors = [r for r in caplog.records if r.levelno >= 40]
    assert errors, (
        "写会话失败却**没有任何 ERROR 日志** —— 静默失败。"
        "用户只会看到「伴侣一直没找我」，而我们无从排查。"
    )
    text = " ".join(r.getMessage() for r in errors)
    assert "11:FriendMessage:default" in text, (
        f"ERROR 里没有 umo ⇒ 无法定位是哪条会话的消息丢了：{text!r}"
    )
    assert "RuntimeError" in text or "磁盘满了" in text, (
        f"ERROR 里没有失败原因 ⇒ 无法判断为什么写不进去：{text!r}"
    )


async def test_missing_repository_must_be_logged(caplog):
    """🔴 会话仓库缺失（**我们自己的装配失误**）→ 必须记 ERROR。

    ⚠️ 这条对应"能力缺口要喊"：仓库没接上时投递功能整体失效，
       而这个失效如果没日志，就退化成本项目最贵的那类 bug（能力缺失但不喊）。
    """
    d = SessionDelivery(sessions=None)

    with caplog.at_level("ERROR"):
        ok = await d.send("11:FriendMessage:default", _Chain("你好"))

    assert ok is False
    errors = [r for r in caplog.records if r.levelno >= 40]
    assert errors, "会话仓库缺失却没有任何日志 —— 装配缺口被静默了"
    text = " ".join(r.getMessage() for r in errors)
    assert "仓库" in text or "装配" in text, (
        f"日志没说明是「仓库缺失/装配问题」⇒ 排查时看不出根因：{text!r}"
    )


async def test_empty_chain_must_be_logged(caplog):
    """🔴 空消息链（异常情况）→ 必须记 WARNING，且带上 umo。

    ⚠️ 为什么不返回 True 就够：插件那边只会看到 False，
       它不知道是"上游给了空链"还是"我们写失败了" —— 那正是最需要日志的场景。
    """
    sessions = _FakeSessions()
    d = SessionDelivery(sessions=sessions)

    with caplog.at_level("WARNING"):
        ok = await d.send("11:FriendMessage:someone", _Chain(""))

    assert ok is False
    assert not sessions.appended
    warns = [r for r in caplog.records if r.levelno >= 30]
    assert warns, "收到空消息链却没有任何日志 —— 静默丢弃（本项目头号禁忌）"
    text = " ".join(r.getMessage() for r in warns)
    assert "11:FriendMessage:someone" in text, (
        f"日志里没有 umo ⇒ 不知道是谁的空链：{text!r}"
    )


# ---------------------------------------------------------------------------
# ③ 降级可见（不静默丢内容）
# ---------------------------------------------------------------------------


def test_chain_to_text_marks_non_text_components():
    """非文本组件降级成 `[类型]` 标记（用户能看出"这里本来有东西"）。"""

    class Image:
        pass

    class Plain:
        def __init__(self, t: str) -> None:
            self.text = t

    out = chain_to_text([Plain("看图："), Image()])
    assert "看图：" in out
    assert "[Image]" in out, f"非文本组件被**静默丢掉**了：{out!r}"


def test_chain_to_text_warns_when_degrading(caplog):
    """🔴 降级**必须留日志** —— 静默丢内容是本项目最贵的 bug 类型。"""

    class Record:
        pass

    with caplog.at_level("WARNING"):
        chain_to_text([Record()])

    assert any("降级" in r.message or "降级" in str(r.msg) for r in caplog.records), (
        "非文本组件被降级却**没有任何日志**（静默丢弃）"
    )


def test_chain_to_text_handles_none_and_plain_string():
    """边界：`None` → 空串；普通字符串链不炸。"""
    assert chain_to_text(None) == ""
    assert chain_to_text([]) == ""
