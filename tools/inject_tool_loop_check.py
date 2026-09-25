#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证：**每条新判据都必须能被证明会失败**（情绪app 铁律）。

做法：对每个"注入"（人为把某处改回坏行为）跑对应判据 —— 必须变红；
      然后逐字节还原（sha256 比对），并确认恢复后判据重新变绿。

⚠️ 两条纪律（本项目踩过）：
  ① **改前备份 + 改后 sha256 比对**（不留伤）；
  ② 判据"全绿"只有配合"注入能变红"才有意义 —— 否则可能是判据根本没跑。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

RUNNER = ROOT / "host/astrbot_host/compat/core/astr_main_agent.py"
TOOLS = ROOT / "host/astrbot_host/compat/core/agent/tool.py"
REG = ROOT / "host/astrbot_host/registry.py"
LOADER = ROOT / "host/astrbot_host/loader/plugin_loader.py"
PROV = ROOT / "host/astrbot_host/provider.py"

#: (名字, 文件, 原文, 替换成, 期望变红的判据 node id)
INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    (
        "① runner 去掉 step_until_done（插件会静默跳过工具循环）",
        RUNNER,
        "    async def step_until_done(\n        self, max_step: int\n    ) -> AsyncGenerator[LLMResponse, None]:",
        "    async def _disabled_step_until_done(\n        self, max_step: int\n    ) -> AsyncGenerator[LLMResponse, None]:",
        ["test_runner_exposes_the_api_the_plugin_calls"],
    ),
    (
        "② 循环不执行工具（回到'说出来就结束'）",
        RUNNER,
        "        await self._execute_tools(resp)",
        "        pass  # 注入：不执行工具",
        [
            "test_tool_loop_executes_the_tool_and_feeds_result_back",
            "test_real_plugin_tool_loop_really_executes",
        ],
    ),
    (
        "③ 工具结果不回填上下文（模型拿不到结果）",
        RUNNER,
        "        self._messages.append(assistant_msg)\n        self._messages.extend(tool_results)",
        "        pass  # 注入：不回填",
        [
            "test_tool_loop_executes_the_tool_and_feeds_result_back",
            "test_unknown_tool_is_backfilled_as_error_not_crash",
        ],
    ),
    (
        "④ arguments 给 dict 而不是 JSON 字符串（协议错）",
        RUNNER,
        '"arguments": _json_args(args),',
        '"arguments": args,',
        ["test_tool_loop_executes_the_tool_and_feeds_result_back"],
    ),
    (
        "⑤ 达上限不拔工具（模型会无限要工具）",
        RUNNER,
        "            self._req.func_tool = None",
        "            pass  # 注入：不拔工具",
        ["test_tool_loop_stops_after_max_steps_and_strips_tools"],
    ),
    (
        "⑥ 工具找不到时抛异常（而非回填错误给模型）",
        RUNNER,
        '                logger.warning(\n                    "未找到指定的工具: %s（可用：%s），已把错误作为工具结果回填",',
        '                raise RuntimeError("injected: tool not found")\n                logger.warning(\n                    "未找到指定的工具: %s（可用：%s），已把错误作为工具结果回填",',
        ["test_unknown_tool_is_backfilled_as_error_not_crash"],
    ),
    (
        "⑦ 工具异常不记 ERROR（静默失败）",
        RUNNER,
        '                logger.error("工具 %s 执行失败：%s", name, exc, exc_info=True)',
        "                pass  # 注入：不记日志",
        ["test_tool_handler_exception_is_logged_and_backfilled"],
    ),
    (
        "⑧ 参数不按 schema 过滤（把非期望参数也传进去）",
        RUNNER,
        "                valid_args = {k: v for k, v in tool_args.items() if k in expected}",
        "                valid_args = dict(tool_args)  # 注入：不过滤",
        ["test_tool_args_filtered_by_schema"],
    ),
    (
        "⑨ ToolSet.merge 变空操作（插件工具进不来）",
        TOOLS,
        "        for tool in getattr(other, \"tools\", None) or []:\n            self.add_tool(tool)",
        "        pass  # 注入：merge 不做事",
        ["test_toolset_has_the_real_collection_api", "test_real_plugin_tools_merge_into_request"],
    ),
    (
        "⑩ get_full_tool_set 返回空（工具一个都交不出去）",
        REG,
        "        tool_set = ToolSet()\n        for spec in self._tools.values():",
        "        tool_set = ToolSet()\n        for spec in []:  # 注入：空",
        ["test_tool_manager_exposes_real_api_and_full_tool_set", "test_real_plugin_tools_merge_into_request"],
    ),
    (
        "⑪ 不绑插件实例（handler 缺 self，执行必错）",
        LOADER,
        "            if instance is not None:\n                spec.handler = functools.partial(raw, instance)\n            n += 1",
        "            n += 1  # 注入：不绑实例",
        [
            "test_real_plugin_tool_handler_is_bound_to_instance",
            "test_real_plugin_tool_loop_really_executes",
        ],
    ),
    (
        "⑫ partial 归属判定回退（重载后绑到旧实例）—— 本轮真 bug 的复现",
        LOADER,
        "            raw = handler.func if isinstance(handler, functools.partial) else handler\n            owner = (\n                getattr(spec, \"handler_module_path\", None)\n                or getattr(raw, \"__module__\", None)\n            )",
        "            raw = handler.func if isinstance(handler, functools.partial) else handler\n            owner = getattr(handler, \"__module__\", None)  # 注入：partial.__module__ 恒为 functools",
        ["test_reload_rebinds_tools_to_the_new_instance"],
    ),
    (
        "⑬ tool_calls_result 放回忽略集合（工具结果回不到模型）",
        PROV,
        '        "audio_urls",\n        "extra_user_content_parts",',
        '        "audio_urls",\n        "tool_calls_result",\n        "extra_user_content_parts",',
        ["test_provider_forwards_tool_calls_result_as_messages"],
    ),
    (
        "⑭ text_chat 不拼接工具结果消息",
        PROV,
        "        for block in _iter_tool_calls_results(tool_calls_result):",
        "        for block in []:  # 注入：不拼接",
        ["test_provider_forwards_tool_calls_result_as_messages"],
    ),
    (
        "⑮ build_main_agent 不并入插件工具",
        RUNNER,
        "    _merge_plugin_tools(req, plugin_context)",
        "    pass  # 注入：不并入插件工具",
        [
            "test_build_main_agent_wires_plugin_tools_into_the_request",
            "test_real_plugin_tools_merge_into_request",
        ],
    ),
    (
        "⑯ 签名兜底重新变成'无条件吞 TypeError'",
        RUNNER,
        "            if not _looks_like_signature_mismatch(exc, args):\n                raise",
        "            pass  # 注入：无条件吞",
        ["test_handler_inner_typeerror_propagates_and_is_backfilled"],
    ),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(judges: list[str]) -> tuple[bool, str]:
    """跑判据，返回 (是否有失败, 摘要)。"""
    cmd = [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    cmd += [f"host/tests/test_tool_loop.py::k" if False else f"host/tests/test_tool_loop.py::{j}" for j in judges]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    out = proc.stdout.strip().splitlines()
    tail = out[-1] if out else ""
    return proc.returncode != 0, tail


def main() -> int:
    before = {p: sha(p) for p in {RUNNER, TOOLS, REG, LOADER, PROV}}
    failures: list[str] = []
    print("=" * 78)
    for name, path, old, new, judges in INJECTIONS:
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"❌ {name}\n   注入点找不到（脚本与代码不同步）→ 跳过")
            failures.append(name)
            continue
        if src.count(old) != 1:
            print(f"❌ {name}\n   注入点出现 {src.count(old)} 次（不唯一）→ 跳过")
            failures.append(name)
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            caught, tail = run(judges)
        finally:
            path.write_text(src, encoding="utf-8")
        mark = "✅" if caught else "❌ 未被抓住"
        print(f"{mark} {name}\n     {tail}")
        if not caught:
            failures.append(name)

    print("=" * 78)
    print("还原校验（sha256 必须与改前一致）：")
    for p, h in before.items():
        now = sha(p)
        ok = "✅" if now == h else "❌"
        print(f"  {ok} {p.relative_to(ROOT)}")
        if now != h:
            failures.append(f"还原失败: {p}")

    # 恢复后必须重新全绿
    proc = subprocess.run(
        [PY, "-m", "pytest", "host/tests", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    tail = proc.stdout.strip().splitlines()[-1]
    print(f"\n恢复后 host 全量：{tail}")
    if proc.returncode != 0:
        failures.append("恢复后 host 全量不绿")

    print("=" * 78)
    if failures:
        print(f"❗ {len(failures)} 项有问题：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"🎉 全部 {len(INJECTIONS)} 组注入都被抓住；还原逐字节一致；恢复后全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
