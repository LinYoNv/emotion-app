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
    // ⚠️ 只列**真的有拓展页**的插件：没页面的插件在侧边栏里没有可点的东西。
    (p) => Array.isArray(p.pages) && p.pages.length > 0,
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


def main() -> int:
    files = [HOST, LOADER, JS, CSS]
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        for rel, want in snap.items():
            if (ROOT / rel).read_text(encoding="utf-8") != want:
                print(f"❌ {rel} 与基线不一致 —— 上次可能停在注入态，请先还原")
                return 2
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
