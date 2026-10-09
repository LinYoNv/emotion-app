#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明 `tests/test_ui_tokens.py` 的判据**真的会失败**。

## 本项目的硬规矩（踩过 5 次以上）
一条判据"现在是绿的"证明不了任何事 —— 必须**手工注入一次**让它变红，
才能证明它守的是真东西。实现要点（每条都是踩出来的）：

  ① **基线落盘到工具之外**（`tools/.inject_*_baseline.json`）：
     只放内存的话，脚本被墙钟杀掉时源码停在注入态，
     下次运行读到的"注入前"就是已损坏的文件 ⇒ 结尾那句"逐字节还原 ✅"是假绿。
  ② **每轮从落盘快照重读**：不这么做，第二轮会叠在第一轮上。
  ③ 跑判据前**清 `__pycache__`** + pytest `-p no:cacheprovider`：
     CPython 判 `.pyc` 有效看 (mtime 整秒, 字节数)，同秒同长会让第二次的
     注入**不生效**（假绿）。
  ④ **要求输出里同时出现 `failed` 和 `passed`**：
     注入写出语法错时 pytest 也返回非 0，那是**假抓** —— 判据根本没跑起来。
  ⑤ 注入文本先用 `compile()`/`ast.parse()` **预检**，别把工具自己写坏。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_ui_tokens_baseline.json"
PYTEST = ROOT / ".venv" / "bin" / "python"
TESTFILE = "tests/test_ui_tokens.py"

#: (编号, 说明, 文件, 原文, 注入后)
INJECTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "A1", "删掉 `--line` 别名 ⇒ `var(--line)` 变成未定义",
        "emotion_app/web/static/app.css",
        "  --line: var(--border);\n",
        "",
    ),
    (
        "A2", "再删一个别名 `--input-bg`",
        "emotion_app/web/static/app.css",
        "  --input-bg: var(--sunken);\n",
        "",
    ),
    (
        "A3", "弹层层级退回硬编码 20（低于侧边栏 50）",
        "emotion_app/web/static/app.css",
        "  z-index: var(--z-modal);\n  backdrop-filter: blur(2px);",
        "  z-index: 20;\n  backdrop-filter: blur(2px);",
    ),
    (
        "A4", "把 `--z-modal` 调到比侧边栏还低 ⇒ 栈序不自洽",
        "emotion_app/web/static/app.css",
        "--z-scrim: 40; --z-sidebar: 50; --z-modal: 60;",
        "--z-scrim: 40; --z-sidebar: 50; --z-modal: 45;",
    ),
    (
        "B1", "给 `.empty` 加**第二条**定义（覆盖掉居中）",
        "emotion_app/web/static/app.css",
        "button.danger:hover { border-color: var(--danger); color: var(--danger); background: var(--danger-soft); }",
        "button.danger:hover { border-color: var(--danger); color: var(--danger); background: var(--danger-soft); }\n"
        ".empty { color: var(--muted); padding: 20px 4px; }",
    ),
    (
        "C1", "`@font-face` 改成引外部 CDN（无网环境会静默失败）",
        "emotion_app/web/static/app.css",
        'src: url("/static/fonts/geist-latin.woff2") format("woff2");',
        'src: url("https://fonts.gstatic.com/s/geist/v5/x.woff2") format("woff2");',
    ),
    (
        "C2", "`--font-sans` 去掉中文回落（只剩 Geist）",
        "emotion_app/web/static/app.css",
        '  --font-sans: "Geist", ui-sans-serif, system-ui, -apple-system,\n'
        '               "PingFang SC", "MiSans", "HarmonyOS Sans SC",\n'
        '               "Noto Sans CJK SC", "Source Han Sans SC",\n'
        '               "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif;',
        '  --font-sans: "Geist", ui-sans-serif, sans-serif;',
    ),
    (
        "D1", "index.html 去掉字体预载",
        "emotion_app/web/static/index.html",
        '<link rel="preload" href="/static/fonts/geist-latin.woff2" as="font" type="font/woff2" crossorigin />',
        "",
    ),
    (
        "D2", "index.html 去掉预载的 `crossorigin`（会重复下载）",
        "emotion_app/web/static/index.html",
        '<link rel="preload" href="/static/fonts/geist-latin.woff2" as="font" type="font/woff2" crossorigin />',
        '<link rel="preload" href="/static/fonts/geist-latin.woff2" as="font" type="font/woff2" />',
    ),
    (
        "D3", "index.html 去掉 favicon link",
        "emotion_app/web/static/index.html",
        '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />',
        "",
    ),
    (
        "E1", "发送按钮丢掉 primary（最重要的操作看不出是主操作）",
        "emotion_app/web/static/index.html",
        '<button id="send" class="primary" type="button">发送</button>',
        '<button id="send" type="button">发送</button>',
    ),
    (
        "E3", "CSS 里删掉 `button.primary` 定义（HTML 用了但样式没了）",
        "emotion_app/web/static/app.css",
        "button.primary {\n  background: var(--accent); border-color: var(--accent); color: var(--on-accent);\n  font-weight: 600;\n}",
        "",
    ),
    (
        "E2", "JS 里把主题令牌写进内联样式（回到改版前那种做法）",
        "emotion_app/web/static/app.js",
        'el.style.color = bad ? "var(--danger)" : "";',
        'el.style.color = bad ? "var(--danger)" : "var(--muted)";',
    ),
]


