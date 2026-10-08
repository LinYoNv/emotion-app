# -*- coding: utf-8 -*-
"""会话 umo 标签 + 未读徽标（情绪 2026-09-26 要的两个功能）。

## 这一组判据守的是什么

情绪的原话：
> 「每个会话都带一个唯一标签 umo，这样我就能自己到陪伴那边添加"用户"和
>   测试主动消息等东西了，**陪伴插件不就是靠 umo 去识别用户的嘛**」
> 「还有给"聊天"加一个功能：用户没有阅读的时候出个提示，
>   就像"插件"旁边那个圆圈 3 一样，提示用户有未读消息」

所以要证明的是**用户在那个界面上真的看得见、拿得走**，
而不是"接口返回了 200"（本项目踩过：人设导入接口全绿，面板片段表却空着）。

涵盖四层：
  ①  HTTP：新建会话**自带**唯一 umo；`/umo` 能换且如实返回旧值；
  ②  HTTP：`/read` 清零、`/unread` 汇总形状（含**反向**：未登录读不到）；
  ③  领域：只有**插件主动消息**会 +1（用户自己聊天不 +1）—— 语义核心；
  ④  反查：`by_user_id(umo 第三段)` 能找回该会话（主动消息落点判据）。

⚠️ **为什么不用 `conftest.mk`**：那个夹具只挂 provider 相关的东西，
   而 `create_app(..., sessions=...)` 要显式注入会话仓库 —— 用 `mk` 会拿到
   503「会话服务未就绪」，测的根本不是这条链路（我第一版就是这么错的）。
   这里照 `tests/test_s3_http.py` 的 `ctx` 口径自己装配。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.domain.backends import OpenAIChatBackend  # noqa: E402
from emotion_app.domain.chat import ChatService  # noqa: E402
from emotion_app.domain.message import ROLE_ASSISTANT, ROLE_USER, Message  # noqa: E402
from emotion_app.domain.persona_repo import PersonaRepository  # noqa: E402
from emotion_app.domain.session_repo import SessionRepository  # noqa: E402
from emotion_app.domain.settings import SettingsService  # noqa: E402
from emotion_app.infra.config import Config  # noqa: E402
from emotion_app.storage.sqlite_store import SqliteStore  # noqa: E402
from emotion_app.web.app import create_app  # noqa: E402
from emotion_app.web.auth import AuthManager  # noqa: E402

TOKEN = "t" * 20


def _reply(text: str = "收到") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    })


class _Ctx:
    """一个装好的 app + 客户端 + 会话仓库（判据要用仓库直插消息）。"""

    def __init__(self, client, store, sessions) -> None:
        self.client = client
        self.store = store
        self.sessions = sessions

    async def close(self) -> None:
        await self.store.close()

    # ---- 常用动作（少写重复） ----

    async def new_session(self, title: str = "甲") -> dict:
        r = await self.client.post("/api/v1/sessions", json={"title": title})
        assert r.status_code == 201, f"新建会话失败：{r.status_code} {await r.get_json()}"
        return await r.get_json()

    async def get_session(self, sid: str) -> dict:
        r = await self.client.get(f"/api/v1/sessions/{sid}")
        assert r.status_code == 200, r.status_code
        return await r.get_json()


@pytest.fixture
async def ctx(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "umo.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    settings = SettingsService(store, cfg)
    await settings.load()
    personas = PersonaRepository(store)
    sessions = SessionRepository(store)
    upstream = OpenAIChatBackend(
        transport=httpx.MockTransport(lambda request: _reply())
    )
    await upstream.open()
    chat = ChatService(cfg, upstream, settings=settings, personas=personas)
    chat.bind_sessions(sessions)

    app = create_app(
        cfg, store, AuthManager(access_token=TOKEN), chat,
        personas=personas, settings=settings, sessions=sessions,
    )
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    c = _Ctx(client, store, sessions)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def anon(tmp_path):
    """未登录客户端（反向判据用）—— 同样要挂上会话仓库，否则 401 会被 503 盖掉。"""
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "anon.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    sessions = SessionRepository(store)
    app = create_app(cfg, store, AuthManager(access_token=TOKEN), sessions=sessions)
    try:
        yield app.test_client()
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# ① 每个会话自带唯一 umo
# ---------------------------------------------------------------------------


async def test_new_session_gets_its_own_umo(ctx):
    """🔴 新建会话**自带**一个专属 umo（不是空、不是全局默认）。

    注入 bug：把 `create()` 里那段自动分配删掉（回到"空串=跟随全局"）→ 必挂。
    """
    c = ctx
    a = await c.new_session("甲")
    b = await c.new_session("乙")

    assert a["user_id"], "新会话没有自动分配插件用户 ID"
    assert a["user_id"] != b["user_id"], "两个会话拿到了**同一个** umo 标签（不唯一）"
    for s in (a, b):
        # 完整 umo 必须由后端算好（前端绝不自己拼 —— 平台 id 是宿主的定义）
        assert s["umo"] == f"11:FriendMessage:{s['user_id']}", s["umo"]
        assert s["platform"] == "11", s
        # 🔴 **形状**也要钉住：只验"不含非法字符"太松 ——
        #    `uuid4().hex` 同样满足那些（无冒号、非全数字、无空白），
        #    但它没有可辨识前缀、长度还会随实现漂移。
        #    ⚠️ 注入验证第一轮就是这么放过去的（注入③仍然全绿）。
        uid = s["user_id"]
        assert not uid.isdigit(), f"全数字会被插件当成 QQ 用户：{uid!r}"
        assert ":" not in uid and not any(c.isspace() for c in uid), uid
        assert re.fullmatch(r"sess-[0-9a-f]{12}", uid), (
            f"标签形状不符合约定（应为 sess-<12位十六进制>）：{uid!r}"
        )


async def test_umo_survives_reload(ctx):
    """刷新后 umo 不变（它是插件的**数据主键**，漂了等于换库）。"""
    c = ctx
    created = await c.new_session("甲")
    again = await c.get_session(created["id"])
    assert again["umo"] == created["umo"]
    assert again["user_id"] == created["user_id"]


async def test_umo_can_be_regenerated(ctx):
    """面板「换一个专用 umo」：换得掉、如实返回旧值、旧标签不再能反查到会话。"""
    c = ctx
    s = await c.new_session("甲")
    old = s["user_id"]

    r = await c.client.post(f"/api/v1/sessions/{s['id']}/umo")
    assert r.status_code == 200, r.status_code
    d = await r.get_json()
    assert d["previous"] == old, "没有如实返回被替换掉的旧标签"
    assert d["session"]["user_id"] != old

    # 反查走的是 user_id（主动消息投递的落点判据）
    rl = await c.client.get("/api/v1/sessions")
    listing = (await rl.get_json())["data"]
    me = next(x for x in listing if x["id"] == s["id"])
    assert me["user_id"] == d["session"]["user_id"]


async def test_umo_route_requires_auth(ctx, anon):
    """反向判据：未登录**不能**换 umo（那是一条写操作）。"""
    c = ctx
    s = await c.new_session("甲")
    r = await anon.post(f"/api/v1/sessions/{s['id']}/umo")
    assert r.status_code == 401, f"未登录也能换 umo：{r.status_code}"


async def test_umo_route_404_for_unknown_session(ctx):
    """不存在的会话要如实 404，不能静默当成功。"""
    c = ctx
    r = await c.client.post("/api/v1/sessions/nope/umo")
    assert r.status_code == 404, r.status_code


async def test_old_session_without_user_id_reports_empty_umo(ctx):
    """存量会话（没绑标签）→ `umo` 是**空串**，绝不编一个假的出来。

    ⚠️ 这条守的是"如实报告"：编一个看起来正常的 umo 会让用户
       拿着它去陪伴面板建档，然后那条会话**永远收不到主动消息**。
    """
    c = ctx
    # 直接往仓库里塞一条旧形状的会话（没有 user_id）
    repo = c.sessions
    await repo.create(title="老的", session_id="legacy-1", user_id="x")
    await repo.update("legacy-1", {"user_id": ""})

    d = await c.get_session("legacy-1")
    assert d["user_id"] == ""
    assert d["umo"] == "", f"没绑标签时应为空串，得到 {d['umo']!r}"
    assert d["platform"] == ""


# ---------------------------------------------------------------------------
# ② 未读：只有插件主动消息会 +1
# ---------------------------------------------------------------------------


async def test_user_chat_does_not_mark_unread(ctx):
    """🔴 **用户自己聊天不产生未读**（他此刻就在页面上看着）。

    这条是未读语义的核心：若它挂了，用户每说一句都会看到红点 ——
    那是假阳性，比不做还烦。

    注入 bug：把 `ChatService.record` 的 append 改成 `unread_delta=1` → 必挂。
    """
    c = ctx
    s = await c.new_session("甲")
    repo = c.sessions

    await repo.append(
        s["id"],
        [Message(role=ROLE_USER, content="在吗"), Message(role=ROLE_ASSISTANT, content="在的")],
    )
    d = await c.get_session(s["id"])
    assert d["unread"] == 0, f"用户自己的聊天被算成未读：{d['unread']}"


async def test_proactive_message_marks_unread_then_read_clears_it(ctx):
    """插件主动消息 → 未读 +1；面板打开该会话（`/read`）→ 清零。"""
    c = ctx
    s = await c.new_session("甲")
    repo = c.sessions

    await repo.append(
        s["id"], [Message(role=ROLE_ASSISTANT, content="午饭吃完了吧。")], unread_delta=1
    )
    d = await c.get_session(s["id"])
    assert d["unread"] == 1, d["unread"]

    r = await c.client.post(f"/api/v1/sessions/{s['id']}/read")
    assert r.status_code == 200
    assert (await r.get_json())["session"]["unread"] == 0

    # 再打开一次也不该出错（幂等）
    assert (await c.client.post(f"/api/v1/sessions/{s['id']}/read")).status_code == 200


async def test_delivery_port_marks_unread(ctx):
    """🔴 **接线**判据：真 `SessionDelivery.send()` 才会让未读 +1。

    ⚠️ 为什么必须走端口而不是直接 `repo.append(..., unread_delta=1)`：
       直接调仓库只证明"这个参数有用"，证明不了**投递那条路上传了它**。
       本项目的老坑就是"函数对 ≠ 接线对"（注入验证第一轮 3/10 没抓住）。
       注入 bug：把 `delivery.py` 的 `unread_delta=1` 改成 0 → 本判据必挂。
    """
    from emotion_app.web.delivery import SessionDelivery

    c = ctx
    s = await c.new_session("甲")

    class _Chain:
        def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
            return "我想你了"

    delivery = SessionDelivery(sessions=c.sessions, settings=None)
    ok = await delivery.send(s["umo"], _Chain())
    assert ok is True, "投递被如实拒绝了（那插件会走降级路径）"

    d = await c.get_session(s["id"])
    assert d["unread"] == 1, f"主动消息没被标成未读：{d['unread']}"
    assert d["message_count"] == 1

    # 面板打开它 → 清零（走真路由）
    await c.client.post(f"/api/v1/sessions/{s['id']}/read")
    assert (await c.get_session(s["id"]))["unread"] == 0


async def test_unread_endpoint_shape(ctx):
    """`GET /api/v1/unread` 只报**有未读**的会话，total 是它们之和。"""
    c = ctx
    a = await c.new_session("甲")
    b = await c.new_session("乙")
    repo = c.sessions

    await repo.append(a["id"], [Message(role=ROLE_ASSISTANT, content="hi")], unread_delta=1)
    await repo.append(b["id"], [Message(role=ROLE_ASSISTANT, content="hi")], unread_delta=1)
    await repo.append(b["id"], [Message(role=ROLE_ASSISTANT, content="hi")], unread_delta=1)
    await c.client.post(f"/api/v1/sessions/{a['id']}/read")

    d = await (await c.client.get("/api/v1/unread")).get_json()
    assert d["sessions"] == {b["id"]: 2}, d["sessions"]
    assert d["total"] == 2, d
    assert a["id"] not in d["sessions"], "已读的会话仍出现在未读表里"


async def test_unread_route_requires_auth(anon):
    """反向：未登录读不到未读汇总（它带着会话 id）。"""
    assert (await anon.get("/api/v1/unread")).status_code == 401


async def test_unread_delta_cannot_go_negative(tmp_path):
    """领域层：`unread_delta` 传负数**不会**把计数压成负数。

    守的 bug：一个"看着像 0、其实是坏的"状态（负计数会让徽标永远不消失）。
    """
    from emotion_app.domain.session_repo import SessionRepository
    from emotion_app.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "u.db"))
    await store.open()
    try:
        repo = SessionRepository(store)
        s = await repo.create(title="甲")
        await repo.append(s.id, [Message(role=ROLE_ASSISTANT, content="x")], unread_delta=-5)

        # 闸①：**落盘的原值**不许是负数（否则前端拿到负号，徽标逻辑直接崩）
        raw = (await store.load_sections(["sessions"]))["sessions"][s.id]
        assert raw["unread"] == 0, f"落盘写进了负数未读：{raw['unread']!r}"

        # 闸②：读回来的值也不许是负数（`from_dict` 那道夹取）
        assert (await repo.get(s.id)).unread == 0
        assert await repo.unread_total() == 0
    finally:
        await store.close()


async def test_by_user_id_finds_session_from_umo(ctx):
    """⭐ 端到端落点判据：拿 umo 第三段能反查到**这条**会话。

    这是主动消息"该落到哪个会话"的唯一依据（`delivery._resolve_session_id`）。
    """
    c = ctx
    s = await c.new_session("甲")
    repo = c.sessions

    third = s["umo"].split(":", 2)[2]
    found = await repo.by_user_id(third)
    assert found is not None, "按 umo 第三段反查不到会话 → 主动消息会落进孤儿会话"
    assert found.id == s["id"]


@pytest.mark.parametrize("bad", ["sess-abc def", "a:b", "x" * 130])
async def test_patch_still_rejects_illegal_user_id(ctx, bad):
    """用户手改的标签仍要过校验（自动生成不代表校验可以松）。"""
    c = ctx
    s = await c.new_session("甲")
    r = await c.client.patch(f"/api/v1/sessions/{s['id']}", json={"user_id": bad})
    assert r.status_code == 400, f"{bad!r} 竟然被接受了：{r.status_code}"


async def test_concurrent_creates_get_distinct_labels(ctx):
    """并发新建也不会撞标签（唯一性检查必须真的生效）。

    ⚠️ 用一个较小的并发数（8）：目的是证明"检查 + 写入"这条路径在竞争下
       仍然产出不同值，不是压测。
    """
    c = ctx
    created = await asyncio.gather(
        *[c.client.post("/api/v1/sessions", json={"title": f"c{i}"}) for i in range(8)]
    )
    ids = [(await r.get_json())["user_id"] for r in created]
    assert len(set(ids)) == len(ids), f"并发下标签撞车了：{ids}"
