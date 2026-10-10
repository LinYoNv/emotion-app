#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：`PluginHost.plugin_detail()` 的判据**真的会红**吗？

## 为什么必须有这一步
本项目铁律：「单测全绿 ≠ 判据有效」。判据写完永远绿，往往是它**永远不会失败**
（踩过：否定式断言的字面串与产品实际打的串对不上 → 那条断言永远为真）。

这里逐个把 `plugin_detail()` 的关键实现改坏，看对应判据是否**真的 failed**。

## 三道防线（都是踩过的坑，缺一条就会得到假结论）
  ① **基线落盘**在脚本之外（本文件旁边 `.inject_plugin_detail_baseline.json`）——
     脚本被墙钟杀掉时源码可能停在注入态，下一次运行读到的"注入前"就是坏文件
     （2026-09-25 真实事故：`api_admin.py` 的 `require_auth()` 真丢了）。
     启动时先自检残留：不一致就**拒绝运行**。
  ② **每轮从落盘快照重读**，不叠加上一轮的注入（踩过：注入累加 → 全"红"却是假的）。
  ③ **跑前清 `__pycache__`** + pytest `-p no:cacheprovider`。
     CPython 判 `.pyc` 有效用 (mtime 整秒, 字节数)：同秒同长度的改写会让它
     加载**上一次注入**的字节码 → 假绿/假红（踩过）。
  ④ 要求输出里**真的出现 `failed`**，且同批里**必须有 `passed`**
     （只有 failed 可能是语法错/收集错，判据根本没跑起来）。

用法：python3 tools/inject_plugin_detail_check.py
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
TARGET = ROOT / "host" / "astrbot_host" / "host_app.py"
#: 🔴 基线**落盘到工具之外**（见模块说明①）
BASELINE = Path(__file__).resolve().parent / ".inject_plugin_detail_baseline.json"

#: 只跑这几条判据（跑全量太慢，且与本能力无关）
TEST_NODE = "host/tests/test_plugin_detail.py"

