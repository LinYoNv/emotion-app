# -*- coding: utf-8 -*-
"""插件 Web API 的**路由匹配**（逐行照真实现 `dashboard/api/plugins.py:146-180`）。

⭐⭐ 为什么这一段要**逐行照抄**而不是自己写个简单的：

   插件登记的路由是 **Flask 风格**的，支持两种参数写法：

       `/xxx/page/persona/<persona_id>`        → 单段参数（不跨 `/`）
       `/xxx/page/files/<path:file_path>`      → **贪婪**参数（可跨 `/`）

   🔴 我们实测过：陪伴插件的 190 条路由里**没有**参数路由，但
      `memory_companion` 等其它插件有 —— 自作聪明写个"按 `/` 切分"的匹配器，
      遇到 `<path:...>` 就会**静默匹配失败**（表现为 404，看起来像"路由没挂上"，
      排查成本极高）。

   → 所以正则、`lstrip("/")`、`re.escape`、`.upper()` 全部照抄，
     连**变量名与顺序**都保持一致，方便将来跟真实现逐行对拍。
"""
from __future__ import annotations

import re
from typing import Any

#: 照真实现 `plugins.py:153` 的正则 —— 匹配 `<name>` 与 `<path:name>`
_ROUTE_PARAM_RE = re.compile(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>")


def normalize_plugin_api_route(route: str) -> str:
    """确保路由以 `/` 开头（照真实现 `plugins.py:146-151`）。"""
    route = route.strip()
    if not route.startswith("/"):
        route = f"/{route}"
    return route


def plugin_api_route_pattern(route: str) -> str:
    """把插件登记的 Flask 风格路由编译成**正则**（照真实现 `plugins.py:153-164`）。

    * `<name>`         → `(?P<name>[^/]+)`  单段
    * `<path:name>`    → `(?P<name>.*)`     **贪婪**（可含 `/`）
    * 其余字符          → `re.escape` 原样转义
    """
    normalized = normalize_plugin_api_route(route)
    chunks: list[str] = []
    pos = 0
    for match in _ROUTE_PARAM_RE.finditer(normalized):
        chunks.append(re.escape(normalized[pos : match.start()]))
        name = match.group(2)
        # `path:` 前缀 → 贪婪匹配；否则只吃一段
        chunks.append(f"(?P<{name}>.*)" if match.group(1) else f"(?P<{name}>[^/]+)")
        pos = match.end()
    chunks.append(re.escape(normalized[pos:]))
    return "".join(chunks)


def match_registered_web_api(
    registered_web_apis: Any,
    subpath: str,
    method: str,
) -> tuple[Any, dict[str, Any]] | None:
    """在已登记的路由里找一条匹配 `subpath` + `method` 的（照真实现 `plugins.py:166-180`）。

    ⚠️ **顺序敏感**：真实现是**先到先得**（`for` 里第一个匹配就返回），
       不去找"最精确"的那条。我们照此办理 —— 改了顺序就可能让
       某条被遮蔽的路由行为与真 AstrBot 不一致。

    Args:
        registered_web_apis: 真实现那种 `(route, handler, methods, desc)` 元组列表。
        subpath: 去掉挂载前缀后的路径，如 `astrbot_plugin_x/page/overview`。
        method: HTTP 方法（大小写不敏感）。

    Returns:
        `(view_handler, path_params)`；没匹配到返回 `None`。
    """
    request_path = f"/{subpath.lstrip('/')}"
    request_method = method.upper()

    for route, view_handler, methods, _desc in registered_web_apis:
        allowed_methods = [item.upper() for item in methods]
        if request_method not in allowed_methods:
            continue

        pattern = plugin_api_route_pattern(route)
        matched = re.fullmatch(pattern, request_path)
        if matched:
            return view_handler, matched.groupdict()
    return None
