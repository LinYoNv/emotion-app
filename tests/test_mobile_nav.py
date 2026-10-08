# -*- coding: utf-8 -*-
"""面板 UI 结构判据（静态文件级 + 装配级）。

## 守什么（情绪 2026-09-27 截图报障）
「没有开关，打不开侧边栏」—— 手机 390×700 下用户**没有任何入口**能打开导航。

**根因**（真浏览器实测）：唯一的开关 `#sidebar-toggle` 住在 `.sidebar-head` 里，
而手机端 `.sidebar` 是 `transform: translateX(-100%)` 的抽屉
⇒ 抽屉关着时开关跟着跑到屏幕外（实测 `rect.x = -268`），
`document.elementFromPoint` 在那坐标上命中 **`null`**（那里什么都没有）。

⇒ 修法：在 `.content`（抽屉**之外**）加一个手机端顶栏 `#mobile-bar`，
开关 `#mobile-menu` 住在里面，永远在视口内。

## 为什么要有静态文件级判据（而不是只靠真浏览器脚本）
真浏览器脚本（`tools/mobile_sidebar_check.py`）是**端到端**证据、
`tools/` 里的东西不进 `pytest` CI；而这类"结构被改回去"的回归
（有人清理 DOM 时顺手删了顶栏、或把开关挪回侧边栏）单测能立刻抓到。
两层都要有：**这里守"结构与接线"，真浏览器守"真的能点开"。**

⚠️ 判据全部**读真实文件**（不是复制一份期望值）——
"同一个概念多处写死"必然漂移（本项目反复踩）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"
CSS = ROOT / "emotion_app" / "web" / "static" / "app.css"
JS = ROOT / "emotion_app" / "web" / "static" / "app.js"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _js() -> str:
    return JS.read_text(encoding="utf-8")


# ===========================================================================
# ① 结构：顶栏开关必须在**抽屉之外**
# ===========================================================================


def test_mobile_menu_button_exists_and_is_outside_sidebar():
    """🔴 手机端开关必须存在，且**不在** `<aside id="sidebar">` 里面。

    这是本 bug 的根因所在：开关住在被它控制的抽屉里，
    抽屉一关（`translateX(-100%)`）开关就跟着跑到屏幕外 ——
    用户**完全没有入口**（真浏览器实测 `rect.x=-268`、命中 `null`）。
    """
    html = _html()
    assert 'id="mobile-menu"' in html, "手机端没有开关（用户打不开导航）"

    # 取 <aside id="sidebar"> ... </aside> 区间，断言开关**不在**其中
    m = re.search(r'<aside id="sidebar".*?</aside>', html, re.S)
    assert m, "找不到侧边栏结构（页面结构变了？）"
    assert 'id="mobile-menu"' not in m.group(0), (
        "开关又被放进侧边栏了 —— 手机端抽屉一关它就会跟着跑到屏幕外"
    )
    # 而且它应该在 `.content` 里（抽屉之外）
    cm = re.search(r'<main id="main" class="content">.*?</main>', html, re.S)
    assert cm and 'id="mobile-menu"' in cm.group(0), (
        "开关不在内容区里 —— 必须放在抽屉之外才永远可见"
    )


def test_mobile_bar_is_hidden_by_default_and_shown_only_on_mobile():
    """顶栏默认隐藏（桌面端不需要第二条入口），只在 ≤640px 显示。"""
    html, css = _html(), _css()
    assert 'id="mobile-bar"' in html, "没有顶栏容器"
    # 默认隐藏
    assert re.search(r"\.mobile-bar\s*\{\s*display:\s*none", css), (
        "顶栏没有默认隐藏 —— 桌面端会多出一条重复入口"
    )
    # 手机媒体查询里显示
    mq = re.search(r"@media \(max-width: 640px\) \{(.*)", css, re.S)
    assert mq, "找不到 ≤640px 媒体查询"
    assert re.search(r"\.mobile-bar\s*\{[^}]*display:\s*flex", mq.group(1)), (
        "手机端媒体查询里没把顶栏显示出来 —— 手机用户仍看不到开关"
    )


def test_content_is_column_so_topbar_stacks_above_panels():
    """`.content` 必须是**列**方向，顶栏才能压在内容之上（而不是挤成侧栏）。

    ⚠️ 这条守的是真踩过的坑：`.content` 原来是 `display:flex`（默认 row），
       加顶栏后 iframe/面板会被挤成一条（同类事故：全局 `label{flex-direction:column}`
       把 `.switch-row` 排成竖排、每行 126px 撑爆面板）。
    """
    css = _css()
    block = re.search(r"\.content\s*\{([^}]*)\}", css)
    assert block, "找不到 .content 规则"
    assert "flex-direction: column" in block.group(1), (
        ".content 不是列方向 —— 顶栏会与面板并排（挤坏布局）"
    )
    cb = re.search(r"\.content-body\s*\{([^}]*)\}", css)
    assert cb, "没有 .content-body 规则（顶栏下的内容体会塌掉）"
    assert "flex: 1" in cb.group(1), ".content-body 没撑满剩余高度"


# ===========================================================================
# ② 接线：JS 必须真的把它接上
# ===========================================================================


def test_mobile_menu_is_wired_to_the_same_toggle():
    """🔴 开关必须接到**同一个** `toggleSidebar()`。

    只写 `if ($("mobile-menu"))` 而不加 `.onclick = ...` 就是一个死按钮
    （本项目最典型的"模块能工作 ≠ 被接上了"）。
    """
    js = _js()
    assert re.search(r'\$\("mobile-menu"\)\.onclick\s*=\s*toggleSidebar', js), (
        "顶栏开关没有绑定 toggleSidebar —— 那是个点不动的按钮"
    )


def test_both_toggles_share_one_aria_sync():
    """两个开关的 `aria-expanded` 必须由**同一处**同步。

    各写一遍必然漂移（本项目反复踩的「同一概念多条写入路径」）——
    实测过：改了一处、另一处不同步，屏幕阅读器会报错状态。
    """
    js = _js()
    assert "function syncSidebarAria(" in js, "没有统一的 aria 同步函数"
    body = re.search(r"function syncSidebarAria\(.*?\n\}", js, re.S)
    assert body, "找不到 syncSidebarAria 实现"
    assert "sidebar-toggle" in body.group(0) and "mobile-menu" in body.group(0), (
        "aria 同步没覆盖两个开关（会漂移）"
    )
    # open/closeDrawer 必须走这个函数，而不是各自 set 一个 id
    for fn in ("openDrawer", "closeDrawer"):
        m = re.search(rf"function {fn}\(\) \{{(.*?)\n\}}", js, re.S)
        assert m, f"找不到 {fn}"
        assert "syncSidebarAria(" in m.group(1), f"{fn} 没走统一同步"
        assert 'setAttribute("aria-expanded"' not in m.group(1), (
            f"{fn} 里又手写了一遍 aria —— 那就是第二条写入路径"
        )


def test_topbar_title_tracks_current_panel():
    """顶栏标题要跟随当前页（手机看不见导航，得知道自己在哪一页）。

    ⚠️ 更新点必须在 `showPanel()` 这个**唯一**的面板切换处 ——
       十几处 `switchToTab` 调用点各写一遍必然漂移。
    """
    js = _js()
    m = re.search(r"function showPanel\(name\) \{(.*?)\n\}", js, re.S)
    assert m, "找不到 showPanel"
    assert "mobile-bar-title" in m.group(1), (
        "showPanel 里没更新顶栏标题 —— 手机上不知道当前在哪一页"
    )


def test_sidebar_toggle_still_wired_for_desktop():
    """桌面端行为不许回归：侧边栏里那个开关仍要绑上。"""
    js = _js()
    assert '$("sidebar-toggle").onclick = toggleSidebar;' in js, (
        "桌面端开关被改坏了（防回归判据）"
    )
    assert '$("sidebar-scrim").onclick = closeDrawer;' in js, "遮罩点击关闭被改坏了"


def test_html_tags_are_balanced():
    """🔴 HTML 标签必须闭合 —— 加顶栏时新增了一个 `<div class="content-body">`。

    ⚠️ 未闭合的 div 会被浏览器"猜"着补，页面**看起来可能还行**，
       但 DOM 结构已经不是我们写的那个（后续 `querySelector` 会莫名其妙失配）。
       这条用纯解析器验，不依赖浏览器容错。
    """
    from html.parser import HTMLParser

    void = {"meta", "link", "br", "hr", "img", "input", "source", "area", "base", "col"}

    class _P(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[tuple[str, tuple[int, int]]] = []
            self.bad: list[str] = []

        def handle_starttag(self, tag, attrs):  # noqa: ANN001
            if tag not in void:
                self.stack.append((tag, self.getpos()))

        def handle_endtag(self, tag):  # noqa: ANN001
            if not self.stack:
                self.bad.append(f"多余的 </{tag}> @ {self.getpos()}")
                return
            if self.stack[-1][0] == tag:
                self.stack.pop()
                return
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    self.bad.append(
                        f"</{tag}> 之前有未闭合：{[s[0] for s in self.stack[i + 1:]]} "
                        f"@ {self.stack[i][1]}"
                    )
                    del self.stack[i:]
                    return

    p = _P()
    p.feed(_html())
    assert not p.bad, "HTML 有错配标签：" + "；".join(p.bad)
    assert not p.stack, "HTML 有未闭合标签：" + str([(t, pos) for t, pos in p.stack])


# ===========================================================================
# ③ 会话页：把「umo 怎么用」讲清楚（情绪 2026-09-27 的第二个问题）
# ===========================================================================


def test_session_page_explains_how_to_route_proactive_to_this_session():
    """🔴 会话页必须有「怎么让主动消息落到这个会话」的说明。

    守的是情绪的**实际困惑**：「主动消息只能给到 default 这个默认用户 umo 吗，
    不能我自己新建会话然后去陪伴通过 umo 添加吗」。

    真相是**本来就能**（新建会话自动分配 `sess-<hex>`，投递也按 umo 找会话 ——
    真机实测：消息落进目标会话、unread +1、不串、无孤儿），
    但界面**没说怎么做**，而截图里那条存量会话是 `default`
    ⇒ 看起来像"产品只支持 default"。**能力在、用户不知道 = 等于没有。**
    """
    html = _html()
    m = re.search(r'<details class="umo-howto">(.*?)</details>', html, re.S)
    assert m, "会话页没有「怎么让主动消息落到这个会话」的说明块"
    block = m.group(1)
    for needle, why in (
        ("default", "没点明前提：老会话沿用 default，得先换掉"),
        ("复制 umo", "没说怎么拿到 umo"),
        ("陪伴", "没说要去陪伴面板添加用户"),
        ("回落", "没说插件侧没用户时会落到兜底（用户会以为坏了）"),
    ):
        assert needle in block, f"说明块缺「{why}」（找不到 {needle!r}）"


def test_session_info_flags_non_exclusive_umo():
    """🔴 会话信息栏要标出"这个 umo 不是本条专属"（否则用户看不出区别）。

    ⚠️ 判据必须是**两条**（形状 + 共用数），只写"共用 ≥2"会在真实场景漏掉：
    实测 `default` 只挂在**一条**会话上时那条不成立，
    可它依然是**全局兜底** —— 正是用户困惑的那条。

    ⚠️ **不许硬编码 `"default"`**：全局回落值是配置项（`plugins.umo_user_id`），
       写死字面量在配置一改后会静默说错话。
    """
    js = _js()
    m = re.search(r"function refreshSessionInfo\(\) \{(.*?)\n\}", js, re.S)
    assert m, "找不到 refreshSessionInfo"
    body = m.group(1)
    assert "warn" in body, "信息栏没标出异常状态（用户看不见）"
    assert "sess-" in body, "没有按「专属形状」判断（只靠共用计数会漏掉全局兜底那条）"
    assert "state.sessions.filter" in body, "没有按现场会话列表统计共用数"
    # 🔴 只查**代码行**，不查注释 —— 注释里正在讨论"不要硬编码 default"，
    #    整块文本匹配会把那条说明误判成违规（第一版就这么假红了）。
    code_lines = [
        ln for ln in body.splitlines()
        if ln.strip() and not ln.strip().startswith(("//", "*", "/*"))
    ]
    code = "\n".join(code_lines)
    assert '"default"' not in code and "'default'" not in code, (
        "代码里硬编码了 default —— 全局回落值是可配置的，写死会静默说错话"
    )
    # title 要给出动作（否则用户知道有问题但不知道怎么办）
    assert "换一个专用 umo" in body, "提示里没说该怎么做"
