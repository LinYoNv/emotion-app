# -*- coding: utf-8 -*-
"""按会话绑人设的契约测试。

## 为什么单开这个文件

面板**早就有**「绑定人设」下拉（`index.html:98` / `app.js:880` 填充 / `:950` 提交），
后端 `PATCH /api/v1/sessions/<id>` **也接受** `persona_id`（`api_admin.py:940`），
`Session.persona_id` **也存得下**。

🔴 **但存了不读** —— 实测（真造两个人设 + 绑定的会话）：

```
① 会话A 绑的人设        = 94ce904e…（测试甲）
② 全局 active_persona_id = （空）
③ resolve_persona() 实际取到 = None      ← 会话绑的甲被完全无视
④ 【应该】取到           = 测试甲
```

三个真 bug，逐个有判据：

| # | bug | 位置 |
|---|---|---|
| ① | `resolve_persona()` **不看会话** —— 全局空着就返回 None | `domain/chat.py:255` |
| ② | 调用它时**不传 session_id**，函数也**没有这个参数** | `web/app.py:386` |
| ③ | 首次聊天把**全局**人设**写进**会话 → **永久钉死**，之后不再跟随全局 | `domain/chat.py:344` |

⚠️ bug ③ 最阴：它与面板上写的「留空 = 用当前人设」**语义直接矛盾** ——
   用户在面板改了全局人设，这个会话却再也不会跟着变，而且**没有任何提示**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.domain.chat import ChatService  # noqa: E402
from emotion_app.domain.persona import Persona  # noqa: E402
from emotion_app.domain.persona_repo import PersonaRepository  # noqa: E402
from emotion_app.domain.session_repo import SessionRepository  # noqa: E402
from emotion_app.domain.settings import SettingsService  # noqa: E402
from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.sqlite_store import SqliteStore  # noqa: E402


async def _svc(tmp_path: Path):
    """造一套装配好的服务（真 SqliteStore，临时库）。"""
    store = SqliteStore(tmp_path / "t.db")
    await store.open()
    cfg = Config()
    personas = PersonaRepository(store)
    sessions = SessionRepository(store)
    settings = SettingsService(store, cfg)
    await settings.load()
    svc = ChatService(cfg, settings=settings, personas=personas)
    svc.bind_sessions(sessions)
    return svc, personas, sessions, settings, store


async def _mk_persona(personas: PersonaRepository, name: str) -> Persona:
    return await personas.create(Persona(name=name, description=f"我是{name}"))


# ---------------------------------------------------------------------------
# ① 会话绑的人设必须生效（最高优先）
# ---------------------------------------------------------------------------


async def test_session_persona_wins_over_global(tmp_path):
    """🔴 核心判据：会话绑了人设 → **就用它**，哪怕全局是另一个。

    注入 bug：把 `resolve_persona` 改回 `pid = persona_id or settings.active_persona_id`
    （即不看会话）→ 本判据必须挂。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        a = await _mk_persona(personas, "甲")
        b = await _mk_persona(personas, "乙")
        s = await sessions.create(title="会话", persona_id=a.id)
        await settings.set_active_persona(b.id)   # 全局设成乙

        got = await svc.resolve_persona(session_id=s.id)

        assert got is not None, "会话绑了人设却解析出 None"
        assert got.name == "甲", (
            f"会话绑的是「甲」，却解析出「{got.name}」—— 会话人格被全局覆盖了"
        )
    finally:
        await store.close()


async def test_session_persona_works_when_global_is_empty(tmp_path):
    """🔴 全局为空时，会话绑的人设**仍然生效**。

    这条正是实测抓到的形态：全局 `active_persona_id` 是空串，
    于是 `resolve_persona()` 返回 None —— 会话绑的甲**被完全无视**。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        a = await _mk_persona(personas, "甲")
        s = await sessions.create(title="会话", persona_id=a.id)
        assert settings.active_persona_id == "", "前置条件：全局人设应为空"

        got = await svc.resolve_persona(session_id=s.id)

        assert got is not None, (
            "全局人设为空时，会话绑的人设也被无视了（应回落到会话人格）"
        )
        assert got.name == "甲"
    finally:
        await store.close()


async def test_session_without_persona_falls_back_to_global(tmp_path):
    """会话**没绑**人设 → 回落全局（面板写的是「留空 = 用当前人设」）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        b = await _mk_persona(personas, "乙")
        s = await sessions.create(title="会话", persona_id="")
        await settings.set_active_persona(b.id)

        got = await svc.resolve_persona(session_id=s.id)

        assert got is not None and got.name == "乙", (
            f"会话没绑人设时应回落全局「乙」，实际 {got and got.name}"
        )
    finally:
        await store.close()


