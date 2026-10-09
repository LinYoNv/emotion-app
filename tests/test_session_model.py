# -*- coding: utf-8 -*-
"""**按会话选模型**（情绪 2026-10-09 要的能力）的判据。

## 需求原文
> 「在这个设置页-会话中可以选择为不同的会话修改它的对话模型。
>   假设糯米茨的对话模型为 a，切换到陪伴会话设对话模型为 b。
>   就是允许用户自行选择为会话选择对话模型。」
> 「全局的对话模型删就是了。插件用哪个模型让用户自己在插件页面改。」

## 这条链路上最容易出的 bug —— 都是"静默"型的
本项目的经验：判定"有没有生效"不能看**接口返回**，要看**真正发出去的那个请求**。
所以下面每条成功判据都要落到"**上游实际收到了哪个 base_url / key / model**"。

  ① 🔴 **流式路径**：面板**永远走流式**。只在 `complete()`（非流式）里读
     `session.model` 的话，功能**整个不生效**，而且不报错 ——
     这正是第五轮踩过的同型事故（流式没有派发给插件）。
  ② 🔴 **读会话的时机**：`resolve_target` 必须在 `prepare_session` **之后**调，
     否则 `session` 还没取到，读到的是空串。
  ③ 🔴 **选了却用不了时必须报错**，不能静默回落 ——
     否则回复是另一个模型生成的，而界面写着用户选的那个。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from emotion_app.domain.chat import ChatService
from emotion_app.domain.session_repo import SessionRepository
from emotion_app.infra.config import Config
from emotion_app.infra.providers_store import ProviderStore
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 16


class _Recorder:
    """假上游：**把每次收到的请求记下来**（判据要看的就是这个）。

    ⚠️ 签名必须照**真实 `ChatBackend` 协议**：`chat(messages, *, model, base_url, ...)`
       —— 目标不是以 `target` 对象传进来的，而是拆成 `base_url`/`api_key`/`model`
       三个关键字（`chat.py` 里 `self._client.chat(..., model=..., base_url=...)`）。
       我第一版按 `chat(target, messages)` 写，直接 `TypeError`
       —— 判据要先跑通，才谈得上守东西。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _note(self, model: str, base_url: str, api_key: str) -> None:
        self.calls.append(
            {
                # ⚠️ 归一成"最终请求会打到的完整 URL"，判据直接比它更贴真实
                "url": f"{base_url.rstrip('/')}/chat/completions",
                "auth": f"Bearer {api_key}" if api_key else "",
                "model": model,
            }
        )

    async def chat(self, messages, *, model="", base_url="", api_key="", **kw):  # noqa: ANN001
        from emotion_app.domain.upstream import ChatResult

        self._note(model, base_url, api_key)
        return ChatResult(content="在呢", model=model, finish_reason="stop")

    async def chat_stream(self, messages, *, model="", base_url="", api_key="", **kw):  # noqa: ANN001
        from emotion_app.domain.upstream import StreamChunk

        self._note(model, base_url, api_key)
        yield StreamChunk(delta="在")
        yield StreamChunk(delta="呢")
        yield StreamChunk(done=True)


