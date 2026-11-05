# -*- coding: utf-8 -*-
"""文本类响应的 **gzip 压缩**（面板静态资源 + 插件页资源**共用**一份实现）。

## 为什么独立成模块

原来这段逻辑写在 `app.py` 里（`_gzip_response`），只能给 `/` 和 `/static/*` 用。
插件拓展页（尤其「陪伴小屋」那 7MB 首屏）走的是**另一条路由**
（`plugin_pages_web._serve`），够不着它 —— 于是那些资源一直是**未压缩**直出。

🔴 **实测代价**（2026-11-05，用户报"陪伴小屋加载太久"）：

    首屏 71 个文件 / **7.23 MB**
    bundle.css 单文件 **1,415,808 字节**，`curl -sI` 抓下来
    `content-length: 1415808`、**没有 content-encoding**

压缩后 **7.23 MB → 1.84 MB（省 75%）**，`bundle.css` 1383 KB → 238 KB（17%）。

⚠️ 一份实现给两处用，是刻意的：**两份必然漂移**（本项目已踩过多次
「同一个概念两条路径、后来只改了一条」）。

## 行为约定

* 只压**文本类**（`text/*`、JS/JSON/XML/SVG）—— 图片/woff2/gif 已经是压缩格式，
  再 gzip 纯属浪费 CPU；
* 小于 `MIN_BYTES` 不压（压完可能更大，而 HTTP 头反而多几十字节）；
* 压完**不比原始小**就放弃（如实回退，不硬塞）；
* 必须带 **`Vary: Accept-Encoding`** —— 否则中间的代理会把压缩版
  缓存给不支持 gzip 的客户端，得到一堆乱码。
"""
from __future__ import annotations

import gzip as _gzip
from typing import Any

#: 小于这个字节数不压：HTTP 头本身就几十字节，压了得不偿失
MIN_BYTES = 1024

#: 这些 MIME **不压**（已是压缩格式，或本来就是二进制）
_NEVER = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "font/woff",
        "font/woff2",
        "application/zip",
        "application/gzip",
        "application/octet-stream",
    }
)

#: 这些 MIME 虽然不以 `text/` 开头，但确实是文本
_TEXTUAL = frozenset(
    {
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/json",
        "application/manifest+json",
        "application/xml",
        "application/xhtml+xml",
        "image/svg+xml",
    }
)


def is_compressible(mimetype: str) -> bool:
    """这个 MIME 值不值得压。"""
    ct = (mimetype or "").split(";")[0].strip().lower()
    if not ct or ct in _NEVER:
        return False
    return ct.startswith("text/") or ct in _TEXTUAL


def _accepts_gzip(accept_encoding: str | None) -> bool:
    return "gzip" in (accept_encoding or "").lower()


#: **绝不能压**的 MIME（流式/长连接）。压缩要攒够数据块才吐，
#: 会把逐帧送达的内容**按帧攒住** —— 用户看到的是"卡住不动"，
#: 而且**不报错**，只表现为"变慢"（极易误判成网络问题）。
#: ⚠️ 官方 AstrBot 用 `GZipMiddleware`，其源码注释也显式说明了跳过 SSE。
_STREAMING = frozenset({"text/event-stream"})


def should_compress(mimetype: str, *, status_code: int = 200, accept_encoding: str | None = None) -> bool:
    """**一个地方决定"这个响应该不该压"**（纯函数，可直接单测）。

    🔴 抽成纯函数是刻意的（2026-11-05）：我原来把判断写在 `after_request`
       闭包里，而闭包**在测试里拿不到** ⇒ 只能走 HTTP 客户端断言 ⇒
       Quart 测试 client 序列化时会重建响应头，`Content-Encoding` 读不到 ⇒
       **注入反证不红 = 假判据**（`content-length` 已经是压缩后的值，
       但断言看不见，于是"守住了 SSE"是个谎言）。
       纯函数能被直接调用，判据才真的有效。
    """
    ct = (mimetype or "").split(";")[0].strip().lower()
    if ct in _STREAMING:
        return False
    if status_code != 200:
        return False
    if not _accepts_gzip(accept_encoding):
        return False
    return is_compressible(mimetype)


async def maybe_gzip(resp: Any, *, accept_encoding: str | None = None) -> Any:
    """就地把响应压成 gzip；不值得压时**原样返回**（不抛错、不改状态）。

    ⚠️ **Quart 的 `Response.get_data()` 是协程**（Flask 里是同步的）——
       不 `await` 会拿到 coroutine 对象，`len()` 直接 TypeError，
       然后被兜成 500「内部错误」，**静态文件全挂**。实测踩到过一次。

    Args:
        resp: Quart `Response`（或任何有 `get_data/set_data/headers/status_code` 的壳）。
        accept_encoding: 请求的 `Accept-Encoding` 头；不传就取 `quart.request` 的。

    Returns:
        同一个（可能已被压缩的）响应对象。
    """
    if resp.status_code != 200:
        return resp
    if not is_compressible(resp.headers.get("Content-Type", "")):
        return resp
    if accept_encoding is None:
        # 延迟取，避免本模块在 import 时就绑死请求上下文
        from quart import request as _request

        accept_encoding = _request.headers.get("Accept-Encoding")
    if not _accepts_gzip(accept_encoding):
        return resp
    if resp.headers.get("Content-Encoding"):
        return resp  # 上游已经压过了（别二次压）

    body = await resp.get_data()
    if len(body) < MIN_BYTES:
        return resp

    packed = _gzip.compress(body, compresslevel=6)
    if len(packed) >= len(body):
        return resp  # 压不动就别硬塞

    resp.set_data(packed)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(packed))
    resp.headers["Vary"] = "Accept-Encoding"
    return resp
