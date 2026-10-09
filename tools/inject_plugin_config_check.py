#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：证明插件配置相关的判据**真的会失败**。

照既定纪律：基线落盘 + 每轮从快照重读 + 清 `__pycache__` + 要求同批有 `passed`。

## 守的是"配置页看起来能用、实际存不进/存错地方"这一类
插件配置的失败模式**全是静默的**：
  * schema 没接出来 → 前端画不出表单，用户看到"这插件没配置项"
  * 保存只写内存不落盘 → 用户看到"保存成功"，重启就没了
  * 保存写进全局配置 → 改了没用（插件读的是自己那份）
  * 加了原生的"热重载"文案 → **假信息**（我们没这能力）
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / ".inject_plugin_config_baseline.json"
PY = ROOT / ".venv" / "bin" / "python"
HOST = "host/astrbot_host/host_app.py"
APP = "emotion_app/web/app.py"
LOADER = "host/astrbot_host/loader/plugin_loader.py"
TESTS = "tests/test_plugin_config.py"

INJECTIONS = [
    (
        "P1",
        "宿主不留配置对象（我实现前的真实状态）⇒ schema 读不出来",
        LOADER,
        "            rec.config = plugin_config",
        "            pass  # 注入：不留住配置",
    ),
    (
        "P2",
        "保存只改内存不落盘（用户看到成功、重启即丢）",
        HOST,
        "        save(config)",
        "        cfg.update(config)  # 注入：只改内存、不写盘",
    ),
    (
        "P3",
        "保存改成整体替换（其余几百个键静默丢失）",
        HOST,
        "        save(config)",
        # ⚠️ 我第一版注入写的是 `save({})` + `save(config)` —— 那是**空操作**：
        #    `AstrBotConfig._prepare_config_snapshot` 里是 `if replace_config:`
        #    （**空 dict 是 falsy**）⇒ 传空 dict 什么也不做，
        #    两次调用合起来与原代码**逐字等价**，"抓不住"是**正确结论**。
        #    → 换成**真的清空再写**，才有可观察差异（这才是"整体替换"的语义）。
        "        cfg.clear()  # 注入：整体替换（丢其余键）\n        save(config)",
    ),
    (
        "P4",
        "配置读接口漏鉴权（本项目真事故形态）",
        APP,
        """        # 🔴 第一行必须鉴权（本项目真事故：漏了它 → 未登录也能读，
        #    而服务绑 0.0.0.0）
        denied = require_auth()
        if denied is not None:
            return denied
        if service.host is None:""",
        """        if service.host is None:""",
    ),
    (
        "P5",
        "宿主没起来时报 200 + 空配置（把未就绪伪装成没配置项）",
        APP,
        # 锚点用字符串拼接避开嵌套三重引号（我第一次直接嵌套，
        # Python 解析器把内层 `"""` 当成了外层结尾 → 语法错）
        "        denied = require_auth()\n"
        "        if denied is not None:\n"
        "            return denied\n"
        "        if service.host is None:\n"
        "            # \u26a0\ufe0f **\u4e0d\u8fd4\u56de\u7a7a\u914d\u7f6e**\uff1a\u90a3\u4f1a\u628a\"\u5bbf\u4e3b\u6ca1\u8d77\u6765\"\u663e\u793a\u6210\"\u8fd9\u63d2\u4ef6\u6ca1\u914d\u7f6e\"\n"
        "            return (\n"
        "                jsonify(\n"
        "                    {\n"
        "                        \"error\": {\n"
        "                            \"message\": \"\u63d2\u4ef6\u5bbf\u4e3b\u672a\u542f\u52a8\uff08\u8bf7\u770b\u670d\u52a1\u65e5\u5fd7\u91cc\u7684\u539f\u56e0\uff09\",\n"
        "                            \"type\": \"unavailable\",\n"
        "                        }\n"
        "                    }\n"
        "                ),\n"
        "                503,\n"
        "            )\n",
        "        if service.host is None:\n"
        "            return jsonify({\"plugin_name\": plugin_id, \"metadata\": None,\n"
        "                            \"config\": {}, \"i18n\": {}}), 200\n",
    ),
    (
        "P6",
        "文案照抄真实现的【正在热重载】（假信息：我们没这能力）",
        HOST,
        # 用拼接避开嵌套三重引号
        '        return (\n'
        '            f"\u63d2\u4ef6 {plugin_id} \u7684\u914d\u7f6e\u5df2\u4fdd\u5b58\u3002"\n'
        '            "\u26a0\ufe0f \u672c\u670d\u52a1**\u5c1a\u672a\u5b9e\u73b0\u63d2\u4ef6\u70ed\u91cd\u8f7d**\uff0c\u9700\u8981**\u91cd\u542f\u670d\u52a1**\u540e\u751f\u6548\u3002"\n'
        '        )\n',
        '        return f"\u4fdd\u5b58\u63d2\u4ef6 {plugin_id} \u6210\u529f~ \u673a\u5668\u4eba\u6b63\u5728\u70ed\u91cd\u8f7d\u63d2\u4ef6\u3002"\n',
    ),
]


def _run():
    for pc in ROOT.rglob("__pycache__"):
        if ".venv" in str(pc):
            continue
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [str(PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    return r.returncode, r.stdout + r.stderr


def _sum(out: str) -> tuple[int, int]:
    import re
    f = re.search(r"(\d+) failed", out)
    p = re.search(r"(\d+) passed", out)
    return (int(f.group(1)) if f else 0, int(p.group(1)) if p else 0)


def main() -> int:
    files = [HOST, APP, LOADER]
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

    rc, out = _run()
    failed, passed = _sum(out)
    if rc != 0 or failed:
        print(f"❌ 基线不绿（failed={failed} passed={passed}）")
        print(out[-2000:])
        return 1
    print(f"✅ 基线全绿：{passed} passed\n")

    results = []
    try:
        for no, desc, rel, old, new in INJECTIONS:
            src = snap[rel]
            if src.count(old) != 1:
                print(f"  ⚠️ {no} 锚点不唯一({src.count(old)}) —— {desc}")
                results.append((no, desc, "ANCHOR", 0, 0)); continue
            # 语法预检（注入文本自己写坏会让 pytest 收集失败 = 假抓）
            inj = src.replace(old, new)
            import ast as _ast
            try:
                _ast.parse(inj)
            except SyntaxError as e:
                print(f"  ⚠️ {no} 注入文本语法错：{e}")
                results.append((no, desc, "SYNTAX", 0, 0)); continue
            (ROOT / rel).write_text(inj, encoding="utf-8")
            try:
                rc, out = _run()
            finally:
                (ROOT / rel).write_text(src, encoding="utf-8")
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
