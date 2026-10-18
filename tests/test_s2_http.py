# -*- coding: utf-8 -*-
"""S2 HTTP 测试：人设 CRUD · 设置 · 提示词预览 · 面板。"""
from __future__ import annotations

import httpx
import pytest

from emotion_app.domain.chat import ChatService
from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.domain.settings import SettingsService
from emotion_app.domain.backends import OpenAIChatBackend
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 20


def _ok(content: str = "收到") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "m1",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    })


@pytest.fixture
async def ctx(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "s2http.db")
    cfg.upstream.model = "env-model"
    cfg.upstream.api_key = "sk-env"

    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    settings = SettingsService(store, cfg)
    await settings.load()
    personas = PersonaRepository(store)
    upstream = OpenAIChatBackend(transport=httpx.MockTransport(lambda r: _ok()))
    await upstream.open()
    chat = ChatService(cfg, upstream, settings=settings, personas=personas)

    app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat,
                     personas=personas, settings=settings)
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    yield client, store
    await store.close()


# ===========================================================================
# 鉴权
# ===========================================================================


async def test_admin_requires_auth(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "noauth.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    c = app.test_client()

    for method, path in (("get", "/api/v1/personas"), ("get", "/api/v1/settings")):
        r = await getattr(c, method)(path)
        assert r.status_code == 401, path
    await store.close()


async def test_access_token_works_on_admin_api(ctx):
    """⭐ 回归：访问令牌作为 Bearer **也能**调管理接口。

    实测踩到：最初 `/v1/*` 认 Bearer、`/api/v1/*` 只认 cookie
    → 用令牌直接调管理接口 401，而 App 端配置正需要这个用法。
    """
    client, _ = ctx
    await client.post("/api/v1/auth/logout")     # 清掉会话 cookie

    for path in ("/api/v1/personas", "/api/v1/settings"):
        r = await client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200, path


async def test_arbitrary_bearer_rejected_on_admin_api(ctx):
    """🔴 反面：随便的 Bearer 不能进管理接口。"""
    client, _ = ctx
    await client.post("/api/v1/auth/logout")
    r = await client.get("/api/v1/personas", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ===========================================================================
# 人设 CRUD
# ===========================================================================


async def test_persona_crud_flow(ctx):
    client, _ = ctx

    # 建
    r = await client.post("/api/v1/personas", json={"name": "小爱", "description": "描述"})
    assert r.status_code == 201
    p = await r.get_json()
    pid = p["id"]
    assert p["name"] == "小爱"

    # 列
    r = await client.get("/api/v1/personas")
    body = await r.get_json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1

    # 取
    r = await client.get(f"/api/v1/personas/{pid}")
    assert (await r.get_json())["name"] == "小爱"

    # 改
    r = await client.patch(f"/api/v1/personas/{pid}", json={"personality": "温柔"})
    assert r.status_code == 200
    assert (await r.get_json())["personality"] == "温柔"

    # 删
    r = await client.delete(f"/api/v1/personas/{pid}")
    assert r.status_code == 200
    assert (await (await client.get("/api/v1/personas")).get_json())["data"] == []


async def test_create_persona_requires_name(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"description": "没有名字"})
    assert r.status_code == 400
    assert (await r.get_json())["error"]["type"] == "invalid_request_error"


async def test_create_persona_rejects_bad_body(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json="not an object")
    assert r.status_code == 400


async def test_get_missing_persona_404(ctx):
    client, _ = ctx
    r = await client.get("/api/v1/personas/nope")
    assert r.status_code == 404
    assert (await r.get_json())["error"]["type"] == "not_found"


async def test_patch_missing_persona_404(ctx):
    client, _ = ctx
    r = await client.patch("/api/v1/personas/nope", json={"name": "x"})
    assert r.status_code == 404


async def test_duplicate_persona(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "原版", "model": "m"})
    pid = (await r.get_json())["id"]

    r = await client.post(f"/api/v1/personas/{pid}/duplicate", json={})
    assert r.status_code == 201
    clone = await r.get_json()
    assert clone["id"] != pid
    assert clone["model"] == "m"
    assert len((await (await client.get("/api/v1/personas")).get_json())["data"]) == 2


