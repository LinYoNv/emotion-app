#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明 `tests/test_settings_ping.py` 的判据**真的会失败**。

照本项目既定纪律（见 `tools/inject_ui_tokens_check.py` 头部）：
基线落盘 + 每轮从快照重读 + 清 `__pycache__` + 要求同批有 `passed`。

## 本批要守的那件事
「测试连接」的原 bug 是**判据测错了对象**（打的是我们自己的 `/v1/models`）。
所以注入的关键不在于"把代码改坏"，而在于**把它改回"看起来成功但根本没打上游"**——
如果判据抓不住那种改法，它就守不住这个 bug。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _inject_guard import check_baseline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_settings_ping_baseline.json"
PYTEST = ROOT / ".venv" / "bin" / "python"
TESTFILE = "tests/test_settings_ping.py"
SRC = "emotion_app/web/api_admin.py"

INJECTIONS: list[tuple[str, str, str, str]] = [
    (
        "P1",
        "模拟原 bug：不打上游，直接返回成功（这正是被修掉的那个形态）",
        """        try:
            async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:""",
        """        return (
            jsonify({"ok": True, "base_url": base_url, "model": model,
                     "model_count": 0, "model_present": None,
                     "detail": "连接正常"}),
            200,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException:""",
    ),
    (
        "P2",
        "鉴权失败也报成功（把 401/403 分支删掉）",
        """        if resp.status_code in (401, 403):
            return _fail(
                f"上游拒绝鉴权（HTTP {resp.status_code}）—— 检查 API Key 是否正确",
                "upstream_auth",
            )""",
        "",
    ),
    (
        "P3",
        "路由去掉鉴权（本项目真实事故形态：`/api/v1/logs` 漏鉴权返 200）",
        # ⚠️ 锚点必须**唯一** —— 我第一版用了 `require_auth()` + `_svc()` 那四行，
        #    而它在文件里出现 **5 次**（每个设置端点都长一个样）⇒ 工具报 ANCHOR 跳过。
        #    改用本端点**独有**的上下文（下面这句 `model = str(eff.get(...))`
        #    只在 ping 里出现），锚点就唯一了。
        """        if denied is not None:
            return denied
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}

        eff = settings.effective_upstream()
        base_url = str(body.get("base_url") or eff.get("base_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip() or str(eff.get("api_key") or "")
        model = str(eff.get("model") or "").strip()""",
        """        if False:
            return None
        _, settings, _ = _svc()
        if settings is None:
            return _err("设置服务未就绪", "server_error", 503)

        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}

        eff = settings.effective_upstream()
        base_url = str(body.get("base_url") or eff.get("base_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip() or str(eff.get("api_key") or "")
        model = str(eff.get("model") or "").strip()""",
    ),
    (
        "P4",
        "忽略表单值、只用已保存的配置（'改地址后测试测的是旧地址'）",
        # ⚠️ 同上：单看 `base_url = str(body.get(...))` 那一行会出现 **2 次**
        #    （`/settings/models` 里也有一份）。必须带上后面那行 `api_key` 才唯一。
        """        base_url = str(body.get("base_url") or eff.get("base_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip() or str(eff.get("api_key") or "")
        model = str(eff.get("model") or "").strip()""",
        """        base_url = str(eff.get("base_url") or "").strip()
        api_key = str(eff.get("api_key") or "")
        model = str(eff.get("model") or "").strip()""",
    ),
    (
        "P5",
        "把 API Key 回显进响应体（脱敏失守）",
        """                        "base_url": base_url,
                    }
                ),
                502,
            )""",
        """                        "base_url": base_url,
                        "echo_key": api_key,
                    }
                ),
                502,
            )""",
    ),
    (
        "P6",
        "超时也报成功（把 TimeoutException 分支删掉，让它走通用 HTTPError）",
        """        except httpx.TimeoutException:
            return _fail(
                f"连接上游超时（{timeout:.0f}s）：{url} —— 检查地址是否可达/是否需要代理",
                "upstream_timeout",
            )
""",
        "",
    ),
]


def _run():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PYTEST), "-m", "pytest", TESTFILE, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    return r.returncode, r.stdout + r.stderr


def _sum(out: str) -> tuple[int, int]:
    import re
    f = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return (int(f.group(1)) if f else 0, int(p.group(1)) if p else 0)


def main() -> int:
    if BASELINE.exists():
        snap = json.loads(BASELINE.read_text(encoding="utf-8"))
        # 🔴 2026-10-10：抽到 `tools/_inject_guard.py`（原 11 份副本会漂移）——
        #    工作区干净 ⇒ 基线过期、自动前进；有未提交改动 ⇒ 拒绝运行。
        _v, snap = check_baseline(root=ROOT, snap=snap, base=snap, sha=lambda t: t)
        if _v == "stale":
            return 2
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print("✅ 基线自检通过")
    else:
        snap = {SRC: (ROOT / SRC).read_text(encoding="utf-8")}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ 已落盘基线 → {BASELINE}")

    rc, out = _run()
    failed, passed = _sum(out)
    if rc != 0 or failed:
        print(f"❌ 基线不绿（failed={failed} passed={passed}）")
        print(out[-2000:])
        return 1
    print(f"✅ 基线全绿：{passed} passed\n")

    results = []
    try:
        for no, desc, old, new in INJECTIONS:
            src = snap[SRC]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一（{src.count(old)}）—— {desc}")
                results.append((no, desc, "ANCHOR", 0, 0))
                continue
            inj = src.replace(old, new)
            try:
                ast.parse(inj)
            except SyntaxError as e:
                print(f"  ⚠️ {no} 注入文本语法错：{e}")
                results.append((no, desc, "SYNTAX", 0, 0))
                continue
            (ROOT / SRC).write_text(inj, encoding="utf-8")
            try:
                rc, out = _run()
            finally:
                (ROOT / SRC).write_text(src, encoding="utf-8")
            f, p = _sum(out)
            verdict = "CAUGHT" if (f > 0 and p > 0) else ("FAKE" if f > 0 else "MISSED")
            results.append((no, desc, verdict, f, p))
            icon = {"CAUGHT": "✅", "FAKE": "⚠️", "MISSED": "❌"}[verdict]
            print(f"  {icon} {no} failed={f} passed={p} —— {desc}")
    finally:
        for rel, want in snap.items():
            (ROOT / rel).write_text(want, encoding="utf-8")
        print("\n✅ 已还原")

    bad = [r for r, w in snap.items() if (ROOT / r).read_text(encoding="utf-8") != w]
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
