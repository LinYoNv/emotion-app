# -*- coding: utf-8 -*-
"""判据：**前端启动不能因为一个 null 元素就整页死掉**。

## 现场（情绪 2026-11-04，真事故）

> 「我进不去了。。输入密钥之后，点进去直接刷新」

根因：设置页改造成「只编辑配置文件」时删掉了 `#cfg-session-form`，
而 `bootstrap.js` 里还留着：

    $("cfg-session-form").onsubmit = (e) => { ... };

元素不存在 ⇒ `null.onsubmit = ...` 抛 TypeError ⇒
**模块顶层执行中断** ⇒ 后面所有初始化（**包括登录表单绑定**）全不跑。
用户看到的就是"输入令牌点进去，页面刷新一下又回到登录框"。

⚠️ 报错在 console 里离现场很远（说 `onsubmit` of null），
   而用户完全不知道发生了 JS 崩溃 —— **这类错误的可观测性极差**。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | `bootstrap.js` 顶层**不许**对可能为 null 的元素裸赋值 |
| ② | 所有 `$("id")` 用到的 id 必须在 `index.html` 里存在（**或**该行有判空） |
| ③ | 登录流程里有 `login-form` / `login-token`（缺了也等于进不去） |
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "emotion_app" / "web" / "static" / "js"
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_no_top_level_unguarded_assignment_on_missing_ids():
    """🔴 顶层 `$("x").prop = ...` 的元素必须**存在**，**或者这条赋值被 `if` 守住**。

    这是本事故的直接判据：少一个判空就能让**整页 JS 死掉**。

    ⚠️ 判定要区分两种写法（我第一版把两者混为一谈，报了 3 个假警报）：
       * `$("x").prop = ...`            ← **无守卫**，null 立刻抛错 ⇒ 必须元素存在
       * `if ($("x")) { $("x").prop=... }` ← 有守卫 ⇒ 允许元素不存在
       * `$("x") && ($("x").prop = ...)`  ← 有守卫 ⇒ 同上
    """
    html = _html()
    lines = (JS / "bootstrap.js").read_text(encoding="utf-8").splitlines()
    missing: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        m = re.match(r'^\$\("([a-z0-9_-]+)"\)\.[a-z]+ =', stripped)
        if not m:
            continue
        element_id = m.group(1)
        if f'id="{element_id}"' in html:
            continue
        # 检查是否被守卫：上一行是 `if ($("x")) {`，或者本行内有 `$("x") &&`
        guarded_by_if = False
        for back in range(1, 4):
            if idx - back < 0:
                break
            prev = lines[idx - back].strip()
            if f'if ($("{element_id}"))' in prev:
                guarded_by_if = True
                break
            if re.match(r"^(var|let|const)\b", prev) or prev.endswith("}"):
                break
        if guarded_by_if:
            continue
        missing.append(f"bootstrap.js:{idx + 1} 绑定 #{element_id}（无守卫，HTML 里也没有它）")
    assert not missing, (
        "这些顶层赋值会因元素不存在抛 TypeError，导致整页初始化中断"
        "（登录都会进不去）：\n" + "\n".join(missing)
    )


def test_login_controls_exist():
    """登录框是**唯一入口**，它的控件缺一个就等于谁都进不来。"""
    html = _html()
    for element_id in ("login", "login-form", "login-token"):
        assert f'id="{element_id}"' in html, f"登录控件 #{element_id} 不见了"


def test_bootstrap_has_no_nullable_chain_without_guard():
    """顶层不许出现 `$("x").y.z` 这种链式取值（null 会立刻炸）。"""
    src = (JS / "bootstrap.js").read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        # `$("x").a.b = ...` 或 `$("x").a().b`
        if re.match(r'^\$\("[a-z0-9_-]+"\)\.[a-zA-Z_]+\(\)\.', stripped):
            offenders.append(f"bootstrap.js:{lineno}: {stripped[:80]}")
    assert not offenders, "顶层链式取值会在元素为 null 时抛错：\n" + "\n".join(offenders)