async def test_persona_api_key_not_leaked(ctx):
    """🔴 人设的 key 不出现在任何 API 响应里。"""
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x", "api_key": "sk-persona-secret"})
    pid = (await r.get_json())["id"]

    for path in ("/api/v1/personas", f"/api/v1/personas/{pid}", f"/api/v1/personas/{pid}/preview"):
        text = await (await client.get(path)).get_data(as_text=True)
        assert "sk-persona-secret" not in text, path

    p = await (await client.get(f"/api/v1/personas/{pid}")).get_json()
    assert p["api_key_configured"] is True


# ===========================================================================
# 激活 / 取消
# ===========================================================================


async def test_activate_and_deactivate(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "小爱"})
    pid = (await r.get_json())["id"]

    r = await client.post(f"/api/v1/personas/{pid}/activate")
    assert r.status_code == 200

    body = await (await client.get("/api/v1/personas")).get_json()
    assert body["active_persona_id"] == pid

    r = await client.post("/api/v1/personas/deactivate")
    assert r.status_code == 200
    body = await (await client.get("/api/v1/personas")).get_json()
    assert body["active_persona_id"] == ""


async def test_activate_missing_404(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas/nope/activate")
    assert r.status_code == 404


async def test_delete_active_persona_clears_selection(ctx):
    """删掉当前人设 → 选择要被清空，不留悬空引用。"""
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x"})
    pid = (await r.get_json())["id"]
    await client.post(f"/api/v1/personas/{pid}/activate")

    await client.delete(f"/api/v1/personas/{pid}")
    body = await (await client.get("/api/v1/personas")).get_json()
    assert body["active_persona_id"] == ""


# ===========================================================================
# 提示词片段 + 预览（⭐ 情绪 A3）
# ===========================================================================


async def test_replace_sections(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x"})
    pid = (await r.get_json())["id"]

    r = await client.put(f"/api/v1/personas/{pid}/sections", json=[
        {"title": "说话风格", "content": "简洁", "order": 30, "source": "user"},
        {"title": "禁忌", "content": "别提某事", "order": 40, "source": "persona", "enabled": False},
    ])
    assert r.status_code == 200
    p = await r.get_json()
    assert len(p["prompt_sections"]) == 2

    # 再取一次，确认落盘了
    p2 = await (await client.get(f"/api/v1/personas/{pid}")).get_json()
    assert p2["prompt_sections"][0]["title"] == "说话风格"


async def test_replace_sections_requires_array(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x"})
    pid = (await r.get_json())["id"]
    r = await client.put(f"/api/v1/personas/{pid}/sections", json={"not": "array"})
    assert r.status_code == 400


async def test_replace_sections_rejects_bad_source(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x"})
    pid = (await r.get_json())["id"]
    r = await client.put(f"/api/v1/personas/{pid}/sections",
                         json=[{"title": "t", "source": "hacker"}])
    assert r.status_code == 400


async def test_preview_shows_assembled_prompt(ctx):
    """⭐ 预览必须体现"人设 → 提示词"的组装结果（情绪 A3 的核心）。"""
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={
        "name": "小爱", "description": "我是描述", "personality": "温柔",
    })
    pid = (await r.get_json())["id"]

    r = await client.get(f"/api/v1/personas/{pid}/preview")
    assert r.status_code == 200
    d = await r.get_json()

    assert "【角色设定】" in d["text"]
    assert "我是描述" in d["text"]
    assert "【性格】" in d["text"]
    assert d["describe"]["section_count"] >= 2
    assert any(s["title"] == "角色设定" for s in d["sections"])


async def test_preview_time_section_is_last(ctx):
    client, _ = ctx
    r = await client.post("/api/v1/personas", json={"name": "x", "description": "d"})
    pid = (await r.get_json())["id"]
    d = await (await client.get(f"/api/v1/personas/{pid}/preview")).get_json()
    assert d["sections"][-1]["title"] == "当前时间"


# ===========================================================================
# 设置
# ===========================================================================


async def test_get_settings_is_redacted(ctx):
    client, _ = ctx
    r = await client.get("/api/v1/settings")
    assert r.status_code == 200
    text = await r.get_data(as_text=True)
    assert "sk-env" not in text
    d = await r.get_json()
    assert d["settings"]["upstream"]["api_key_source"] == "env"
    assert "upstream_model" in d["writable_fields"]


async def test_update_settings(ctx):
    client, _ = ctx
    r = await client.patch("/api/v1/settings", json={"upstream_model": "new-model"})
    assert r.status_code == 200
    d = await r.get_json()
    assert d["settings"]["upstream"]["model"] == "new-model"


async def test_update_settings_reports_ignored_fields(ctx):
    """🔴 不可改的字段要**明确告知被忽略**，不能静默吞掉。"""
    client, _ = ctx
    r = await client.patch("/api/v1/settings", json={
        "upstream_model": "ok", "server_port": 1, "auth_access_token": "hack",
    })
    d = await r.get_json()
    assert "server_port" in d["ignored_fields"]
    assert "auth_access_token" in d["ignored_fields"]
    assert d["settings"]["upstream"]["model"] == "ok"


async def test_clear_overrides(ctx):
    client, _ = ctx
    await client.patch("/api/v1/settings", json={"upstream_model": "runtime"})
    r = await client.delete("/api/v1/settings/overrides", json={})
    assert r.status_code == 200
    d = await r.get_json()
    assert d["settings"]["upstream"]["model"] == "env-model"


# ===========================================================================
# 聊天用上"当前人设"（⭐ 端到端串联）
# ===========================================================================


async def test_chat_uses_active_persona(tmp_path):
    """⭐ 设了当前人设 → 聊天请求的 system 里带上人设内容。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j

        captured.update(_j.loads(request.content))
        return _ok()

    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "e2e.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    settings = SettingsService(store, cfg)
    await settings.load()
    personas = PersonaRepository(store)
    upstream = OpenAIChatBackend(transport=httpx.MockTransport(handler))
    await upstream.open()
    chat = ChatService(cfg, upstream, settings=settings, personas=personas)
    app = create_app(cfg, store, AuthManager(access_token=TOKEN), chat,
                     personas=personas, settings=settings)
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    # 建人设 + 激活
    r = await client.post("/api/v1/personas", json={
        "name": "小爱", "description": "我是一个测试角色",
    })
    pid = (await r.get_json())["id"]
    await client.post(f"/api/v1/personas/{pid}/activate")

    # 聊天
    r = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "你好"}],
    })
    assert r.status_code == 200

    system_msg = captured["messages"][0]
    assert system_msg["role"] == "system"
    assert "我是一个测试角色" in system_msg["content"]
    await store.close()


async def test_chat_without_persona_still_works(ctx):
    """没人设也要能聊（只带时间片段）。"""
    client, _ = ctx
    r = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "你好"}],
    })
    assert r.status_code == 200


# ===========================================================================
# 面板静态资源
# ===========================================================================


async def test_panel_served(ctx):
    """面板 HTML 必须引用**模块化后的入口**。

    ⚠️ 2026-10-17 重构：前端从单文件 `app.js` 拆成 ES 模块
    （`js/main.js` + `js/core/` + `js/views/` + `js/bootstrap.js`）。
    这条判据原本断言 `/static/app.js`，重构后**必然红** —— 不是产品坏了，
    是路径契约变了。改成断言新入口。
    """
    client, _ = ctx
    r = await client.get("/")
    assert r.status_code == 200
    html = await r.get_data(as_text=True)
    assert "情绪app" in html
    assert '/static/js/main.js' in html
    # `type="module"` 是**必须的**：去掉它浏览器不会按模块解析，`import` 直接语法错
    assert 'type="module"' in html


async def test_static_assets_served(ctx):
    """静态资源能取到，且内容对。

    🔴 除了入口，还**逐个验各模块**：模块化最典型的坏法是
    "入口 200、子模块 404"（相对路径算错），而只验入口的判据**看不出来**
    —— 本次重构就真踩到过（`bootstrap.js` 里 `../views/` 解析成 `/static/views/`）。
    """
    client, _ = ctx
    #: (路径, 必须出现的片段) —— 片段挑的是**该文件独有的标识**
    modules = (
        ("/static/js/main.js", "GLOBALS"),          # 兼容层清单
        ("/static/js/bootstrap.js", "addEventListener"),  # 事件绑定
        ("/static/js/core/core.js", "export async function applyRoute"),
        ("/static/js/views/chat.js", "export async function loadChat"),
        ("/static/js/views/persona.js", "export async function loadPersonas"),
        ("/static/js/views/session.js", "export async function loadSessions"),
        ("/static/js/views/settings.js", "export async function loadLogs"),
        ("/static/js/views/providers.js", "export async function loadProviderSources"),
        ("/static/js/views/plugins.js", "export async function loadPluginSwitch"),
        ("/static/app.css", "--accent"),
    )
    for path, needle in modules:
        r = await client.get(path)
        assert r.status_code == 200, f"{path} 取不到（{r.status_code}）"
        body = await r.get_data(as_text=True)
        assert needle in body, f"{path} 内容里没有 {needle!r}"


async def test_panel_is_open_but_data_is_not(ctx):
    """面板页面免鉴权（只是壳），但**数据接口要鉴权**。"""
    client, _ = ctx
    assert (await client.get("/")).status_code == 200
    # 登出后数据接口仍应 401
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/personas")).status_code == 401


# ===========================================================================
# 「获取模型」按钮的后端：POST /api/v1/settings/models
#
# 🔴 为什么值得单独测：
#    这个接口**直连上游**（不是走我们自己的 chat backend），
#    失败模式很多（超时/鉴权/非 JSON/空列表/地址写错），
#    而**失败路径最容易写成静默**（本项目最贵的 bug 类型）。
#    这里每条失败都要求"说清是哪一类问题、且可行动"。
# ===========================================================================


def _models_app(cfg, store, transport):
    """造一个带**打桩 transport** 的 app（不打桩就会真的打外网）。"""
    from emotion_app.domain.persona_repo import PersonaRepository as _PR
    from emotion_app.domain.settings import SettingsService as _SS

    async def _mk():
        settings = _SS(store, cfg)
        await settings.load()
        personas = _PR(store)
        app = create_app(
            cfg, store, AuthManager(access_token=TOKEN),
            personas=personas, settings=settings,
        )
        app.config["EMOTION_MODELS_TRANSPORT"] = transport
        client = app.test_client()
        await client.post("/api/v1/auth/login", json={"token": TOKEN})
        return client

    return _mk()


def _json_transport(payload, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.fixture
async def settings_store(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "models.db")
    cfg.upstream.base_url = "https://upstream.test/v1"
    cfg.upstream.api_key = "sk-env-key"
    cfg.upstream.model = "env-model"
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    yield cfg, store
    await store.close()


async def test_fetch_models_success(settings_store):
    """正常路径：返回模型列表 + 说明用的是哪套参数。"""
    import asyncio

    cfg, store = settings_store
    transport = _json_transport(
        {"object": "list", "data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-b"}]}
    )
    client = await _models_app(cfg, store, transport)

    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = await r.get_json()
    # 去重且保序（中转站常返回重复项）
    assert d["models"] == ["m-b", "m-a"], d["models"]
    assert d["source"]["base_url"] == "https://upstream.test/v1"
    assert d["source"]["used_saved_api_key"] is True
    assert d["source"]["used_form_api_key"] is False


async def test_fetch_models_uses_form_values_before_save(settings_store):
    """🔴 **表单里还没保存的值也要能用**。

    真实用法：用户先改 API 地址 → 想拉模型看看 → 选 → 最后才保存。
    如果只认"已保存配置"，**第一次填地址时按钮就是废的**。
    判据：传了 base_url/base_key，请求就必须打到**传进来的**那个地址、带**那把** key。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "form-model"}]})

    cfg, store = settings_store
    client = await _models_app(cfg, store, httpx.MockTransport(handler))

    r = await client.post(
        "/api/v1/settings/models",
        json={"base_url": "https://other.test/v1", "api_key": "sk-form"},
    )
    assert r.status_code == 200
    d = await r.get_json()
    assert seen["url"] == "https://other.test/v1/models", seen["url"]
    assert seen["auth"] == "Bearer sk-form", seen["auth"]
    assert d["source"]["used_form_api_key"] is True
    assert d["source"]["base_url"] == "https://other.test/v1"


