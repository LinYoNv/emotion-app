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

    def getlist(self, key: str) -> list[ValueT]:
        return [v for k, v in self._pairs if k == key]

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
        url = getattr(request_, "url", None)
        self.path: str = getattr(url, "path", "") if url else ""
        raw_headers = getattr(request_, "headers", {}) or {}
        self.headers = _Headers(
            {str(k).lower(): str(v) for k, v in dict(raw_headers).items()}
        )
        self.cookies: dict[str, str] = dict(getattr(request_, "cookies", {}) or {})
        self.content_type: str | None = self.headers.get("content-type")
        client = getattr(request_, "client", None)
        self.client_host: str | None = getattr(client, "host", None) if client else None
        self.path_params: dict[str, Any] = path_params or {}
        self.plugin_name: str | None = plugin_name
        self.username: str | None = username
        qp = getattr(request_, "query_params", None)
        pairs = list(qp.multi_items()) if qp is not None and hasattr(qp, "multi_items") else []
        self.query: PluginMultiDict[str] = PluginMultiDict[str](pairs)

    async def body(self) -> bytes:
        """读原始请求体（照真实现）。"""
        body = getattr(self._request, "body", None)
        if callable(body):
            return await body()
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
