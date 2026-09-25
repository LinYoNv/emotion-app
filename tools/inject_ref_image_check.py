#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（A1：生图参考图真发上游）：**每条新判据都必须能被证明会失败**。

做法：对每个"注入"（人为把某处改回坏行为）跑对应判据 —— 必须变红；
      然后逐字节还原（sha256 比对），并确认恢复后**全量**重新变绿。

🔴 本组最要紧的一条是 ① 与 ⑥：**把参考图静默丢掉**（改回修之前的行为）。
   本项目最贵的一课就是这一类 —— 参考图被过滤掉后**无日志、无报错**，
   用户只能凭感觉报「好像没用上参考图」。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

BACKENDS = ROOT / "emotion_app/domain/backends.py"
IMAGE_GEN = ROOT / "emotion_app/domain/image_gen.py"
IMAGE_TOOL = ROOT / "emotion_app/domain/image_tool.py"

T = "tests/test_s6_image_reference.py"

#: (名字, 文件, 原文, 替换成, 期望变红的判据 node id)
INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    (
        "① 参考图又变回「静默丢弃」（P-N6 之前的老行为）",
        BACKENDS,
        "        if reference_image:\n            # ⚠️ 读不到**响亮报错**，绝不降级成\"悄悄不带参考图\"",
        "        if False and reference_image:  # 注入：参考图被静默丢弃\n            # ⚠️ 读不到**响亮报错**，绝不降级成\"悄悄不带参考图\"",
        [
            f"{T}::test_backend_selfie_uses_director_endpoint",
            f"{T}::test_tool_entry_passes_reference_all_the_way_to_http",
        ],
    ),
    (
        "② edit 不再走真 img2img（退化成精准参考）",
        BACKENDS,
        "            if kind == KIND_EDIT:\n                log.info(\n                    \"生图带参考图\",",
        "            if False and kind == KIND_EDIT:  # 注入\n                log.info(\n                    \"生图带参考图\",",
        [f"{T}::test_backend_edit_uses_img2img_endpoint"],
    ),
    (
        "③ information_extracted 写成 0.7（上游实测会 400）",
        IMAGE_GEN,
        '        params["director_reference_information_extracted"] = [1.0] * len(refs)',
        '        params["director_reference_information_extracted"] = [0.7] * len(refs)  # 注入',
        [f"{T}::test_director_information_extracted_must_be_one"],
    ),
    (
        "④ 不再为精准参考切模型（NAI5 + director 实测会 500）",
        IMAGE_GEN,
        "        if model.strip().casefold() not in NAI_DIRECTOR_MODELS:",
        "        if False:  # 注入：不切模型",
        [f"{T}::test_generate_switches_model_off_nai5_and_is_loud"],
    ),
    (
        "⑤ 切模型但**不打日志**（静默改画风）",
        IMAGE_GEN,
        "        if model.strip().casefold() not in NAI_DIRECTOR_MODELS:\n            log.warning(",
        "        if model.strip().casefold() not in NAI_DIRECTOR_MODELS:\n            None and log.warning(  # 注入：不记日志",
        [f"{T}::test_generate_switches_model_off_nai5_and_is_loud"],
    ),
    (
        "⑥ 读不到参考图时返回空串（= 静默降级成纯文生图）",
        IMAGE_GEN,
        '    if not path.is_file():\n        raise ImageError(\n            f"参考图不存在或不是文件：{text}", code="reference_unreadable"\n        )',
        '    if not path.is_file():\n        return ""  # 注入：静默空串',
        [
            f"{T}::test_reference_data_uri_raises_on_unreadable",
            f"{T}::test_backend_reference_unreadable_raises_not_silently_dropped",
        ],
    ),
    (
        "⑦ 精准参考只发 3 个数组（五个必须等长）",
        IMAGE_GEN,
        '        params["director_reference_secondary_strength_values"] = [0.5] * len(refs)',
        "        _ = None  # 注入：少一个数组",
        [f"{T}::test_generate_sends_director_five_arrays_of_equal_length"],
    ),
    (
        "⑧ 直接把**文件路径**当参考图发（不是 data URI）",
        BACKENDS,
        "            uri = reference_data_uri(reference_image)",
        "            uri = reference_image  # 注入：直接传路径",
        [
            f"{T}::test_backend_edit_uses_img2img_endpoint",
            f"{T}::test_tool_entry_passes_reference_all_the_way_to_http",
        ],
    ),
    (
        "⑨ edit 没参考图时不再拒绝（退化成纯文生图）",
        BACKENDS,
        '            if kind == KIND_EDIT:\n                raise ImageError("edit 缺少参考图", code="reference_unreadable")',
        "            if kind == KIND_EDIT:\n                pass  # 注入：不拒绝",
        [f"{T}::test_backend_edit_without_reference_raises"],
    ),
    (
        "⑩ 工具层不再把 reference_unreadable 如实告诉模型",
        IMAGE_TOOL,
        '        if exc.code == "reference_unreadable":',
        "        if False:  # 注入",
        [f"{T}::test_tool_reports_reference_unreadable_to_model"],
    ),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_judges(nodes: list[str]) -> tuple[bool, str]:
    """跑判据；返回 (是否全绿, 摘要)。"""
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
