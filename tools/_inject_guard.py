# -*- coding: utf-8 -*-
"""注入验证工具的**共享启动自检** —— 把"基线过期"与"停在注入态"分开。

## 为什么要抽成共享模块（2026-10-10）

这段逻辑原先在 **11 个** `tools/inject_*_check.py` 里**各有一份副本**
（复制粘贴出来的）。它们的**失效机理完全相同**：基线文件不在 git 里，
源码每正常提交一次，"当前 ≠ 基线"就成立 ⇒ 工具把**正常提交**
误判成**注入残留**，一律拒绝运行。实测一次批量跑 **13 套全红**，
而产品完全正常。

⚠️ 「同一概念多条写入路径」= 必然漂移：我修好了其中一份，
另外十份照样是错的。⇒ 抽到这里，**一处修、全部生效**。

## 判据（用 git 区分两者）

* 文件与 git **干净**（内容被提交过）⇒ **基线过期**，自动前进并打印说明；
* 文件与 git **有未提交差异** ⇒ 真的可能停在注入态 ⇒ **拒绝运行**。

⚠️ 不能简单删掉这个保护：它是 2026-09-25 那次**真事故**的产物
（脚本被墙钟杀掉、源码停在注入态，而下次那句"还原校验 ✅ 逐字节一致"
比对的是自己的注入产物 = 假绿，结果 `api_admin.py` 的 `require_auth()` 真的丢了）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable


def git_is_clean(root: Path, paths: tuple[str, ...] = ("host", "emotion_app")) -> bool:
    """这些路径在 git 里是否干净（无未提交改动）。

    ⚠️ **默认只看被测源码（`host`/`emotion_app`），不看 `tools`**
       （2026-10-10 实测踩到）：注入的**目标**是源码，工具自身被编辑
       （比如正在改这个 guard）**不代表**源码停在注入态。
       我第一版把 `tools` 也写进来，结果"给工具加个注释"就让所有工具
       报"工作区有未提交改动" —— 又一个假报警。

    ⚠️ `git` 不存在/不是仓库时返回 `False`（**保守**：宁可让工具拒绝运行，
       也不要放行一个可能停在注入态的源码 —— 那是本项目最贵的一类假绿）。
    """
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=root, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return False
    return r.returncode == 0 and not r.stdout.strip()


def check_baseline(
    *,
    root: Path,
    snap: dict[str, str],
    base: dict,
    sha: Callable[[str], str],
    label: str = "源码",
    nest: bool = True,
) -> tuple[str, dict]:
    """比对当前快照与落盘基线，返回 `(裁决, 新的基线)`。

    裁决取值：
      * `"ok"`   —— 一致，或基线过期但工作区干净（已自动前进，`base` 是新的）；
      * `"exist"`—— 没有基线（`base` 为空），调用方应落盘后再跑；
      * `"stale"`—— 与基线不一致且**工作区有未提交改动** ⇒ **必须停下来**。

    `base` 的形状兼容两种历史写法（11 份副本里有两种）：
      * `{path: {"sha256": ...}}`（`inject_conversation_manager` 那种）
      * `{path: "<literal source text>"}`（`inject_nav_plugin_pages` /
        `inject_ui_tokens` 那种）

    🔴 `nest`：**返回/写回的形状必须与调用方期望的一致**（2026-10-10 踩到）。
       两种历史写法里，调用方后面是这么用 `snap` 的：
         * 嵌套式工具：`base[path]["sha256"]`；
         * 平铺式工具（`ui_tokens`/`plugin_config`/`settings_ping` 等）：
           **直接 `snap[rel]` 当源码文本用**。
       ⇒ 平铺式工具必须传 `nest=False`，否则自动前进后写回嵌套形状，
         调用方拿到 dict 当文本用 → `TypeError: data must be str, not dict`
         （实测：`ui_tokens` 就是这么挂的，而且**只在"提交后第一次跑"才发作**——
          因为不 stale 时我原样返回 `base`，形状恰好还对）。
    """
    def _want(v):
        if isinstance(v, dict):
            return v.get("sha256")
        if isinstance(v, str):
            return sha(v)
        return None

    def _pack() -> dict:
        if nest:
            return {p: {"sha256": sha(t)} for p, t in snap.items()}
        return {p: sha(t) for p, t in snap.items()}

    if not base:
        return "exist", _pack()

    stale = [p for p, t in snap.items() if _want(base.get(p)) and _want(base.get(p)) != sha(t)]
    if not stale:
        return "ok", base

    if git_is_clean(root):
        print(f"ℹ️ {len(stale)} 个文件与基线不一致，但工作区干净 ⇒ "
              f"是正常提交造成基线过期，按当前{label}刷新基线")
        return "ok", _pack()

    print(f"❌ {label}与落盘基线不一致，且工作区**有未提交改动** —— "
          f"上次可能停在注入态，请先还原（`git diff` 看一眼）")
    for p in stale:
        print(f"   {p}")
    return "stale", base