async def test_session_persona_pointing_to_missing_persona_falls_back(tmp_path):
    """会话绑的人设**已不存在** → 响亮记日志并回落全局（不崩、不静默无提示）。

    ⚠️ 这是"人设被删了但会话还指着它"的真实场景。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        b = await _mk_persona(personas, "乙")
        s = await sessions.create(title="会话", persona_id="不存在的id")
        await settings.set_active_persona(b.id)

        got = await svc.resolve_persona(session_id=s.id)

        assert got is not None and got.name == "乙", (
            "会话指向的人设不存在时应回落全局，而不是返回 None"
        )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# ② 显式 persona_id 仍然最高（不能因为加了会话就把它挤掉）
# ---------------------------------------------------------------------------


async def test_explicit_persona_id_still_highest(tmp_path):
    """显式传入的 `persona_id` **优先级最高**（会话/全局都不能盖过它）。

    优先级：显式 > 会话 > 全局。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        a = await _mk_persona(personas, "甲")
        b = await _mk_persona(personas, "乙")
        c = await _mk_persona(personas, "丙")
        s = await sessions.create(title="会话", persona_id=a.id)
        await settings.set_active_persona(b.id)

        got = await svc.resolve_persona(c.id, session_id=s.id)

        assert got is not None and got.name == "丙", (
            f"显式指定应最高优先，实际 {got and got.name}"
        )
    finally:
        await store.close()


async def test_no_session_id_keeps_old_behavior(tmp_path):
    """**不传** session_id → 行为与以前完全一致（全局人设）。

    这条是**向后兼容**判据：`/v1/chat/completions` 不带 session_id 时
    （无状态单轮）不该因为这次改动而变。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        b = await _mk_persona(personas, "乙")
        await settings.set_active_persona(b.id)

        got = await svc.resolve_persona()

        assert got is not None and got.name == "乙"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# ③ 不能把全局人设「钉死」到会话上
# ---------------------------------------------------------------------------


async def test_chat_does_not_pin_global_persona_into_session(tmp_path):
    """🔴 首次聊天**不能**把全局人设写进会话 —— 否则永久钉死。

    现象：会话本来没绑人设（= 跟随全局）。聊一次之后
    `session.persona_id` 被写成当时的全局人设 → 用户之后在面板改全局，
    **这个会话再也不跟着变**，且**没有任何提示**。
    这与面板上「留空 = 用当前人设」的文案**直接矛盾**。

    注入 bug：把 `_prepare_session` 里那段 `if persona_id and not session.persona_id:
    ...update(...)` 放回去 → 本判据必须挂。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        b = await _mk_persona(personas, "乙")
        s = await sessions.create(title="会话", persona_id="")
        await settings.set_active_persona(b.id)

        # 模拟一次聊天：解析人设 + 走 _prepare_session
        persona = await svc.resolve_persona(session_id=s.id)
        await svc.prepare_session(_req(s.id, persona))

        after = await sessions.get(s.id)
        assert after.persona_id == "", (
            f"会话本来没绑人设，聊天后被钉成了 {after.persona_id!r} —— "
            "用户之后改全局人设，这个会话将不再跟随（且无任何提示）"
        )

        # 再验证一次"跟随全局"仍然有效
        c = await _mk_persona(personas, "丙")
        await settings.set_active_persona(c.id)
        followed = await svc.resolve_persona(session_id=s.id)
        assert followed is not None and followed.name == "丙", (
            f"没绑人设的会话应跟随全局变化，实际 {followed and followed.name}"
        )
    finally:
        await store.close()


def _req(session_id: str, persona):
    """造一个最小的 ChatRequest（只用到 session_id / persona）。"""
    from emotion_app.domain.chat import ChatRequest

    return ChatRequest(session_id=session_id, persona=persona)


# ===========================================================================
# 方案 B：会话 ↔ 插件用户（umo 第三段）
# ===========================================================================


