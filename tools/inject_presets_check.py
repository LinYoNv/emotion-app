#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（N4：本地扩展预设）：**每条新判据都必须能被证明会失败**。

本组最要紧的两条：
  ① 路由改回 `PRESETS` —— 函数判据会全绿，但**面板上那 7 条消失**
     （本项目踩过「函数对 ≠ 接线对」）；
  ④ 把扩展**塞进** `PRESETS` —— 忠实副本悄悄腐化（对着 AstrBot 源数据的那条比对失去意义）。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

PRESETS_PY = ROOT / "emotion_app/infra/provider_presets.py"
T = "tests/test_provider_presets.py"

#: (名字, 文件, 原文, 替换成, 期望变红的判据 node id)
INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    (
        "① list_presets 改回只读 PRESETS（面板上扩展消失）",
        PRESETS_PY,
        "        picked = list(ALL_PRESETS)",
        "        picked = list(PRESETS)  # 注入",
        [
            f"{T}::test_route_actually_serves_local_extensions",
            f"{T}::test_list_presets_filters_by_type",
        ],
    ),
    (
        "② 扩展的适配器换成我们跑不了的",
        PRESETS_PY,
        "        id='zhipu-openai', label='智谱 GLM（OpenAI 格式）', provider='zhipu',\n        type='openai_chat_completion',",
        "        id='zhipu-openai', label='智谱 GLM（OpenAI 格式）', provider='zhipu',\n        type='zhipu_chat_completion',  # 注入",
        [f"{T}::test_local_extensions_are_all_executable_openai_chat"],
    ),
    (
        "③ longcat 扩展地址去掉 /v1（源数据那个坑又回来了）",
        PRESETS_PY,
        "api_base='https://api.longcat.chat/openai/v1',",
        "api_base='https://api.longcat.chat/openai',  # 注入",
        [f"{T}::test_longcat_extension_url_carries_v1"],
    ),
    (
        "④ 把扩展塞进忠实副本（腐化对着 AstrBot 源的比对）",
        PRESETS_PY,
        "ALL_PRESETS: tuple[ProviderPreset, ...] = PRESETS + LOCAL_EXTENSION_PRESETS",
        "ALL_PRESETS: tuple[ProviderPreset, ...] = PRESETS + LOCAL_EXTENSION_PRESETS\nPRESETS = ALL_PRESETS  # 注入：污染忠实副本",
        [
            f"{T}::test_preset_ids_match_source_exactly",
            f"{T}::test_local_extensions_do_not_pollute_the_faithful_copy",
        ],
    ),
    (
        "⑤ groq 的 note 不再说明「未剔除 reasoning」",
        PRESETS_PY,
        '        note=NOTE_LOCAL_EXTENSION + "；⚠️ AstrBot 的 groq 适配器会从 assistant 历史里剔除 reasoning 字段（Groq 会拒收），本平台未做该清理，长对话若带上思维链可能被 Groq 拒绝",',
        "        note=NOTE_LOCAL_EXTENSION,  # 注入",
        [f"{T}::test_local_extension_notes_disclose_unported_adapter_behaviour"],
    ),
    (
        "⑥ find_preset 改回只搜忠实副本（面板按 id 找不到扩展）",
        PRESETS_PY,
        "    for preset in ALL_PRESETS:\n        if preset.id == wanted:",
        "    for preset in PRESETS:  # 注入\n        if preset.id == wanted:",
        [f"{T}::test_find_preset_reaches_local_extensions"],
    ),
    (
        "⑦ 给 anthropic（真专有）也加一条 OpenAI 扩展",
        PRESETS_PY,
        "    ProviderPreset(\n        id='xiaomi-openai',",
        "    ProviderPreset(\n        id='anthropic-openai', label='Anthropic（OpenAI 格式）', provider='anthropic',\n        type='openai_chat_completion', provider_type=PROVIDER_TYPE_CHAT, api_base='https://api.anthropic.com/v1',\n        note=NOTE_LOCAL_EXTENSION,\n    ),  # 注入\n    ProviderPreset(\n        id='xiaomi-openai',",
        [f"{T}::test_known_non_openai_vendors_are_not_extended"],
    ),
    (
        "⑧ 扩展的 api_base 留空（让用户自己猜）",
        PRESETS_PY,
        "api_base='https://api.xiaomimimo.com/v1',\n        note=NOTE_LOCAL_EXTENSION,",
        "api_base='',  # 注入\n        note=NOTE_LOCAL_EXTENSION,",
        [f"{T}::test_local_extensions_have_urls_and_notes"],
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
