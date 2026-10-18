# -*- coding: utf-8 -*-
"""面板前端的**设计令牌与静态资源装配**判据（源码级）。

## 为什么要有这一批
本轮 UI 改版踩到的三个真问题，**都不是"样式写错了"，而是"装配漏了"**：

  ① `--line` / `--text` / `--input-bg` 被 `app.css` 读，但**从来没被定义过**
     ⇒ `border: 1px solid` 解析成 `currentColor`，那圈描边是**亮灰的**。
     ⇒ 判据：**凡是被 `var()` 引用的自定义属性，必须在 `:root` 里有定义**。
  ② 同一个选择器 `.empty` 在文件里出现**两次**，后一条把前一条的
     `text-align:center` 覆盖掉 ⇒ 空态文字贴在左上角。
     ⇒ 判据：关键选择器的**声明块数量必须为 1**（"同一概念多条写入路径"）。
  ③ 自托管字体文件必须**真的存在**，且 `index.html` 必须引用它们 ——
     字体文件漏了不会报错，只会静默回落成系统字体。

⚠️ 这些都是**静态可判**的（读文件即可），所以放源码级测试而不是浏览器测试；
    真浏览器那一层在 `tools/ui_polish_check.py`（它证明"真的渲染出来了"）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "emotion_app" / "web" / "static"
CSS = STATIC / "app.css"
HTML = STATIC / "index.html"
FONTS = STATIC / "fonts"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# ---------------------------------------------------------------------------
# ① 令牌完整性
# ---------------------------------------------------------------------------


def test_every_referenced_custom_property_is_defined():
    """🔴 **凡被 `var()` 引用的令牌都必须有定义**。

    这是本轮抓到的真 bug：`--line` / `--text` / `--input-bg` 三个名字
    被配置文件那段样式读了，但全文件**没有一处定义它们** ——
    `var(--line)` 解析成空 ⇒ `border:1px solid` 变成 `currentColor`。
    ⚠️ 而且它**不会报错**、页面也照常打开，只是那圈描边比周围亮一档。
    """
    css = _strip_comments(_css())
    referenced = set(re.findall(r"var\(\s*--([a-zA-Z0-9-]+)", css))
    defined = set(re.findall(r"--([a-zA-Z0-9-]+)\s*:", css))
    missing = sorted(referenced - defined)
    assert not missing, (
        f"这些令牌被引用但从未定义（会静默解析成空）：{missing}"
    )


def test_design_tokens_exist():
    """令牌表的核心成员必须在（少一个就会有一片样式静默失效）。"""
    css = _css()
    for name in (
        "--bg", "--panel", "--panel-2", "--sunken", "--hover",
        "--border", "--border-strong",
        "--fg", "--muted", "--faint",
        "--accent", "--accent-soft", "--accent-line", "--on-accent",
        "--ok", "--warn", "--danger",
        "--radius", "--radius-sm",
        "--font-sans", "--font-mono",
        "--shadow", "--shadow-sm", "--shadow-lg",
        "--z-scrim", "--z-sidebar", "--z-modal",
    ):
        assert re.search(re.escape(name) + r"\s*:", css), f"令牌缺失：{name}"


def test_modal_z_index_is_above_sidebar_and_scrim():
    """🔴 层级必须真的自洽：**弹层要盖得住侧边栏与遮罩**。

    改版前 `.modal` 写死 `z-index: 20`，而 `.sidebar` 是 `var(--z-sidebar)=50`
    ⇒ 手机端抽屉开着时打开的弹层会被抽屉压住一半，
    表现和"按钮点了没反应"一模一样（用户只会以为坏了）。
    判据：`.modal` 必须用令牌，且栈序 弹层 > 侧边栏 > 遮罩。
    """
    css = _strip_comments(_css())
    assert re.search(r"\.modal\s*\{[^}]*z-index:\s*var\(--z-modal\)", css), (
        "`.modal` 没有用 `var(--z-modal)` —— 写死数字会与侧边栏层级脱节"
    )
    vals = {}
    for name in ("--z-scrim", "--z-sidebar", "--z-modal"):
        m = re.search(re.escape(name) + r":\s*(\d+)", css)
        assert m, f"缺少层级令牌 {name}"
        vals[name] = int(m.group(1))
    assert vals["--z-scrim"] < vals["--z-sidebar"] < vals["--z-modal"], (
        f"层级顺序不对：{vals}（应 遮罩 < 侧边栏 < 弹层）"
    )


# ---------------------------------------------------------------------------
# ② 选择器唯一性（防"同一概念多条写入路径"）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selector", [".empty", ".hint", ".panel", "button"])
def test_key_selectors_have_single_definition(selector: str):
    """🔴 关键选择器**只许有一个声明块**。

    实测事故：`.empty` 被定义了两次，后一条（`padding:20px 4px`，**没有**
    `text-align:center`）把前一条覆盖掉 ⇒ 空态文字贴在左上角，
    右侧一大片空白（真机截图确认）。
    ⚠️ CSS 里"重复定义"不报错、不警告，只按后来者生效 —— 所以必须有判据。
    """
    css = _strip_comments(_css())
    # 只数**顶格**的选择器（`^`），忽略 `.empty strong` 这类后代/子规则
    hits = re.findall(r"^" + re.escape(selector) + r"\s*\{", css, re.M)
    assert len(hits) == 1, (
        f"`{selector}` 有 {len(hits)} 个声明块（应为 1）—— 后者会静默覆盖前者"
    )


# ---------------------------------------------------------------------------
# ③ 自托管字体：文件 + 引用 + 许可
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname", [
    "geist-latin.woff2", "geist-latin-ext.woff2",
    "geist-mono-latin.woff2", "geist-mono-latin-ext.woff2",
])
def test_self_hosted_font_files_exist_and_are_woff2(fname: str):
    """字体文件必须真的在磁盘上，且是 woff2（`wOF2` 魔数）。

    ⚠️ 文件漏了**不会报错** —— 浏览器静默回落到系统字体，
    表现只是"看着不太一样"。所以要有判据。
    """
    p = FONTS / fname
    assert p.is_file(), f"自托管字体缺失：{p}"
    head = p.read_bytes()[:4]
    assert head == b"wOF2", f"{fname} 不是 woff2（魔数 {head!r}）"
    assert p.stat().st_size > 1024, f"{fname} 太小（{p.stat().st_size} B），可疑"


def test_font_faces_reference_self_hosted_files():
    """`@font-face` 必须指向 `/static/fonts/...`，**不许引外部源**。

    零构建 + 可能无外网的部署 ⇒ 引 `fonts.googleapis.com` 会静默失败
    （离线环境下文字直接回落，且控制台只有一条被忽略的网络错误）。
    """
    css = _css()
    srcs = re.findall(r"@font-face\s*\{[^}]*?src:\s*url\(([^)]+)\)", css, re.S)
    assert srcs, "没有任何 @font-face"
    for s in srcs:
        u = s.strip().strip('"').strip("'")
        assert u.startswith("/static/fonts/"), (
            f"@font-face 引了非自托管源：{u}（外部源在无网环境会静默失败）"
        )


def test_font_family_lists_cjk_fallbacks():
    """🔴 `--font-sans` 里**必须有中文字族**。

    自托管的是 latin/latin-ext 子集，**不含中文字形**；
    少了中文回落，中文会落到浏览器的默认字体（各平台不一致，观感很怪）。
    ⚠️ 本机实测 `fc-match "sans-serif:lang=zh-cn"` 命中的是
       **Noto Sans CJK SC**，所以这一串里尤其不能漏它。
    """
    css = _css()
    m = re.search(r"--font-sans:\s*([^;]+);", css)
    assert m, "找不到 --font-sans"
    fam = m.group(1)
    for need in ("PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei"):
        assert need in fam, f"--font-sans 缺少中文回落：{need}"
    assert fam.strip().startswith('"Geist"'), "Geist 必须在最前（否则英文也不用它）"


def test_font_license_is_present():
    """OFL 要求随分发保留许可 —— 这是**合规**判据，不是形式主义。"""
    lic = FONTS / "LICENSE.txt"
    assert lic.is_file(), "字体许可文件缺失（OFL 1.1 要求随分发保留）"
    text = lic.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text


# ---------------------------------------------------------------------------
# ④ 静态资源接线
# ---------------------------------------------------------------------------


def test_html_preloads_self_hosted_font_and_links_favicon():
    """`index.html` 必须预载字体、且给出 favicon。

    * 预载：不预载会先渲染系统字体再换 Geist（整页文字跳动）。
      ⚠️ `crossorigin` 不能省 —— 字体请求是 CORS 模式，少了会**下载两次**。
    * favicon：改版前 `/favicon.ico` 实测 **404**，浏览器每次开面板都打一条 404。
    """
    html = HTML.read_text(encoding="utf-8")
    assert 'href="/static/fonts/geist-latin.woff2"' in html, "没有预载自托管字体"
    m = re.search(r"<link[^>]*geist-latin\.woff2[^>]*>", html)
    assert m and "crossorigin" in m.group(0), (
        "字体预载缺 `crossorigin` —— 会导致重复下载"
    )
    assert re.search(r'<link[^>]*rel="icon"[^>]*>', html), "没有 favicon link"


def test_favicon_file_exists():
    """favicon 文件真的在（光有 link 没有文件还是 404）。"""
    p = STATIC / "favicon.svg"
    assert p.is_file(), "favicon.svg 缺失"
    assert p.read_text(encoding="utf-8").lstrip().startswith("<svg"), "favicon 不是 SVG"


@pytest.mark.parametrize("bid,what", [
    ("send", "发送"),
    ("new-session", "新会话"),
])
def test_primary_action_buttons_are_marked(bid: str, what: str):
    """🔴 **主操作按钮必须被标成 primary**，逐个点名的判据。

    ⚠️ 为什么不能只断言"HTML 里存在某个 primary"（注入验证当场抓到的假绿）：
       我第一版就是那么写的，于是把**发送按钮**的 primary 去掉之后判据**照样绿**
       （因为别的按钮还带着 primary）—— 它守的是"这个词出现过"，
       而不是"这个操作被标出来了"。**这正是"命名与目标不符"的判据**
       （本项目通则：判据要守**那件事**，不是守一个恰好相关的字面量）。
    """
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r'<button[^>]*id="' + re.escape(bid) + r'"[^>]*>', html)
    assert m, f"找不到按钮 #{bid}（{what}）"
    assert "primary" in m.group(0), (
        f"#{bid}（{what}）没有 primary —— 界面里最重要的操作会看不出是主操作"
    )


def test_primary_button_class_is_defined():
    """CSS 必须真的给出 `button.primary` 的样式。

    ⚠️ 只定义不用 = 白写；只用了没定义 = 主按钮静默退回普通按钮
    （页面照样打开，只是"最重要的那个操作"看不出来）。两边都要判。
    """
    css = _strip_comments(_css())
    assert re.search(r"button\.primary\s*\{", css), "CSS 里没有 button.primary"
    m = re.search(r"button\.primary\s*\{([^}]*)\}", css)
    body = m.group(1)
    assert "var(--accent)" in body, "primary 按钮没用强调色"
    assert "var(--on-accent)" in body, "primary 按钮的前景色没用 --on-accent（对比度会失守）"
    html = HTML.read_text(encoding="utf-8")
    assert 'class="primary"' in html, "HTML 里没有任何 primary 按钮"


def test_no_legacy_hardcoded_theme_in_js():
    """🔴 JS **不许把主题色写进内联样式**。

    改版前 `toast()` 里写 `el.style.color = "var(--muted)"` —— 把主题色
    抄进了 JS：样式表改主题时这处不跟着变，而且给"已保存"这类**成功**消息
    上了个次级文字色，语义也不对。
    判据：JS 里允许出现 `var(--danger)` 这类**语义**令牌，
    但不许出现 `var(--muted)` / `var(--fg)` / `var(--panel)` 这类**底色/前景**令牌
    （那些该由样式表决定），也不许出现裸色值。
    """
    # ⚠️ 2026-10-17：前端拆成 ES 模块 ⇒ 检查**全部模块**，不是某一个文件。
    _parts = [STATIC / "js" / "main.js", STATIC / "js" / "bootstrap.js"]
    _parts += sorted((STATIC / "js" / "core").glob("*.js"))
    _parts += sorted((STATIC / "js" / "views").glob("*.js"))
    js = "\n".join(p.read_text(encoding="utf-8") for p in _parts if p.is_file())
    # 🔴 取**整条语句的右侧**（到行尾的 `;`），不是只取紧跟在 `=` 后的那个字符串。
    #    原因（注入验证当场抓到）：改版前那处写的是**三元**——
    #        el.style.color = bad ? "var(--danger)" : "var(--muted)";
    #    只匹配 `= "..."` 的正则**看不见**它（等号后面是 `bad`，不是引号），
    #    于是注入 E2 时判据纹丝不动 = 假绿。
    #    通则：判据要覆盖**这条语句所有可能写出颜色的位置**，包括条件表达式。
    stmts = re.findall(r"\.style\.[a-zA-Z]+\s*=\s*([^;\n]*)", js)
    assert stmts, "没扫到任何内联样式赋值（正则或代码结构变了）"
    for v in stmts:
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", v), f"JS 内联样式里有裸色值：{v}"
        assert "rgba(" not in v and "rgb(" not in v, f"JS 内联样式里有裸色值：{v}"
        for bad in ("--muted", "--fg", "--bg", "--panel", "--border", "--sunken", "--hover"):
            assert bad not in v, (
                f"JS 内联样式里写了主题令牌 {bad}（应交给样式表）：{v}"
            )