def _run_tests() -> tuple[int, str]:
    """跑判据；清字节码缓存（否则同秒同长改动会假绿）。"""
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PYTEST), "-m", "pytest", TESTFILE, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    return r.returncode, r.stdout + r.stderr


def _summarize(out: str) -> tuple[int, int]:
    import re
    f = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return (int(f.group(1)) if f else 0, int(p.group(1)) if p else 0)


def main() -> int:
    files = sorted({inj[2] for inj in INJECTIONS})

    # ---- 启动自检：上次是否被中途杀掉、源码停在注入态 ----
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        for rel, want in snap.items():
            cur = (ROOT / rel).read_text(encoding="utf-8")
            if cur != want:
                print(f"❌ 源码 {rel} 与基线快照不一致 —— 上次运行可能被中断在注入态。")
                print("   请先手动还原（或从 /root/backup 取备份），再运行本工具。")
                return 2
        print("✅ 启动自检：源码与基线快照一致")
    else:
        snap = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in files}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ 已落盘基线快照 → {BASELINE}")

    # ---- 基线必须全绿 ----
    rc, out = _run_tests()
    failed, passed = _summarize(out)
    if rc != 0 or failed:
        print(f"❌ 基线不绿（failed={failed} passed={passed}），注入验证无意义：")
        print(out[-2500:])
        return 1
    print(f"✅ 基线全绿：{passed} passed\n")

    results = []
    try:
        for no, desc, rel, old, new in INJECTIONS:
            path = ROOT / rel
            # 🔴 每轮从**落盘快照**重读，避免叠加
            src = snap[rel]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一（{src.count(old)} 次），跳过：{desc}")
                results.append((no, desc, "ANCHOR", 0, 0))
                continue
            injected = src.replace(old, new)
            # ⑤ 预检：别把工具自己写坏
            if path.suffix == ".py":
                try:
                    ast.parse(injected)
                except SyntaxError as e:
                    print(f"  ⚠️ {no} 注入文本语法错，跳过：{e}")
                    results.append((no, desc, "SYNTAX", 0, 0))
                    continue
            path.write_text(injected, encoding="utf-8")
            try:
                rc, out = _run_tests()
            finally:
                path.write_text(src, encoding="utf-8")   # 立刻还原

            failed, passed = _summarize(out)
            # ④ 必须同时有 failed 和 passed（只有 failed = 假抓）
            if failed > 0 and passed > 0:
                verdict = "CAUGHT"
            elif failed > 0 and passed == 0:
                verdict = "FAKE"      # 判据没跑起来
            else:
                verdict = "MISSED"
            results.append((no, desc, verdict, failed, passed))
            icon = {"CAUGHT": "✅", "FAKE": "⚠️", "MISSED": "❌"}[verdict]
            print(f"  {icon} {no} failed={failed} passed={passed} —— {desc}")
    finally:
        # 无论怎么退出都还原（含 Ctrl-C / SIGTERM）
        for rel, want in snap.items():
            (ROOT / rel).write_text(want, encoding="utf-8")
        print("\n✅ 已从基线快照还原全部文件")

    # ---- 逐字节还原校验 ----
    bad = [rel for rel, want in snap.items() if (ROOT / rel).read_text(encoding="utf-8") != want]
    if bad:
        print(f"❌ 还原校验失败：{bad}")
        return 1
    print("✅ 还原校验：逐字节一致")

    caught = sum(1 for *_, v, _, _ in results if v == "CAUGHT")
    print(f"\n抓住 {caught}/{len(INJECTIONS)}")
    for no, desc, v, f, p in results:
        if v != "CAUGHT":
            print(f"   ❌ {no} [{v}] failed={f} passed={p} —— {desc}")
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
