# -*- coding: utf-8 -*-
"""`astrbot.core.tools` —— 内置工具包（兼容层）。

⭐ 依据：真 AstrBot `core/tools/`（v4.27.5，只读参考）。

⚠️ **这是"包"而不是"模块"**：真实现里 `core/tools` 是目录，
插件既写 `from astrbot.core.tools.message_tools import ...`，
也写 `from astrbot.core.tools import web_search_tools as search_tools`。
两种写法都要能走通。
"""
from __future__ import annotations

from . import message_tools

__all__ = ["message_tools", "web_search_tools"]


def __getattr__(name: str):
    """惰性暴露 `web_search_tools`。

    🔴 **为什么惰性**：`web_search_tools` 里有一整排搜索后端（tavily / bocha /
    brave / firecrawl / exa / baidu），它们的实现依赖各自的 key 配置。
    在**包导入期**就把它拉进来会让"没配搜索"的宿主持有一堆无用状态。

    但惰性**不等于静默缺失**：真的 `import` 失败时，`__getattr__` 会抛
    `AttributeError` 并把原因带上 —— 插件那边的 `try/except ImportError`
    会看到它（`news.py:516` 就在 `fetch()` 里 import）。
    """
    if name == "web_search_tools":
        from . import web_search_tools as _mod

        return _mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
