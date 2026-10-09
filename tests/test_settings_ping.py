# -*- coding: utf-8 -*-
"""「测试连接」的后端判据：`POST /api/v1/settings/ping`。

## 🔴 为什么必须有这一批（情绪 2026-10-09 报障）

情绪：「点击『测试连接』竟然是**秒返回正常**，检查一下。」

查实：前端那个按钮打的是 `fetch("/v1/models")` —— 而那是**我们自己的**
OpenAI 兼容端点，它**只回显内存里的模型名、一个上游请求都不发**
（实测 `HTTP 200，耗时 0.0016 秒`）。
⇒ **key 全废、上游全断、域名解析不了，它照样秒回"连接正常"。**

这类缺陷的形态是「**判据测错了对象**」：它不是"判据太弱"，而是
**根本没测到那个东西**。所以这批判据的重点**不是"成功时返回什么"**，
而是下面这两条：

  ① **上游不通时它必须 `ok=false`** —— 这才是"测试连接"存在的意义；
  ② **每一种不通都要能被区分出来**（超时/连不上/鉴权/非 JSON/HTTP 错误），
     因为用户拿到的是一句提示，混在一起就等于没说。

⚠️ 判据里**不许**出现"只要 200 就算过"这种写法 —— 那正是被修掉的那个 bug。
"""
from __future__ import annotations

import httpx
import pytest

from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 16


def _ping_app(cfg, store, transport):
    """造一个带**打桩 transport** 的 app。

    ⚠️ 用 `EMOTION_PING_TRANSPORT`（**独立**于 `EMOTION_MODELS_TRANSPORT`）——
       两者是不同能力，共用一个注入点会让"只想打桩其中一个"变成不可能。
    """
    from emotion_app.domain.persona_repo import PersonaRepository
    from emotion_app.domain.settings import SettingsService

    async def _mk():
        settings = SettingsService(store, cfg)
        await settings.load()
        personas = PersonaRepository(store)
        app = create_app(
            cfg, store, AuthManager(access_token=TOKEN),
            personas=personas, settings=settings,
        )
        app.config["EMOTION_PING_TRANSPORT"] = transport
        client = app.test_client()
        await client.post("/api/v1/auth/login", json={"token": TOKEN})
        return client

    return _mk()


@pytest.fixture
async def ping_store(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "ping.db")
    cfg.upstream.base_url = "https://upstream.test/v1"
    cfg.upstream.api_key = "sk-env-key"
    cfg.upstream.model = "env-model"
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    yield cfg, store
    await store.close()


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok_models(*ids: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})

    return _transport(handler)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


async def test_ping_requires_auth(ping_store):
    """🔴 新增路由**必须**先鉴权（本项目真实事故：`/api/v1/logs` 漏了鉴权
    → 未登录返 200，而服务绑 0.0.0.0 = 公网可读）。"""
    cfg, store = ping_store
    from emotion_app.domain.settings import SettingsService

    settings = SettingsService(store, cfg)
    await settings.load()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN), settings=settings)
    app.config["EMOTION_PING_TRANSPORT"] = _ok_models("m")
    client = app.test_client()
    # ⚠️ **不登录**
    r = await client.post("/api/v1/settings/ping", json={})
    assert r.status_code == 401, f"未鉴权竟然返回 {r.status_code}（必须是 401）"


# ---------------------------------------------------------------------------
# 成功路径 —— 但要证明"它真的打了上游"
# ---------------------------------------------------------------------------


async def test_ping_reports_model_present(ping_store):
    """上游可达且**含**当前配置的模型 → `model_present=True`。"""
    cfg, store = ping_store
    client = await _ping_app(cfg, store, _ok_models("m-a", "env-model", "m-b"))
    r = await client.post("/api/v1/settings/ping", json={})
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = await r.get_json()
    assert d["ok"] is True
    assert d["model"] == "env-model"
    assert d["model_count"] == 3
    assert d["model_present"] is True
    assert "env-model" in d["detail"]


