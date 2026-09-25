#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（人设固定参考图）：**每条新判据都必须能被证明会失败**。

本组最要紧的两条：
  ③ 兜底逻辑**漏进 `edit`** —— 会把"改用户给的这张图"静默变成"画角色"；
  ⑦ 用兜底图时**不打日志** —— 参考图从哪来无从追溯（本项目最贵的一课）。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

TOOL = ROOT / "emotion_app/domain/image_tool.py"
MAIN = ROOT / "emotion_app/__main__.py"
CFG = ROOT / "emotion_app/infra/config.py"
T = "tests/test_s6_image_reference.py"

INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    (
        "① 兜底逻辑整个去掉（selfie 不再带人设图）",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if False and self._persona_reference:  # 注入",
        [f"{T}::test_persona_reference_is_used_for_selfie_without_explicit_path"],
    ),
    (
        "② 显式路径被人设图顶掉（顺序反了）",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if kind == image_gen.KIND_SELFIE and self._persona_reference:  # 注入：不看 ref",
        [f"{T}::test_explicit_path_wins_over_persona_reference"],
    ),
    (
        "③ 兜底逻辑漏进 edit（静默换掉「改这张图」的语义）",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if not ref and self._persona_reference:  # 注入：连 edit 也兜底",
        [f"{T}::test_persona_reference_does_not_leak_into_edit"],
    ),
    (
        "④ 兜底逻辑漏进 text2img（给风景图注入角色）",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if not ref and kind != image_gen.KIND_EDIT and self._persona_reference:  # 注入",
        [f"{T}::test_persona_reference_does_not_leak_into_text2img"],
    ),
    (
        "⑤ 没配人设图时也硬塞一个空参考图（把 selfie 弄崩）",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if not ref and kind == image_gen.KIND_SELFIE:  # 注入：不判空\n            self._persona_reference = self._persona_reference or ''",
        [f"{T}::test_persona_reference_falls_back_cleanly_when_unset"],
    ),
    (
        "⑥ 人设图读不到时静默降级成纯文生图",
        TOOL,
        "        if not ref and kind == image_gen.KIND_SELFIE and self._persona_reference:",
        "        if (not ref and kind == image_gen.KIND_SELFIE and self._persona_reference\n                and __import__('pathlib').Path(self._persona_reference).is_file()):  # 注入",
        [f"{T}::test_persona_reference_missing_file_is_loud_not_silent"],
    ),
    (
        "⑦ 用兜底图时不打日志（参考图从哪来无从追溯）",
        TOOL,
        '            log.info(\n                "selfie 未指定参考图，使用人设固定参考图",',
        '            None and log.info(  # 注入：不留痕\n                "selfie 未指定参考图，使用人设固定参考图",',
        [f"{T}::test_persona_reference_use_is_logged"],
    ),
    (
        "⑧ 装配处不传 persona_reference（配置白填）",
        MAIN,
        "        persona_reference=config.image.persona_reference,",
        "        persona_reference=\"\",  # 注入",
        ["tests/test_assembly.py::test_persona_reference_is_wired_from_config"],
    ),
    (
        "⑨ set_persona_reference 改成空操作（切人设不生效）",
        TOOL,
        "        self._persona_reference = str(path or \"\").strip()\n        log.info(",
        "        log.info(  # 注入：不真的换\n",
        [f"{T}::test_set_persona_reference_switches_default"],
    ),
    (
        "⑩ config 的 env 绑定去掉（env 配了也没用）",
        CFG,
        '    "IMAGE_PERSONA_REFERENCE": ("image", "persona_reference", "str"),',
        '    # "IMAGE_PERSONA_REFERENCE": 注入',
        ["tests/test_assembly.py::test_persona_reference_env_key_is_wired"],
    ),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_judges(nodes: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", *nodes, "-q", "--no-header", "-x", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]
    return proc.returncode == 0, (tail[0] if tail else "?")


def main() -> int:
    files = sorted({inj[1] for inj in INJECTIONS})
    before = {f: sha(f) for f in files}
    print("注入前 sha256：")
    for f, h in before.items():
        print(f"  {f.relative_to(ROOT)}  {h[:16]}…")

    failures: list[str] = []
    for name, path, old, new, nodes in INJECTIONS:
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"\n❌ {name}：原文匹配 {text.count(old)} 次（应为 1），跳过")
            failures.append(name)
            continue
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        try:
            green, summary = run_judges(nodes)
        finally:
            path.write_text(text, encoding="utf-8")
        if green:
            print(f"\n❌ {name}：注入后判据**仍然全绿** —— 判据抓不到这个 bug！")
            failures.append(name)
        else:
            print(f"\n✅ {name}\n     {summary}")

    print("\n还原校验（sha256 必须与改前一致）：")
    ok_restore = True
    for f, h in before.items():
        same = sha(f) == h
        ok_restore &= same
        print(f"  {'✅' if same else '🔴'} {f.relative_to(ROOT)}")

    print("\n恢复后全量（main + host）：")
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests", "host/tests", "-q", "--no-header",
         "--deselect", "tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print("  " + [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1])

    if failures or not ok_restore or proc.returncode != 0:
        print(f"\n🔴 未通过：注入未抓住 = {failures}；还原一致 = {ok_restore}")
        return 1
    print(f"\n🎉 全部 {len(INJECTIONS)} 组注入都被抓住；还原逐字节一致；恢复后全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
