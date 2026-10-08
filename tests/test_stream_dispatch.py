# -*- coding: utf-8 -*-
"""差分判据：**流式与非流式两条路都要把用户的话派发给插件**（2026-09-26 修的真 bug）。

## 这个文件守的两个真 bug

### bug A：流式聊天**完全没有派发**给插件

宿主把"用户说了一句话"派发给插件的地方**只写在非流式分支里**
（`app.py` 的 `if not req.stream:`），而流式分支（`async def sse()`）**一行派发都没有**。
我们的面板聊天**永远走流式**（`static/app.js` 发 `stream: true`）
→ 用户在面板里说的每一句话，插件**一次都没收到过**：
没建档、没 umo、记忆/关系/被动状态管线拿不到用户的话。

🔴 **本文件第一条判据就是差分判据**：同一个 app、同一条链路，
唯一变量 = `stream` 开关，两条请求都**必须**派发到假插件。
把 `app.py` 的流式派发改回"没有" → 这条必红。

⚠️ 为什么原来的判据没抓住：`tools/proactive_multisession_check.py:196`
用的是 `"stream": False` —— **判据只覆盖了不被使用的那条路**，
所以它全绿。教训：**判据要盯用户真正走的那条路**。

### bug B：LLM 钩子按**全局**用户 id 取，不按会话

`apply_llm_hooks(messages)` 原来恒取 `config.plugins.umo_user_id`（`default`），
于是**同一次聊天里**：派发建档用的是会话的 user_id，
而记忆/关系/被动状态注入按全局 `default` 算 → **多会话串台**，
且两边各自看起来都正常。

## 🔴 本文件的两条设计约束（照项目铁律）

1. **测生产代码里那一份判据**：`plugin_host.plugin_reply_text()`，不在测试里复刻；
2. **没有"跳过"**：前置不满足就是失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
#: ⚠️ 宿主兼容层与 `emotion_app` **是并列的两个交付物**（不在同一个包里）——
#:    用它的真类（`MessageEventResult` 等）造桩就必须把 `host/` 也加进 sys.path。
#:    （生产侧是 `plugin_host.ensure_host_on_path()` 干这件事。）
if str(APP_DIR / "host") not in sys.path:
    sys.path.insert(0, str(APP_DIR / "host"))

TOKEN = "t" * 20

UPSTREAM_TEXT = "上游回复"
PLUGIN_TEXT = "插件接管回复：已记住你说的话"


# ---------------------------------------------------------------------------
# 假件（形状照生产：`dispatch_message` / `MessageEventResult`）
# ---------------------------------------------------------------------------


class _Res:
    """模拟 `DispatchResult`（只带集成层关心的字段）。"""

    def __init__(self, emitted=None, stopped=False, executed=None, errors=None):
        self.emitted = emitted or []
        self.stopped = stopped
        self.executed = executed or []
        self.errors = errors or []


def _plugin_result(text: str = PLUGIN_TEXT, with_image: bool = False) -> _Res:
    """造一个"插件接管了"的产出。

    🔴 **用宿主兼容层的【真类】**（`MessageEventResult` / `Plain` / `Image`），
       不是测试里自己搓的假链 —— 否则测的是"我的桩理解得对不对"，
       而项目要的是"产品代码对真插件的产出判得对不对"。
       （实测：搓假链时 `plugin_reply_text` 会把链对象 `str()` 成
         `<tests...._Chain object at 0x…>`，看起来像产品 bug，其实是我桩没造好。）
    """
    from astrbot_host.compat.core.message.components import Image, Plain
    from astrbot_host.compat.core.message.message_event_result import MessageEventResult

    chain = MessageEventResult()
    chain.chain.append(Plain(text))
    if with_image:
        chain.chain.append(Image(url="https://example.invalid/x.png"))
    return _Res(
        emitted=[chain], stopped=True, executed=["probe.main_on_message"]
    )


class FakeHost:
    """假插件宿主：只实现 `dispatch_message`（派发路径的唯一入口）。

    🔴 **必须长得像真的 `PluginHostService`**：`app.py` 用
       `await host.dispatch_message(text, user_id=uid)` 调它，
       并把返回值交给 `plugin_host.plugin_reply_text()`。
       桩长得不像 → 测的是"我的桩"而不是产品代码。
    """

    def __init__(self, res=None, raises=None):
        self.res = res
        self.raises = raises
        self.calls: list[dict] = []

    async def dispatch_message(self, text, **kw):
        self.calls.append({"text": text, **kw})
        if self.raises is not None:
            raise self.raises
        return self.res


class FakeUpstream:
    """假上游后端（`ChatBackend` 形状）：**记录调用**，供"没有去调上游"判据。

    ⚠️ 非流式的 `chat()` 与流式的 `chat_stream()` 都要实现 ——
       只实现一个的话，"没调上游"这条断言在另一条路上永远为真（假绿）。
    """

    def __init__(self, content: str = UPSTREAM_TEXT):
        self.content = content
        self.calls: list[dict] = []

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def chat(self, messages, *, model="", **kw):  # noqa: ANN001
        self.calls.append({"kind": "chat", "messages": messages, "model": model})
        from emotion_app.domain.upstream import ChatResult

        return ChatResult(content=self.content, model=model or "m1", finish_reason="stop")

    async def chat_stream(self, messages, *, model="", **kw):  # noqa: ANN001
        self.calls.append({"kind": "stream", "messages": messages, "model": model})
        from emotion_app.domain.upstream import StreamChunk

        for piece in ("上游", "回复"):
            yield StreamChunk(delta=piece)
        yield StreamChunk(done=True)

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def make(tmp_path):
    """工厂：造一个**开了插件宿主**的 app client + 假件。

    用项目现成的夹具口径（`tests/conftest.py` 的 `mk` 同源：真 `Config`、
    真 `SqliteStore`、真 `create_app`），只多两件事：
      ① 注入假的上游后端（可控、可观察"有没有被调用"）；
      ② 注入假的 `EMOTION_PLUGIN_HOST` + 真 `SessionRepository`。
    """
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.session_repo import SessionRepository
    from emotion_app.infra.config import Config
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.web.app import create_app
    from emotion_app.web.auth import AuthManager

    opened: list[SqliteStore] = []

    async def _make(*, host=None, upstream=None, token: str = TOKEN):
        cfg = Config()
        cfg.auth.access_token = token
        cfg.storage.sqlite_path = str(tmp_path / "stream_dispatch.db")
        cfg.upstream.model = "m1"

        store = SqliteStore(cfg.storage.sqlite_path)
        await store.open()
        opened.append(store)

        sessions = SessionRepository(store)
        chat = ChatService(cfg, upstream or FakeUpstream())
        chat.bind_sessions(sessions)

        app = create_app(cfg, store, AuthManager(access_token=token), chat, sessions=sessions)
        # 🔴 插件的**数据目录**绝不碰项目 `data/`，更不碰 `/opt/astrbot`
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "data" / "profile.json")
        app.config["EMOTION_PROVIDERS_PATH"] = str(tmp_path / "data" / "providers.json")
        # ⚠️ 假宿主：`_plugin_first_reply_payload` 判"宿主在不在"就看这一项
        app.config["EMOTION_PLUGIN_HOST"] = host

        c = app.test_client()
        c.app = app                    # type: ignore[attr-defined]
        c.sessions = sessions          # type: ignore[attr-defined]
        await c.post("/api/v1/auth/login", json={"token": token})
        return c

    try:
        yield _make
    finally:
        for s in opened:
            try:
                await s.close()
            except Exception:  # noqa: BLE001 — 收尾失败不该掩盖真正的断言失败
                pass


def _body(text: str, *, stream: bool, session_id: str = "") -> dict:
    body: dict = {
        "model": "m1",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
    }
    if session_id:
        body["session_id"] = session_id
    return body


def _sse_deltas(raw: str) -> tuple[str, list[dict]]:
    """解析 SSE：返回（content 拼接结果, 所有 data 帧的 payload）。"""
    acc: list[str] = []
    frames: list[dict] = []
    for line in raw.splitlines():
        if not line.startswith("data: ") or "[DONE]" in line:
            continue
        payload = json.loads(line[6:])
        frames.append(payload)
        for ch in payload.get("choices") or []:
            acc.append(str((ch.get("delta") or {}).get("content") or ""))
    return "".join(acc), frames


async def _post(c, text, *, stream, session_id=""):
    return await c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=_body(text, stream=stream, session_id=session_id),
    )


# ===========================================================================
# 一、差分判据（**这条必须能区分"修了"与"没修"**）
# ===========================================================================


async def test_both_stream_modes_dispatch_to_plugin(make):
    """🔴🔴 **差分判据**：`stream: true` 与 `stream: false` **都要派发**，且 user_id 正确。

    唯一变量 = `stream` 开关 —— 其余（app、插件宿主、会话、消息）完全一致。
    所以"一条派发了、另一条没有"只能归因于流式分支漏了派发。

    ⚠️ 判据会把"用户真正走的那条路"（流式）算进来：
       `tools/proactive_multisession_check.py` 只发 `stream: false`，
       所以流式漏派发它一次都看不见 —— 这不是它写错了，是**覆盖面**问题。
    """
    host = FakeHost(_plugin_result())
    c = await make(host=host)
    session_id = "sess-diff"
    await c.sessions.create(session_id=session_id, title="差分会话", user_id="user_diff")

    # ⚠️ 两条路发**同一句话** —— 差分实验的前提是"唯一变量 = stream 开关"。
    #    发不同文本就分不清"差异来自哪里"了。
    text = "你好呀"
    seen: dict[str, list[dict]] = {}
    for label, stream in (("流式", True), ("非流式", False)):
        host.calls.clear()
        resp = await _post(c, text, stream=stream, session_id=session_id)
        assert resp.status_code == 200, f"{label}：HTTP {resp.status_code}"
        seen[label] = list(host.calls)

    # ① 两条路都必须派发**恰好一次**
    assert len(seen["流式"]) == 1, (
        f"🔴 流式聊天没有派发给插件（收到 {len(seen['流式'])} 次）—— "
        "面板永远走流式，这等于插件从没收到过用户的话"
    )
    assert len(seen["非流式"]) == 1, f"非流式派发次数不对：{seen['非流式']}"

    # ② 两条路都必须用**会话绑的** user_id（不是全局 default）
    for label, calls in seen.items():
        assert calls[0]["user_id"] == "user_diff", (
            f"{label}：派发用的 user_id 是 {calls[0]['user_id']!r}，"
            "应为会话绑的 'user_diff'（回落全局 = 多会话串台）"
        )
        assert calls[0]["text"] == text, (
            f"{label}：派发出去的用户文本是 {calls[0]['text']!r}，应为 {text!r}"
        )


async def test_stream_dispatch_uses_session_user_id_not_global(make):
    """会话没绑 user_id → 回落全局配置（**行为与以前一致**，不是串台）。"""
    host = FakeHost(_plugin_result())
    c = await make(host=host)
    # 不建会话：`session_id` 指向一个不存在的会话 → 取不到绑定
    await _post(c, "你好", stream=True, session_id="不存在")

    assert len(host.calls) == 1, f"流式没派发：{host.calls}"
    assert host.calls[0]["user_id"] == "", (
        "会话取不到绑定时应传**空串**，由宿主回落到全局 "
        f"`plugins.umo_user_id`；实际 {host.calls[0]['user_id']!r}"
    )


# ===========================================================================
# 二、插件接管：文本要真的进流、且**不调上游**
# ===========================================================================


async def test_stream_plugin_takeover_sends_plugin_text(make):
    """插件接管时：SSE 里**真的有那段文本**，形状与既有帧一致。"""
    host = FakeHost(_plugin_result())
    upstream = FakeUpstream()
    c = await make(host=host, upstream=upstream)

    resp = await _post(c, "记住我说的话", stream=True)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["Content-Type"], resp.headers
    # 响应头/结束语义不变（与非流式分支之外的那条流式路径一致）
    assert resp.headers.get("X-Accel-Buffering") == "no"

    raw = (await resp.get_data()).decode()
    text, frames = _sse_deltas(raw)

    assert text == PLUGIN_TEXT, f"SSE 里的正文不是插件那段回复：{text!r}"

    # 形状：第一个 chunk 带 role、正文单独一个 delta、最后 finish_reason=stop
    assert frames[0]["choices"][0]["delta"].get("role") == "assistant", frames[0]
    assert frames[0]["object"] == "chat.completion.chunk"
    contents = [
        (f["choices"][0]["delta"] or {}).get("content")
        for f in frames
        if f.get("choices") and (f["choices"][0]["delta"] or {}).get("content")
    ]
    assert contents == [PLUGIN_TEXT], f"正文 delta 形状不对：{contents}"
    reasons = [
        f["choices"][0]["finish_reason"]
        for f in frames
        if f.get("choices") and f["choices"][0].get("finish_reason")
    ]
    assert reasons == ["stop"], f"结束语义不对：{reasons}"
    assert raw.rstrip().endswith("data: [DONE]"), "没有以 [DONE] 收尾"


async def test_stream_plugin_takeover_does_not_call_upstream(make):
    """🔴🔴 插件接管时**不许再调上游** —— 否则用户收到两条互相矛盾的回复。

    ⚠️ 判据落在假上游的 `calls` 上（不是"看日志"）：流式走
       `chat_stream()`、非流式走 `chat()`，两个都要为空。
    """
    host = FakeHost(_plugin_result())
    upstream = FakeUpstream()
    c = await make(host=host, upstream=upstream)

    await _post(c, "画一张图", stream=True)
    assert upstream.calls == [], (
        f"插件已经接管，却又调了上游：{upstream.kinds()} —— "
        "用户会收到两条互相矛盾的回复"
    )


async def test_stream_non_text_content_is_logged_not_silently_dropped(make, caplog):
    """插件回复带图时：**不静默** —— 流式分支必须打一条说明"未透传附件"。"""
    import logging

    host = FakeHost(_plugin_result(with_image=True))
    c = await make(host=host)

    with caplog.at_level(logging.INFO, logger="emotion_app.web.app"):
        resp = await _post(c, "发张图", stream=True)
        await resp.get_data()

    # 文本照旧发出去（非文本段没有通道，但文本不能因此丢）
    text, _ = _sse_deltas((await resp.get_data()).decode())
    assert text == PLUGIN_TEXT, f"带附件时连文本都丢了：{text!r}"

    msgs = [r.getMessage() for r in caplog.records]
    assert any("未透传附件" in m for m in msgs), (
        "插件回复里的图片被**静默丢弃**了 —— 本项目最贵的那类 bug。"
        f"实际日志：{msgs}"
    )


# ===========================================================================
# 三、插件没接管：行为与以前**逐字一致**
# ===========================================================================


async def test_stream_plain_chat_falls_through_to_upstream(make):
    """普通聊天：仍要调上游，且 SSE 内容 = 上游的回复（形状不变）。"""
    host = FakeHost(_Res(emitted=[], stopped=False, executed=["on_message"]))
    upstream = FakeUpstream("上游回复")
    c = await make(host=host, upstream=upstream)

    resp = await _post(c, "你好呀", stream=True)
    text, frames = _sse_deltas((await resp.get_data()).decode())

    assert text == "上游回复", f"普通聊天的回复变了：{text!r}"
    assert upstream.kinds() == ["stream"], (
        f"普通聊天必须调上游（流式）；实际 {upstream.kinds()}"
    )
    assert len(host.calls) == 1, "普通聊天也要派发（插件可能只是没产出）"
    assert frames[0]["choices"][0]["delta"].get("role") == "assistant"


async def test_stream_dispatch_failure_does_not_break_chat(make):
    """插件派发抛异常 → **聊天照常**（但生产代码里必须记 ERROR，见 `_plugin_first_reply_payload`）。"""
    host = FakeHost(raises=RuntimeError("插件炸了"))
    upstream = FakeUpstream("上游回复")
    c = await make(host=host, upstream=upstream)

    resp = await _post(c, "你好呀", stream=True)
    text, _ = _sse_deltas((await resp.get_data()).decode())
    assert resp.status_code == 200
    assert text == "上游回复", f"插件炸了之后聊天没接上：{text!r}"


async def test_stream_without_plugin_host_is_unchanged(make):
    """没装插件宿主（`EMOTION_PLUGIN_HOST` 为 None）→ 流式行为与以前一致。"""
    upstream = FakeUpstream("上游回复")
    c = await make(host=None, upstream=upstream)

    resp = await _post(c, "你好呀", stream=True)
    text, _ = _sse_deltas((await resp.get_data()).decode())
    assert text == "上游回复"
    assert upstream.kinds() == ["stream"]


async def test_non_stream_plugin_takeover_still_works(make):
    """非流式那条路**没有被改坏**（它是本次修复的"参照物"）。"""
    host = FakeHost(_plugin_result())
    upstream = FakeUpstream()
    c = await make(host=host, upstream=upstream)

    resp = await _post(c, "记住", stream=False)
    body = await resp.get_json()
    assert body["choices"][0]["message"]["content"] == PLUGIN_TEXT
    assert body["choices"][0]["finish_reason"] == "stop"
    assert upstream.calls == [], "非流式接管时也不该调上游"


# ===========================================================================
# 四、bug B：LLM 钩子按会话取 uid
# ===========================================================================


class HookProbe:
    """把"钩子收到了哪个 umo"记下来（钩子拿到的 event 就是 umo 的载体）。"""

    def __init__(self) -> None:
        self.umos: list[str] = []

    async def __call__(self, messages, *, session_id=""):  # noqa: ANN001
        # 生产实现（`plugin_host.apply_llm_hooks`）就是从这里取 uid 拼 umo；
        # 所以判据落在"钩子被调用时能拿到 session_id"上。
        self.umos.append(session_id)
        return messages


async def _hook_service(tmp_path, sessions):
    """造一个真的 `PluginHostService`（未 start：只测钩子路径的 uid 解析）。"""
    from emotion_app.infra.config import Config
    from emotion_app.web.plugin_host import PluginHostService

    cfg = Config()
    cfg.plugins.umo_user_id = "default"
    cfg.plugins.data_dir = str(tmp_path / "plugin_data")
    svc = PluginHostService(cfg)
    svc.host = object()          # 非 None 即可（`apply_llm_hooks` 只判空）
    svc.sessions = sessions
    return svc


def _register_umo_probe(seen: list[str]):
    """往真注册表里挂一个 `OnLLMRequestEvent` 钩子，记录它看到的 umo。

    ⚠️ **必须用真注册表 + 真 `run_llm_request_hooks`**：
       这条判据要证明的是"宿主编出来的 umo 真的是会话的那个"，
       而不是"我调了一个我自己的函数"。
    """
    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.registry import EventType, StarHandlerMetadata, star_handlers_registry

    async def probe(event, req):  # noqa: ANN001
        seen.append(getattr(event, "unified_msg_origin", ""))

    star_handlers_registry.append(
        StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name="probe.main_umo_probe",
            handler_name="umo_probe",
            handler_module_path="probe.main",
            handler=probe,
        )
    )
    return star_handlers_registry


async def test_chat_service_passes_session_id_into_hook_both_paths():
    """🔴🔴 **接线判据**：`complete()` 与 `stream()` **两条路**都要把 `req.session_id`
    交给钩子。

    ⚠️ 为什么必须有这条（注入验证实测漏网）：钩子层单独测是绿的
       —— 但那是**直接调 `apply_llm_hooks(session_id=...)`**。
       把 `chat.py` 里 `self._apply_llm_hook(messages, req.session_id)`
       改回 `self._apply_llm_hook(messages)`，全文件 59 条判据**一条都不红**：
       因为"传没传下去"这件事只有**走 `ChatService` 的真实调用**才看得见。
       → 本项目通则：**「模块能工作 ≠ 被接上了」，接线必须有独立判据。**
    """
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.domain.message import Message
    from emotion_app.infra.config import Config

    got: list[str] = []

    async def hook(messages, *, session_id=""):
        got.append(session_id)
        return messages

    def _svc() -> ChatService:
        cfg = Config()
        cfg.upstream.model = "m1"
        cfg.upstream.base_url = "https://stub.invalid/v1"
        svc = ChatService(cfg, FakeUpstream())
        svc.bind_llm_hook(hook)
        return svc

    # ① 非流式
    await _svc().complete(
        ChatRequest(
            messages=[Message(role="user", content="你好")],
            session_id="sess-complete",
            persist=False,
        )
    )
    # ② 流式（必须**完整消费**生成器，否则尾部逻辑不跑）
    async for _chunk in _svc().stream(
        ChatRequest(
            messages=[Message(role="user", content="你好")],
            session_id="sess-stream",
            persist=False,
        )
    ):
        pass

    assert got == ["sess-complete", "sess-stream"], (
        f"钩子收到的 session_id 是 {got} —— 应为 ['sess-complete', 'sess-stream']。"
        "缺一个 = 那条路没把 req.session_id 传下去（钩子只能回落全局 default → 串台）"
    )


async def test_hook_uid_follows_session(tmp_path, make):
    """🔴🔴 **钩子按会话取 uid**：会话 A → `user_a`、会话 B → `user_b`。

    ⚠️ 这条就是 bug B 的判据：旧实现恒取全局 `plugins.umo_user_id`（`default`）
       → 两个会话的 umo 完全相同 → 必红。
    """
    from emotion_app.domain.message import Message
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.storage.sqlite_path = str(tmp_path / "hooks.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    try:
        from emotion_app.domain.session_repo import SessionRepository

        sessions = SessionRepository(store)
        await sessions.create(session_id="A", title="会话A", user_id="user_a")
        await sessions.create(session_id="B", title="会话B", user_id="user_b")

        svc = await _hook_service(tmp_path, sessions)
        seen: list[str] = []
        reg = _register_umo_probe(seen)
        try:
            msgs = [Message(role="user", content="你好")]
            await svc.apply_llm_hooks(msgs, session_id="A")
            await svc.apply_llm_hooks(msgs, session_id="B")
        finally:
            reg.clear()
    finally:
        await store.close()

    assert seen == [
        "11:FriendMessage:user_a",
        "11:FriendMessage:user_b",
    ], (
        f"钩子看到的 umo 不对：{seen} —— 应为各自会话绑的 user_id。"
        "若两条都是 11:FriendMessage:default，就是 bug B（按全局取 uid → 多会话串台）"
    )


async def test_hook_uid_falls_back_to_global_without_session(tmp_path):
    """没有会话绑定（空 session_id / 会话不存在）→ 回落全局 `umo_user_id`。"""
    from emotion_app.domain.message import Message
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.infra.config import Config

    cfg = Config()
    cfg.storage.sqlite_path = str(tmp_path / "hooks2.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    try:
        from emotion_app.domain.session_repo import SessionRepository

        svc = await _hook_service(tmp_path, SessionRepository(store))
        seen: list[str] = []
        reg = _register_umo_probe(seen)
        try:
            msgs = [Message(role="user", content="你好")]
            await svc.apply_llm_hooks(msgs)                      # 完全不传
            await svc.apply_llm_hooks(msgs, session_id="没有这个会话")
        finally:
            reg.clear()
    finally:
        await store.close()

    assert seen == ["11:FriendMessage:default"] * 2, (
        f"取不到会话绑定时应回落全局 config.plugins.umo_user_id；实际 {seen}"
    )


async def test_hook_uid_reads_sessions_from_service_not_config(tmp_path, make):
    """🔴 反向：**会话绑了就必须用会话的** —— 哪怕全局配的是别的值。

    与上一条配对：上一条证明"取不到会回落"，这条证明"取得到**不会**回落"。
    只测前者的话，一个恒回落全局的实现也能全绿。
    """
    from emotion_app.domain.message import Message
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.infra.config import Config
    from emotion_app.domain.session_repo import SessionRepository

    cfg = Config()
    cfg.storage.sqlite_path = str(tmp_path / "hooks3.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    try:
        sessions = SessionRepository(store)
        await sessions.create(session_id="C", title="会话C", user_id="user_c")
        svc = await _hook_service(tmp_path, sessions)
        svc.config.plugins.umo_user_id = "全局不该被用"      # 故意设成会露馅的值
        seen: list[str] = []
        reg = _register_umo_probe(seen)
        try:
            await svc.apply_llm_hooks(
                [Message(role="user", content="你好")], session_id="C"
            )
        finally:
            reg.clear()
    finally:
        await store.close()

    assert seen == ["11:FriendMessage:user_c"], (
        f"会话绑了 user_c，钩子却拿到 {seen} —— 说明回落了全局配置（串台）"
    )


# ===========================================================================
# 五、向后兼容：老式一参数钩子仍要能用
# ===========================================================================


async def test_legacy_one_arg_hook_still_works():
    """🔴 **老式 `async (messages)` 钩子不许炸**（向后兼容，硬要求）。

    ⚠️ 用 `inspect.signature` 判断能不能收 `session_id`，
       **不用宽 `except TypeError` 兜底** —— 那会把钩子内部自己抛的
       TypeError 也咽掉，把真 bug 换成"看起来合理"的签名问题。
    """
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.message import Message

    svc = ChatService.__new__(ChatService)      # 不跑 __init__（避免依赖 config）
    svc._llm_hook = None

    seen: list[str] = []

    async def legacy(messages):                 # ⚠️ 只有 1 个参数
        seen.append("legacy")
        return list(messages)

    svc.bind_llm_hook(legacy)
    msgs = [Message(role="user", content="你好")]
    out = await svc._apply_llm_hook(msgs, "some-session")

    assert seen == ["legacy"], "老式一参数钩子没被调用（或炸了）"
    assert out == msgs


async def test_new_two_arg_hook_receives_session_id():
    """新形状 `async (messages, *, session_id="")` 必须**真的收到**会话 id。"""
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.message import Message

    svc = ChatService.__new__(ChatService)
    svc._llm_hook = None

    got: list[str] = []

    async def hook(messages, *, session_id=""):
        got.append(session_id)
        return messages

    svc.bind_llm_hook(hook)
    await svc._apply_llm_hook([Message(role="user", content="你好")], "sess-42")
    assert got == ["sess-42"], f"新形状钩子收到的 session_id 是 {got}"


async def test_hook_with_kwargs_accepts_session_id():
    """`**kwargs` 形状也收得下 `session_id`（插件/第三方可能这么写）。"""
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.message import Message

    svc = ChatService.__new__(ChatService)
    svc._llm_hook = None

    got: list[str] = []

    async def hook(messages, **kw):
        got.append(kw.get("session_id", "<缺失>"))
        return messages

    svc.bind_llm_hook(hook)
    await svc._apply_llm_hook([Message(role="user", content="你好")], "s7")
    assert got == ["s7"], f"`**kwargs` 形状没收到 session_id：{got}"


async def test_hook_internal_type_error_is_not_mistaken_for_signature_mismatch():
    """🔴 **反向判据**：钩子内部抛的 `TypeError` 不能被当成"签名不匹配"。

    旧写法（`try: hook(messages, session_id=...)  except TypeError: hook(messages)`）
    会静默重试 + 吞掉真错 —— 本项目踩过同类坑（宽 `except` 让错误信息离根因三层）。
    现在按签名判定：这里应当**原样返回**（照 `_apply_llm_hook` 的兜底契约），
    且**不许**再拿老签名重试一次。
    """
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.message import Message

    svc = ChatService.__new__(ChatService)
    svc._llm_hook = None
    calls: list[str] = []

    async def buggy(messages, *, session_id=""):
        calls.append(session_id)
        raise TypeError("我自己写错了")

    svc.bind_llm_hook(buggy)
    msgs = [Message(role="user", content="你好")]
    out = await svc._apply_llm_hook(msgs, "s8")

    assert out is msgs, "钩子抛错时应原样返回（聊天不受影响）"
    assert calls == ["s8"], (
        f"钩子被调用了 {len(calls)} 次（{calls}）—— "
        "说明代码在拿老签名重试，那会把真 TypeError 咽掉"
    )


# ===========================================================================
# 六、接线：两条路的判据必须是**同一份**
# ===========================================================================
#
# ⚠️ 下面几条是**源码级**判据（AST，不是字符串搜索）。为什么必须用 AST：
#    注释与 docstring 里也写着这些函数名（本文件就在说明"为什么"），
#    纯字符串搜索会把**注释**当成代码 → 判据看着绿、其实什么都没守
#    （本项目踩过同类坑：否定式断言的字面串与产品文案不一致 → 永远绿）。


def _func_src(path: Path, name: str) -> str:
    """取某个函数的**源码片段**（含嵌套函数）。找不到就炸。"""
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{path.name} 里找不到函数 {name}()")


def _called_names(path: Path, func_name: str) -> list[str]:
    """列出该函数体里**被调用**的名字（按出现顺序，含嵌套函数内部）。"""
    import ast

    src = path.read_text(encoding="utf-8")
    body_src = _func_src(path, func_name)
    out: list[str] = []
    for node in ast.walk(ast.parse(body_src)):
        if isinstance(node, ast.Call):
            fn = node.func
            out.append(getattr(fn, "id", None) or getattr(fn, "attr", None) or "")
    return [n for n in out if n]


def test_session_uid_rule_lives_in_one_place():
    """🔴 **同一概念只许一份实现**：`app.py` 不自己判定会话 user_id。

    bug B 的根源就是"派发按会话、钩子按全局"两条路径各写一套。
    这条判据守着"以后别再分叉"。
    """
    app_py = APP_DIR / "emotion_app" / "web" / "app.py"
    host_py = APP_DIR / "emotion_app" / "web" / "plugin_host.py"

    app_src = app_py.read_text(encoding="utf-8")
    assert "from emotion_app.web.plugin_host import session_user_id" in app_src, (
        "app.py 没有复用 plugin_host 的会话 user_id 判据 —— 两份实现会漂移"
    )

    # `app._session_user_id` 只做转发：函数体里只有对 session_user_id 的调用，
    # 不许出现 `sessions.get(...)`（那就是复刻了一份判定）。
    body = _func_src(app_py, "_session_user_id")
    assert "session_user_id(" in body, f"`_session_user_id` 没有转发：{body}"
    assert ".get(" not in body, (
        f"`app.py` 的 `_session_user_id` 里又自己读了会话 —— 判据应只有一处：{body}"
    )

    # 钩子路径必须用**同一个** resolve_user_id，且不许再直读全局 umo_user_id。
    hook_body = _func_src(host_py, "apply_llm_hooks")
    hook_code = "\n".join(
        line for line in hook_body.splitlines()
        if not line.strip().startswith("#")
    )
    assert "resolve_user_id(" in hook_code, (
        "`apply_llm_hooks` 没有用 `resolve_user_id()` —— 钩子又在自己拼 uid"
    )
    # ⚠️ 用 AST 取"表达式里出现的属性名"而不是搜字符串：
    #    docstring 里就写着 `plugins.umo_user_id`（解释这个 bug），
    #    字符串搜索会永远为真 —— 那正是"假判据"的形态。
    import ast

    attrs: list[str] = []
    for node in ast.walk(ast.parse(hook_body)):
        if isinstance(node, ast.Attribute):
            attrs.append(node.attr)
    assert "umo_user_id" not in attrs, (
        f"`apply_llm_hooks` 里还在读 `.umo_user_id` —— 那就是 bug B 的写法（{attrs}）"
    )


def test_stream_branch_calls_dispatch_before_upstream():
    """🔴 源码级判据：流式分支里**派发必须在调上游之前**（且两条路都派发）。

    行为判据（第一条差分判据）已经覆盖了"有没有派发"；这条额外守
    **顺序** —— 派发写在 `svc.stream(req)` 之后就等于没修
    （插件接管时用户照样先收到上游那条，两条矛盾回复）。

    ⚠️ 派发**不在 `sse()` 生成器里**（实测：生成器一开跑 Quart 的 app 上下文
       就没了，`jsonify`/`app.config` 都会抛 `RuntimeError: Not within an app
       context`）。所以判据落在**流式分支整体**上：从 `if req.stream` 之后
       到 `sse()` 定义之前，必须已经算好 `plugin_content`；
       而 `svc.stream(req)` 只出现在 `sse()` 里。

    ⚠️ 全程用 **AST 的行号**比较，不做字符串 `index`：注释里也写着这些名字
       （本文件/`app.py` 的注释就在解释"为什么"），
       字符串搜索会把注释当代码 —— 那是"假判据"的典型形态。
    """
    import ast

    app_py = APP_DIR / "emotion_app" / "web" / "app.py"
    route = _func_src(app_py, "chat_completions")
    tree = ast.parse(route)          # `route` 是函数定义，本身可解析

    def _calls(name: str) -> list[ast.Call]:
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == name:
                out.append(node)
        return out

    # ① 两条路都要派发（非流式 + 流式各一次）
    dispatched = _calls("_plugin_first_reply_payload")
    assert len(dispatched) == 2, (
        f"`_plugin_first_reply_payload` 应被调 **2** 次（非流式 + 流式），"
        f"实际 {len(dispatched)} 次 —— 只调 1 次说明流式那条路没派发（bug A）"
    )

    # ② `sse()` 的定义位置 = "流式分支的分界线"
    sse_defs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "sse"
    ]
    assert len(sse_defs) == 1, "`sse()` 生成器不见了"
    sse_at = sse_defs[0].lineno

    # ③ 流式那条派发在 `sse()` **之前**（= 响应返回之前、调上游之前）
    stream_dispatch = max(c.lineno for c in dispatched)
    assert stream_dispatch < sse_at, (
        f"流式分支的派发在第 {stream_dispatch} 行，而 `sse()` 在第 {sse_at} 行 —— "
        "派发写进生成器里了（生成器没有 Quart 上下文，实测会 RuntimeError）"
    )

    # ④ 上游只许在 `sse()` 里调（生成器之外调 = 响应没返回就先把上游调了）
    upstream = [c for c in _calls("stream")]
    assert upstream, "`sse()` 里没有调上游 —— 普通聊天就没回复了"
    assert all(c.lineno > sse_at for c in upstream), (
        f"`svc.stream(...)` 在 `sse()` 之外被调用"
        f"（行号 {[c.lineno for c in upstream]}，`sse()` 在 {sse_at}）"
    )

    # ⑤ 真·顺序：派发早于上游
    assert stream_dispatch < min(c.lineno for c in upstream), (
        "派发写在调上游之后了 —— 插件接管时会先给用户一条上游回复，"
        "然后又是一条插件的，两条互相矛盾"
    )


# ===========================================================================
# 七、真 `PluginHostService.dispatch_message` 的**内部逻辑**（不是假宿主）
# ===========================================================================
#
# 🔴 为什么单开这一节（2026-09-26 真环境实测漏网的真 bug）：
#    上面所有判据都把 `dispatch_message` **整个换成了 `FakeHost`** ——
#    于是"接线对"证明得了，**"真方法内部算对不对"证明不了**。
#    实测就是这么漏掉一个 `UnboundLocalError` 的：
#
#        try:
#            uid = uid or fallback_user_id(self.config)   # ← 参数其实叫 user_id
#        except Exception:                                 # ← 宽 except 把它接住
#            logger.error("插件事件派发失败（本条消息按【无插件】处理）")
#            return None                                   # ← 返回 None = "插件没说话"
#
#    后果：**整条派发静默失效**（用户说的话插件永远收不到），
#    而 19 条判据全绿、HTTP 照样 200、面板照样能聊天。
#    这正是本项目铁律说的两件事同时发生：
#      ①「宽 except 会把真因换掉」——报错变成一句听起来合理的"派发失败"；
#      ②「模块能工作 ≠ 被接上了」+「单测全绿不算证据」。
#    ⚠️ 而**真机判据**（`test_plugin_chat_integration.py` 那条）能抓到它 ——
#       因为它走的是真 `PluginHostService`。这条是它的**快速版**：
#       不需要加载 11 个真插件，用假 `astrbot_host.dispatch` 就能守内部逻辑。


class _RecordingDispatch:
    """替换 `astrbot_host.dispatch.dispatch`：记下**它收到的 event**。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event):  # noqa: ANN001
        self.events.append(event)
        from astrbot_host.dispatch import DispatchResult

        return DispatchResult()