async def test_ping_flags_model_absent(ping_store):
    """🔴 上游**可达**但**不含**你配的模型 → 必须说出来。

    这是"半通"状态里最容易被说成"正常"的一种：
    地址和 key 都对（所以能连上、能列模型），但你填的模型名不存在
    ⇒ 聊天时上游会报 `model_not_found`。
    ⚠️ 单看"HTTP 200"会得出"连接正常"的**错误结论**。
    """
    cfg, store = ping_store
    client = await _ping_app(cfg, store, _ok_models("m-a", "m-b"))
    r = await client.post("/api/v1/settings/ping", json={})
    d = await r.get_json()
    assert r.status_code == 200
    assert d["ok"] is True, "上游确实可达 —— 这不算连接失败"
    assert d["model_present"] is False
    assert "不含" in d["detail"], d["detail"]


async def test_ping_empty_model_list_is_not_failure(ping_store):
    """⚠️ 上游 200 但**没有模型列表** → **不算连接失败**（与 `/settings/models` 判据**故意不同**）。

    理由：有些中转站确实不实现 `/models`，但聊天端点照样能用。
    `models` 那里空列表要报错（用户明确要列表，给空就是没拿到）；
    而 `ping` 只要"通了"就算通 —— 否则会给用户一个**假的故障**。
    """
    cfg, store = ping_store
    client = await _ping_app(cfg, store, _ok_models())
    r = await client.post("/api/v1/settings/ping", json={})
    d = await r.get_json()
    assert r.status_code == 200
    assert d["ok"] is True, "上游可达却报了失败 —— 那是个假故障"
    assert d["model_count"] == 0
    assert "不支持" in d["detail"] or "没有返回模型列表" in d["detail"]


# ---------------------------------------------------------------------------
# 🔴 失败路径 —— 本批最要紧的部分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expect_type,fragment",
    [
        (401, "upstream_auth", "鉴权"),
        (403, "upstream_auth", "鉴权"),
        (500, "upstream_error", "500"),
        (429, "upstream_error", "429"),
    ],
)
async def test_ping_reports_http_failures(ping_store, status, expect_type, fragment):
    """上游返回非 2xx → **`ok=false` + 能区分是哪一类**。

    ⚠️ 这条判据的要点是 `ok is False`：修 bug **之前**那版无论怎样都返回
       "连接正常"（因为它压根没打上游），所以**只有断言 ok=false 才守得住**。
    """
    cfg, store = ping_store

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream said no")

    client = await _ping_app(cfg, store, _transport(handler))
    r = await client.post("/api/v1/settings/ping", json={})
    assert r.status_code == 502, f"上游 {status} 时我们该报 502，实得 {r.status_code}"
    d = await r.get_json()
    assert d["ok"] is False
    assert d["error_type"] == expect_type
    assert fragment in d["detail"], d["detail"]


async def test_ping_reports_timeout(ping_store):
    """超时 → `upstream_timeout`（且提示要包含"检查地址/代理"这种**可行动**的信息）。"""
    cfg, store = ping_store

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = await _ping_app(cfg, store, _transport(handler))
    r = await client.post("/api/v1/settings/ping", json={})
    d = await r.get_json()
    assert r.status_code == 502
    assert d["ok"] is False
    assert d["error_type"] == "upstream_timeout"
    assert "超时" in d["detail"]


async def test_ping_reports_unreachable(ping_store):
    """连不上（DNS/连接拒绝）→ `upstream_unreachable`，且带上异常类名。"""
    cfg, store = ping_store

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    client = await _ping_app(cfg, store, _transport(handler))
    r = await client.post("/api/v1/settings/ping", json={})
    d = await r.get_json()
    assert r.status_code == 502
    assert d["ok"] is False
    assert d["error_type"] == "upstream_unreachable"
    assert "ConnectError" in d["detail"]


