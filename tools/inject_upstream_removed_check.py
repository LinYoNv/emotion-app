#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明 `tools/upstream_removed_check.py` 的判据**真的会失败**。

照既定纪律：基线落盘 + 每轮从快照重读 + 清 `__pycache__` + 要求同批有 `passed`。

## 本批要守的那件事
「删一块 UI」的真实风险是**删过头**或**删不干净**，两者都是静默的：
  * 删过头：把别处的接线一起切掉 → 按钮还在，点了没反应，控制台零报错
  * 删不干净：`$("x").onclick` 在元素不存在时抛错 → 脚本后半段全死
所以注入要**两头都试**。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _inject_guard import check_baseline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_upstream_baseline.json"
PY = ROOT / ".venv" / "bin" / "python"
HTML = "emotion_app/web/static/index.html"
JS = "emotion_app/web/static/app.js"
CHECK = "tools/upstream_removed_check.py"

#: (编号, 说明, 文件, 原文, 替换)
INJECTIONS = [
    (
        "U1",
        "删过头：把日志页的「立即刷新」接线也切掉（我真实犯过的错）",
        JS,
        '$("logs-refresh").onclick = () => loadLogs({});',
        "",
    ),
    (
        "U2",
        "删不干净：把已删元素的处理器加回来（元素不存在 → 抛错 → 后半段全死）",
        JS,
        '$("cfg-reset").onclick = () => resetConfigProfile();',
        '$("cfg-reset").onclick = () => resetConfigProfile();\n'
        '$("ping").onclick = async () => { toast($("settings-hint"), "x"); };',
    ),
    (
        "U3",
        "没删干净：HTML 里把「测试连接」按钮留着",
        HTML,
        '<h3>设置</h3>',
        '<h3>设置</h3>\n    <button type="button" id="ping">测试连接</button>',
    ),
    (
        "U4",
        "删过头：源列表**渲染函数直接返回**（页面在、内容是空的）",
        JS,
        """  ul.innerHTML = "";
  if (!list.length) {
    empty.classList.remove("hidden");""",
        """  ul.innerHTML = "";
  return;                      // 注入：渲染逻辑被"误删"，列表永远空
  if (!list.length) {
    empty.classList.remove("hidden");""",
    ),
]


def _run():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PY), CHECK], cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    return r.returncode, r.stdout + r.stderr


def _counts(out: str) -> tuple[int, int]:
    import re
    return (
        len(re.findall(r"^  ❌", out, re.M)),
        len(re.findall(r"^  ✅", out, re.M)),
    )


def main() -> int:
    files = [HTML, JS]
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        # 🔴 2026-10-10：抽到 `tools/_inject_guard.py`（原 11 份副本会漂移）；
        #    工作区干净 ⇒ 基线过期、自动前进；有未提交改动 ⇒ 拒绝运行。
        _v, snap = check_baseline(root=ROOT, snap=snap, base=snap, sha=lambda t: t, nest=False)
        if _v == "stale":
            return 2
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 基线自检通过 → {BASELINE.name}")
    else:
        snap = {f: (ROOT / f).read_text(encoding="utf-8") for f in files}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 已落盘基线 → {BASELINE.name}")

    rc, out = _run()
    bad, good = _counts(out)
    if rc != 0 or bad:
        print(f"❌ 基线不绿（❌{bad} ✅{good}）")
        print(out[-1500:])
        return 1
    print(f"✅ 基线全绿：{good} 条判据\n")

    results = []
    try:
        for no, desc, rel, old, new in INJECTIONS:
            src = snap[rel]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一({src.count(old)}) —— {desc}")
                results.append((no, desc, "ANCHOR", 0, 0)); continue
            (ROOT / rel).write_text(src.replace(old, new), encoding="utf-8")
            try:
                rc, out = _run()
            finally:
                (ROOT / rel).write_text(src, encoding="utf-8")
            b, g = _counts(out)
            verdict = "CAUGHT" if b > 0 else "MISSED"
            results.append((no, desc, verdict, b, g))
            icon = "✅" if verdict == "CAUGHT" else "❌"
            print(f"  {icon} {no} ❌{b} ✅{g} —— {desc}")
    finally:
        for rel, want in snap.items():
            (ROOT / rel).write_text(want, encoding="utf-8")
        print("\n✅ 已还原")

    bad_files = [r for r, w in snap.items() if (ROOT / r).read_text(encoding="utf-8") != w]
    if bad_files:
        print(f"❌ 还原校验失败：{bad_files}")
        return 1
    print("✅ 还原校验：逐字节一致")

    caught = sum(1 for *_, v, _, _ in results if v == "CAUGHT")
    print(f"\n抓住 {caught}/{len(INJECTIONS)}")
    for no, desc, v, b, g in results:
        if v != "CAUGHT":
            print(f"   ❌ {no} [{v}] —— {desc}")
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
