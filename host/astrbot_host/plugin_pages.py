# -*- coding: utf-8 -*-
"""插件**拓展页**服务 —— 让插件的配置面板真的打得开。

## 为什么必须有它（情绪报的 bug，根因就在这）

情绪原话：「插件的配置入口没有喵」。

根因不是插件没写页面，而是**我们只挂了 API、没挂页面**：

    上一轮我做的是：
        /api/plugins/extensions/<plugin_path>   ← 插件**数据**接口 ✅
    漏掉的是：
        /plugins/<plugin_id>/pages/<page_name>       ← 页面**入口配置**
        /plugins/<plugin_id>/pages/<page_name>/assets/<path>  ← 页面静态资源
        /astrbot_plugin_xxx/page/...            ← 页面 JS **硬编码**的接口根路径

    于是 `metadata.yaml` 里声明了 `pages:` 的插件（陪伴面板/QQ空间/一起房间）
    在面板里**完全没有入口**。

## 三条路由各解决什么

① **入口配置** `/plugins/<id>/pages/<name>`
   返回该页的基本信息（插件名、页名、标题、入口文件）。
   真实现（`plugin_page_service.get_plugin_page_entry_config`）还带 locale/i18n 与
   一次性 `asset_token`；我们是**单用户、同源**，不需要那套防越权令牌，
   但**保留同样的字段名**（前端/插件可能读）。

② **页面资源** `/plugins/<id>/pages/<name>/assets/<path:asset_path>`
   把 `<插件>/pages/<name>/` 下的文件发出去（HTML/CSS/JS/图片）。

   🔴 **安全**：必须**防目录穿越**（`../../etc/passwd`）。
      这里用 `resolve()` 后校验"仍在页面根目录内",并且**拒绝符号链接逃逸**。

③ **根路径兼容** `/<plugin_dir_name>/<path:rest>`
   插件页 JS 里**硬编码**了 `const HTTP_API = "/astrbot_plugin_private_companion/page"`
   （实测 `companion-panel/app.js:1`）——**不带任何前缀**。
   真 AstrBot 靠 iframe + postMessage 桥接绕开它；
   我们**同源直连**，所以直接在根路径上再挂一条，把请求转发到同一个宿主。
   ⚠️ 这条路由**必须排在静态/其它路由之后注册**，否则会抢走 `/static/...` 之类。

## 与真实现的差异（如实记录）

| | 真 AstrBot | 我们 |
|---|---|---|
| 页面加载 | iframe + `postMessage` 桥（`plugin_page_bridge.js`） | **同源直连**（就是自己的页面） |
| 资源鉴权 | 一次性 JWT `asset_token`（60s TTL） | 复用主服务会话鉴权 |
| 主题/locale 注入 | 改写 `<html data-theme>` + i18n context | 注入 `<meta name="color-scheme">` |

差异的原因是我们**单用户 + 同源**，那套令牌是为了跨源 iframe 防越权 ——
但插件页在**直连模式**下本来就走 `HTTP_API` 硬编码路径，所以①③必须都在。
"""
from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("emotion_app.plugins")

#: 页面入口文件名（照真实现 `PLUGIN_PAGE_ENTRY_FILE_NAME`）
PAGE_ENTRY_FILE = "index.html"
#: 页面根目录名（照真实现 `PLUGIN_PAGE_ROOT_DIR_NAME`）
PAGE_ROOT_DIR = "pages"


@dataclass
class PluginPage:
    """一个可访问的插件拓展页。"""

    plugin_id: str          # 插件目录名（= 路由里的 <plugin_id>）
    page_name: str          # 页名（= `metadata.yaml` 的 pages[].name）
    title: str
    entry_file: str = PAGE_ENTRY_FILE
    #: 页面根目录（`<插件>/pages/<page_name>`）
    root: Path = field(default_factory=Path)

    @property
    def entry_path(self) -> Path:
        return self.root / self.entry_file

    @property
    def available(self) -> bool:
        """入口 HTML 真的存在吗（不存在就不是"能打开的页面"）。"""
        return self.entry_path.is_file()


def discover_pages(
    records: list[Any],
    *,
    plugin_root_of: Any,
) -> list[PluginPage]:
    """从加载记录里收集**真正可打开**的拓展页。

    Args:
        records: `LoadReport.loaded`（`PluginRecord` 列表）。
        plugin_root_of: `(record) -> Path`，取该插件的源码根目录。

    ⚠️ **如实报告**：声明了 `pages:` 但 `index.html` 不存在时，
       不静默丢弃 —— 打 WARNING 并跳过（否则"页面呢？"无从解释）。
    """
    out: list[PluginPage] = []
    for rec in records:
        pages = getattr(rec, "pages", None) or []
        if not pages:
            continue
        try:
            root = Path(plugin_root_of(rec))
        except Exception:  # noqa: BLE001
            logger.warning("取插件根目录失败：%s", getattr(rec, "dir_name", rec))
            continue

        for spec in pages:
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            page = PluginPage(
                plugin_id=rec.dir_name,
                page_name=name,
                title=str(spec.get("title") or name),
                root=root / PAGE_ROOT_DIR / name,
            )
            if not page.available:
                logger.warning(
                    "插件 %s 声明了页面 %s，但找不到入口文件 %s —— 该页不可用",
                    rec.dir_name,
                    name,
                    page.entry_path,
                )
                continue
            out.append(page)
    return out


