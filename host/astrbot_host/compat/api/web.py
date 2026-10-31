# -*- coding: utf-8 -*-
"""`astrbot.api.web` 兼容层 —— 插件 Web API 的请求对象。

⭐⭐ 依据：真 AstrBot `api/web.py`（453 行）。

🔴 **为什么必须实现**：实测 `astrbot_plugin_together_companion/main.py:33`
   `from astrbot.api.web import request` —— 缺这个模块**插件直接加载失败**。

⚠️ **与真实现的差异（如实记录）**：真实现基于 **FastAPI/Starlette**
   （`jsonable_encoder` / `FileResponse` / `StarletteUploadFile` …）。
   本项目不引入 web 框架之外的重依赖，所以这里：
   * ✅ 实现**被插件用到的核心**：`request` 代理 + `PluginRequest` + `PluginMultiDict`
   * ✅ 用 `contextvars` 保持与真实现**相同的注入机制**
     （`_request_var` + 代理读当前上下文 —— 这样多请求并发不会串）
   * ⚠️ **未实现**：`jsonable_encoder` / `FileResponse` / `JSONResponse` /
     `StreamingResponse` / `PluginUploadFile`（依赖 Starlette 类型）
     —— 访问它们会得到 **ImportError**（不静默给假对象）。

   判据：插件在**没有真正挂载 Web API** 时不会碰这些；
   等我们把 190 个路由真正挂载时再补。
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

ValueT = TypeVar("ValueT")
DefaultT = TypeVar("DefaultT")

#: 当前请求上下文（照真实现的机制 —— contextvar 而非全局变量）
_request_var: contextvars.ContextVar["PluginRequest"] = contextvars.ContextVar(
    "astrbot_plugin_web_request"
)


class PluginMultiDict(Generic[ValueT]):
    """保留重复键的类字典（照真实现 —— 请求参数可能同名多个）。"""

    def __init__(self, pairs: list[tuple[str, ValueT]]) -> None:
        self._pairs = pairs

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self._pairs:
            if k == key:
                return v
        return default

    def __getitem__(self, key: str) -> ValueT:
        """按 key 取**第一个**值（真 AstrBot 的 `PluginMultiDict` 就是这么用的）。

        🔴🔴 **2026-10-30 补的真 bug**：原来没有 `__getitem__` ⇒ 官方插件里
        所有 `query[key]` 形式（如 `acquaintance_api._route_query` 的
        `{key: query[key] for key in PERSON_ROUTE_KEYS}`）都会抛
        `TypeError: object is not subscriptable` ⇒ 被上层 catch 成 500/400，
        页面看到的就是“权限读取失败/待读取”。

        ⚠️ 缺键时**必须抛 KeyError**（不是返回 None）：官方代码用
        `query.keys()` 先判断存在性，靠 KeyError 暴露被绕过的情况。
        """
        for k, v in self._pairs:
            if k == key:
                return v
        raise KeyError(key)

    def getlist(self, key: str) -> list[ValueT]:
        return [v for k, v in self._pairs if k == key]

    def __bool__(self) -> bool:
        """空查询字典是 **falsy** —— 官方多处写 `if request.query:` 做“不允许带参”的校验。"""
        return bool(self._pairs)

    def keys(self) -> list[str]:  # type: ignore[override]
        return [k for k, _ in self._pairs]

    def __contains__(self, key: str) -> bool:
        return any(k == key for k, _ in self._pairs)

    def __iter__(self) -> Iterator[tuple[str, ValueT]]:
        return iter(self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)

    def items(self) -> list[tuple[str, ValueT]]:
        return list(self._pairs)

    def __repr__(self) -> str:
        return f"PluginMultiDict({self._pairs!r})"


@dataclass
class _Headers:
    """最小 headers 包装（真实现用 Starlette 的 `Headers`）。

    ⚠️ 只实现 `.get()`（大小写不敏感）—— 插件实际只用到这个。
    """

    _raw: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key.lower(), default)

    def __contains__(self, key: str) -> bool:
        return key.lower() in self._raw

    def __getitem__(self, key: str) -> str:
        return self._raw[key.lower()]


def _query_pairs(request_: Any) -> list[tuple[str, str]]:
    """取出**保留重复键与顺序**的查询参数。

    🔴🔴 **2026-10-30 修真 bug：原来永远返回空列表**。

    原实现读的是 `request_.query_params`（Starlette/真 AstrBot 的形状），
    而 Quart 的 `Request` **没有这个属性**，`getattr(..., None)` 得到 None
    ⇒ `pairs` 恒为 `[]` ⇒ 插件收到的 `request.query` **永远是空的**。

    后果不是报错而是**静默取不到参数**（本项目最贵的一类 bug）：
    陪伴页 `acquaintance/authorization?subject_id=...` 被后端判成
    「query 里没有 subject_id」→ 返回 `acquaintance_request_invalid`
    → 页面「身份与权限」永远停在“待读取”、按钮永远禁用
    （实测截图：无法设为核心用户、无法开放主动消息）。

    ✅ 三种形状都收：Starlette 的 `query_params.multi_items()`、
       Werkzeug/Quart 的 `args.items(multi=True)`、以及裸 dict。
    ⚠️ **必须保序且保留重复键** —— 插件用 `len(query.getlist(k)) != 1`
       来拒绝重复参数，合并去重会**静默放宽**它的校验。
    """
    qp = getattr(request_, "query_params", None)
    multi = getattr(qp, "multi_items", None)
    if callable(multi):
        return [(str(key), str(value)) for key, value in multi()]

    args = getattr(request_, "args", None)
    items = getattr(args, "items", None)
    if callable(items):
        try:
            return [(str(key), str(value)) for key, value in items(multi=True)]
        except TypeError:  # 不是 multi 版本就退回单值
            return [(str(key), str(value)) for key, value in items()]
    if isinstance(args, dict):
        return [(str(key), str(value)) for key, value in args.items()]
    return []


def _client_host(request_: Any) -> str | None:
    """取发起方 IP（Starlette 用 `client.host`，Quart/ASGI 用 `remote_addr`）。

    ⚠️ 拿不到时**返回 None**（不是空串）：官方 `_is_local_dashboard_request`
       把 None 当"非本机"处理，这是它自己的既定语义，我们不改。
    """
    client = getattr(request_, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if host:
        return str(host)
    remote = getattr(request_, "remote_addr", None)
    if remote:
        return str(remote)
    scope = getattr(request_, "scope", None)
    if isinstance(scope, dict):
        peer = scope.get("client")
        if isinstance(peer, (tuple, list)) and peer:
            return str(peer[0])
    return None


class PluginRequest:
    """暴露给插件 Web API 处理器的请求对象（照真实现字段）。"""

    def __init__(
        self,
        request_: Any,
        *,
        path_params: dict[str, Any] | None = None,
        plugin_name: str | None = None,
        username: str | None = None,
    ) -> None:
        self._request = request_
        # 从底层请求对象提取（**属性名照真实现**）
        self.method: str = getattr(request_, "method", "GET")
        # 🔴 `request.path`（不是 `request.url.path`）：Starlette 的 URL 有
        #    `.path`，而 **Quart 的 `url` 是普通 `str`** ⇒ 取 `.path` 只会拿到
        #    默认值，插件读 `request.path.startswith(...)` 的判断会恒为 False。
        self.path: str = str(
            getattr(request_, "path", None)
            or getattr(getattr(request_, "url", None), "path", "")
            or ""
        )
        raw_headers = getattr(request_, "headers", {}) or {}
        self.headers = _Headers(
            {str(k).lower(): str(v) for k, v in dict(raw_headers).items()}
        )
        self.cookies: dict[str, str] = dict(getattr(request_, "cookies", {}) or {})
        self.content_type: str | None = self.headers.get("content-type")
        # 🔴 **2026-10-30 修第三个静默空值**：Quart（ASGI）请求**没有 `client`**
        #    属性，只有 `remote_addr`（以及 `scope["client"]`）。原来只读
        #    `request_.client.host` ⇒ `client_host` **恒为 None** ⇒ 官方
        #    `together/common.py:_is_local_dashboard_request` 判定"不是本机"
        #    ⇒ `browser_launch_available=False` ⇒ **本机浏览器唤起被静默降级**。
        #    实测：`scope['client']==('127.0.0.1', 12345)`、`remote_addr=='127.0.0.1'`。
        self.client_host: str | None = _client_host(request_)
        self.path_params: dict[str, Any] = path_params or {}
        self.plugin_name: str | None = plugin_name
        self.username: str | None = username
        self.query: PluginMultiDict[str] = PluginMultiDict[str](_query_pairs(request_))

    async def body(self) -> bytes:
        """读原始请求体（照真实现）。

        🔴🔴 **2026-10-29 修真 bug：原来永远返回 `b""`**。

        原实现是：

        ```python
        body = getattr(self._request, "body", None)
        if callable(body):
            return await body()      # ← 在 Quart 上永远不会走到
        return b""
        ```

        **两个错叠在一起**：

        ① Quart 的 `Request` **没有** `body` 方法，走的是 `Request.data` /
           `get_data()`；`getattr(request, "body")` 拿到的是
           `quart.wrappers.request.Body`（一个**可 await 的容器**，
           `quart/wrappers/request.py:35`）。
        ② 那个 `Body` 类**只实现了 `__await__`，没有 `__call__`**
           ⇒ `callable(body)` 是 **False** ⇒ 直接落到 `return b""`。

        实测证据（2026-10-29，`confirm_binding` 打不开）：

        ```
        [DIAG] PluginRequest.body: inner_type=LocalProxy body_attr=Body callable=False
        [DIAG] _body 收到 0 字节: b'' | Content-Length='2'
        ```

        上层看到的现象是 `setup_binding_confirmation_failed`
        / "the pending private window could not be confirmed" ——
        **和"没绑定"一模一样**，所以极难查（正是本项目最贵的静默 bug）。

        ✅ 正确写法：先按 **`get_data()`**（Quart/Flask 通用、返回 bytes），
           再兼容"可调用"和"可直接 await 的 Body 容器"两种形状。
        """
        req = self._request

        # ① Quart / Flask 通用：get_data() 返回 bytes（首选，语义最明确）
        get_data = getattr(req, "get_data", None)
        if callable(get_data):
            try:
                data = get_data()
                if hasattr(data, "__await__"):
                    data = await data
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
            except Exception:  # noqa: BLE001 — 读不到就往下试别的形状
                pass

        # ② 有 body() 方法（Starlette/FastAPI 形状）
        body = getattr(req, "body", None)
        if callable(body):
            out = body()
            if hasattr(out, "__await__"):
                out = await out
            if isinstance(out, (bytes, bytearray)):
                return bytes(out)

        # ③ Quart 的 Body 容器：可 await（**不可调用**）
        if body is not None and hasattr(body, "__await__"):
            out = await body
            if isinstance(out, (bytes, bytearray)):
                return bytes(out)

        # ④ 已缓存成属性的 data（Flask 形状）
        data = getattr(req, "data", None)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)

        return b""

    async def json(self, default: Any = None) -> Any:
        """读 JSON 体（照真实现 —— 解析失败返回 default，不抛）。"""
        import json as _json

        try:
            raw = await self.body()
            return _json.loads(raw) if raw else default
        except Exception:  # noqa: BLE001
            return default

    def __repr__(self) -> str:
        return f"PluginRequest({self.method} {self.path})"


class PluginRequestProxy:
    """指向当前插件 Web 处理器的类型化代理（照真实现）。

    ⚠️ 不在处理器内访问会 **抛 RuntimeError**（照真实现）——
       这比返回 None 好：静默 None 会让插件以为"没参数"。
    """

    def _get_current(self) -> PluginRequest:
        try:
            return _request_var.get()
        except LookupError as exc:
            raise RuntimeError(
                "astrbot.api.web.request is only available inside a plugin "
                "Web API handler."
            ) from exc

    @property
    def method(self) -> str:
        return self._get_current().method

    @property
    def path(self) -> str:
        return self._get_current().path

    @property
    def headers(self) -> _Headers:
        return self._get_current().headers

    @property
    def cookies(self) -> dict[str, str]:
        return self._get_current().cookies

    @property
    def content_type(self) -> str | None:
        return self._get_current().content_type

    @property
    def client_host(self) -> str | None:
        return self._get_current().client_host

    @property
    def path_params(self) -> dict[str, Any]:
        return self._get_current().path_params

    @property
    def query(self) -> PluginMultiDict[str]:
        return self._get_current().query

    @property
    def plugin_name(self) -> str | None:
        return self._get_current().plugin_name

    @property
    def username(self) -> str | None:
        return self._get_current().username

    async def body(self) -> bytes:
        return await self._get_current().body()

    async def json(self, default: Any = None) -> Any:
        return await self._get_current().json(default)


#: 全局代理单例（照真实现 `web.py:322`）
request: PluginRequestProxy = PluginRequestProxy()


def bind_request(req: PluginRequest) -> contextvars.Token:
    """把请求绑定到当前上下文（宿主挂载 Web API 时调用）。"""
    return _request_var.set(req)


def unbind_request(token: contextvars.Token) -> None:
    _request_var.reset(token)


__all__ = [
    "PluginMultiDict",
    "PluginRequest",
    "PluginRequestProxy",
    "PluginResponse",
    "bind_request",
    "bind_request_context",
    "error_response",
    "file_response",
    "json_response",
    "request",
    "stream_response",
    "unbind_request",
]

# ---------------------------------------------------------------------------
# 响应助手（照真实现 `web.py:342-453`）
# ---------------------------------------------------------------------------
#
# 🔴 **实测踩到**：`astrbot_plugin_nai_image` import `error_response` ——
#    缺它插件加载失败。所以这几个助手**必须实现**。
#
# ⚠️ 真实现返回 Starlette 的 `JSONResponse` / `FileResponse` / `StreamingResponse`。
#    我们不引入 Starlette，所以用一个**最小响应对象**：
#    带 `.status_code` / `.body` / `.headers` / `.media_type`，
#    并实现 `__iter__`（Starlette 响应可迭代）——
#    等宿主真正挂载 Web API 时，由适配层把它转成 Quart `Response`。


@dataclass
class PluginResponse:
    """最小响应对象（等价于真实现里的 Starlette Response）。

    ⚠️ **不是** Starlette 的类 —— 但字段与迭代行为对齐，
       宿主挂载 Web API 时转成 Quart Response 即可。
    """

    body: bytes = b""
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None

    def __iter__(self) -> Iterator[bytes]:
        # Starlette 的 Response 可迭代（ASGI body 分块）
        yield self.body

    def __len__(self) -> int:
        return len(self.body)

    def __repr__(self) -> str:
        return f"PluginResponse({self.status_code}, {len(self.body)} bytes)"


def json_response(
    data: Any = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> PluginResponse:
    """构造 JSON 响应（照真实现签名与信封）。"""
    import json as _json

    payload = {} if data is None else data
    body = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    hdrs = {"content-type": "application/json"}
    if headers:
        hdrs.update(headers)
    return PluginResponse(
        body=body, status_code=status_code, headers=hdrs,
        media_type="application/json",
    )


def error_response(
    message: str,
    *,
    status_code: int = 400,
    data: Any = None,
    headers: dict[str, str] | None = None,
) -> PluginResponse:
    """构造标准错误响应（**照真实现的信封格式**）。

    信封：`{"status": "error", "message": ..., "data": ...}`
    """
    return json_response(
        {"status": "error", "message": message, "data": data},
        status_code=status_code,
        headers=headers,
    )


def file_response(
    path: str,
    *,
    filename: str | None = None,
    media_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> PluginResponse:
    """构造文件响应（照真实现签名）。

    ⚠️ 真实现用 Starlette `FileResponse`（惰性读盘）；我们**立即读进内存** ——
       对小图/小文件无差别，大文件会占内存（记在此处备查）。
    """
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.is_file():
        # 照真实现语义：文件不存在应报错（不静默返回空响应）
        raise FileNotFoundError(f"File not found: {path}")
    body = p.read_bytes()
    hdrs = {"content-type": media_type or "application/octet-stream"}
    if filename:
        hdrs["content-disposition"] = f'attachment; filename="{filename}"'
    if headers:
        hdrs.update(headers)
    return PluginResponse(body=body, status_code=200, headers=hdrs, media_type=media_type)


def stream_response(
    content: Any,
    *,
    media_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> PluginResponse:
    """构造流式响应（照真实现签名）。

    ⚠️ 真实现返回 Starlette `StreamingResponse`（真流式）；
       我们**先收集再返回**（语义一致，只是不增量）。
       已在此显式记录，避免被误认为真流式。
    """
    chunks: list[bytes] = []
    if isinstance(content, (bytes, bytearray)):
        chunks.append(bytes(content))
    elif isinstance(content, str):
        chunks.append(content.encode("utf-8"))
    else:
        for chunk in content:
            chunks.append(
                chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
            )
    hdrs = {"content-type": media_type or "application/octet-stream"}
    if headers:
        hdrs.update(headers)
    return PluginResponse(
        body=b"".join(chunks), status_code=200, headers=hdrs, media_type=media_type
    )


#: 🔴 真实现的同名函数（`web.py:325-339`）—— 它是 **`@contextmanager`**，
#:    用法 `with bind_request_context(req): ...`，
#:    **不是**返回 Token 的普通函数。原先少写了装饰器与 `yield` →
#:    宿主侧照真实现写 `with ...` 会直接 `AttributeError: __enter__`。
@contextmanager
def bind_request_context(req: PluginRequest) -> Iterator[PluginRequest]:
    token = _request_var.set(req)
    try:
        yield req
    finally:
        _request_var.reset(token)