@pytest.fixture
def fake_dispatch(monkeypatch):
    """把 `astrbot_host.dispatch.dispatch` 换成记录器（**保留真方法的其余逻辑**）。

    ⚠️ 只替换**最外那一步**（真派发要跑 11 个插件，太慢）；
       `dispatch_message` 自己的参数解析/uid 解析/平台校验**全部是真代码** ——
       要被测的正是它们。
    """
    sys.path.insert(0, str(APP_DIR / "host"))
    import astrbot_host.dispatch as dispatch_mod

    rec = _RecordingDispatch()
    monkeypatch.setattr(dispatch_mod, "dispatch", rec, raising=True)
    return rec


def _service_with_user(config_umo: str = "default"):
    """造一个真的 `PluginHostService`（`host` 非 None、无会话仓库）。"""
    from emotion_app.infra.config import Config
    from emotion_app.web.plugin_host import PluginHostService

    cfg = Config()
    cfg.plugins.umo_user_id = config_umo
    svc = PluginHostService(cfg)
    svc.host = object()        # 非 None 即可（`dispatch_message` 只判空）
    return svc


async def test_dispatch_message_resolves_uid_without_exploding(fake_dispatch):
    """🔴🔴 **真 `dispatch_message` 不许炸** —— 这是漏网那个 `UnboundLocalError` 的判据。

    ⚠️ 用**真方法**（只把最外层的 `dispatch` 换成记录器）。
       断言落在**事件里的 umo 第三段**上 —— 那是 `uid` 解析的**唯一可观察结果**：
       `UnboundLocalError` 会让整条路返回 None、事件根本不会被派发。

    🔴 这条判据在"`uid = uid or …`"那种写法下**必红**（本地验证过：
       把 `uid = user_id or …` 改回 `uid = uid or …` → 本判据挂）。
    """
    svc = _service_with_user("default")
    res = await svc.dispatch_message("你好呀")          # 不传 user_id → 走回落

    assert res is not None, (
        "🔴 `dispatch_message` 返回了 None = 整条派发静默失效。"
        "真实现里这通常是**内部抛异常被宽 except 接住**了 —— "
        "症状是「聊天照常、插件永远收不到用户的话」（本项目最难查的那类）"
    )
    assert len(fake_dispatch.events) == 1, "事件没被派发出去"
    umo = fake_dispatch.events[0].unified_msg_origin
    assert umo == "11:FriendMessage:default", (
        f"umo 不对：{umo!r} —— uid 的回落没生效（`uid` 可能没被赋值）"
    )


