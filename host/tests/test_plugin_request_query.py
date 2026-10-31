# -*- coding: utf-8 -*-
"""`PluginRequest.query` / `.path` 的**真 Quart 请求**判据。

## 为什么必须用真 Quart 请求，不能用 Mock

2026-10-30 线上故障的根因是「**读 Starlette 专有属性，在 Quart 上静默为空**」：

    self.query = PluginMultiDict(list(request_.query_params.multi_items()))   # 原实现
    self.path  = str(request_.url.path)                                       # 原实现

Quart 的 `Request` **没有** `query_params`，且 `url` 是 **`str`**（不是 Starlette
那个有 `.path` 的 URL 对象）⇒ `query` 恒为空、`path` 恒为空串。

因此**用 Mock 测不出这个 bug**：Mock 会照着调用方的愿望返回属性，
`getattr(mock, "query_params")` 一定"成功"。判据必须让**真实的 Quart 请求对象**
参与，才能证明「这个形状在真宿主上取不到值」。

## 依据

* 真 AstrBot 4.27.5 `astrbot/api/web.py` 的 `PluginRequest.__init__`
  确实读 `request_.query_params.multi_items()` / `request_.url.path` /
  `request_.client.host` —— 也就是说**真实现本身是 Starlette 形状**，
  我们的兼容层必须在 Quart 形状上做出**等价语义**。
* 官方陪伴插件 7.0.0（`astrbot_host/acquaintance_api.py`）的 `_query(allowed, required)`：

      query = request.query
      if (set(query.keys()) - set(allowed) or set(required) - set(query.keys())
              or any(len(query.getlist(key)) != 1 for key in query.keys())):
          raise TaskError("acquaintance_request_invalid", ...)

  ⇒ 它依赖 `keys()` / `get()` / `getlist()` / `in` 四件套，
    且**用 `len(getlist(k)) != 1` 拒绝重复参数** —— 合并去重会**静默放宽**它的校验。

## 覆盖矩阵

| 判据 | 形状 |
|---|---|
| 多值同键保序保重 | 真 Quart 请求 `?a=1&a=2&b=3` |
| `len(getlist("a")) == 2` | 官方拒绝重复参数的依据 |
| 无查询串 | 真 Quart 请求（无 `?`） |
| `path` | 真 Quart 请求，含 path 参数形态 |
| URL 编码 / 中文 | 百分号编码 + 原样 UTF-8 |
| Starlette 形状回归 | 带 `query_params.multi_items()` 的假对象 |
| 裸 dict 形状 | `args` 就是 dict |
| **端到端性质** | 真 Quart app + `bind_request_context` + 官方同形 `_query` |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from quart import Quart

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from astrbot_host.compat.api.web import PluginMultiDict, PluginRequest  # noqa: E402

#: 探针 app —— 把一次真 Quart 请求上的 `PluginRequest` 全字段快照出来。
APP = Quart("compat-plugin-request-probe")


def _snapshot(*, path_params: dict | None = None) -> dict:
    """在**真实请求上下文内**构造 `PluginRequest` 并导出可断言的快照。"""
    from quart import request as quart_request

    raw = quart_request._get_current_object()
    req = PluginRequest(
        raw,
        path_params=dict(path_params or {}),
        plugin_name="probe",
        username="owner",
    )
    return {
        "method": req.method,
        "path": req.path,
        "path_params": req.path_params,
        "query_pairs": [[k, v] for k, v in req.query.items()],
        "query_keys": list(req.query.keys()),
        "query_len": len(req.query),
        "query_bool": bool(req.query),
        "get_a": req.query.get("a"),
        "getlist_a": req.query.getlist("a"),
        "get_missing": req.query.get("missing"),
        "getlist_missing": req.query.getlist("missing"),
        "get_name": req.query.get("name"),
        "get_space": req.query.get("space"),
        "getlist_dup": req.query.getlist("dup"),
        "contains_a": "a" in req.query,
        "contains_missing": "missing" in req.query,
        "client_host": req.client_host,
        "content_type": req.content_type,
    }


@APP.route("/probe/x1")
async def probe_x1():
    return _snapshot()


@APP.route("/probe/<first>/<second>")
async def probe_two(first: str, second: str):
    return _snapshot(path_params={"first": first, "second": second})


@APP.route("/api/v1/plugins/extensions/probe/x1")
async def probe_extension_shape():
    return _snapshot()


async def _get(query_string: str, path: str = "/probe/x1") -> dict:
    """发一次真 Quart 请求，返回快照。"""
    url = f"{path}?{query_string}" if query_string else path
    client = APP.test_client()
    response = await client.get(url)
    assert response.status_code == 200, f"{url} → {response.status_code}"
    return await response.get_json()


# ---------------------------------------------------------------------------
# 1) 多值同键：必须保序、保重复、不合并
# ---------------------------------------------------------------------------


async def test_multivalue_query_preserves_order_and_duplicates():
    """`?a=1&a=2&b=3` → getlist/get/keys/items/len/in 与请求顺序一致。"""
    snap = await _get("a=1&a=2&b=3")

    # getlist：重复键**全部保留**，顺序即请求顺序
    assert snap["getlist_a"] == ["1", "2"], snap["getlist_a"]
    # get：官方 `_query` 用它取值（单值场景）
    assert snap["get_a"] == "1", snap["get_a"]
    assert snap["query_pairs"] == [["a", "1"], ["a", "2"], ["b", "3"]], snap["query_pairs"]
    # keys()：官方 `set(query.keys()) - set(allowed)` 直接吃这个
    assert snap["query_keys"] == ["a", "a", "b"], snap["query_keys"]
    # len：pairs 条数（不是去重后的键数）
    assert snap["query_len"] == 3, snap["query_len"]
    assert snap["query_bool"] is True
    # in
    assert snap["contains_a"] is True
    assert snap["contains_missing"] is False


async def test_len_getlist_exposes_duplicate_count_for_official_rejection():
    """官方用 `len(query.getlist(k)) != 1` 拒绝重复参数 ⇒ 兼容层**不许去重**。

    这条是本轮最容易被"顺手优化"破坏的性质：
    一旦把 `?a=1&a=2` 合并成一条，官方的严格校验就**静默失效**，
    变成"接受歧义参数" —— 从拒绝变成猜。
    """
    snap = await _get("a=1&a=2&b=3")
    assert len(snap["getlist_a"]) == 2, snap["getlist_a"]

    single = await _get("a=only")
    assert len(single["getlist_a"]) == 1, single["getlist_a"]

    # 三值同键也不合并
    triple = await _get("dup=x&dup=y&dup=z")
    assert triple["getlist_dup"] == ["x", "y", "z"], triple["getlist_dup"]
    assert triple["query_len"] == 3, triple["query_len"]


# ---------------------------------------------------------------------------
# 2) 无查询串：必须真的"空"，且形状齐全（不是 None、不抛）
# ---------------------------------------------------------------------------


async def test_absent_query_is_empty_but_shape_complete():
    snap = await _get("")

    assert snap["query_len"] == 0, snap["query_len"]
    assert snap["query_bool"] is False, "无参数时 bool(query) 必须为 False（官方 `if request.query:` 依赖它）"
    assert snap["query_pairs"] == []
    assert snap["query_keys"] == []
    assert snap["getlist_a"] == [], snap["getlist_a"]
    assert snap["getlist_missing"] == []
    assert snap["get_a"] is None, snap["get_a"]
    assert snap["get_missing"] is None
    assert snap["contains_missing"] is False


# ---------------------------------------------------------------------------
# 3) path：必须是真实请求路径（Quart 的 `url` 是 str，取 `.path` 会恒为空）
# ---------------------------------------------------------------------------


async def test_path_is_real_request_path():
    snap = await _get("", path="/probe/x1")
    assert snap["path"] == "/probe/x1", snap["path"]
    assert snap["path"] != "", "path 为空串 ⇒ 官方 `request.path.startswith(...)` 判断恒假"


async def test_path_with_path_params():
    """带路径参数时 path 仍是**真实路径**（不是路由模板）。"""
    snap = await _get("", path="/probe/alpha/beta")
    assert snap["path"] == "/probe/alpha/beta", snap["path"]
    assert snap["path_params"] == {"first": "alpha", "second": "beta"}, snap["path_params"]


async def test_path_keeps_extension_prefix_for_official_identity_check():
    """官方 `management_identity._authenticated_api_key` 判的就是这个前缀。

        request.path.startswith("/api/v1/plugins/extensions/")

    Quart 上 `url` 是 str ⇒ 原实现取到空串 ⇒ 该判断恒 False。
    """
    snap = await _get("", path="/api/v1/plugins/extensions/probe/x1")
    assert snap["path"].startswith("/api/v1/plugins/extensions/"), snap["path"]


# ---------------------------------------------------------------------------
# 4) URL 编码与中文值
# ---------------------------------------------------------------------------


async def test_url_encoded_and_chinese_values_are_decoded():
    snap = await _get("name=%E8%B4%B4&space=a%20b&a=%E4%BD%A0%E5%A5%BD")
    assert snap["get_name"] == "贴", snap["get_name"]
    assert snap["get_space"] == "a b", snap["get_space"]
    assert snap["get_a"] == "你好", snap["get_a"]


async def test_plus_and_percent_plus_are_distinguished():
    """`+` 在查询串里是空格，字面加号必须写成 `%2B`。

    ⚠️ HTTP 线上只有 ASCII，中文一定以百分号编码到达 ——
       `Quart.test_client` 也拒绝非 ASCII 的 URL（实测 `UnicodeEncodeError`），
       所以"原样 UTF-8 查询串"不是可达场景，这里只断言线上真实会出现的编码形态。
    """
    snap = await _get("space=a+b&a=%2Bplus%2B")
    assert snap["get_space"] == "a b", snap["get_space"]
    assert snap["get_a"] == "+plus+", snap["get_a"]


async def test_multivalue_chinese_values_keep_order():
    snap = await _get("dup=%E4%B8%80&dup=%E4%BA%8C")
    assert snap["getlist_dup"] == ["一", "二"], snap["getlist_dup"]


# ---------------------------------------------------------------------------
# 5) Starlette 形状回归（防将来换底层框架时静默退化）
# ---------------------------------------------------------------------------


class _FakeStarletteQueryParams:
    """只复刻 Starlette `QueryParams` 被测到的那一个方法。"""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


class _FakeStarletteUrl:
    """Starlette 的 `URL` 有 `.path`（Quart 的 `url` 是 str）。"""

    def __init__(self, path: str) -> None:
        self.path = path


class _FakeStarletteClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeStarletteRequest:
    """Starlette/FastAPI 形状的假请求（**不 import starlette**，只复制形状）。"""

    def __init__(self, pairs, *, path="/probe/x1", client_host="10.0.0.7") -> None:
        self.method = "GET"
        self.query_params = _FakeStarletteQueryParams(pairs)
        self.url = _FakeStarletteUrl(path)
        self.headers = {}
        self.cookies = {}
        self.client = _FakeStarletteClient(client_host)


def test_starlette_shaped_request_still_yields_same_pairs():
    pairs = [("a", "1"), ("a", "2"), ("b", "3")]
    req = PluginRequest(_FakeStarletteRequest(pairs))

    assert req.query.items() == pairs, req.query.items()
    assert req.query.getlist("a") == ["1", "2"]
    assert req.query.keys() == ["a", "a", "b"]
    assert len(req.query) == 3
    assert req.query.get("b") == "3"
    # 这条支路没有 `.path` 属性 ⇒ 必须退回 `url.path`（Starlette 形状）
    assert req.path == "/probe/x1", req.path
    # Starlette 形状下 client.host 可读（对比 Qurt 上没有 .client，见报告）
    assert req.client_host == "10.0.0.7", req.client_host


def test_starlette_shaped_request_without_query_is_empty():
    req = PluginRequest(_FakeStarletteRequest([]))
    assert len(req.query) == 0
    assert req.query.getlist("a") == []
    assert req.query.get("a") is None


# ---------------------------------------------------------------------------
# 6) 裸 dict 形状（`args` 就是 dict）
# ---------------------------------------------------------------------------


class _FakeDictArgsRequest:
    """只提供 `args`（dict）与 `path` 的最简形状。"""

    def __init__(self, pairs: dict) -> None:
        self.method = "GET"
        self.args = dict(pairs)
        self.path = "/plain/x1"
        self.headers = {}
        self.cookies = {}


def test_bare_dict_args_shape_still_yields_pairs():
    req = PluginRequest(_FakeDictArgsRequest({"a": "1", "b": "2"}))
    assert req.query.items() == [("a", "1"), ("b", "2")], req.query.items()
    assert req.query.get("b") == "2"
    assert req.query.getlist("a") == ["1"]
    assert len(req.query) == 2
    assert req.path == "/plain/x1", req.path


def test_dict_shape_does_not_crash_on_items_signature():
    """`dict.items` 是 callable，但**不接受 `multi=True`** —— 必须降级而不是抛。"""
    req = PluginRequest(_FakeDictArgsRequest({}))
    assert len(req.query) == 0
    assert req.query.items() == []


# ---------------------------------------------------------------------------
# 7) 端到端性质判据：真 Quart app + `bind_request_context` + 官方同形 `_query`
# ---------------------------------------------------------------------------
#
# 🔴 这是本轮的关键判据：它不看兼容层内部，只看「官方插件那种写法能不能拿到参数」。
#    旧实现（只读 `query_params`）下，`?read_mode=stored` 会拿到空 query、
#    `?bogus=1` 会**被放行**（因为看不到任何字段）—— 也就是线上那个
#    「页面永远读不到身份、无法设为核心用户」的同型失败。


@pytest.fixture(scope="module")
def astrbot_api_web():
    """把 compat 注册成 `astrbot`（官方插件的 import 路径），用完还原原状态。"""
    from astrbot_host.shadow import (
        install_shadow_package,
        is_installed,
        uninstall_shadow_package,
    )

    was_installed = is_installed()
    if not was_installed:
        install_shadow_package()
    try:
        import astrbot.api.web as web_mod

        # 影子包必须指向**同一个模块对象**，否则测得不是被测代码
        import astrbot_host.compat.api.web as compat_web

        assert web_mod is compat_web
        yield web_mod
    finally:
        if not was_installed:
            uninstall_shadow_package()


def _official_like_app(web_mod) -> Quart:
    """复刻官方 `astrbot_host/acquaintance_api.py` 的两种 `_query` 用法。"""
    app = Quart("compat-official-shape-probe")

    async def _enter() -> "object":
        from quart import request as quart_request

        return web_mod.PluginRequest(
            quart_request._get_current_object(),
            plugin_name="probe",
            username="owner",
        )

    def _official_query(allowed, required=()):
        # ⚠️ 与官方**逐字同形**：读模块级代理 `astrbot.api.web.request.query`
        query = web_mod.request.query
        if (
            set(query.keys()) - set(allowed)
            or set(required) - set(query.keys())
            or any(len(query.getlist(key)) != 1 for key in query.keys())
        ):
            return {"status": "error", "code": "acquaintance_request_invalid"}
        return {
            "status": "ok",
            "code": None,
            "query": {key: query.get(key) for key in allowed if key in query},
            "request_path": web_mod.request.path,
            "client_host": web_mod.request.client_host,
        }

    @app.route("/api/plug/probe/directory")
    async def directory():
        req = await _enter()
        with web_mod.bind_request_context(req):
            # 官方官方 `directory()`：self._query(("read_mode",))
            return _official_query(("read_mode",))

    @app.route("/api/plug/probe/authorization")
    async def authorization():
        req = await _enter()
        with web_mod.bind_request_context(req):
            # 官方 `authorization()`：self._query(("subject_id", ...), required=("subject_id",))
            return _official_query(("subject_id",), required=("subject_id",))

    return app


async def _official_get(app: Quart, url: str) -> dict:
    client = app.test_client()
    response = await client.get(url)
    assert response.status_code == 200, f"{url} → {response.status_code}"
    return await response.get_json()


async def test_official_shape_handler_reads_query_end_to_end(astrbot_api_web):
    """`?read_mode=stored` → 官方校验通过，且**真的取到了值**。"""
    app = _official_like_app(astrbot_api_web)

    ok = await _official_get(app, "/api/plug/probe/directory?read_mode=stored")
    assert ok["status"] == "ok", ok
    assert ok["query"] == {"read_mode": "stored"}, ok
    # 顺带：同一次调用里 path 也必须是真实路径（不是空串）
    assert ok["request_path"] == "/api/plug/probe/directory", ok


async def test_official_shape_handler_accepts_no_query(astrbot_api_web):
    """不带参数 → 通过（官方 `directory()` 允许无参）。"""
    app = _official_like_app(astrbot_api_web)

    ok = await _official_get(app, "/api/plug/probe/directory")
    assert ok["status"] == "ok", ok
    assert ok["query"] == {}, ok


async def test_official_shape_handler_rejects_unknown_field(astrbot_api_web):
    """`?bogus=1` → 报错。

    🔴 这是旧实现的**确凿失败点**：旧实现看不到任何 query 字段，
        `set(query.keys()) - {"read_mode"}` 是空集 ⇒ 校验**被绕过**，
        返回 200 —— 线上「损坏的参数静默放行」同型。
    """
    app = _official_like_app(astrbot_api_web)

    bad = await _official_get(app, "/api/plug/probe/directory?bogus=1")
    assert bad["status"] == "error", bad
    assert bad["code"] == "acquaintance_request_invalid", bad


async def test_official_shape_handler_rejects_duplicate_field(astrbot_api_web):
    """重复字段 → 报错（官方 `len(getlist(k)) != 1`）。"""
    app = _official_like_app(astrbot_api_web)

    bad = await _official_get(app, "/api/plug/probe/directory?read_mode=stored&read_mode=live")
    assert bad["status"] == "error", bad
    assert bad["code"] == "acquaintance_request_invalid", bad


async def test_official_authorization_requires_subject_id(astrbot_api_web):
    """官方 `authorization()` 的必填 `subject_id`：有则过、无则拒。

    线上报的就是这一条（`acquaintance_request_invalid /
    the directory does not accept these query fields`）。
    """
    app = _official_like_app(astrbot_api_web)

    ok = await _official_get(app, "/api/plug/probe/authorization?subject_id=human-1")
    assert ok["status"] == "ok", ok
    assert ok["query"] == {"subject_id": "human-1"}, ok

    missing = await _official_get(app, "/api/plug/probe/authorization")
    assert missing["status"] == "error", missing
    assert missing["code"] == "acquaintance_request_invalid", missing


async def test_official_authorization_rejects_repeated_subject_id(astrbot_api_web):
    app = _official_like_app(astrbot_api_web)

    bad = await _official_get(
        app, "/api/plug/probe/authorization?subject_id=human-1&subject_id=human-2"
    )
    assert bad["status"] == "error", bad
    assert bad["code"] == "acquaintance_request_invalid", bad


# ---------------------------------------------------------------------------
# 8) 代理在上下文外必须**抛错**（静默 None 会让插件以为"没参数"）
# ---------------------------------------------------------------------------


def test_proxy_outside_handler_raises(astrbot_api_web):
    with pytest.raises(RuntimeError):
        _ = astrbot_api_web.request.query


# ---------------------------------------------------------------------------
# 9) PluginMultiDict 自身的形状（官方只用 get/getlist/keys/in/len/items）
# ---------------------------------------------------------------------------


def test_multidict_surface_used_by_official_plugin():
    """官方 `_query` 实际用到的四件套 + items/len，一个都不能缺。"""
    pairs = [("a", "1"), ("a", "2"), ("b", "3")]
    q = PluginMultiDict(list(pairs))

    assert q.get("a") == "1"
    assert q.getlist("a") == ["1", "2"]
    assert q.keys() == ["a", "a", "b"]
    assert "b" in q and "z" not in q
    assert len(q) == 3
    assert q.items() == pairs
    assert list(q) == pairs, "`__iter__` 产出的必须是 (key, value) 对"


def test_multidict_subscript_returns_first_value_and_missing_raises_key_error():
    query = PluginMultiDict([("a", "first"), ("a", "second"), ("b", "third")])
    assert query["a"] == "first"
    assert query["b"] == "third"
    with pytest.raises(KeyError) as caught:
        query["missing"]
    assert caught.value.args == ("missing",)


@pytest.mark.parametrize("pairs,expected", [([], False), ([("empty", "")], True), ([("a", "1"), ("a", "2")], True)])
def test_multidict_truth_follows_parameter_presence(pairs, expected):
    assert bool(PluginMultiDict(pairs)) is expected


_PERSON_ROUTE_KEYS = ("platform", "bot_id", "account_id", "role_id")


@pytest.fixture
def official_route_app(astrbot_api_web):
    from quart import request as quart_request

    app = Quart("compat-official-route-probe")
    web_mod = astrbot_api_web

    @app.get("/authorization")
    async def authorization():
        req = web_mod.PluginRequest(quart_request._get_current_object())
        with web_mod.bind_request_context(req):
            query = web_mod.request.query
            allowed = ("subject_id", *_PERSON_ROUTE_KEYS)
            required = ("subject_id",)
            # 官方 acquaintance_api.py:452-457 的字段与重复值校验。
            if (set(query.keys()) - set(allowed) or set(required) - set(query.keys())
                    or any(len(query.getlist(key)) != 1 for key in query.keys())):
                return {"status": "error", "code": "acquaintance_request_invalid"}
            # 官方 acquaintance_api.py:442-449 的完整路由与下标访问契约。
            supplied = set(query.keys()).intersection(_PERSON_ROUTE_KEYS)
            if supplied and supplied != set(_PERSON_ROUTE_KEYS):
                return {"status": "error", "code": "acquaintance_route_required"}
            route = {key: query[key] for key in _PERSON_ROUTE_KEYS} if supplied else None
            return {"status": "ok", "subject_id": query["subject_id"], "route": route}

    @app.get("/forbid-query")
    async def forbid_query():
        req = web_mod.PluginRequest(quart_request._get_current_object())
        with web_mod.bind_request_context(req):
            if web_mod.request.query:
                return {"status": "error", "code": "acquaintance_request_invalid"}
            return {"status": "ok"}

    return app


async def test_official_complete_person_route_survives_real_quart_request(official_route_app):
    result = await _official_get(official_route_app,
        "/authorization?subject_id=synthetic-person&platform=onebot&bot_id=bot-a&account_id=account-a&role_id=role-a")
    assert result == {"status": "ok", "subject_id": "synthetic-person", "route": {
        "platform": "onebot", "bot_id": "bot-a", "account_id": "account-a", "role_id": "role-a",
    }}


async def test_official_absent_optional_person_route_stays_none(official_route_app):
    result = await _official_get(official_route_app, "/authorization?subject_id=synthetic-person")
    assert result == {"status": "ok", "subject_id": "synthetic-person", "route": None}


@pytest.mark.parametrize("missing", _PERSON_ROUTE_KEYS)
async def test_official_partial_person_route_is_explicitly_required(official_route_app, missing):
    from urllib.parse import urlencode

    route = {"platform": "onebot", "bot_id": "bot-a", "account_id": "account-a", "role_id": "role-a"}
    pairs = [("subject_id", "synthetic-person"), *[(key, value) for key, value in route.items() if key != missing]]
    result = await _official_get(official_route_app, "/authorization?" + urlencode(pairs))
    assert result == {"status": "error", "code": "acquaintance_route_required"}


@pytest.mark.parametrize("query", [
    "subject_id=synthetic-person&unknown=forbidden",
    "subject_id=synthetic-person&subject_id=other-person",
    "subject_id=synthetic-person&platform=onebot&platform=other&bot_id=bot-a&account_id=account-a&role_id=role-a",
])
async def test_official_person_route_rejects_unknown_or_duplicate_fields(official_route_app, query):
    result = await _official_get(official_route_app, "/authorization?" + query)
    assert result == {"status": "error", "code": "acquaintance_request_invalid"}


@pytest.mark.parametrize("suffix,expected", [
    ("", {"status": "ok"}),
    ("?empty=", {"status": "error", "code": "acquaintance_request_invalid"}),
    ("?present=1", {"status": "error", "code": "acquaintance_request_invalid"}),
])
async def test_official_forbid_query_distinguishes_empty_from_present(official_route_app, suffix, expected):
    assert await _official_get(official_route_app, "/forbid-query" + suffix) == expected


async def test_real_quart_asgi_peer_is_preserved_for_official_local_dashboard_check():
    import ipaddress
    from urllib.parse import urlsplit
    from quart import request as quart_request

    # 官方 together/common.py:137-169 的等价纯判定，不启动房间或浏览器。
    def is_loopback(value):
        raw = str(value or "").strip().strip("[]")
        if not raw:
            return False
        if raw.lower() == "localhost":
            return True
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        return address.is_loopback or bool(mapped and mapped.is_loopback)

    def normalized(value):
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            hostname = urlsplit(f"//{raw}").hostname or raw
            address = ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            return ""
        return str(getattr(address, "ipv4_mapped", None) or address)

    def is_local_dashboard_request(client_host, request_host):
        if is_loopback(client_host):
            return True
        client_address = normalized(client_host)
        server_address = normalized(request_host)
        return bool(client_address and client_address == server_address)

    app = Quart("compat-real-quart-client-peer")

    @app.get("/peer")
    async def peer():
        raw = quart_request._get_current_object()
        wrapped = PluginRequest(raw)
        return {"quart_has_client": hasattr(raw, "client"), "remote_addr": raw.remote_addr,
                "scope_client": raw.scope["client"][0], "client_host": wrapped.client_host,
                "official_local": is_local_dashboard_request(wrapped.client_host, wrapped.headers.get("Host"))}

    response = await app.test_client().get("/peer", headers={"Host": "localhost", "X-Forwarded-For": "203.0.113.10"},
                                           scope_base={"client": ("127.0.0.1", 12345)})
    assert response.status_code == 200
    assert await response.get_json() == {"quart_has_client": False, "remote_addr": "127.0.0.1",
        "scope_client": "127.0.0.1", "client_host": "127.0.0.1", "official_local": True}
