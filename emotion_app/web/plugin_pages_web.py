# -*- coding: utf-8 -*-
"""插件拓展页的 **Web 路由**（把 `astrbot_host.plugin_pages` 接进 Quart）。

分成单独文件而不是塞进 `app.py`，理由同 `plugin_routes.py`：
`app.py` 已经 600+ 行（本项目硬约束单文件 ≤1500 行），
页面服务有自己的安全关键点（目录穿越），单独放便于审。

## 三条路由

    ① GET /api/v1/plugins/pages
         列出所有可打开的拓展页（面板的"插件"页用它渲染入口）。

    ② GET /api/v1/plugins/<plugin_id>/pages/<page_name>
       GET /api/v1/plugins/<plugin_id>/pages/<page_name>/assets/<path:asset_path>
         页面本体与静态资源。无 asset_path 时发入口 `index.html`。
         ⚠️ 页面里的**相对路径**（`./app.js`）会自然解析到 `/assets/...`，
            因为浏览器是按"当前 URL 的目录"拼的 —— 这正是我们要的形状。

    ③ GET/POST/... /<plugin_dir_name>/<path:rest>
         🔴 插件页 JS **硬编码**了 `HTTP_API = "/astrbot_plugin_private_companion/page"`
            （实测 `companion-panel/app.js:1`，**不带前缀**）。
            真 AstrBot 靠 iframe+postMessage 桥绕开；我们同源直连，
            所以必须在根路径上再挂一条，把请求转给同一个宿主派发器。
         ⚠️ 这条是**兜底**，注册顺序在最后（`mount_plugin_page_routes` 的调用点
            在所有具体路由之后），但仍显式跳过已知的框架前缀以防万一。
"""
from __future__ import annotations

import logging
from typing import Any

from quart import Response, jsonify, redirect, request

logger = logging.getLogger("emotion_app.plugins")

#: 🔴 这些前缀**绝不**能被兜底路由吃掉（它们是框架/应用自己的命名空间）
RESERVED_PREFIXES = (
    "api",
    "static",
    "health",
    "v1",
    "favicon.ico",
)


