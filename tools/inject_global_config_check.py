#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入验证（P-N5）：**每条新判据都必须能被证明会失败**（情绪app 铁律）。

做法：对每个"注入"（人为把某处改回坏行为）跑对应判据 —— 必须变红；
      然后逐字节还原（sha256 比对），并确认恢复后**全量**重新变绿。

⚠️ 两条纪律（本项目踩过）：
  ① **改前备份 + 改后 sha256 比对**（不留伤）；
  ② 判据"全绿"只有配合"注入能变红"才有意义 —— 否则可能是判据根本没跑，
     或者只测了函数、没测**接线**（P-N6 那次就漏抓过一条同类的）。
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path("/root/Ai-works/情绪app")
PY = str(ROOT / ".venv/bin/python")

PLUGIN_HOST = ROOT / "emotion_app/web/plugin_host.py"
COMPAT_CFG = ROOT / "host/astrbot_host/compat/core/config/astrbot_config.py"
CONTEXT = ROOT / "host/astrbot_host/compat/core/star/context.py"
INFRA_CFG = ROOT / "emotion_app/infra/config.py"

E2E = "tests/test_plugin_host_e2e.py"
GLOB = "host/tests/test_global_config.py"

#: (名字, 文件, 原文, 替换成, 期望变红的判据 node id)
INJECTIONS: list[tuple[str, pathlib.Path, str, str, list[str]]] = [
    (
        "① 装配处改回 config={}（P-N5 的原始 bug）",
        PLUGIN_HOST,
        "                    config=self._load_global_config(),",
        "                    config={},  # 注入：回到恒空配置",
        [f"{E2E}::test_assembled_host_reads_real_global_config"],
    ),
    (
        "② 用 plain utf-8 读（真配置带 BOM → JSONDecodeError）",
        COMPAT_CFG,
        '        with open(target, encoding="utf-8-sig") as fp:',
        '        with open(target, encoding="utf-8") as fp:',
        [
            f"{GLOB}::test_reads_file_with_utf8_bom",
            f"{GLOB}::test_parses_the_real_cmd_config",
        ],
    ),
    (
        "③ 读不到配置时静默给 {}（不记日志）",
        COMPAT_CFG,
        '    except FileNotFoundError:\n        logger.error(\n            "AstrBot 全局配置不存在：%s —— 插件的 get_config() 将返回空字典，"',
        '    except FileNotFoundError:\n        return {}\n        logger.error(\n            "AstrBot 全局配置不存在：%s —— 插件的 get_config() 将返回空字典，"',
        [f"{GLOB}::test_missing_file_logs_error_and_returns_empty"],
    ),
    (
        "④ JSON 坏时静默给 {}（不记日志）",
        COMPAT_CFG,
        "    except (OSError, ValueError) as exc:\n        # ValueError 覆盖 json.JSONDecodeError（它是 ValueError 子类）\n        logger.error(",
        "    except (OSError, ValueError) as exc:\n        return {}\n        logger.error(",
        [f"{GLOB}::test_broken_json_logs_error"],
    ),
    (
        "⑤ 返回 dict 子类而不是 plain dict",
        COMPAT_CFG,
        "    return data",
        "    class _Sub(dict):\n        pass\n\n    return _Sub(data)  # 注入：非 plain dict",
        [f"{GLOB}::test_returns_plain_dict"],
    ),
    (
        "⑥ get_config 改回直接返回 self._ports.config（可能为 None）",
        CONTEXT,
        "        cfg = self._ports.config\n        return cfg if cfg is not None else {}",
        "        return self._ports.config  # 注入：可能返回 None",
        [
            f"{GLOB}::test_get_config_empty_when_host_injects_nothing",
            f"{E2E}::test_missing_global_config_is_loud_not_silent",
        ],
    ),
    (
        "⑦ umo 改成 keyword-only（按位置传的 12 处会被静默打断）",
        CONTEXT,
        "    def get_config(self, umo: str | None = None) -> Any:",
        "    def get_config(self, *, umo: str | None = None) -> Any:",
        [f"{GLOB}::test_get_config_keeps_umo_positional"],
    ),
    (
        "⑧ global_config_path 默认值写错（生产会静默降级）",
        INFRA_CFG,
        '    global_config_path: str = "data/cmd_config.json"',
        '    global_config_path: str = "data/plugins/cmd_config.json"',
        [f"{E2E}::test_real_cmd_config_is_reachable_by_default_config"],
    ),
    (
        "⑨ 装配处不读配置（_load_global_config 恒返回 {}）",
        PLUGIN_HOST,
        "        raw_path = str(getattr(self.config.plugins, \"global_config_path\", \"\") or \"\")",
        "        return {}  # 注入：不真读\n        raw_path = str(getattr(self.config.plugins, \"global_config_path\", \"\") or \"\")",
        [f"{E2E}::test_assembled_host_reads_real_global_config"],
    ),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(nodes: list[str]) -> tuple[bool, str]:
    cmd = [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *nodes]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = proc.stdout.strip().splitlines()
    return proc.returncode != 0, (out[-1] if out else "<no output>")


def main() -> int:
    files = {PLUGIN_HOST, COMPAT_CFG, CONTEXT, INFRA_CFG}
    before = {p: sha(p) for p in files}
    bad: list[str] = []

    print("=" * 78)
    for name, path, old, new, nodes in INJECTIONS:
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"❌ {name}\n   注入点找不到（脚本与代码不同步）")
            bad.append(name)
            continue
        if src.count(old) != 1:
            print(f"❌ {name}\n   注入点出现 {src.count(old)} 次（不唯一）")
            bad.append(name)
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            caught, tail = run(nodes)
        finally:
            path.write_text(src, encoding="utf-8")
        print(f"{'✅' if caught else '❌ 未被抓住'} {name}\n     {tail}")
        if not caught:
            bad.append(name)

    print("=" * 78)
    print("还原校验（sha256 必须与改前一致）：")
    for p, h in before.items():
        now = sha(p)
        print(f"  {'✅' if now == h else '❌'} {p.relative_to(ROOT)}")
        if now != h:
            bad.append(f"还原失败: {p}")

    proc = subprocess.run(
        [PY, "-m", "pytest", "tests", "host/tests", "-q", "--no-header",
         "-p", "no:cacheprovider",
         "--deselect", "tests/test_plugin_host_e2e.py::test_router_matches_real_implementation_live"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )
    tail = proc.stdout.strip().splitlines()[-1]
    print(f"\n恢复后全量（main+host）：{tail}")
    if proc.returncode != 0:
        bad.append("恢复后全量不绿")

    print("=" * 78)
    if bad:
        print(f"❗ {len(bad)} 项有问题：")
        for b in bad:
            print(f"   - {b}")
        return 1
    print(f"🎉 全部 {len(INJECTIONS)} 组注入都被抓住；还原逐字节一致；恢复后全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
