#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：本轮（2026-10-08）三组修复的判据**真的会红**吗？

## 守哪三件事
  A. **日志必须带日期** —— 否则 `logs/serve.out`（追加、跨多天）判不出
     "这条是历史残留还是当前仍在发生"。
  B. **上游响应体必须进异常字符串** —— 否则 `上游返回 403` / `不是 JSON`
     这两条高频错误**永远看不到上游到底回了什么**（诊断信息被吞）。
  C. **真 AstrBot 模块别名**（W5）—— 否则插件间 40 处
     `import_module("data.plugins.X.main")` 100% 失败。

## 四道防线（缺一条就会得到假结论，全是踩过的坑）
  ① 基线**落盘**在脚本之外（`.inject_round14_baseline.json`）——
     脚本被墙钟杀掉时源码可能停在注入态，下次读到的"注入前"就是坏文件。
     启动时自检残留：不一致就**拒绝运行**。
  ② 每轮从落盘快照**重读**（不叠加）。
  ③ 跑前清 `__pycache__` + `-p no:cacheprovider`（同秒同长度的改写 →
     加载上一次字节码 → 假绿）。
  ④ 要求输出里**真的出现 `failed`**，且同批**必须有 `passed`**
     （只有 failed 可能是收集错，判据根本没跑起来）。

用法：python3 tools/inject_round14_check.py
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

LOGGING = ROOT / "emotion_app" / "infra" / "logging_setup.py"
UPSTREAM = ROOT / "emotion_app" / "domain" / "upstream.py"
LOADER = ROOT / "host" / "astrbot_host" / "loader" / "plugin_loader.py"

BASELINE = Path(__file__).resolve().parent / ".inject_round14_baseline.json"

#: 本脚本要守的判据（三组一起跑 —— 漏一组就少守一块）
TEST_NODES = [
    "tests/test_config_logging.py",
    "tests/test_upstream.py",
    "host/tests/test_loader_real_plugin.py",
]

#: `(名字, 说明, 文件, 原文, 改后)`
INJECTIONS: list[tuple[str, str, Path, str, str]] = [
    (
        "A1 日志退回只有时分秒",
        "把 TextFormatter 的格式串改回 `%H:%M:%S` —— 这正是修复前的形态，"
        "日志跨多天时判不出是哪次运行。",
        LOGGING,
        "base = f\"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<7}",
        "base = f\"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7}",
    ),
    (
        "B1 异常不再带 body",
        "`UpstreamError` 退回 `super().__init__(message)` —— body 只存成属性，"
        "而所有日志点写的都是 `logger.error('...%s', exc)` ⇒ **body 一个字都进不了日志**。"
        "这正是本 bug 的原始形态。",
        UPSTREAM,
        '            super().__init__(f"{message} | 上游响应体: {detail}")',
        "            super().__init__(message)",
    ),
    (
        "B2 body 不截断",
        "去掉 500 字符上限 —— 上游回一整页 HTML 时会把日志灌爆"
        "（诊断价值被噪音淹没）。",
        UPSTREAM,
        "            if len(detail) > self.BODY_IN_MESSAGE_LIMIT:\n"
        '                detail = detail[: self.BODY_IN_MESSAGE_LIMIT] + "…(截断)"',
        "            pass  # 注入：不截断",
    ),
    (
        "C1 不注册真 AstrBot 别名",
        "注掉 `_register_real_astrbot_alias` 调用 —— 插件间 40 处 "
        "`import_module(\"data.plugins.X.main\")` 会 100% ModuleNotFoundError"
        "（W5 的原始症状）。",
        LOADER,
        "        PluginLoader._register_real_astrbot_alias(name, pkg, main_mod)",
        "        pass  # 注入：不注册别名",
    ),
    (
        "C2 别名不逐级建中间包",
        "去掉逐级 `setdefault` —— `data/` 是**隐式命名空间包**、不会自动在 "
        "`sys.modules` 建中间层 ⇒ `data.plugins.X` 的 KeyError，"
        "`from data.plugins.X.main import Y` 这种写法拿不到。",
        LOADER,
        '            for i in range(1, len(parts) + 1):\n'
        '                sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))',
        "            pass  # 注入：不建中间包",
    ),
    (
        "C3 别名改掉规范模块名",
        "把别名包的真对象 `__name__` 改掉 —— `star_map` / handler 的 "
        "`handler_module_path` 都按**规范名**索引，改了会让两套索引脱节"
        "（「同一概念多条写入路径」）。",
        LOADER,
        '            sys.modules[".".join(parts)] = pkg  # 关键：用**真对象**覆盖占位',
        '            pkg.__name__ = ".".join(parts)  # 注入：改规范名\n'
        '            sys.modules[".".join(parts)] = pkg',
    ),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _purge_pycache() -> None:
    for p in ROOT.rglob("__pycache__"):
        if ".venv" in p.parts:
            continue
        shutil.rmtree(p, ignore_errors=True)


def _files() -> list[Path]:
    return [LOGGING, UPSTREAM, LOADER]


def _snapshot() -> dict[str, str]:
    return {str(p): p.read_text(encoding="utf-8") for p in _files()}


def _restore(snap: dict[str, str]) -> None:
    for k, v in snap.items():
        Path(k).write_text(v, encoding="utf-8")
    _purge_pycache()


def _run_tests() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest", *TEST_NODES,
         "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900, env=env,
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
    for path, text in snap.items():
        want = (base.get(path) or {}).get("sha256")
        if want and want != _sha(text):
            print("❌ 源码与落盘基线不一致 —— 上次注入验证可能被中断、源码停在注入态。")
            print(f"   {path}")
            print(f"   基线 {want[:16]}… 当前 {_sha(text)[:16]}…")
            print(f"   基线文件：{BASELINE}")
            print("   👉 若确认当前源码是**正确的**，删掉基线文件重跑即可重建。")
            return 2

    if not base:
        base = {p: {"sha256": _sha(t)} for p, t in snap.items()}
        BASELINE.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ 已记录基线（{len(base)} 个文件）→ {BASELINE}")
    else:
        print("✅ 基线自检通过")

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
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as exc:
            print(f"\n❌ {name}：注入文本自己写坏了（{exc}）—— 记为未抓住")
            results.append((name, False, f"注入文本语法错：{exc}"))
            continue
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