def resolve_page_asset(page: PluginPage, asset_path: str) -> Path | None:
    """把 `asset_path` 解析成页面根目录内的真实文件；越界/不存在返回 `None`。

    🔴 **目录穿越防护**（这是本模块唯一的安全关键点）：

        `Path(root) / "../../../../etc/passwd"` 会**跳出**页面目录。
        所以解析成绝对路径后必须再校验"仍在 root 之下"。
        另外 `resolve()` 也会展开符号链接 —— 插件若在页面目录里放一个
        指向 `/etc` 的软链，同样必须被挡住。

    ⚠️ 空路径 → 入口文件（照真实现：请求目录就当请求 `index.html`）。
    """
    raw = (asset_path or "").strip().lstrip("/")
    if not raw:
        raw = page.entry_file

    root = page.root.resolve()
    # 先解析，再判断归属 —— 顺序不能反（反了就是"先信任后校验"）
    try:
        target = (root / raw).resolve()
    except OSError:
        return None

    if target != root and root not in target.parents:
        logger.warning(
            "插件页资源请求越界，已拒绝：plugin=%s page=%s path=%r → %s",
            page.plugin_id,
            page.page_name,
            asset_path,
            target,
        )
        return None
    if not target.is_file():
        return None
    return target


def guess_mimetype(path: Path) -> str:
    """按扩展名猜 Content-Type（照真实现用 mimetypes）。"""
    ctype, _ = mimetypes.guess_type(str(path))
    if ctype:
        return ctype
    # `.js` 在部分系统上会被猜成 text/plain → 浏览器拒绝作为模块执行
    if path.suffix == ".js":
        return "text/javascript"
    if path.suffix == ".css":
        return "text/css"
    return "application/octet-stream"


def inject_page_context(
    html: str,
    *,
    is_dark: bool = True,
    bridge_src: str | None = None,
    plugin_id: str = "",
    page_name: str = "",
) -> str:
    """往页面 HTML 的 `<head>` 里注入三样东西（顺序都不能错）。

    ① `<meta name="color-scheme">` —— 照真实现注入主题提示。
    ② 🔴 **本页属于哪个插件**（`<meta name="emotion-plugin-id">`）—— 2026-09-27 补。
    ③ 🔴 **桥接垫片的 `<script src>`** —— 必须**最早**加载。

    为什么③必须存在（实测结论）：
        插件页 JS 有两条取数路径：
          ① `window.AstrBotPluginPage` 桥（`.apiGet/.apiPost/.ready`）——**首选**
          ② HTTP 直连（`HTTP_API` 硬编码）—— 仅 standalone/`?debug_http=1` 模式
        真 AstrBot 把页面塞进 **iframe**，用 `postMessage` 把①做成跨源桥；
        我们**同源**，所以给一个同源实现的垫片就够，插件代码一行都不用改。

        ⚠️ 不注入会怎样（我实测过）：插件退到 `isHttpApiMode()`，
           而那个模式要求 `?debug_http=1` 或 standalone 标记，两者都没有
           → 抛「未检测到 AstrBot 官方插件 Page 桥接」→ **页面空白**。

    为什么②必须存在（2026-09-27 实测的**根因修复**，不是给日志降噪）：
        插件的候选列表是 `page/x` → `x` → `/x` → **`<插件名>/page/x`** → …
        （`companion-panel/app.js:6702`）。垫片从前只拿得到**没有插件名**的
        `page/x`，只能拼成 `/api/plugins/extensions/page/x` → **必然 404**，
        然后靠插件自己一级一级重试。日志里于是出现成组的「插件路由未命中」
        （实测 19 条，每组 3 条 = 它试的前三级）。

        ⚠️ **更严重的后果不是噪音**：`一起房间` 的 `launcher.js:47`
        **只生成 `page/x` 一种候选、且没有重试** ⇒ 它**永远 404**
        （实测页面报「未找到该路由」），永远取不到数。

        ✅ 根因修法：**服务端在注入时就把"本页属于哪个插件"告诉垫片**
        （这个信息只有服务端有），垫片据此把候选**直接补全**成
        `<插件名>/page/x` —— 一次命中，不再靠盲试。
        ⚠️ **不改插件代码**（那是别人的东西，插件一升级就丢）。

    ⚠️ 只做**最小注入**：不动插件自己的任何 script/style。
    """
    head_end = _find_head_end(html)
    if head_end is None:
        return html

    parts = [f'<meta name="color-scheme" content="{"dark" if is_dark else "light"}">']
    if plugin_id:
        # ② 必须在③**之前**：垫片是同步脚本，执行时立刻读这两个 meta。
        # ⚠️ 属性值要转义 —— 插件目录名理论上不该含引号，但**不许假设**
        #    （含引号会撑破 meta 标签 → 垫片读不到 → 又回到盲试）。
        parts.append(
            f'<meta name="emotion-plugin-id" content="{_attr_escape(plugin_id)}">'
            f'<meta name="emotion-plugin-page" content="{_attr_escape(page_name)}">'
        )
    if bridge_src:
        # 放在最前：插件的 app.js 在自己的 `<script>` 里立刻就会查这个全局
        parts.append(f'<script src="{bridge_src}"></script>')
    return html[:head_end] + "".join(parts) + html[head_end:]


def _attr_escape(value: object) -> str:
    """HTML 属性值转义（`&` 必须先转，否则 `&quot;` 会被二次转义）。"""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _find_head_end(html: str) -> int | None:
    """返回 `<head ...>` 结束标签之后的位置（没有 head 返回 None）。"""
    lower = html.lower()
    idx = lower.find("<head")
    if idx == -1:
        return None
    end = html.find(">", idx)
    if end == -1:
        return None
    return end + 1