def _write_providers(path: Path, *, models: list[tuple[str, str, str]], key: str = "sk-A") -> None:
    """造一份 `providers.json`（两层结构：源 + 模型）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "default_provider_id": "",
                "provider_sources": [
                    {"id": "srcA", "api_base": "https://a.test/v1", "key": key,
                     "provider_type": "chat_completion"},
                    {"id": "srcB", "api_base": "https://b.test/v1", "key": "sk-B",
                     "provider_type": "chat_completion"},
                ],
                "providers": [
                    {"id": mid, "model": mname, "provider_source_id": src,
                     "provider_type": "chat_completion"}
                    for mid, mname, src in models
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
async def make(tmp_path):
    """造 app + 假上游，并**用真的 `providers.json` 走真解析链**。"""
    opened: list[SqliteStore] = []

    async def _make(*, providers: list[tuple[str, str, str]] | None = None):
        cfg = Config()
        cfg.auth.access_token = TOKEN
        cfg.storage.sqlite_path = str(tmp_path / "sess_model.db")
        cfg.upstream.base_url = "https://env-fallback.test/v1"
        cfg.upstream.api_key = "sk-env"
        cfg.upstream.model = "env-model"

        store = SqliteStore(cfg.storage.sqlite_path)
        await store.open()
        opened.append(store)
        sessions = SessionRepository(store)

        rec = _Recorder()
        chat = ChatService(cfg, rec)
        chat.bind_sessions(sessions)

        app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat, sessions=sessions)
        ppath = tmp_path / "data" / "providers.json"
        app.config["EMOTION_PROVIDERS_PATH"] = str(ppath)
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "data" / "profile.json")
        if providers:
            _write_providers(ppath, models=providers)

        c = app.test_client()
        c.app = app                    # type: ignore[attr-defined]
        c.sessions = sessions          # type: ignore[attr-defined]
        c.rec = rec                    # type: ignore[attr-defined]
        await c.post("/api/v1/auth/login", json={"token": TOKEN})
        return c

    try:
        yield _make
    finally:
        for s in opened:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# ① 会话级字段：能存下来、能读回来
# ===========================================================================


async def test_session_model_persists_roundtrip(make):
    """`model` 字段要能存进会话并**跨一次重新读取**还在（不是只活在内存里）。"""
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="测试会话")
    await c.sessions.update(s.id, {"model": "srcA/model-a"})

    # 🔴 重新打开存储再读 —— 只查内存会漏掉"没落盘"这类 bug
    fresh = SessionRepository(c.sessions._store)  # noqa: SLF001
    again = await fresh.get(s.id)
    assert again is not None
    assert again.model == "srcA/model-a", f"model 没落盘：{again.model!r}"


async def test_legacy_session_without_model_defaults_to_empty(make):
    """⚠️ **旧会话**（JSON 里没有 `model` 键）→ 空串，**不是**某个默认模型。

    理由：空串 = "没选"，会让聊天走老路径（向后兼容）；
      若给个非空默认值，那些会话会突然被指到一个**用户从没选过**的模型上。
    """
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="老会话")
    raw = await c.sessions._store.load_store()  # noqa: SLF001
    raw["sessions"][s.id].pop("model", None)     # 模拟旧数据
    await c.sessions._store.save_sections({"sessions": raw["sessions"]})  # noqa: SLF001

    again = await SessionRepository(c.sessions._store).get(s.id)  # noqa: SLF001
    assert again is not None
    assert again.model == ""


# ===========================================================================
# ② 🔴 真正发出去的请求用哪个模型 —— 非流式
# ===========================================================================


async def test_nonstream_uses_session_model(make):
    """会话选了模型 → 上游收到的必须是**那个源的地址、那把 key、那个模型名**。

    ⚠️ 判据落到 `url` / `auth` / `model` **三样**，不能只看 model 名：
       如果只换名字而地址/key 还是环境变量那套，请求会打到**错的端点**上
       （模型名对上、端点不对 = 401 或 model_not_found，很难查）。
    """
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="A 会话")
    await c.sessions.update(s.id, {"model": "srcA/model-a"})

    r = await c.post(
        "/v1/chat/completions",
        json={"model": "", "session_id": s.id,
              "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    assert c.rec.calls, "上游根本没被调用"
    call = c.rec.calls[-1]
    assert call["url"] == "https://a.test/v1/chat/completions", call["url"]
    assert call["auth"] == "Bearer sk-A", call["auth"]
    assert call["model"] == "model-a", call["model"]


async def test_stream_uses_session_model(make):
    """🔴 **流式路径同样要用会话模型** —— 面板永远走流式，漏了这里功能等于没做。

    ⚠️ 这条是本文件的**核心判据**：第五轮踩过同型事故
       （流式聊天没有派发给插件 ⇒ 用户说的每句话插件都没收到过，而不报错）。
    """
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="A 会话")
    await c.sessions.update(s.id, {"model": "srcA/model-a"})

    r = await c.post(
        "/v1/chat/completions",
        json={"model": "", "session_id": s.id,
              "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    await r.get_data()          # 必须**把流读完**，否则生成器不跑、上游永远不会被调用
    assert c.rec.calls, "流式路径没有调用上游"
    call = c.rec.calls[-1]
    assert call["url"] == "https://a.test/v1/chat/completions", call["url"]
    assert call["model"] == "model-a", call["model"]


async def test_two_sessions_use_their_own_models(make):
    """🔴 **需求原文的场景**：两条会话各用各的模型，互不串。

    「糯米茨的对话模型为 a，陪伴会话设对话模型为 b」——就是这条。
    """
    c = await make(providers=[
        ("srcA/model-a", "model-a", "srcA"),
        ("srcB/model-b", "model-b", "srcB"),
    ])
    s1 = await c.sessions.create(title="糯米茨")
    s2 = await c.sessions.create(title="陪伴")
    await c.sessions.update(s1.id, {"model": "srcA/model-a"})
    await c.sessions.update(s2.id, {"model": "srcB/model-b"})

    for sid, want_url, want_model in (
        (s1.id, "https://a.test/v1/chat/completions", "model-a"),
        (s2.id, "https://b.test/v1/chat/completions", "model-b"),
    ):
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "", "session_id": sid,
                  "messages": [{"role": "user", "content": "hi"}], "stream": False},
        )
        assert r.status_code == 200, await r.get_data(as_text=True)

    got = [(x["url"], x["model"]) for x in c.rec.calls]
    assert got == [
        ("https://a.test/v1/chat/completions", "model-a"),
        ("https://b.test/v1/chat/completions", "model-b"),
    ], got


# ===========================================================================
# ③ 没选 / 选了却用不了
# ===========================================================================


async def test_session_without_model_falls_back_to_env(make):
    """没给会话选模型 → 走老路径（环境变量），**保持向后兼容**。"""
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="没选模型的会话")
    r = await c.post(
        "/v1/chat/completions",
        json={"model": "", "session_id": s.id,
              "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    call = c.rec.calls[-1]
    assert call["url"] == "https://env-fallback.test/v1/chat/completions", call["url"]
    assert call["model"] == "env-model", call["model"]


async def test_deleted_model_is_rejected_loudly(make):
    """🔴 会话选了模型，但那个模型已被删除 → **必须报错**，不能回落。

    ⚠️ 这条是"不静默降级"的落点：如果悄悄回落到环境变量那个模型，
       用户看到的是**另一个模型**生成的回复，而界面上写着他选的那个 ——
       他无从察觉（本项目最贵的 bug 类型）。
    """
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="A 会话")
    await c.sessions.update(s.id, {"model": "srcA/model-a"})
    # 模型被删了
    _write_providers(Path(c.app.config["EMOTION_PROVIDERS_PATH"]), models=[])

    r = await c.post(
        "/v1/chat/completions",
        json={"model": "", "session_id": s.id,
              "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code >= 400, f"模型没了却成功了（{r.status_code}）—— 静默降级"
    body = await r.get_data(as_text=True)
    assert "srcA/model-a" in body, f"报错里没说是哪个模型：{body[:200]}"
    assert c.rec.calls == [], "模型不可用却还是打了上游"


async def test_request_level_model_override_still_wins(make):
    """请求里显式带 `model` → 临时指定优先（但端点仍用会话那套）。

    ⚠️ 这是刻意的：面板/插件可以在**不改变会话设置**的前提下试一个模型。
    """
    c = await make(providers=[("srcA/model-a", "model-a", "srcA")])
    s = await c.sessions.create(title="A 会话")
    await c.sessions.update(s.id, {"model": "srcA/model-a"})

    r = await c.post(
        "/v1/chat/completions",
        json={"model": "临时模型", "session_id": s.id,
              "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 200, await r.get_data(as_text=True)
    call = c.rec.calls[-1]
    assert call["model"] == "临时模型", call["model"]
    # 端点仍来自会话选的那个模型所属的源
    assert call["url"] == "https://a.test/v1/chat/completions", call["url"]