async def test_dispatch_message_uses_given_user_id(fake_dispatch):
    """传了 `user_id` → umo 第三段就是它（**不许**被回落覆盖）。"""
    svc = _service_with_user("全局不该用")
    res = await svc.dispatch_message("你好呀", user_id="explicit_uid")

    assert res is not None, "`dispatch_message` 返回 None（内部炸了）"
    umo = fake_dispatch.events[0].unified_msg_origin
    assert umo == "11:FriendMessage:explicit_uid", f"umo 不对：{umo!r}"


async def test_dispatch_message_empty_user_id_falls_back(fake_dispatch):
    """空串 `user_id`（会话没绑）→ 用全局 `plugins.umo_user_id`（**逐字兼容**）。"""
    svc = _service_with_user("3412642082")
    await svc.dispatch_message("你好呀", user_id="")

    umo = fake_dispatch.events[0].unified_msg_origin
    assert umo == "11:FriendMessage:3412642082", f"空串没回落到全局配置：{umo!r}"


async def test_dispatch_message_host_absent_returns_none(fake_dispatch):
    """反向：宿主没起来 → **如实返回 None**（不假装派发过）。"""
    svc = _service_with_user()
    svc.host = None
    assert await svc.dispatch_message("你好呀") is None
    assert fake_dispatch.events == [], "宿主没起来却把事件派出去了"


async def test_dispatch_message_reports_handler_errors_loudly(monkeypatch, caplog):
    """插件 handler 抛错 → **记 WARNING**（不吞、不静默）。

    ⚠️ 用**独立**的注入（不复用 `fake_dispatch` 夹具）：这里要让 `dispatch`
       返回一个**带 errors** 的结果，而不是记录调用。
    """
    import logging

    sys.path.insert(0, str(APP_DIR / "host"))
    import astrbot_host.dispatch as dispatch_mod
    from astrbot_host.dispatch import DispatchResult

    async def with_errors(event):  # noqa: ANN001
        r = DispatchResult()
        r.executed.append("probe.main_on_message")
        r.errors.append(("probe.main_on_message", "RuntimeError: 插件炸了"))
        return r

    monkeypatch.setattr(dispatch_mod, "dispatch", with_errors, raising=True)

    svc = _service_with_user()
    with caplog.at_level(logging.WARNING, logger="emotion_app.plugins"):
        res = await svc.dispatch_message("你好呀")

    assert res is not None and res.errors, "错误没被带回来"
    assert any("抛错" in r.getMessage() for r in caplog.records), (
        f"handler 抛错没留 WARNING：{[r.getMessage() for r in caplog.records]}"
    )