def mount_plugin_page_routes(app: Any, service: Any, require_auth: Any) -> None:
    """把插件拓展页的三条路由挂到 `app` 上。"""
    # 🔴 **必须先保证 `host/` 在 `sys.path` 上**（实测踩到）：
    #    `PluginHostService` 只在 `start()` 时才调 `_ensure_host_on_path()`，
    #    而本函数在**挂路由阶段**（服务启动更早）就要 import。
    #    漏了这一步 → `ModuleNotFoundError: No module named 'astrbot_host'`
    #    → **服务直接起不来**（不是"插件页不可用"，是整个面板挂掉）。
    from .plugin_host import ensure_host_on_path

    ensure_host_on_path()

    from astrbot_host.plugin_pages import (
        guess_mimetype,
        inject_page_context,
        resolve_page_asset,
    )

    def _host_pages() -> list[Any]:
        host = service.host
        report = getattr(host, "report", None) or getattr(service, "report", None)
        return list(getattr(report, "pages", []) or [])

    # ---------------- ① 列表 ----------------

    @app.get("/api/v1/plugins/pages")
    async def list_plugin_pages():  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        if service.host is None:
            return jsonify({"pages": [], "message": "插件宿主未启动"}), 200
        return (
            jsonify(
                {
                    "pages": [
                        {
                            "plugin_id": p.plugin_id,
                            "page_name": p.page_name,
                            "title": p.title,
                            "url": f"/api/v1/plugins/{p.plugin_id}/pages/{p.page_name}",
                        }
                        for p in service.host.report.pages
                    ]
                }
            ),
            200,
        )

    # ---------------- ② 页面与资源 ----------------

    def _find(plugin_id: str, page_name: str) -> Any:
        pages = _host_pages()
        for p in pages:
            if p.plugin_id == plugin_id and p.page_name == page_name:
                return p
        return None

    async def _serve(plugin_id: str, page_name: str, asset_path: str = "") -> Any:
        """发页面文件；找不到/越界都**显式报错**（不静默返回空）。"""
        if service.host is None:
            return jsonify({"error": "插件宿主未启动", "type": "unavailable"}), 503

        page = _find(plugin_id, page_name)
        if page is None:
            return (
                jsonify(
                    {
                        "error": f"没有这个插件页：{plugin_id}/{page_name}",
                        "type": "not_found",
                        "available": [
                            f"{p.plugin_id}/{p.page_name}" for p in _host_pages()
                        ],
                    }
                ),
                404,
            )

        target = resolve_page_asset(page, asset_path)
        if target is None:
            # 越界和"不存在"返回同一状态码（**别泄露**目录结构），
            # 但服务端日志里已经区分记录了（见 resolve_page_asset）。
            return jsonify({"error": "资源不存在", "type": "not_found"}), 404

        data = target.read_bytes()
        ctype = guess_mimetype(target)

        # 入口 HTML 注入主题提示（照真实现注入 color-scheme）
        if target.suffix == ".html":
            try:
                html = data.decode("utf-8")
            except UnicodeDecodeError:
                html = data.decode("utf-8", errors="replace")
            data = inject_page_context(
                html,
                is_dark=True,
                # 🔴 注入**我们自己的**桥接垫片（同源 HTTP 版）。
                #    不给它，插件页会报「未检测到 AstrBot 官方插件 Page 桥接」→ 白屏。
                bridge_src="/static/plugin_page_bridge.js",
            ).encode("utf-8")
            ctype = "text/html; charset=utf-8"

        resp = Response(data, mimetype=ctype)
        # 🔴 插件页资源同样**不能长缓存** —— 否则插件升级后用户看到的还是旧页面
        #    （本项目实测事故：面板 CSS 缓存 12h 导致"改完看不见"）。
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # 🔴 **URL 必须以 `/` 结尾**（实测关键细节）：
    #    插件页 HTML 用的是**相对路径**（`./app.css`、`./js/features/x.js`）。
    #    浏览器按"当前 URL 的目录"解析：
    #      /pages/companion-panel   → 目录是 /pages/         → ./app.css 打到 ../app.css ❌
    #      /pages/companion-panel/  → 目录是 companion-panel/ → ./app.css 正确 ✅
    #    所以不带斜杠的那条**必须重定向**，否则插件页所有静态资源 404、页面白屏。
    @app.get("/api/v1/plugins/<plugin_id>/pages/<page_name>")
    async def plugin_page_entry(plugin_id: str, page_name: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        return redirect(f"/api/v1/plugins/{plugin_id}/pages/{page_name}/", code=308)

    @app.get("/api/v1/plugins/<plugin_id>/pages/<page_name>/")
    async def plugin_page_entry_slash(plugin_id: str, page_name: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        return await _serve(plugin_id, page_name, "")

    @app.get("/api/v1/plugins/<plugin_id>/pages/<page_name>/<path:asset_path>")
    async def plugin_page_asset(plugin_id: str, page_name: str, asset_path: str):  # type: ignore[no-untyped-def]
        denied = require_auth()
        if denied is not None:
            return denied
        return await _serve(plugin_id, page_name, asset_path)

    # ---------------- ③ 根路径兼容（插件页 JS 硬编码） ----------------

    @app.route("/<plugin_dir>/<path:rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def plugin_root_relative(plugin_dir: str, rest: str):  # type: ignore[no-untyped-def]
        """把 `/<插件名>/<路径>` 转发给插件 API 派发器。

        🔴 为什么必须存在：插件页 JS 里写死
            `const HTTP_API = "/astrbot_plugin_private_companion/page";`
            —— 请求会打到**根路径**，不加这条就只有 404。
        """
        if plugin_dir in RESERVED_PREFIXES:
            # 正常不该走到这（具体路由优先），但显式挡一道更安全
            return jsonify({"error": "not found", "type": "not_found"}), 404

        denied = require_auth()
        if denied is not None:
            return denied

        host = service.host
        if host is None:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "插件宿主未启动（查看服务日志）",
                        "data": {},
                    }
                ),
                503,
            )

        from .plugin_routes import not_found_payload, to_quart_response

        plugin_path = f"{plugin_dir}/{rest}".strip("/")
        try:
            result = await host.call_api(
                plugin_path,
                request_obj=request,
                method=request.method,
                username="owner",
            )
        except LookupError as exc:
            near = [
                a.route
                for a in (host.report.mounted if host.report else [])
                if a.route.startswith(f"/{plugin_dir}")
            ][:8]
            logger.warning(
                "插件根路径路由未命中：%s %s（%s）",
                request.method,
                plugin_path,
                exc,
            )
            return jsonify(not_found_payload(plugin_path, near)), 404
        except Exception:  # noqa: BLE001
            logger.exception("插件根路径 API 执行失败：%s", plugin_path)
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "插件执行失败（见服务日志）",
                        "data": {},
                    }
                ),
                500,
            )

        return await to_quart_response(result)
