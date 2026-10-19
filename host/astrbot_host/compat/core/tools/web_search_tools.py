# -*- coding: utf-8 -*-
"""`astrbot.core.tools.web_search_tools` 兼容层 —— 网页搜索后端。

⭐ 依据：真 AstrBot `core/tools/web_search_tools.py`（v4.27.5，只读参考）。

## 调用方式（陪伴 7.0.0 `astrbot_host/news.py:516`）

```python
from astrbot.core.tools import web_search_tools as search_tools
raw = await search_tools._tavily_search(settings, {"query": ..., "max_results": ...})
for item in raw or ():
    item.get("url") ...          # ← 按 **dict** 读
```

⚠️ 注意它**两种读法都用**：`item.get(...)` 与 `getattr(item, ...)`，
所以返回的 `SearchResult` 必须是「**dict 子类**」——只有属性访问会在
`item.get` 那里炸 `AttributeError`，只有 dict 会让 `getattr` 拿不到值。
⇒ 这里用 `dict` 子类，属性名与键名一一对应。

## 与真实现的差异（如实记录）

* 真实现有一套 `_KeyRotator`（多 key 轮转 + 失败换 key）。我们**只用第一个 key**：
  自用场景通常一把 key；多 key 轮转的价值在"配额打满"，而那属于运维策略，
  不该由兼容层替用户决定（换 key 会改变"哪个账号被计费"，是**语义变化**）。
* 真实现的 `SearchResult` 是 `@pydantic.dataclasses.dataclass`；我们用 dict 子类
  （见上，两种读法都要支持）。
* **没有实现的**：`web_search` / `web_extract` 等**工具类**（真实现里它们是
  `@builtin_tool` 装饰的 FunctionTool，供模型直接调用）。陪伴**不走那条路** ——
  它只调这几个 `_xxx_search` 私有函数。缺工具类时若有人 import，会显式
  `AttributeError`（PEP 562 的默认行为），不会静默返回空。

## 🔴 未配置 key 时的行为（项目铁律）

每个后端在**没配 key 时抛 `ValueError`**，与真实现一致
（`web_search_tools.py:193-195` "Tavily API key is not configured"）。
**绝不返回空列表假装搜到 0 条** —— 那会让"没配搜索"表现成"今天没新闻"。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger("astrbot")

__all__ = [
    "SearchResult",
    "_tavily_search",
    "_bocha_search",
    "_brave_search",
    "_firecrawl_search",
    "_baidu_search",
    "_exa_search",
    "_anysearch_search",
]

#: 可重试的 HTTP 状态（照真实现 `_RETRYABLE_HTTP_STATUSES`）
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class SearchResult(dict):
    """一条搜索结果（**dict 子类** —— 见模块 docstring 的两种读法）。"""

    _FIELDS = ("title", "url", "snippet", "favicon")

    def __init__(
        self,
        title: str = "",
        url: str = "",
        snippet: str = "",
        favicon: str | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(
            title=title, url=url, snippet=snippet, favicon=favicon, **extra
        )

    # 属性读法（`getattr(item, "url")`）
    @property
    def title(self) -> str:
        return self.get("title") or ""

    @property
    def url(self) -> str:
        return self.get("url") or ""

    @property
    def snippet(self) -> str:
        return self.get("snippet") or ""

    @property
    def favicon(self) -> str | None:
        return self.get("favicon")

    def __repr__(self) -> str:
        return f"SearchResult(title={self.title!r}, url={self.url!r})"


def _first_key(settings: dict, name: str) -> str:
    """取第一个非空 key（统一处理 str / list 两种存法）。

    ⚠️ 真实现要求 list；陪伴 `news.py:529-533` 已经把它**规整成 list 或空**，
    但别处可能传裸字符串 ⇒ 两种情况都接受，避免 `IndexError`。
    """
    value = (settings or {}).get(name)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


async def _post_json(url: str, payload: dict, headers: dict) -> dict:
    """统一的 POST + JSON（`trust_env=True` 照真实现，让宿主代理生效）。"""
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                reason = await response.text()
                raise Exception(
                    f"{url} 搜索失败：{reason}，status: {response.status}"
                )
            return await response.json()


async def _tavily_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """Tavily 搜索（照真实现 `:177-235`）。"""
    key = _first_key(provider_settings, "websearch_tavily_key")
    if not key:
        raise ValueError("Error: Tavily API key is not configured in AstrBot.")
    data = await _post_json(
        "https://api.tavily.com/search",
        payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    return [
        SearchResult(
            title=item.get("title") or "",
            url=item.get("url") or "",
            snippet=item.get("content") or "",
            favicon=item.get("favicon"),
        )
        for item in data.get("results", [])
    ]


async def _bocha_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """博查搜索（照真实现 `:297-360`）。"""
    key = _first_key(provider_settings, "websearch_bocha_key")
    if not key:
        raise ValueError("Error: Bocha API key is not configured in AstrBot.")
    data = await _post_json(
        "https://api.bochaai.com/v1/web-search",
        payload,
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # 照真实现：显式关 brotli，规避 aiohttp >= 3.13.3 的解压不兼容
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        rows = data["data"]["webPages"]["value"]
    except (KeyError, TypeError):
        # ⚠️ 形状变了要显式报，不能当"0 条结果"
        logger.warning("博查搜索返回形状异常，已按空结果处理")
        return []
    return [
        SearchResult(
            title=item.get("name") or "",
            url=item.get("url") or "",
            snippet=item.get("snippet") or item.get("summary") or "",
        )
        for item in rows
    ]


async def _brave_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """Brave 搜索（照真实现 `:363-421`）。"""
    key = _first_key(provider_settings, "websearch_brave_key")
    if not key:
        raise ValueError("Error: Brave API key is not configured in AstrBot.")
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=payload,
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        ) as response:
            if response.status != 200:
                reason = await response.text()
                raise Exception(f"Brave 搜索失败：{reason}，status: {response.status}")
            data = await response.json()
    rows = data.get("web", {}).get("results", [])
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("description", ""),
        )
        for item in rows
    ]


async def _firecrawl_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """Firecrawl 搜索（照真实现 `:424-520`）。"""
    key = _first_key(provider_settings, "websearch_firecrawl_key")
    if not key:
        raise ValueError("Error: Firecrawl API key is not configured in AstrBot.")
    data = await _post_json(
        "https://api.firecrawl.dev/v2/search",
        payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    rows = data.get("data", [])
    if isinstance(rows, dict):
        rows = rows.get("web", [])
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("description") or item.get("snippet") or "",
        )
        for item in (rows or [])
    ]


async def _baidu_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """百度 AI 搜索（照真实现 `:549-600`）。"""
    key = (provider_settings or {}).get("websearch_baidu_app_builder_key", "")
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        raise ValueError("Error: Baidu AI Search API key is not configured in AstrBot.")
    data = await _post_json(
        "https://qianfan.baidubce.com/v2/ai_search/web_search",
        payload,
        {
            "Authorization": f"Bearer {key}",
            "X-Appbuilder-Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content") or item.get("abstract") or "",
        )
        for item in data.get("references", [])
    ]


async def _exa_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """Exa 搜索（照真实现 `:1024-1090`）。"""
    key = _first_key(provider_settings, "websearch_exa_key")
    if not key:
        raise ValueError("Error: Exa API key is not configured in AstrBot.")
    data = await _post_json(
        "https://api.exa.ai/search",
        payload,
        {"x-api-key": key, "Content-Type": "application/json"},
    )
    return [
        SearchResult(
            title=item.get("title") or "",
            url=item.get("url") or "",
            snippet=item.get("text") or item.get("summary") or "",
        )
        for item in data.get("results", [])
    ]


async def _anysearch_search(provider_settings: dict, payload: dict) -> list[SearchResult]:
    """AnySearch（照真实现 `web_search_tools.py` 对应实现）。"""
    key = _first_key(provider_settings, "websearch_anysearch_key")
    if not key:
        raise ValueError("Error: AnySearch API key is not configured in AstrBot.")
    data = await _post_json(
        "https://api.anysearch.ai/v1/search",
        payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    rows = data.get("results") or data.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("results") or rows.get("items") or []
    return [
        SearchResult(
            title=item.get("title") or item.get("name") or "",
            url=item.get("url") or item.get("link") or "",
            snippet=item.get("snippet") or item.get("content") or "",
        )
        for item in (rows or [])
    ]
