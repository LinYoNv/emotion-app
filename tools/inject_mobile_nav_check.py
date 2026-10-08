#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：手机导航修复的判据**真的会红**吗？

## 守什么（情绪 2026-09-27 截图报障）
「没有开关，打不开侧边栏」—— 唯一的开关住在被它控制的抽屉里
（手机端抽屉 `translateX(-100%)`，开关跟着跑到屏幕外，实测 `rect.x=-268`）。
修法：在 `.content`（抽屉之外）加顶栏 `#mobile-bar` + 开关 `#mobile-menu`。

## 三道防线（都是踩过的坑，缺一条就会得到假结论）
  ① **基线落盘**在脚本之外（`.inject_mobile_nav_baseline.json`）——
     脚本被墙钟杀掉时源码可能停在注入态，下次运行读到的"注入前"就是坏文件。
     启动时自检残留：不一致就**拒绝运行**。
  ② **每轮从落盘快照重读**（不叠加 —— 踩过：注入累加 → 全"红"却是假的）。
  ③ **跑前清 `__pycache__`** + `-p no:cacheprovider`（CPython 判 `.pyc` 有效
     用 (mtime 整秒, 字节数)，同秒同长度的改写会加载**上一次**的字节码 → 假绿）。
  ④ 要求输出里**真的出现 `failed`**，且同批里**必须有 `passed`**
     （只有 failed 可能是语法错/收集错，判据根本没跑起来）。

## 覆盖哪些风险（每条都要选"能改变可观察行为"的改法）
  * 开关又回到抽屉里（本 bug 的原始形态）
  * 顶栏默认没隐藏（桌面端多一条重复入口）
  * 手机媒体查询里没显示顶栏
  * `.content` 不是列方向（顶栏与面板并排 → 挤坏布局）
  * 开关没接线（死按钮）/ 两个开关 aria 各自 set（漂移）
  * 顶栏标题不跟随当前页
  * 新增的 `content-body` div 未闭合

用法：python3 tools/inject_mobile_nav_check.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "emotion_app" / "web" / "static" / "index.html"
CSS = ROOT / "emotion_app" / "web" / "static" / "app.css"
JS = ROOT / "emotion_app" / "web" / "static" / "app.js"

BASELINE = Path(__file__).resolve().parent / ".inject_mobile_nav_baseline.json"
TEST_NODES = ["tests/test_mobile_nav.py"]

#: `(名字, 说明, 文件, 原文, 改后)`
INJECTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "① 开关被塞进侧边栏内部（本 bug 的原始形态）",
        "把 `#mobile-menu` 的按钮**插进** `<aside id=\"sidebar\">` 里 ——"
        "这正是本 bug 最初的形态（开关住在被它控制的抽屉里，抽屉一关它就跑到屏幕外）。",
        HTML,
        '  <aside id="sidebar" class="sidebar">\n    <div class="sidebar-head">',
        '  <aside id="sidebar" class="sidebar">\n'
        '    <button id="mobile-menu" class="icon-btn" type="button"\n'
        '            aria-label="打开导航菜单" aria-controls="sidebar" aria-expanded="false">\n'
        '      <span class="burger" aria-hidden="true"></span>\n'
        '    </button>\n'
        '    <div class="sidebar-head">',
    ),
    (
        "② 顶栏默认没隐藏（桌面端多一条重复入口）",
        "`.mobile-bar { display: none }` → `display: flex` —— "
        "桌面端会多出一条重复的开关，且占掉内容区一条高度。",
        CSS,
        ".mobile-bar { display: none; }",
        ".mobile-bar { display: flex; }",
    ),
    (
        "③ 手机媒体查询里没显示顶栏",
        "把媒体查询里那段 `.mobile-bar { display: flex }` 去掉 —— "
        "手机端顶栏仍是 display:none，用户**还是看不到开关**。",
        CSS,
        "  .mobile-bar {\n    display: flex; align-items: center; gap: 10px;",
        "  .mobile-bar-x {\n    display: flex; align-items: center; gap: 10px;",
    ),
    (
        "④ `.content` 不是列方向（顶栏与面板并排）",
        "`flex-direction: column` 去掉 → 回到默认 row —— "
        "顶栏会与面板**并排**，把内容挤成一条（同类事故：全局 label 竖排撑爆面板）。",
        CSS,
        ".content { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }",
        ".content { flex: 1; min-width: 0; min-height: 0; display: flex; }",
    ),
    (
        "⑤ `.content-body` 不撑高（内容体塌掉）",
        "去掉 `flex: 1` —— 顶栏下面的内容体没有高度，面板看不见。",
        CSS,
        ".content-body { flex: 1; min-width: 0; min-height: 0; display: flex; }",
        ".content-body { min-width: 0; min-height: 0; display: flex; }",
    ),
    (
        "⑥ 开关没接线（死按钮）",
        "`$(\"mobile-menu\").onclick = toggleSidebar;` 去掉 —— "
        "按钮在、看得见、点不动（本项目最典型的「模块能工作 ≠ 被接上了」）。",
        JS,
        'if ($("mobile-menu")) $("mobile-menu").onclick = toggleSidebar;',
        'if ($("mobile-menu")) { /* 注入⑥：不接线 */ }',
    ),
    (
        "⑦ 两个开关的 aria 各自 set（漂移）",
        "把 `syncSidebarAria` 的循环改成只 set `sidebar-toggle` —— "
        "顶栏那个开关的 `aria-expanded` 永远不更新（屏幕阅读器状态错）。",
        JS,
        '  ["sidebar-toggle", "mobile-menu"].forEach((id) => {',
        '  ["sidebar-toggle"].forEach((id) => {  // 注入⑦',
    ),
    (
        "⑧ openDrawer 里又手写一遍 aria（第二条写入路径）",
        "在 `openDrawer` 里加一行直接 set `sidebar-toggle` —— "
        "正是「同一概念多条写入路径」的形态（两处必然漂移）。",
        JS,
        "  document.querySelector(\".app\").classList.add(\"drawer-open\");\n"
        "  $(\"sidebar-scrim\").classList.remove(\"hidden\");\n"
        "  syncSidebarAria(true);",
        "  document.querySelector(\".app\").classList.add(\"drawer-open\");\n"
        "  $(\"sidebar-scrim\").classList.remove(\"hidden\");\n"
        "  $(\"sidebar-toggle\").setAttribute(\"aria-expanded\", \"true\");  // 注入⑧\n"
        "  syncSidebarAria(true);",
    ),
    (
        "⑨ 顶栏标题不跟随当前页",
        "`showPanel` 里去掉更新标题那段 —— 手机上用户不知道自己现在在哪一页。",
        JS,
        '  const bar = $("mobile-bar-title");',
        '  const bar = null;  // 注入⑨',
    ),
    (
        "⑩ content-body 那个 div 未闭合",
        "去掉 `</div><!-- /.content-body -->` —— HTML 结构错配"
        "（浏览器会猜着补，DOM 已经不是我们写的那个）。",
        HTML,
        "  </div><!-- /.content-body -->\n</main>",
        "</main>",
    ),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _files() -> list[Path]:
    return [HTML, CSS, JS]


