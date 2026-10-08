#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：对话历史端口的判据**真的会红**吗？

## 为什么必须有
本项目铁则：「单测全绿 ≠ 判据有效」。判据写完永远绿，往往是它**永远不会失败**。

## 三道防线（都是踩过的坑，缺一条就会得到假结论）
  ① **基线落盘**在脚本之外（`.inject_conversation_manager_baseline.json`）——
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

用法：python3 tools/inject_conversation_manager_check.py
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
#: 插件身份 meta 那条修复（2026-09-27 续，「一起房间」取不到数的根因）
PAGES = ROOT / "host" / "astrbot_host" / "plugin_pages.py"
PAGES_WEB = ROOT / "emotion_app" / "web" / "plugin_pages_web.py"

BASELINE = Path(__file__).resolve().parent / ".inject_conversation_manager_baseline.json"
#: 本脚本要守两组判据：对话历史端口 + 页面插件身份注入（同时跑，别漏一组）
TEST_NODES = [
    "tests/test_conversation_manager.py",
    "tests/test_plugin_host_e2e.py::test_injects_plugin_id_meta_into_page_html",
    "tests/test_plugin_host_e2e.py::test_plugin_id_meta_is_escaped_and_optional",
    "tests/test_plugin_host_e2e.py::test_page_route_passes_plugin_id_into_injection",
]

#: `(名字, 说明, 文件, 原文, 改后)`
INJECTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "① 契约少一个真方法（回到「猜」的契约）",
        "把 `add_message_pair` 从 Protocol 里删掉 —— 这正是最初缺口的形态："
        "契约里没有插件真正调的方法，于是**谁也没法实现它**。",
        CTX,
        "    async def add_message_pair(\n        self,\n        cid: str,",
        "    async def _injected_removed(\n        self,\n        cid: str,",
    ),
    (
        "② 契约里留着猜出来的方法",
        "把当年猜的 `get_history` 加回 Protocol —— "
        "只断言「真的在」的话这条能蒙混过关，所以才要反向断言。",
        CTX,
        "    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:",
        "    async def get_history(self, umo: str, limit: int = 20) -> list:\n        ...\n\n"
        "    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:",
    ),
    (
        "③ history 返回 list（插件会静默当空历史）",
        "把 JSON 字符串换成 list —— 插件侧 `isinstance(raw_history, str)` 的分支"
        "会走 else，**静默**当成空历史（上一句白说，还不报错）。",
        MGR,
        '            history=json.dumps(content, ensure_ascii=False),',
        "            history=content,  # 注入③：返回 list",
    ),
    (
        "④ 截断方向反了（丢最近的、留最早的）",
        "`content[-MAX_MESSAGES:]` → `content[:MAX_MESSAGES]` —— "
        "插件下一轮读到的是**最旧**的上文。",
        MGR,
        "        return content[-MAX_MESSAGES:]",
        "        return content[:MAX_MESSAGES]  # 注入④",
    ),
    (
        "⑤ add_message_pair 静默不抛（存档变假象）",
        "对话不存在时 `return` 而不是抛 —— 插件的「存档成功」变成假象"
        "（本项目最忌的那类 bug）。",
        MGR,
        '            raise RuntimeError(f"Conversation with id {cid} not found")',
        "            return  # 注入⑤：静默丢弃",
    ),
    (
        "⑥ 悬空 cid 照返回",
        "`sel` 指向的对话记录已不存在时仍返回那个 cid —— "
        "插件会以为「有对话但读不出来」（比直接说没有更难排查）。",
        MGR,
        '        if cid and isinstance(section.get(str(cid)), dict):\n            return str(cid)\n        return None',
        "        return str(cid) if cid else None  # 注入⑥",
    ),
    (
        "⑦ section 坏掉当空（会静默丢光历史）",
        "格式不对时不报错、当成空 dict —— 下一次保存就把全部历史**覆盖掉**。",
        MGR,
        "            raise RuntimeError(\n"
        '                f"{CONVERSATIONS_SECTION} section 格式错误（期望 dict，得到 "\n'
        '                f"{type(section).__name__}）—— 拒绝覆盖，请人工检查。"\n'
        "            )",
        "            return {}  # 注入⑦",
    ),
    (
        "⑧ umo 之间串数据（所有会话共用一条对话）",
        "把 `sel` 用**固定键**而不是按 umo 分 —— "
        "方案 B 下每个会话一个 umo，串了就全乱。",
        MGR,
        "        section.setdefault(umo, {})\n        if isinstance(section[umo], dict):\n            section[umo][_SEL] = cid\n        else:\n            section[umo] = {_SEL: cid}",
        "        section.setdefault('__shared__', {})\n        section['__shared__'][_SEL] = cid  # 注入⑧",
    ),
    (
        "⑨ 装配不传 conversation_manager",
        "`HostPorts(...)` 少写 `conversation_manager=` —— "
        "端口建得再好，不传进去照样是 None（最初的直接根因）。",
        HOST,
        "                    conversation_manager=conversation_manager,",
        "                    # 注入⑨：不传",
    ),
    (
        "⑩ app 不注入 store",
        "`create_app` 里去掉 `service.store = ...` —— "
        "`_build_conversation_manager()` 会如实降级成 None，功能等于没接上。",
        APP,
        '    service.store = app.config.get("EMOTION_STORE")',
        "    pass  # 注入⑩：不注入 store",
    ),
    (
        "⑪ 没有 store 时造个假端口",
        "`_build_conversation_manager()` 在 store 为 None 时仍造端口 —— "
        "插件会以为能存档，实际写不进去（本项目最忌「看着像能用」）。",
        HOST,
        "        if store is None:\n"
        '            logger.warning(\n'
        '                "没有 store → 对话历史端口不接（插件的主动消息存档将如实失败；"\n'
        '                "它自己的降级路径本来就有）"\n'
        "            )\n"
        "            return None",
        "        if store is None:\n            return StoreConversationManager.__new__(StoreConversationManager)  # 注入⑪",
    ),
    (
        "⑫ 注入函数不写插件身份 meta",
        "`inject_page_context` 里关掉写身份 meta 那段 —— "
        "垫片拿不到插件名，只能盲试候选（「一起房间」会永远 404）。",
        PAGES,
        "    if plugin_id:\n"
        "        # ② 必须在③**之前**：垫片是同步脚本，执行时立刻读这两个 meta。",
        "    if False:\n"
        "        # ② 必须在③**之前**：垫片是同步脚本，执行时立刻读这两个 meta。",
    ),
    (
        "⑬ 页面路由不传 plugin_id",
        "`_serve` 调 `inject_page_context` 时不给 `plugin_id` —— "
        "注入函数再对也白搭（模块能工作 ≠ 被接上了）。",
        PAGES_WEB,
        "                plugin_id=plugin_id,\n                page_name=page_name,\n",
        "",
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
    return [CTX, MGR, HOST, APP, PAGES, PAGES_WEB]


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
