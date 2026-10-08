#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机探针：插件的「主动消息存档」这条链，现在真的通了吗？

## 为什么不能只看"日志里没报错了"
本项目铁则：**单测全绿 + 没报错 ≠ 修好**。要看到**真产物**。
这里的真产物是：`data/emotion.db` 的 `plugin_conversations` section 里
**真的出现了对话记录**，且内容与插件写进去的一致。

## 判据（每条都能失败）
  ① `HostPorts.conversation_manager` 不为 None（端口真的接上了）
  ② `context.conversation_manager` 可访问（不再是 NotImplementedError）
  ③ 走**真插件那条调用序列**存一条主动消息：
       `_ensure_conversation_id_for_umo(umo)` →
       `get_curr_conversation_id` / `new_conversation(umo, platform_id)` →
       `add_message_pair(cid, user, assistant)`
  ④ 存完**从磁盘重读**（新开一个 manager 实例）能看到那条 —— 证明真落盘了
  ⑤ `history` 是 **JSON 字符串**（插件的 str 分支读得到）
  ⑥ 第二次调用 `_ensure_conversation_id_for_umo` **复用同一个 cid**
     （不反复新建 —— 真实现就是"有了就返回"）

⚠️ 本探针**会往 `data/emotion.db` 写一条测试对话**，跑完自己删干净。
   用的是真 store（不是内存替身），否则测不出"真的落盘了"。

用法：python3 tools/probe_conversation_archive.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 🔴 先把 `.env` 读进环境变量（`load_config()` 只读环境变量、不解析 .env；
#    生产是由 `run.sh` 用 `set -a; source .env` 注入的）。
#    ⚠️ 必须**去掉引号** —— 踩过：带引号会被当成错误令牌。
_ENV_FILE = ROOT / ".env"
if _ENV_FILE.is_file():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from emotion_app.domain.conversation_manager import (  # noqa: E402
    CONVERSATIONS_SECTION,
    StoreConversationManager,
)
from emotion_app.infra.config import load_config  # noqa: E402
from emotion_app.storage.sqlite_store import SqliteStore  # noqa: E402

#: 探针用的 umo（**不带 sess- 前缀**，避免与真会话混）
PROBE_UMO = "11:FriendMessage:__cm_probe__"


async def main() -> int:
    ok = True

    config = load_config()
    store = SqliteStore(config.storage.sqlite_path)
    await store.open()
    mgr = StoreConversationManager(store)

    # 先清掉上一次探针可能留下的痕迹（幂等）
    section = await mgr._load()  # noqa: SLF001
    if PROBE_UMO in section:
        section.pop(PROBE_UMO, None)
        await mgr._save(section)  # noqa: SLF001

    try:
        # ---- ③ 真插件那条调用序列 ----
        # 插件 `_ensure_conversation_id_for_umo`：先问"当前 cid"，没有就 new
        cid = await mgr.get_curr_conversation_id(PROBE_UMO)
        print(f"③-a 首次 get_curr_conversation_id → {cid!r}")
        if cid is not None:
            print("   ❌ 第一次就返回了 cid（该是 None）")
            ok = False

        created = await mgr.new_conversation(PROBE_UMO, "11")
        print(f"③-b new_conversation(umo, '11') → {created!r}")
        if not created or len(created) != 36:
            print("   ❌ cid 不是 uuid")
            ok = False

        # 插件第二次进来必须**复用**同一个 cid（不重复新建）
        again = await mgr.get_curr_conversation_id(PROBE_UMO)
        print(f"⑥ 再次 get_curr_conversation_id → {again!r}")
        if again != created:
            print(f"   ❌ 没有复用同一个 cid（{again!r} != {created!r}）")
            ok = False

        await mgr.add_message_pair(
            created,
            {"role": "user", "content": "（探针）你今天怎么样"},
            {"role": "assistant", "content": "（探针）刚琢磨午饭呢，你吃了吗？"},
        )
        print("③-c add_message_pair 成功")

        # ---- ④ **从磁盘重读**（新实例，证明真落盘） ----
        store2 = SqliteStore(config.storage.sqlite_path)
        await store2.open()
        mgr2 = StoreConversationManager(store2)
        try:
            conv = await mgr2.get_conversation(PROBE_UMO, created)
            if conv is None:
                print("   ❌ 从磁盘重读拿不到那条对话 —— 没真的落盘")
                ok = False
            else:
                print(f"④ 磁盘重读 OK：cid={conv.cid} platform={conv.platform_id} title={conv.title!r}")
                # ---- ⑤ history 必须是 JSON 字符串 ----
                if not isinstance(conv.history, str):
                    print(f"   ❌ history 是 {type(conv.history).__name__}，不是 str")
                    ok = False
                else:
                    parsed = json.loads(conv.history)
                    print(f"⑤ history 是 JSON 字符串，解出 {len(parsed)} 条：{parsed}")
                    if len(parsed) != 2 or parsed[1]["content"] != "（探针）刚琢磨午饭呢，你吃了吗？":
                        print("   ❌ 存进去的内容不对")
                        ok = False
        finally:
            await store2.close()

        # ---- 反向：不同 umo 不串 ----
        other = await mgr.get_curr_conversation_id("11:FriendMessage:__cm_probe_other__")
        print(f"↔ 另一个 umo 的当前对话 → {other!r}（该是 None）")
        if other is not None:
            print("   ❌ 串到了别的 umo")
            ok = False

    finally:
        # ---- 清理：删掉探针自己造的数据（**不留脏数据**） ----
        section = await mgr._load()  # noqa: SLF001
        removed = []
        for k in list(section):
            if "__cm_probe__" in str(k):
                removed.append(k)
                section.pop(k, None)
        if removed:
            await mgr._save(section)  # noqa: SLF001
        print(f"\n🧹 已清理探针数据：{removed}")
        left = await mgr._load()  # noqa: SLF001
        stray = [k for k in left if "__cm_probe__" in str(k)]
        if stray:
            print(f"❌ 还有残留：{stray}")
            ok = False
        await store.close()

    print("\n" + ("✅ 全部通过：插件的对话存档链路真的通了" if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