async def test_session_has_user_id_and_it_roundtrips(tmp_path):
    """`Session.user_id` 能存能读（方案 B 的基础）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        s = await sessions.create(title="会话", user_id="alice")
        again = await sessions.get(s.id)
        assert again.user_id == "alice", f"user_id 没存住：{again.user_id!r}"
        assert again.to_dict()["user_id"] == "alice", "to_dict 漏了 user_id"
    finally:
        await store.close()


async def test_by_user_id_finds_the_right_session(tmp_path):
    """🔴 按 user_id 找会话 —— 主动消息靠它落进**用户自己的**会话。

    注入 bug：把 `by_user_id` 改成永远返回 None → 本判据必须挂。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        a = await sessions.create(title="甲会话", user_id="alice")
        b = await sessions.create(title="乙会话", user_id="bob")

        got = await sessions.by_user_id("bob")

        assert got is not None, "按 user_id 找不到会话"
        assert got.id == b.id, f"找错了会话：{got.title}"
        assert (await sessions.by_user_id("alice")).id == a.id
    finally:
        await store.close()


async def test_by_user_id_returns_none_for_unknown(tmp_path):
    """查不到的 user_id → None（调用方会新建，不抛）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        await sessions.create(title="甲", user_id="alice")
        assert await sessions.by_user_id("nobody") is None
        assert await sessions.by_user_id("") is None
    finally:
        await store.close()


async def test_by_user_id_warns_on_duplicate(tmp_path, caplog):
    """🔴 多个会话绑同一 user_id → 返回最早的 + **留 WARNING**（不静默）。

    为什么必须留日志：那是配置歧义 —— 主动消息只会落到其中一个，
    用户如果不知道，会以为"另一个会话的主动消息坏了"。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        first = await sessions.create(title="先建的", user_id="dup")
        await sessions.create(title="后建的", user_id="dup")

        with caplog.at_level("WARNING"):
            got = await sessions.by_user_id("dup")

        assert got is not None and got.id == first.id, "应返回最早创建的那个"
        assert any("同一个 user_id" in str(r.message) for r in caplog.records), (
            "重复绑定没有任何日志（静默歧义）"
        )
    finally:
        await store.close()


def test_umo_user_id_takes_third_segment():
    """`umo_user_id` 取第三段（== 插件 user_id == sender_id）。"""
    from emotion_app.web.delivery import umo_user_id

    assert umo_user_id("11:FriendMessage:3412642082") == "3412642082"
    # ⚠️ 第三段**允许含冒号**（照真实现 split(":", 2)）
    assert umo_user_id("11:FriendMessage:a:b") == "a:b"
    # 格式不对 → 返回原串（不静默变空串）
    assert umo_user_id("坏数据") == "坏数据"
    assert umo_user_id("") == ""


async def test_delivery_lands_in_users_own_session(tmp_path):
    """🔴🔴 **决定性判据**：主动消息落进**用户自己的会话**，不是孤儿会话。

    场景（情绪提的）：同时和三个会话聊天，只有一个想开主动消息。
    插件发主动消息时给的是 umo，我们**必须**按 umo 第三段找到
    **它对应的那个会话** —— 而不是用 sha256 另造一个用户找不到的会话。

    注入 bug：把 `_resolve_session_id` 改成直接 `return session_id_for_umo(umo)`
    → 本判据必须挂。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        from emotion_app.web.delivery import SessionDelivery

        own = await sessions.create(title="我的会话", user_id="alice")
        await sessions.create(title="另一个会话", user_id="bob")

        d = SessionDelivery(sessions=sessions, settings=settings)
        ok = await d.send("11:FriendMessage:alice", _Chain("我想你了"))

        assert ok is True, "投递返回了非 True"

        own_msgs = await sessions.messages(own.id)
        assert len(own_msgs) == 1, (
            f"主动消息**没落进用户自己的会话**（该会话有 {len(own_msgs)} 条）—— "
            "多半是又用 sha256 造了个孤儿会话"
        )
        assert own_msgs[0].content == "我想你了"

        # 另一个用户的会话必须**没被污染**
        other = await sessions.by_user_id("bob")
        assert await sessions.messages(other.id) == [], "主动消息串到别的用户了"
    finally:
        await store.close()


async def test_delivery_falls_back_to_new_session_for_unknown_user(tmp_path):
    """用户还没建过会话 → 新建一个**并绑上 user_id**（不是孤儿）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        from emotion_app.web.delivery import SessionDelivery

        d = SessionDelivery(sessions=sessions, settings=settings)
        ok = await d.send("11:FriendMessage:carol", _Chain("在吗"))
        assert ok is True

        found = await sessions.by_user_id("carol")
        assert found is not None, (
            "新建的会话没绑 user_id —— 下次主动消息又会找不到它（孤儿）"
        )
        assert found.user_id == "carol"
    finally:
        await store.close()


