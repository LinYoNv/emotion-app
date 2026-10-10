#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明「插件配置文件」与「会话级上下文策略」的判据**真的会失败**。

照既定纪律：基线落盘 + 每轮从快照重读 + 清 `__pycache__` + 要求同批有 `passed`。

## 守的是"看起来能用、其实没生效 / 不安全"
  * **鉴权漏了**（本项目真事故：公网可读）
  * **扩展名不校验**（用户能把任意文件传进"只收图片"的项）
  * **路径穿越不挡**（能删插件本体）
  * **空提示词不回落到内置**（模型没有摘要格式约束 → 输出漂移，症状极难归因）
  * **会话级总结模型被忽略**（用户以为换了个便宜模型，实际没换）
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _inject_guard import check_baseline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_cf_sess_baseline.json"
PY = ROOT / ".venv" / "bin" / "python"
HOST = "host/astrbot_host/host_app.py"
APP = "emotion_app/web/app.py"
CTX = "emotion_app/domain/context.py"
CHAT = "emotion_app/domain/chat.py"

TESTS = "tests/test_plugin_config.py tests/test_config_profile.py tests/test_session_model.py"

INJECTIONS = [
    (
        "C1",
        "config-files 三条路由去掉鉴权（未登录可读写）",
        APP,
        '''    @app.get("/api/v1/plugins/<plugin_id>/config-files")
    async def plugin_config_files_list(plugin_id: str):  # type: ignore[no-untyped-def]
        """列出某个 `file` 型配置项已有的文件。"""
        guard = _plugin_guard(plugin_id)''',
        '''    @app.get("/api/v1/plugins/<plugin_id>/config-files")
    async def plugin_config_files_list(plugin_id: str):  # type: ignore[no-untyped-def]
        """列出某个 `file` 型配置项已有的文件。"""
        guard = None  # 注入：去掉鉴权''',
    ),
    (
        "C2",
        "上传不校验扩展名（任意文件都能进「只收图片」的项）",
        HOST,
        '''        exts = meta["file_types"]
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if exts and ext not in exts:''',
        '''        exts = meta["file_types"]
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if False:  # 注入：不校验扩展名''',
    ),
    (
        "C3",
        "删除时不做目录归属校验（能删同插件别的项的文件 / 插件本体）",
        HOST,
        '''        try:
            target.resolve().relative_to(root.resolve())
        except ValueError:''',
        '''        try:
            if False:  # 注入：不校验目录归属
                target.resolve().relative_to(root.resolve())
        except ValueError:''',
    ),
    (
        "S1",
        "空提示词不回落内置（直接发空 system 消息）",
        CTX,
        '        Message(role=ROLE_SYSTEM, content=(instruction or "").strip() or SUMMARY_PROMPT),',
        '        Message(role=ROLE_SYSTEM, content=instruction),  # 注入：不回落',
    ),
    (
        "S2",
        "会话级 summary_model 被忽略（永远用默认模型）",
        CHAT,
        '''        policy_model = str(getattr(session.policy, "summary_model", "") or "").strip()
        if policy_model:''',
        '''        policy_model = ""  # 注入：忽略会话级总结模型
        if policy_model:''',
    ),
]


def _clean_pycache():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)


def _run():
    _clean_pycache()
    r = subprocess.run(
        [str(PY), "-m", "pytest", *TESTS.split(), "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    return r.returncode, r.stdout + r.stderr


def _sum(out: str) -> tuple[int, int]:
    f = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return (int(f.group(1)) if f else 0, int(p.group(1)) if p else 0)


def main() -> int:
    files = [HOST, APP, CTX, CHAT]
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        # 🔴 2026-10-10：抽到 `tools/_inject_guard.py`（原 11 份副本会漂移）——
        #    工作区干净 ⇒ 基线过期、自动前进；有未提交改动 ⇒ 拒绝运行。
        _v, snap = check_baseline(root=ROOT, snap=snap, base=snap, sha=lambda t: t)
        if _v == "stale":
            return 2
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print("✅ 基线自检通过")
    else:
        snap = {f: (ROOT / f).read_text(encoding="utf-8") for f in files}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 已落盘基线 → {BASELINE.name}")

    rc, out = _run()
    f0, p0 = _sum(out)
    if f0:
        print(f"❌ 基线不绿（failed={f0} passed={p0}）\n{out[-1800:]}")
        return 1
    print(f"✅ 基线全绿：{p0} passed\n")

    results = []
    try:
        for no, desc, rel, old, new in INJECTIONS:
            src = snap[rel]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一({src.count(old)}) —— {desc}")
                results.append((no, desc, "ANCHOR")); continue
            inj = src.replace(old, new)
            if rel.endswith(".py"):
                try:
                    ast.parse(inj)
                except SyntaxError as e:
                    print(f"  ⚠️ {no} 注入文本语法错：{e}")
                    results.append((no, desc, "SYNTAX")); continue
            (ROOT / rel).write_text(inj, encoding="utf-8")
            try:
                rc_t, out_t = _run()
            finally:
                (ROOT / rel).write_text(src, encoding="utf-8")
            ft, pt = _sum(out_t)
            caught = ft > 0
            results.append((no, desc, "CAUGHT" if caught else "MISSED"))
            print(f"  {'✅' if caught else '❌'} {no} failed={ft}/passed={pt} —— {desc}")
    finally:
        for rel, want in snap.items():
            (ROOT / rel).write_text(want, encoding="utf-8")
        print("\n✅ 已还原")

    bad = [r for r, w in snap.items() if (ROOT / r).read_text(encoding="utf-8") != w]
    if bad:
        print(f"❌ 还原校验失败：{bad}")
        return 1
    print("✅ 还原校验：逐字节一致")

    caught = sum(1 for *_, v in [(a, b, c) for a, b, c in results] if v == "CAUGHT")
    print(f"\n抓住 {caught}/{len(INJECTIONS)}")
    for no, desc, v in results:
        if v != "CAUGHT":
            print(f"   ❌ {no} [{v}] —— {desc}")
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