def _snapshot() -> dict[str, str]:
    return {str(p): p.read_text(encoding="utf-8") for p in _files()}


def _restore(snap: dict[str, str]) -> None:
    for k, v in snap.items():
        Path(k).write_text(v, encoding="utf-8")
    _purge_pycache()


def _purge_pycache() -> None:
    for p in ROOT.rglob("__pycache__"):
        if ".venv" in p.parts:
            continue
        shutil.rmtree(p, ignore_errors=True)


def _run_tests() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest", *TEST_NODES,
         "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    snap = _snapshot()
    base: dict = {}
    if BASELINE.is_file():
        try:
            base = json.loads(BASELINE.read_text(encoding="utf-8"))
        except Exception:
            base = {}

    # ---- ① 启动自检：上次是不是被杀在注入态 ----
    if base:
        for path, text in snap.items():
            want = (base.get(path) or {}).get("sha256")
            if want and want != _sha(text):
                print("❌ 源码与落盘基线不一致 —— 上次注入验证可能被中断、源码停在注入态。")
                print(f"   {path}")
                print(f"   基线 {want[:16]}… 当前 {_sha(text)[:16]}…")
                print(f"   基线文件：{BASELINE}（确认源码没问题后删掉它可重建）")
                return 2
        print("✅ 基线自检通过")
    else:
        base = {p: {"sha256": _sha(t)} for p, t in snap.items()}
        BASELINE.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ 已记录基线（{len(base)} 个文件）→ {BASELINE}")

    def _rescue(signum, _frame):  # noqa: ANN001
        print(f"\n⚠️ 收到信号 {signum} —— 正在还原源码…")
        _restore(snap)
        print("✅ 已还原")
        sys.exit(3)

    signal.signal(signal.SIGTERM, _rescue)
    signal.signal(signal.SIGINT, _rescue)

    _restore(snap)
    rc, out = _run_tests()
    last = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
    print(f"\n[基线] rc={rc} {last[0] if last else out[-200:]}")
    if rc != 0 or "passed" not in out:
        print("❌ 基线就不是绿的 —— 注入验证无意义，先修基线")
        return 1
    base_passed = int(re.search(r"(\d+) passed", out).group(1))

    caught = 0
    results: list[tuple[str, bool, str]] = []
    for name, why, path, old, new in INJECTIONS:
        original = snap[str(path)]
        if old not in original:
            print(f"\n⚠️ 跳过 {name}：{path.name} 里找不到注入锚点（实现已改，注入文本要更新）")
            results.append((name, False, "锚点不存在"))
            continue
        mutated = original.replace(old, new, 1)
        # ③ 注入后先预检（HTML/CSS 不编译，只对 .js 做语法检查）
        if path.suffix == ".js":
            chk = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            _ = chk  # 语法错由下面真实跑测试暴露，这里只是提前提示
        path.write_text(mutated, encoding="utf-8")
        _purge_pycache()
        try:
            rc2, out2 = _run_tests()
        finally:
            _restore(snap)

        real_fail = bool(re.search(r"\b\d+ failed", out2))
        ran_some = bool(re.search(r"\d+ passed", out2))
        ok = rc2 != 0 and real_fail and ran_some
        if ok:
            caught += 1
        last2 = [ln for ln in out2.strip().splitlines() if ln.strip()][-1:]
        print(f"\n{'✅ 抓住' if ok else '❌ 没抓住'} {name}")
        print(f"   该被抓住的理由：{why}")
        print(f"   rc={rc2} 真failed={real_fail} 判据跑起来了={ran_some} | {last2[0] if last2 else ''}")
        results.append((name, ok, last2[0] if last2 else ""))

    print(f"\n{'=' * 62}")
    print(f"注入验证：**{caught}/{len(INJECTIONS)}** 抓住（基线 {base_passed} passed）")
    for name, ok, info in results:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{info}]" if not ok else ""))
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
