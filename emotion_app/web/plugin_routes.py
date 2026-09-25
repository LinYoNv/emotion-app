# -*- coding: utf-8 -*-
"""插件 Web API → Quart 路由的**挂载**（宿主侧协议转换）。

## 为什么需要这一层

插件 handler 的返回值**五花八门**（实测真实现的 `_coerce_view_result`，`asgi_runtime.py:477`）：

    dict / list        → JSON 响应
    PluginResponse     → 按它的 status/headers/body 返回
    Quart Response     → 原样返回
    (content, 状态码)   → 元组解包
    其它                → 当字符串

而我们的服务是 **Quart**。所以这里做**唯一的转换点**，
让插件完全不用知道自己跑在什么宿主上（照作者 §7「传输可换、语义不变」）。

## 两种取 request 的方式，都必须支持

实测插件有**两种**写法，缺一种就有一批插件挂掉：

| 写法 | 用它的插件 | 在 Quart 宿主下 |
|---|---|---|
| `from quart import request` | private_companion / content_companion | ✅ **天然可用**（我们本体就是 Quart） |
| `from astrbot.api.web import request` | together / memory / nai_image | 靠 `bind_request_context` 绑定 contextvar |

⭐ 有意思的是：真 AstrBot 是 FastAPI，为了第一种写法要
**伪造一个 Quart 请求上下文**（`bind_quart_request_context`，`asgi_runtime.py:567`，
里面 `quart_app.test_request_context(...)` 一大段）；
而我们**本来就是 Quart** → 这一块白送，反而比真实现更直接。
"""
from __future__ import annotations

from typing import Any

from quart import Response, jsonify

# 🔴 **必须延迟导入** `astrbot_host`：它是**并列的交付物**（不在 emotion_app 包内），
#    路径由 `plugin_host._ensure_host_on_path()` 在启用插件时才注入。
#    顶层 import 会让"没启用插件"的部署和 401 项主项目测试**被迫依赖 host/**。
#    ⚠️ 也别写成相对导入 `.compat...` —— 那是"astrbot_host 在 emotion_app 包内"的错误假设，
#       实测会卡死在 import 阶段（报错还很难看出根因）。
PluginResponse: Any = None


def _plugin_response_cls() -> Any:
    """取 `PluginResponse` 类（延迟导入 + 只导一次）。"""
    global PluginResponse
    if PluginResponse is None:
        from astrbot_host.compat.api.web import PluginResponse as _PR

        PluginResponse = _PR
    return PluginResponse

#: 插件返回值里应当被当作"原始响应体"的类型（不当 JSON 再包一层）
_PASSTHROUGH = (Response, PluginResponse)


async def to_quart_response(result: Any) -> Response:
    """把插件 handler 的返回值转成 Quart `Response`（照真实现 `_coerce_view_result`）。

    🔴 **不静默吞类型**：拿不准的类型当字符串返回（与真实现一致），
       并在 header 里留痕，便于排查"插件到底返回了什么"。
    """
    # ---- 1) Quart 原生响应：直接用 ----
    if isinstance(result, Response):
        return result

    # ---- 2) 我们的 PluginResponse：按字段搬 ----
    PR = _plugin_response_cls()
    if isinstance(result, PR):
        return Response(
            result.body,
            status=result.status_code,
            headers=result.headers or None,
            content_type=result.media_type or result.headers.get("content-type"),
        )

    # ---- 3) 元组：(content, status) / (content, status, headers) ----
    if isinstance(result, tuple):
        content = result[0] if result else None
        status = next((x for x in result[1:] if isinstance(x, int)), 200)
        headers = next((x for x in result[1:] if isinstance(x, dict)), None)
        if isinstance(content, Response):
            content.status_code = status
            if headers:
                content.headers.update(headers)
            return content
        if isinstance(content, PR):
            return await to_quart_response(
                PR(
                    body=content.body,
                    status_code=status,
                    headers={**(content.headers or {}), **(headers or {})},
                    media_type=content.media_type,
                )
            )
        return _from_content(content, status, headers)

    # ---- 4) dict / list：JSON（真实现走 jsonable_encoder） ----
    if isinstance(result, (dict, list)):
        resp = jsonify(result)
        return resp

    # ---- 5) 其它：当字符串（真实现 `_response_from_content` 的兜底分支） ----
    if isinstance(result, (bytes, bytearray)):
        return Response(bytes(result), status=200)
    if result is None:
        # 插件 handler 没有 return（真实现返回 None 会走到 Response(content=None,...)）
        return Response(b"", status=200)
    return Response(str(result), status=200, content_type="text/plain; charset=utf-8")


def _from_content(
    content: Any, status: int, headers: dict[str, str] | None
) -> Response:
    """`_response_from_content` 的等价实现（`asgi_runtime.py:502`）。"""
    if isinstance(content, (dict, list)):
        resp = jsonify(content)
        resp.status_code = status
        if headers:
            resp.headers.update(headers)
        return resp
    if isinstance(content, (bytes, bytearray)):
        return Response(bytes(content), status=status, headers=headers)
    if content is None:
        return Response(b"", status=status, headers=headers)
    return Response(
        str(content),
        status=status,
        headers=headers,
        content_type="text/plain; charset=utf-8",
    )


def not_found_payload(plugin_path: str, hint: list[str] | None = None) -> dict[str, Any]:
    """插件路由未命中时的**标准答复**（照真实现的信封）。

    ⚠️ 带上相近路由提示 —— 否则用户只看到一句"未找到"，无从下手。
    """
    data: dict[str, Any] = {"path": plugin_path}
    if hint:
        data["similar"] = hint
    return {"status": "error", "message": "未找到该路由", "data": data}
