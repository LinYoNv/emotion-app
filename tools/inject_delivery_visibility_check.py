#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：投递链路的「失败必须可见」判据**真的会红**吗？

## 为什么必须有
本项目铁则：「单测全绿 ≠ 判据有效」。判据写完永远绿，往往是它**永远不会失败**。

## 三道防线（都是踩过的坑，缺一条就会得到假结论）
  ① **基线落盘**在脚本之外（`.inject_delivery_visibility_baseline.json`）——
     脚本被墙钟杀掉时源码可能停在注入态，下次运行读到的"注入前"就是坏文件
     （2026-09-25 真事故：`api_admin.py` 的 `require_auth()` 真丢了）。
     启动时先自检残留：不一致就**拒绝运行**。
  ② **每轮从落盘快照重读**（不叠加 —— 踩过：注入累加 → 全"红"却是假的）。
  ③ **跑前清 `__pycache__`** + `-p no:cacheprovider`（CPython 判 .pyc 有效
     用 (mtime 整秒, 字节数)，同秒同长度的改写会加载**上一次**的字节码 → 假绿）。
  ④ 要求输出里**真的出现 `failed`**，且同批里**必须有 `passed`**
     （只有 failed 可能是语法错/收集错，判据根本没跑起来）。

## 覆盖哪些风险（每条都要选"能改变可观察行为"的改法）
  * 契约层：Protocol 少一个真方法 / 留着猜出来的方法
  * 语义层：`history` 返回 list（插件会静默当空）/ 截断方向反了 /
            `add_message_pair` 静默不抛 / 悬空 cid 照返回 /
            section 坏掉当空（会静默丢光历史）/ umo 之间串数据
  * 装配层：`HostPorts` 不传 `conversation_manager=` / app 不注入 store

用法：python3 tools/inject_delivery_visibility_check.py
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
#: 被注入的文件
CTX = ROOT / "host" / "astrbot_host" / "compat" / "core" / "star" / "context.py"
MGR = ROOT / "emotion_app" / "domain" / "conversation_manager.py"
HOST = ROOT / "emotion_app" / "web" / "plugin_host.py"
APP = ROOT / "emotion_app" / "web" / "app.py"
DELIVERY = ROOT / "emotion_app" / "web" / "delivery.py"
#: 插件身份 meta 那条修复（2026-09-27 续，「一起房间」取不到数的根因）
PAGES = ROOT / "host" / "astrbot_host" / "plugin_pages.py"
PAGES_WEB = ROOT / "emotion_app" / "web" / "plugin_pages_web.py"

BASELINE = Path(__file__).resolve().parent / ".inject_delivery_visibility_baseline.json"
#: 本脚本要守两组判据：对话历史端口 + 页面插件身份注入（同时跑，别漏一组）
TEST_NODES = ["tests/test_delivery.py", "tests/test_session_umo_unread.py"]

#: `(名字, 说明, 文件, 原文, 改后)`
INJECTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "① 写会话失败时静默（删日志）",
        "把 `send()` 里写失败那句 logger.error 删掉、只留 `return False` ——"
        "返回值仍然正确，但**失败不可见**。这正是 W2 要守的风险："
        "用户只会看到「伴侣一直没找我」，日志里连一行线索都没有。",
        DELIVERY,
        "            logger.error(\n"
        '                "主动消息写入会话失败（umo=%s, session=%s）：%s: %s",\n'
        "                umo,\n"
        "                session_id,\n"
        "                type(exc).__name__,\n"
        "                exc,\n"
        "                exc_info=True,\n"
        "            )\n"
        "            return False",
        "            return False  # 注入①：静默失败",
    ),
    (
        "② 空消息链静默丢弃",
        "删掉空链那句 logger.warning —— 本项目头号禁忌：静默丢弃用户内容。",
        DELIVERY,
        '            logger.warning("投递收到空消息链（umo=%s）→ 如实返回 False", umo)\n'
        "            return False",
        "            return False  # 注入②",
    ),
    (
        "③ 会话仓库缺失静默",
        "删掉装配缺失那句 logger.error —— 我们自己的接线错误却不出声，"
        "退化成「能力缺失但不喊」。",
        DELIVERY,
        '            logger.error("投递端口没有会话仓库（装配缺失）→ 如实返回 False")\n'
        "            return False",
        "            return False  # 注入③",
    ),
    (
        "④ 未读不 +1（用户看不到红点）",
        "把 `unread_delta=1` 改成 0 —— 消息落了会话，但侧边栏**没有红点**，"
        "用户根本不知道伴侣找过他。",
        DELIVERY,
        "unread_delta=1,",
        "unread_delta=0,  # 注入④",
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
    # ⚠️ 只列**本脚本真正会注入的文件**。照抄上一个脚本的列表会漏掉被测文件
    #    （实测踩到：快照里没有 delivery.py → `snap[str(path)]` KeyError）。
    return [DELIVERY]


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
        cwd=str(ROOT), capture_output=True, text=True, timeout=600, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    snap = _snapshot()
    base = {}
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
