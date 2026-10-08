#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：**真陪伴插件**走它自己的存档函数，能不能写进我们的 store。

## 为什么还要这一个（上一个探针不够）
上一个 `probe_conversation_archive.py` 是**我按插件的调用顺序手写**的 ——
它证明"端口按契约工作"，但**证明不了"插件真的会这样调"**。
本项目最贵的一课就是：**读我们自己的源码永远看不出插件的真实行为**。

本探针反过来：**加载真插件 → 拿到它的实例 → 调它自己的
`_ensure_conversation_id_for_umo()` 与 `add_message_pair` 那条路径**，
再看我们的 store 里有没有东西。

## 判据
  ① 插件实例的 `self.context.conversation_manager` **不是 None**
     （补之前这里会抛 `NotImplementedError`）
  ② 调插件自己的 `_ensure_conversation_id_for_umo(umo)` 能拿到 cid
  ③ 走**插件自己的** `add_message_pair` 写完 → 我们的 store 里真能看到
  ④ 清理干净（不留脏数据）

用法：python3 tools/probe_real_plugin_archive.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 🔴 `host/` 也要在 path 上（`astrbot_host` 是它的顶层包）
sys.path.insert(0, str(ROOT / "host"))

_ENV_FILE = ROOT / ".env"
if _ENV_FILE.is_file():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

PROBE_UMO = "11:FriendMessage:__realprobe__"
PLUGIN_DIR = "astrbot_plugin_private_companion"


async def main() -> int:
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.host_app import PluginHost
    from astrbot_host.shadow import install_shadow_package
    from emotion_app.domain.conversation_manager import StoreConversationManager
    from emotion_app.infra.config import load_config
    from emotion_app.storage.sqlite_store import SqliteStore

    ok = True
    install_shadow_package()
    config = load_config()
    store = SqliteStore(config.storage.sqlite_path)
    await store.open()
    mgr = StoreConversationManager(store)

    # 清理上次残留
    sec = await mgr._load()  # noqa: SLF001
    for k in [k for k in sec if "__realprobe__" in str(k)]:
        sec.pop(k, None)
    await mgr._save(sec)  # noqa: SLF001

    host = PluginHost(
        plugin_dirs=[str(ROOT / "data" / "plugin_src")],
        auto_initialize=False,          # 只要实例，不跑 initialize（省时间且无副作用）
        mirror_root=None,
    )
    await host.start(ports=HostPorts(
        conversation_manager=mgr,
        config=os.environ.get("__none__") or {},
        data_dir=str(ROOT / "data" / "plugin_data"),
    ))

    try:
        rec = host.loader.records.get(PLUGIN_DIR) if host.loader else None
        if rec is None or rec.instance is None:
            print(f"❌ 插件没加载成功（state={getattr(rec, 'state', '?')}）")
            return 1
        plugin = rec.instance

        # ---- ① 插件通过 context 能拿到端口 ----
        ctx = getattr(plugin, "context", None)
        if ctx is None:
            print("❌ 插件实例没有 context")
            return 1
        try:
            cm = ctx.conversation_manager
        except Exception as exc:  # noqa: BLE001
            print(f"❌ ① ctx.conversation_manager 仍不可用：{type(exc).__name__}: {exc}")
            return 1
        print(f"✅ ① ctx.conversation_manager = {type(cm).__name__}")
        if cm is None:
            print("   ❌ 竟然是 None")
            ok = False

        # ---- ② 调**插件自己的** `_ensure_conversation_id_for_umo` ----
        ensure = getattr(plugin, "_ensure_conversation_id_for_umo", None)
        if ensure is None:
            print("❌ 插件没有 `_ensure_conversation_id_for_umo`（版本变了？）")
            return 1
        cid = await ensure(PROBE_UMO, title="（探针）")
        print(f"② 插件自己的 _ensure_conversation_id_for_umo → {cid!r}")
        if not cid:
            print("   ❌ 插件拿不到 cid（补之前正是这个表现）")
            ok = False

        # ---- ③ 走**插件自己的** add_message_pair 写完 ----
        if cid:
            await cm.add_message_pair(
                cid,
                {"role": "user", "content": "（真插件探针）用户的话"},
                {"role": "assistant", "content": "（真插件探针）插件的话"},
            )
            conv = await mgr.get_conversation(PROBE_UMO, cid)
            if conv is None:
                print("   ❌ 写完后我们 store 里读不到")
                ok = False
            else:
                parsed = json.loads(conv.history)
                print(f"③ 插件写入后我们从 store 读到 {len(parsed)} 条：{parsed}")
                if len(parsed) != 2:
                    print("   ❌ 条数不对")
                    ok = False

            # ---- ④ 再调一次 ensure：必须复用同一个 cid（不重复新建） ----
            cid2 = await ensure(PROBE_UMO, title="（探针）")
            print(f"④ 第二次 _ensure_conversation_id_for_umo → {cid2!r}")
            if cid2 != cid:
                print(f"   ❌ 没复用（{cid2!r} != {cid!r}）—— 每次都会新建一个对话")
                ok = False
            else:
                # 而且历史还在（没被"新建"清掉）
                conv2 = await mgr.get_conversation(PROBE_UMO, cid)
                n = len(json.loads(conv2.history))
                print(f"   历史仍在：{n} 条")
                if n != 2:
                    print("   ❌ 复用后历史丢了")
                    ok = False

    finally:
        await host.stop()
        sec = await mgr._load()  # noqa: SLF001
        removed = [k for k in sec if "__realprobe__" in str(k)]
        for k in removed:
            sec.pop(k, None)
        await mgr._save(sec)  # noqa: SLF001
        print(f"\n🧹 已清理探针数据：{removed}")
        left = [k for k in (await mgr._load()) if "__realprobe__" in str(k)]  # noqa: SLF001
        if left:
            print(f"❌ 还有残留：{left}")
            ok = False
        await store.close()

    print("\n" + ("✅ 真插件走的存档链路真的通了" if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