#: 每条注入：`(名字, 说明, 原文, 改后)`。原文必须**逐字**出现在源码里。
INJECTIONS: list[tuple[str, str, str, str]] = [
    (
        "① 归属用精确匹配（漏子模块）",
        "把 `_handlers_of` 的前缀去掉 → 子模块 `<dir>.atrelay` 的 handler 不再被列出。"
        "这正是**全量跑才暴露**过的真 bug（详情页少列一项）。",
        '        package = module_path.split(".")[0]   # `<目录名>.main` → `<目录名>`\n        prefix = f"{package}."',
        '        package = module_path   # 注入①：精确匹配（漏子模块）\n        prefix = f"{package}"',
    ),
    (
        "② LLM 工具按名字去重（同一工具列两遍）",
        "把配对键从 handler **对象标识**（`id()`）换成**函数名**。"
        "⚠️ 这条我改了两版才对，值得记下来："
        "**第一版**在赋值**之后**插 `continue` —— 与原代码**语义等价**（真正的去重靠"
        "dict 的 `id()` 键 + 后面那次 `pop`，不是靠 `continue`）⇒ 那是**无效注入**，"
        "不是判据弱；**第二版**（本版）改的是键本身：键变了，后面按 `id()` 的 `pop` "
        "必然配不上 ⇒ 每个工具都会走「多出来的」那条分支再列一遍 ⇒ 真的重复。",
        "                tool_by_handler_id[id(unwrapped)] = spec",
        '                tool_by_handler_id[getattr(unwrapped, "__name__", "")] = spec  # 注入②',
    ),
    (
        "③ 归属过滤去掉（串到别家插件）",
        "删掉 `owner != rec.module_path` 那道闸 → 别的插件的工具会出现在本插件详情里。",
        "                if owner != rec.module_path:\n                    continue\n                tool_by_handler_id[id(unwrapped)] = spec",
        "                tool_by_handler_id[id(unwrapped)] = spec",
    ),
    (
        "④ 排序去掉（组件顺序乱）",
        "删掉排序 → 类型顺序不再满足 `PLUGIN_COMPONENT_TYPE_ORDER`。",
        '        components.sort(key=lambda c: order.get(c["type"], 99))',
        '        pass  # 注入④：不排序',
    ),
    (
        "⑤ 不存在的插件返回空壳（404 变 200）",
        "`plugin_detail` 对未知 id 返回一个空 dict 而不是 None。",
        "        rec = self.loader.records.get(plugin_id)\n        if rec is None:\n            return None",
        "        rec = self.loader.records.get(plugin_id)\n        if rec is None:\n            return {'id': plugin_id, 'components': []}",
    ),
    (
        "⑥ 临时字段不清（jsonify 会 500）",
        "删掉兜底清除 → `_handler_obj`（函数对象）留在响应里，JSON 化直接抛错。"
        "⚠️ 第一版这条抓不住是**产品侧的缺陷**：pop 循环里顺手 `pop` 过一次，"
        "兜底清理因此是**不可达代码**。已改成只在兜底处清除 —— 现在这条注入有意义。",
        '        for c in components:\n            for k in [k for k in c if k.startswith("_")]:\n                c.pop(k, None)',
        '        pass  # 注入⑥：不清临时字段',
    ),
    (
        "⑦ 指令的 cmd 不用过滤器里的真名",
        "把指令名的来源换成 handler 名 → 面板上「指令」一列显示的是函数名而不是「陪伴」。",
        '                    flt = cmd_flts[0]\n                    components.append({\n                        **base, "type": "command", "display_type": "指令",\n                        "cmd": _cmd_of(flt) or "未知",',
        '                    flt = cmd_flts[0]\n                    components.append({\n                        **base, "type": "command", "display_type": "指令",\n                        "cmd": h.handler_name,',
    ),
    (
        "⑧ 事件类型中文映射去掉",
        "把 `OnLLMRequestEvent` 那项改成不存在的键 → 钩子的 `event_type_h` 变成英文"
        "枚举名，面板上「触发」一列不再可读。"
        "⚠️ 锚点必须选**判据真正检查的那个键**：我第一版改的是表里最前面那项"
        "（`AdapterMessageEvent`），而断言查的是 `LLM 请求时` ⇒ 抓不住。"
        "那是**注入无效**（选错锚点），不是判据弱。",
        '        "OnLLMRequestEvent": "LLM 请求时",',
        '        "__never_matches__": "LLM 请求时",',
    ),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_baseline() -> dict:
    if not BASELINE.is_file():
        return {}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_baseline(data: dict) -> None:
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _purge_pycache() -> None:
    """🔴 清所有 `__pycache__`（见模块说明③）。"""
    for p in ROOT.rglob("__pycache__"):
        if ".venv" in p.parts:
            continue
        shutil.rmtree(p, ignore_errors=True)


def _restore(src: str) -> None:
    TARGET.write_text(src, encoding="utf-8")
    _purge_pycache()


def _run_tests() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest", TEST_NODE,
         "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    src_now = TARGET.read_text(encoding="utf-8")
    base = _load_baseline()
    key = str(TARGET)

    # ---- ① 启动自检：上一次是不是被杀在注入态？ ----
    if base.get(key) and base[key]["sha256"] != _sha(src_now):
        # 🔴 2026-10-10：用 git 区分**基线过期**与**真的停在注入态**。
        #    基线文件不在 git 里 ⇒ 源码每正常提交一次，"当前 ≠ 基线"就成立
        #    （实测批量跑时 13 套工具全是这个**假报警**，而产品完全正常）。
        #    ⚠️ 不能删掉这个保护（2026-09-25 真事故的产物）。
        import subprocess as _sp
        _r = _sp.run(["git", "status", "--porcelain", "--", "host", "emotion_app"],
                     cwd=ROOT, capture_output=True, text=True, timeout=60)
        if _r.returncode == 0 and not _r.stdout.strip():
            print("ℹ️ 源码与基线不一致，但工作区干净 ⇒ "
                  "是正常提交造成基线过期，按当前源码刷新基线")
            _save_baseline({key: {"sha256": _sha(src_now)}})
        else:
            print("❌ 源码与落盘基线**不一致** —— 上一次注入验证可能被中断、源码停在注入态。")
            print(f"   基线 sha={base[key]['sha256'][:16]}… 当前 sha={_sha(src_now)[:16]}…")
            print(f"   基线文件：{BASELINE}")
            return 2

    if not base.get(key):
        _save_baseline({key: {"sha256": _sha(src_now)}})
        print(f"✅ 已记录基线（{_sha(src_now)[:16]}…）→ {BASELINE}")
    else:
        print(f"✅ 基线自检通过（{_sha(src_now)[:16]}…）")

    # ---- 抢救还原（被 SIGTERM/SIGINT 杀掉时把源码放回去） ----
    original = src_now

    def _rescue(signum, _frame):  # noqa: ANN001
        print(f"\n⚠️ 收到信号 {signum} —— 正在还原源码…")
        _restore(original)
        print("✅ 已还原")
        sys.exit(3)

    signal.signal(signal.SIGTERM, _rescue)
    signal.signal(signal.SIGINT, _rescue)

    # ---- 基线跑一遍（必须有 passed） ----
    _restore(original)
    rc, out = _run_tests()
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
    print(f"\n[基线] rc={rc} {tail[0] if tail else out[-200:]}")
    if rc != 0 or "passed" not in out:
        print("❌ 基线就不是绿的 —— 注入验证无意义，先修基线")
        return 1
    base_passed = int(re.search(r"(\d+) passed", out).group(1))

    caught = 0
    results: list[tuple[str, bool, str]] = []
    for name, why, old, new in INJECTIONS:
        if old not in original:
            print(f"\n⚠️ 跳过 {name}：源码里找不到注入锚点（说明实现已改，注入文本要跟着更新）")
            results.append((name, False, "锚点不存在"))
            continue
        # 🔴 每轮**从原始快照重读**（见模块说明②）
        mutated = original.replace(old, new, 1)
        TARGET.write_text(mutated, encoding="utf-8")
        # 语法预检（踩过：注入文本自己写坏 → 6 条判据全"红"却是假的）
        try:
            compile(mutated, str(TARGET), "exec")
        except SyntaxError as exc:
            print(f"\n❌ {name}：注入文本自己写坏了（{exc}）—— 记为未抓住")
            results.append((name, False, f"注入文本语法错：{exc}"))
            _restore(original)
            continue
        _purge_pycache()
        try:
            rc2, out2 = _run_tests()
        finally:
            _restore(original)

        # 🔴 必须**真的 failed**，且同批**也有 passed**（见模块说明④）
        real_fail = bool(re.search(r"\b\d+ failed", out2)) and "failed" in out2
        ran_some = bool(re.search(r"\d+ passed", out2))
        ok = rc2 != 0 and real_fail and ran_some
        if ok:
            caught += 1
        last = [ln for ln in out2.strip().splitlines() if ln.strip()][-1:]
        print(f"\n{'✅ 抓住' if ok else '❌ 没抓住'} {name}")
        print(f"   为什么这条注入该被抓住：{why}")
        print(f"   rc={rc2} 真failed={real_fail} 判据跑起来了={ran_some} | {last[0] if last else ''}")
        results.append((name, ok, last[0] if last else ""))

    print(f"\n{'=' * 62}")
    print(f"注入验证：**{caught}/{len(INJECTIONS)}** 抓住（基线 {base_passed} passed）")
    for name, ok, info in results:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{info}]" if not ok else ""))
    return 0 if caught == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