class _Chain:
    """最小消息链（形状照 `MessageChain`）。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        return self._text


# ---------------------------------------------------------------------------
# user_id 规整（它是插件的【字典键】，必须逐字节可比）
# ---------------------------------------------------------------------------


async def test_user_id_is_trimmed(tmp_path):
    """🔴 前后空白必须 trim —— 否则造出"看着一样、实际匹配不上"的键。

    注入 bug：把 `_normalize_user_id` 的 `.strip()` 去掉 → 本判据必须挂。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        s = await sessions.create(title="会话", user_id="  alice  ")
        assert s.user_id == "alice", f"没 trim：{s.user_id!r}"
        assert await sessions.by_user_id("alice") is not None, (
            "trim 后应该能用不带空格的 id 找到"
        )
    finally:
        await store.close()


async def test_user_id_rejects_colon(tmp_path):
    """冒号必须**拒绝** —— 它会破坏 umo 的 `A:B:C` 结构（静默错位）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="冒号"):
            await sessions.create(title="会话", user_id="a:b")
    finally:
        await store.close()


async def test_user_id_rejects_inner_whitespace(tmp_path):
    """内部空白也必须拒绝（空格在键里无法肉眼分辨）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        with pytest.raises(ValueError, match="空白"):
            await sessions.create(title="会话", user_id="a b")
    finally:
        await store.close()


async def test_user_id_is_auto_assigned_when_empty(tmp_path):
    """🔴 空串不再是"跟随全局" —— 新建会话时**自动分配一个专属 umo 标签**。

    情绪 2026-09-26：「每个会话都带一个唯一标签 umo，这样我就能自己到陪伴那边
    添加"用户"和测试主动消息等东西了，陪伴插件不就是靠 umo 去识别用户的嘛」

    ⚠️ 这条判据同时守三件事（每一条都能单独失败）：
      ① 自动分配（不是空串）；
      ② **两个会话拿到不同的标签**（"唯一"的意义所在 —— 共用一个就退回了
         旧行为：所有会话在插件眼里是同一个用户）；
      ③ 标签本身**不违反插件侧的三条约束**（非全数字 / 无冒号空白 / 不是
         `scheduler`）—— 见 `session_repo._new_umo_user_id` 的 docstring。
    """
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        s = await sessions.create(title="会话", user_id="")
        assert s.user_id, "空 user_id 应自动分配一个专属标签，而不是留空"
        s2 = await sessions.create(title="会话2", user_id="   ")
        assert s2.user_id, "全空白同样走自动分配"
        assert s.user_id != s2.user_id, "两个会话必须拿到**不同**的 umo 标签"

        for uid in (s.user_id, s2.user_id):
            assert not uid.isdigit(), f"全数字会被插件当成 QQ 用户：{uid!r}"
            assert ":" not in uid and not any(c.isspace() for c in uid), uid
            assert uid.strip().lower() != "scheduler", uid

        # 能用这个标签反查回会话（主动消息投递就靠这条）
        assert (await sessions.by_user_id(s.user_id)).id == s.id
    finally:
        await store.close()


async def test_new_umo_user_id_avoids_collision(tmp_path):
    """`new_umo_user_id()` 不会撞上任何已占用的标签（面板「换一个」用它）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        taken = {(await sessions.create(title=f"c{i}")).user_id for i in range(5)}
        new = await sessions.new_umo_user_id()
        assert new not in taken, f"生成了已被占用的标签：{new!r}"
    finally:
        await store.close()


async def test_user_id_can_be_updated_via_patch(tmp_path):
    """PATCH 能改 user_id（面板保存走这条）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        s = await sessions.create(title="会话", user_id="old")
        updated = await sessions.update(s.id, {"user_id": "new"})
        assert updated.user_id == "new"
        assert await sessions.by_user_id("new") is not None
        assert await sessions.by_user_id("old") is None, "旧 id 应已解绑"
    finally:
        await store.close()


async def test_patch_user_id_with_colon_is_rejected(tmp_path):
    """PATCH 带非法 user_id 也要被拒（不能只挡 create）。"""
    svc, personas, sessions, settings, store = await _svc(tmp_path)
    try:
        s = await sessions.create(title="会话", user_id="ok")
        with pytest.raises(ValueError, match="冒号"):
            await sessions.update(s.id, {"user_id": "bad:one"})
        assert (await sessions.get(s.id)).user_id == "ok", "非法值不该写进去"
    finally:
        await store.close()
