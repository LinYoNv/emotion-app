# -*- coding: utf-8 -*-
"""判据：**导入的函数必须真的被用过** —— 防"写了但没接线"。

## 现场（情绪 2026-11-07）

> 「宿主适配器界面那些"编辑、添加适配器、刷新状态"按钮都没反应」

真相：`views/adapters.js` 写好了 **740 行**（渲染 / 表单 / 保存 / 删除全有），
`bootstrap.js` 也 **import 了 8 个函数** —— 但**一次都没调用**。
7 个函数在文件里各只出现 1 次，就是 import 那一行。

`git log -S` 证实：**这些绑定历史上从未存在过**，不是改坏的，是当初就没接。

⇒ 这正是本项目那条「模块能工作 ≠ 被接上了」的又一份现场。
   单测全绿、接口全通、页面能打开 —— **按钮还是死的**。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | `bootstrap.js` 从 `views/*.js` 导入的每个名字，**至少还要再出现一次** |
| ② | 页面里的按钮 id，`bootstrap.js` 里必须真有一处赋值/监听 |
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "emotion_app" / "web" / "static" / "js"
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"

#: 允许"只导入不用"的例外（写明理由；不要为了过判据而加，要真查清）
_ALLOWED = {
    # 目前没有例外。加之前先想清楚：真的用不上就**删掉 import**。
}


def _imports(src: str) -> dict[str, str]:
    """`{名字: 来源模块}` —— 只解析 `import { a, b } from "..."` 这一种。"""
    out: dict[str, str] = {}
    for m in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*"([^"]+)"', src):
        names, module = m.group(1), m.group(2)
        for raw in names.split(","):
            name = raw.strip().split(" as ")[-1].strip()
            if name:
                out[name] = module
    return out


def test_imported_view_functions_are_actually_called():
    """① 从 `views/*.js` 导入的名字，必须**至少再出现一次**（= 真用了）。

    ⚠️ 这是"写了但没接线"的最直接信号：
       import 了 8 个函数、每个只出现 1 次 ⇒ **整页交互全是死的**，
       但页面能打开、控制台零报错 —— 用户只会说"按钮没反应"。
    """
    src = (JS / "bootstrap.js").read_text(encoding="utf-8")
    orphans: list[str] = []
    for name, module in _imports(src).items():
        if "/views/" not in module or name in _ALLOWED:
            continue
        # 去掉所有 import 行再数，避免"import 行自己算一次"
        body = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("import ")
        )
        if not re.search(rf"\b{re.escape(name)}\b", body):
            orphans.append(f"{name}（来自 {module}）")
    assert not orphans, (
        "这些函数被 import 了但**从未调用** —— 对应按钮/交互是死的：\n  "
        + "\n  ".join(orphans)
    )


def test_adapter_buttons_are_wired():
    """② 适配器页的按钮必须真的绑上事件。

    🔴 这一条是本次报障的**直接判据**：
       按钮 id 都在 HTML 里、函数也都写了，唯独接线漏了。
       只查"函数存在"或"id 存在"都抓不住 —— 必须查**两者连起来**。
    """
    src = (JS / "bootstrap.js").read_text(encoding="utf-8")
    required = {
        "adapter-new": "添加适配器",
        "adapter-refresh": "刷新状态",
        "adapter-save": "保存",
        "adapter-cancel": "取消",
        "adapter-type": "类型切换",
    }
    missing = [f"#{k}（{v}）" for k in required
               if f'$("{k}")' not in src]
    assert not missing, (
        "这些适配器控件没有事件绑定 —— 点了没反应：\n  " + "\n  ".join(missing)
    )
    # 列表是动态重画的 ⇒ 必须用**事件委托**，不能逐行绑
    assert '$("adapter-list").onclick' in src, (
        "适配器列表没有事件委托 —— 行内「编辑/删除」点不动"
    )


def test_buttons_referenced_by_bootstrap_exist_in_html():
    """③ `bootstrap.js` 里 `$("x")` 用到的 id，HTML 里必须有（或那行判空）。

    ⚠️ 这是反向的：绑一个不存在的 id 会**静默无效**
       （`$(null).onclick = ...` 会抛错，但被某些写法兜住后就悄悄没了）。
    """
    html = HTML.read_text(encoding="utf-8")
    raw = (JS / "bootstrap.js").read_text(encoding="utf-8")
    # ⚠️ **必须先剔除注释行** —— 注释里举例写过 `$("x").onclick`，
    #    不剔除会把它当成真实绑定（我第一版就是这样，报了个假警报 `x`）。
    src = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    missing = sorted({
        m.group(1) for m in re.finditer(r'\$\("([a-z0-9_-]+)"\)', src)
        if f'id="{m.group(1)}"' not in html
    })
    # 允许：动态注入的容器（适配器/供应商列表行内的控件不在这里）
    dynamic_ok = {"adapter-type", "adapter-name"}
    missing = [x for x in missing if x not in dynamic_ok]
    assert not missing, (
        "bootstrap 绑了 HTML 里不存在的 id（静默无效）：\n  " + "\n  ".join(missing)
    )
