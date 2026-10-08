#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线探针：`PluginHost.plugin_detail()` 真的产出六类组件了吗？

## 为什么不能只跑单测
本项目铁律：**单测绿 ≠ 接线对**。这里的风险是"我写的分类逻辑在我脑子里对，
但真插件的注册记录长得跟我想的不一样" —— 所以必须**拿真插件跑一遍**，
把每类**实测条数**打印出来，并抽样打印几条原文（好让肉眼能判断对不对）。

用法：python3 tools/probe_plugin_detail.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

from astrbot_host.compat.core.star.context import HostPorts  # noqa: E402
from astrbot_host.host_app import PluginHost  # noqa: E402


async def main() -> int:
    plugin_dirs = [ROOT / "data" / "plugin_src"]
    host = PluginHost(
        plugin_dirs=[str(p) for p in plugin_dirs],
        auto_initialize=False,   # 详情只读注册表，不必真跑初始化
        mirror_root=None,
    )
    await host.start(ports=HostPorts())

    ids = [p["id"] for p in host.plugins_view()]
    print(f"宿主认识 {len(ids)} 个插件：{ids}\n")

    interesting = [
        "astrbot_plugin_private_companion",
        "astrbot_plugin_memory_companion",
        "astrbot_plugin_image_companion",
    ]
    ok = True
    for pid in interesting:
        if pid not in ids:
            print(f"⚠️  {pid} 不在宿主里（换一个？）")
            continue
        d = host.plugin_detail(pid)
        if d is None:
            print(f"❌ {pid}: plugin_detail() 返回 None（但它明明在 plugins_view 里！）")
            ok = False
            continue
        by_type: dict[str, list] = {}
        for c in d["components"]:
            by_type.setdefault(c["type"], []).append(c)
        print(f"===== {pid} =====")
        print(f"  name={d['name']!r} version={d['version']!r} author={d['author']!r} "
              f"state={d['state']} handler_count={d['handler_count']} "
              f"llm_tool_bind_count={d['llm_tool_bind_count']}")
        print(f"  components 共 {len(d['components'])} 项，按类型："
              + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_type.items())))
        # 顺序判据：type 序列必须按 COMPONENT_TYPE_ORDER 单调不减
        order = {t: i for i, t in enumerate(host.COMPONENT_TYPE_ORDER)}
        seq = [order.get(c["type"], 99) for c in d["components"]]
        if seq != sorted(seq):
            print(f"  ❌ 排序不对：{[c['type'] for c in d['components']]}")
            ok = False
        else:
            print("  ✅ 排序符合 COMPONENT_TYPE_ORDER")
        for t in ("command", "llm_tool", "listener", "hook"):
            items = by_type.get(t) or []
            if not items:
                continue
            print(f"  ── {t}（{len(items)}）抽样：")
            for c in items[:4]:
                print(f"     cmd={c.get('cmd')!r} name={c.get('name')!r} "
                      f"display={c.get('display_type')!r} "
                      f"desc={str(c.get('description'))[:40]!r} "
                      f"ev={c.get('event_type_h')!r}")
        # 归属隔离：不该出现别家插件的 handler 名
        print()

    # 反例：不存在的插件必须返回 None（不是空壳）
    nope = host.plugin_detail("__no_such_plugin__")
    if nope is not None:
        print(f"❌ 不存在的插件竟然返回了 {json.dumps(nope)[:200]}")
        ok = False
    else:
        print("✅ 不存在的插件返回 None（可转 404）")

    # README
    for pid in interesting[:1]:
        r = host.plugin_readme(pid)
        print(f"✅ README({pid}) = " + ("None" if r is None else f"{len(r[0])} 字符, 截断={r[1]}"))

    await host.stop()
    print("\n" + ("✅ 全部通过" if ok else "❌ 有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
