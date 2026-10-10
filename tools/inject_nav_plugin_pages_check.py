#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明侧边栏插件分组的两套判据**真的会失败**。

照既定纪律：基线落盘 + 每轮从快照重读 + 清 `__pycache__` + 要求同批有 `passed`。

## 守的是"看起来显示了、其实是写死的/是目录名"
情绪明确要求：「要按我们平台实际有的插件添加，就算以后我自己添加了插件也能自己显示」。
所以本批的注入要证明判据能抓住：
  * **写死插件名**（新增插件不会出现）
  * **用目录名当显示名**（`meme_manager` 而不是「表情包管理器」）
  * **`pages` 没接出来**（分组永远是空的）
  * **收起态没有隐藏**（图标条里多出一串没辨识度的项）
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_nav_plugin_baseline.json"
PY = ROOT / ".venv" / "bin" / "python"
HOST = "host/astrbot_host/host_app.py"
LOADER = "host/astrbot_host/loader/plugin_loader.py"
JS = "emotion_app/web/static/app.js"
CSS = "emotion_app/web/static/app.css"

INJECTIONS = [
    (
        "N1",
        "display_name 拿目录名冒充（字段在、值没意义）",
        HOST,
        '                    "display_name": rec.display_name,',
        '                    "display_name": rec.name,  # 注入：拿目录名冒充',
    ),
    (
        "N2",
        "pages 不接出来（侧边栏分组永远是空的）",
        HOST,
        '                    "pages": [dict(pg) for pg in rec.pages],',
        '                    "pages": [],  # 注入：不接出来',
    ),
    (
        "N3",
        "加载器不读 metadata 的 display_name（回到【零消费】状态）",
        LOADER,
        '            rec.display_name = str(meta.get("display_name") or "")',
        '            rec.display_name = ""  # 注入：不读 metadata 那个字段',
    ),
    (
        "N4",
        "前端把插件名写死（新增插件不会自己出现）",
        JS,
        """  const plugins = ((d && d.plugins) || []).filter(
    (p) => Array.isArray(p.pages) && p.pages.length > 0 && p.state === "loaded",
  );""",
        """  const plugins = ((d && d.plugins) || []).filter(
    (p) => ["astrbot_plugin_memory_companion"].includes(p.id),  // 注入：写死
  );""",
    ),
    (
        "N5",
        "收起态不隐藏分组（图标条里多出一串没辨识度的项）",
        CSS,
        ".app.collapsed .nav-group { display: none; }",
        "/* 注入：去掉这行 */",
    ),
    (
        "N6",
        "去掉 loaded 过滤（跑不起来的插件也会出现在导航里，点进去一片空白）",
        JS,
        '(p) => Array.isArray(p.pages) && p.pages.length > 0 && p.state === "loaded",',
        '(p) => Array.isArray(p.pages) && p.pages.length > 0,  // 注入：不判状态',
    ),
    (
        "N7",
        "分组归属判错（外部插件被说成「本平台自带」）",
        JS,
        "    return /^astrbot_plugin_[A-Za-z0-9_]+\\.main$/.test(mp);",
        "    return true;  // 注入：全都算自带的",
    ),
]


def _run_tests():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PY), "-m", "pytest", "tests/test_plugin_config.py", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    return r.returncode, r.stdout + r.stderr


def _run_browser():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PY), "tools/nav_plugin_pages_check.py"], cwd=ROOT,
        capture_output=True, text=True, timeout=600,
    )
    return r.returncode, r.stdout + r.stderr


def _counts(out: str) -> tuple[int, int]:
    import re
    return (
        len(re.findall(r"^  ❌", out, re.M)),
        len(re.findall(r"^  ✅", out, re.M)),
    )


def _sum_pytest(out: str) -> tuple[int, int]:
    import re
    f = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return (int(f.group(1)) if f else 0, int(p.group(1)) if p else 0)


