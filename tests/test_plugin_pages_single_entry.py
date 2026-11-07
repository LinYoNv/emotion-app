# -*- coding: utf-8 -*-
"""判据：插件拓展页**只能有一个入口**（左侧导航），页面里不再重复列一遍。

## 现场（情绪 2026-11-07）

> 「下面这一节扩展页也可以删了，左侧有了独立webui的显示地方下面这一节就不用保留了的」

原来两处都在列：
* **侧边栏**「插件页面」分组（`#nav-plugin-items`）← `renderNavPluginPages()`
* **插件页底部**「插件拓展页」列表（`#plugin-page-list`）← `loadPluginPages()`

两者**读同一份接口**（`/api/v1/plugins/pages`）、用**同一条过滤规则**
（有 `pages` 且 `state === "loaded"`）⇒ 必然一致，留着只是同一入口出现两次。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 页面里不许再有 `#plugin-page-list` |
| ② | 侧边栏分组必须还在（否则就真的没入口了） |
| ③ | iframe（`#plugin-frame`）必须还在 —— 删列表**不能连带删掉打开能力** |
| ④ | 数据仍要取（`state.pluginPages` 是 `openPluginPage()` 的查找表） |
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "emotion_app" / "web" / "static" / "js"
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"


def test_page_list_is_gone():
    """① 页面里的重复列表必须已删除。"""
    html = HTML.read_text(encoding="utf-8")
    assert 'id="plugin-page-list"' not in html, (
        "#plugin-page-list 还在 —— 拓展页又有两个入口了"
    )


def test_sidebar_group_still_lists_them():
    """② 侧边栏分组必须还在（它是唯一入口）。"""
    html = HTML.read_text(encoding="utf-8")
    assert 'id="nav-plugin-items"' in html, (
        "侧边栏插件页分组没了 —— 那就真的没有入口了"
    )
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    assert "export function renderNavPluginPages" in src, "侧边栏渲染函数被删了"


def test_iframe_still_opens():
    """③ **删列表不能连带删掉打开能力**（这是最容易误伤的一条）。

    列表只是"入口的第二种呈现"，真正打开页面的是 `#plugin-frame` +
    `openPluginPage()`（由 hash 路由触发）。删列表时若把 iframe 一起删了，
    侧边栏点进去就变成**空白**——而且不会有任何报错。
    """
    html = HTML.read_text(encoding="utf-8")
    assert 'id="plugin-frame"' in html, "iframe 被一起删了 —— 侧边栏点进去会空白"
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    assert "export function openPluginPage" in src, "打开函数被删了"


def test_pages_data_is_still_fetched():
    """④ 数据仍要取 —— `state.pluginPages` 是 `openPluginPage()` 的查找表。

    ⚠️ 删了列表就把整个 `loadPluginPages()` 删掉是**错**的：
       那会让 `openPluginPage()` 找不到页面元信息（标题、原始 URL）。
    """
    src = (JS / "views" / "plugins.js").read_text(encoding="utf-8")
    i = src.find("export async function loadPluginPages")
    assert i > 0, "loadPluginPages 被删了 —— openPluginPage 会拿不到页面元信息"
    seg = src[i:i + 2000]
    assert "state.pluginPages = pages" in seg, "不再记录 pluginPages 了"
    assert "/api/v1/plugins/pages" in seg, "不再取拓展页数据了"