async def test_fetch_models_empty_api_key_uses_saved(settings_store):
    """表单 key 留空 = 沿用已保存的（否则用户换个地址就得重打一遍 key）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "x"}]})

    cfg, store = settings_store
    client = await _models_app(cfg, store, httpx.MockTransport(handler))
    r = await client.post("/api/v1/settings/models", json={"api_key": "   "})
    assert r.status_code == 200
    assert seen["auth"] == "Bearer sk-env-key", seen["auth"]


async def test_fetch_models_auth_failure_is_actionable(settings_store):
    """上游 401 → 必须说"是鉴权问题"，而不是笼统的"失败"。"""
    cfg, store = settings_store
    client = await _models_app(cfg, store, _json_transport({"error": "nope"}, status=401))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 502
    msg = (await r.get_json())["error"]["message"]
    assert "鉴权" in msg and "401" in msg, msg


async def test_fetch_models_unreachable_is_actionable(settings_store):
    """连不上 → 说明是"连不上"，并带上类型。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    cfg, store = settings_store
    client = await _models_app(cfg, store, httpx.MockTransport(handler))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 502
    msg = (await r.get_json())["error"]["message"]
    assert "连不上" in msg, msg


async def test_fetch_models_non_json_is_actionable(settings_store):
    """上游返回 200 但不是 JSON（比如地址填成了网页）→ 提示要 OpenAI 兼容端点。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    cfg, store = settings_store
    client = await _models_app(cfg, store, httpx.MockTransport(handler))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 502
    msg = (await r.get_json())["error"]["message"]
    assert "JSON" in msg, msg


async def test_fetch_models_empty_list_is_not_silent_success(settings_store):
    """200 但列表为空 → **不能**当成功返回空数组（那是静默失败）。"""
    cfg, store = settings_store
    client = await _models_app(cfg, store, _json_transport({"data": []}))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 502, "空列表被当成成功了 —— 用户会以为上游没模型"
    assert "没有模型" in (await r.get_json())["error"]["message"]


async def test_fetch_models_blank_base_url_rejected(settings_store):
    """地址为空 → 400 且说清原因（不是 500）。"""
    from emotion_app.domain.settings import SettingsService

    cfg, store = settings_store
    cfg.upstream.base_url = ""
    client = await _models_app(cfg, store, _json_transport({"data": []}))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 400
    assert "地址" in (await r.get_json())["error"]["message"]


async def test_fetch_models_requires_auth(tmp_path):
    """🔴 **安全判据**：未鉴权必须 401。

    这条接口会用**服务端的 API Key** 去打上游 —— 如果漏了鉴权，
    任何人都能拿它当"探测内网/消耗额度"的跳板。
    """
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "noauth.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    try:
        app = create_app(cfg, store, AuthManager(access_token=TOKEN))
        client = app.test_client()
        r = await client.post("/api/v1/settings/models", json={})
        assert r.status_code == 401, f"未鉴权竟能拉模型：{r.status_code}"
    finally:
        await store.close()


async def test_fetch_models_accepts_models_key_shape(settings_store):
    """少数中转站用 `{"models": [...]}` 而不是 `{"data": [...]}` —— 两种都要能吃。"""
    cfg, store = settings_store
    client = await _models_app(cfg, store, _json_transport({"models": ["a", "b"]}))
    r = await client.post("/api/v1/settings/models", json={})
    assert r.status_code == 200
    assert (await r.get_json())["models"] == ["a", "b"]