def _git_is_clean() -> bool:
    """这些文件在 git 里**是否干净**（等于最后一次提交内容）。

    ⚠️ 为什么用它来区分两种"与基线不一致"（2026-10-10 加）：
       基线文件本身**不在 git 里**（`tools/.inject_*` 是隐藏产物、且会被
       `.gitignore` 排除），所以它会**过期** —— 源码正常提交一次，
       "当前 ≠ 基线"就成立了。
       而这条自检的**本意**是抓"上次跑一半被杀、源码正停在注入态"。
       两者的区别很清楚：
         * 文件**与 git 干净** ⇒ 内容是"被人提交过的正常内容" ⇒ 基线过期，可以正常往下跑；
         * 文件**与 git 有未提交差异** ⇒ 真的可能停在注入态 ⇒ 必须停下来让人看。
       ✅ 于是把"拒绝运行"改成上面的两分：只有后者才拒绝。
       ⚠️ 另一个**更彻底**的修法是让基线跟着源码自动前进（快照存 git blob 哈希），
          但那是"悄悄前进"，会把真的注入残留一起吞掉 —— 不取。
    """
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", "host", "emotion_app"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0 and not r.stdout.strip()


def main() -> int:
    files = [HOST, LOADER, JS, CSS]
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        stale = [rel for rel, want in snap.items() if (ROOT / rel).read_text(encoding="utf-8") != want]
        if stale and not _git_is_clean():
            print(f"❌ {stale} 与基线不一致，且工作区**有未提交改动** —— "
                  f"上次可能停在注入态，请先还原（`git diff` 看一眼）")
            return 2
        if stale:
            # 工作区干净 ⇒ 只是有人在两次跑之间正常提交了东西，基线该前进了。
            print(f"ℹ️ {len(stale)} 个文件与基线不一致，但工作区干净 ⇒ "
                  f"是正常提交造成基线过期，按当前源码刷新基线")
            snap = {f: (ROOT / f).read_text(encoding="utf-8") for f in files}
            BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        else:
            print("✅ 启动自检：与基线一致")
    else:
        snap = {f: (ROOT / f).read_text(encoding="utf-8") for f in files}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 已落盘基线 → {BASELINE.name}")

    # 基线：两套都要绿
    rc, out = _run_tests()
    f0, p0 = _sum_pytest(out)
    if rc != 0 or f0:
        print(f"❌ 基线 pytest 不绿（failed={f0} passed={p0}）")
        print(out[-1500:]); return 1
    rc, out = _run_browser()
    b0, g0 = _counts(out)
    if rc != 0 or b0:
        print(f"❌ 基线浏览器判据不绿（❌{b0} ✅{g0}）")
        print(out[-1500:]); return 1
    print(f"✅ 基线全绿：pytest {p0} passed · 浏览器 {g0} 条\n")

    results = []
    try:
        for no, desc, rel, old, new in INJECTIONS:
            src = snap[rel]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一({src.count(old)}) —— {desc}")
                results.append((no, desc, "ANCHOR", "")); continue
            inj = src.replace(old, new)
            if rel.endswith(".py"):
                try:
                    ast.parse(inj)
                except SyntaxError as e:
                    print(f"  ⚠️ {no} 注入文本语法错：{e}")
                    results.append((no, desc, "SYNTAX", "")); continue
            (ROOT / rel).write_text(inj, encoding="utf-8")
            try:
                rc_t, out_t = _run_tests()
                rc_b, out_b = _run_browser()
            finally:
                (ROOT / rel).write_text(src, encoding="utf-8")
            ft, pt = _sum_pytest(out_t)
            bb, gb = _counts(out_b)
            caught = ft > 0 or bb > 0
            verdict = "CAUGHT" if caught else "MISSED"
            results.append((no, desc, verdict, f"pytest failed={ft} · 浏览器 ❌{bb}"))
            icon = "✅" if caught else "❌"
            print(f"  {icon} {no} pytest failed={ft}/passed={pt} · 浏览器 ❌{bb}✅{gb} —— {desc}")
    finally:
        for rel, want in snap.items():
            (ROOT / rel).write_text(want, encoding="utf-8")
        print("\n✅ 已还原")

    bad = [r for r, w in snap.items() if (ROOT / r).read_text(encoding="utf-8") != w]
    if bad:
        print(f"❌ 还原校验失败：{bad}")
        return 1
    print("✅ 还原校验：逐字节一致")

    caught = sum(1 for *_, v, _ in results if v == "CAUGHT")
    print(f"\n抓住 {caught}/{len(INJECTIONS)}")
    for no, desc, v, info in results:
        if v != "CAUGHT":
            print(f"   ❌ {no} [{v}] {info} —— {desc}")
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