async def test_ping_reports_non_json(ping_store):
    """上游返回 HTML（地址填成官网）→ `upstream_bad_payload`，并提示应以 /v1 结尾。"""
    cfg, store = ping_store

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    client = await _ping_app(cfg, store, _transport(handler))
    r = await client.post("/api/v1/settings/ping", json={})
    d = await r.get_json()
    assert r.status_code == 502
    assert d["ok"] is False
    assert d["error_type"] == "upstream_bad_payload"
    assert "/v1" in d["detail"]


async def test_ping_empty_base_url_is_rejected(ping_store):
    """地址为空 → 报 `invalid_api_base`（且信封与其它失败**一致**，都带 `ok`）。

    ⚠️ 我第一版这里走的是 `_err()`，它**没有 `ok` 字段** ——
       于是前端得为这一条单独写个特例。统一信封后一条判断就够。
    """
    cfg, store = ping_store
    cfg.upstream.base_url = ""
    client = await _ping_app(cfg, store, _ok_models("m"))
    r = await client.post("/api/v1/settings/ping", json={"base_url": "  "})
    d = await r.get_json()
    assert r.status_code == 502
    assert d["ok"] is False
    assert d["error_type"] == "invalid_api_base"


async def test_ping_uses_form_values_over_saved(ping_store):
    """🔴 **表单里还没保存的值优先**。

    用户常是「改地址 → 测一下 → 再保存」。只读已保存值的话，
    他刚改完地址点测试，测的还是**旧地址** —— 那又变成"测错了对象"
    （正是本轮修掉的那类缺陷）。
    """
    cfg, store = ping_store
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    client = await _ping_app(cfg, store, _transport(handler))
    await client.post(
        "/api/v1/settings/ping",
        json={"base_url": "https://form.test/v1", "api_key": "sk-form"},
    )
    assert seen["url"] == "https://form.test/v1/models", seen["url"]
    assert seen["auth"] == "Bearer sk-form", seen["auth"]


@pytest.mark.parametrize(
    "label,transport",
    [
        ("成功路径", _ok_models("m")),
        ("鉴权失败路径", _transport(lambda r: httpx.Response(401, text="denied"))),
        ("超时路径", _transport(lambda r: (_ for _ in ()).throw(
            httpx.TimeoutException("t", request=r)))),
        ("连不上路径", _transport(lambda r: (_ for _ in ()).throw(
            httpx.ConnectError("boom", request=r)))),
        ("非 JSON 路径", _transport(lambda r: httpx.Response(200, text="<html>"))),
    ],
)
async def test_ping_response_never_leaks_api_key(ping_store, label, transport):
    """🔴 **响应里绝不能出现 key**，**每条路径都要扫**（面板 key 框是只写的）。

    ⚠️ 判据要扫**整段响应体**，不能只看某个字段 —— 漏在 `detail` 或
       `base_url` 里同样是泄露（本项目踩过"脱敏漏一处"的坑）。

    🔴 **为什么必须参数化到每条路径**（注入验证当场抓出来的假绿）：
       我第一版只测**成功路径**，而注入"把 key 加进失败响应体"时它
       **纹丝不动（14 passed 全绿）** —— 因为成功路径根本走不到 `_fail` 那段。
       ⇒ **判据只覆盖了它想说的事情的一部分，而它看起来是绿的。**
       这正是本项目反复出现的「注入抓不住先问**这条路径测到了吗**」。

    ⚠️ 顺带：这也说明"脱敏"这个属性**不能只在一条路径上成立** ——
       它是**整个端点**的属性，所以每条出口都要验。
    """
    cfg, store = ping_store
    secret = "sk-super-secret-do-not-leak"
    cfg.upstream.api_key = secret
    client = await _ping_app(cfg, store, transport)
    r = await client.post("/api/v1/settings/ping", json={})
    body = await r.get_data(as_text=True)
    assert secret not in body, f"[{label}] 响应体里泄露了 API Key：{body[:200]}"
