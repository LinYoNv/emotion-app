#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机实验：**插件用「某个会话的 umo」发主动消息，会不会落到那条会话里？**

## 情绪的问题（2026-09-27 截图）
「主动消息只能给到 default 这个默认用户 umo 吗，不能我自己新建会话
  然后去陪伴通过 umo 添加吗」

这句话要拆成**两个可验证的问题**：

  Q1：新会话能不能拿到**自己的** umo（不是 `default`）？
  Q2：插件按那个 umo 发主动消息时，会不会落到**那条会话**里？

两个都必须是"真的"，而不是"代码看起来是这么写的"。

## 判据
  Q1  新建一个会话（照面板的做法：POST /api/v1/sessions 空 body）
      → 它的 `user_id` 形如 `sess-<12hex>`、`umo` = `11:FriendMessage:<user_id>`
      → **不是**全局 `default`
  Q2  ① 用**真 SessionDelivery**（宿主那条投递链）发一条主动消息，
         umo 用 Q1 那条会话的 umo
      → 消息落进**那条会话**（不是别的会话、不是新造的孤儿会话）
      → 那条会话 `unread` **+1**（徽标语义）
      → 别的会话 unread **不变**（不串）
      ② 插件若**还没建这个用户**，投递也应成功（会话已存在 → 直接写）
  Q3  清理：删掉本实验造的会话，确认列表回到原样

⚠️ 本实验会**真写** `data/emotion.db`（用真 store），跑完自己清干净。

用法：python3 tools/probe_proactive_umo_routing.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "host"))

_ENV = ROOT / ".env"
if _ENV.is_file():
    for _l in _ENV.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _, _v = _l.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


async def main() -> int:
    from emotion_app.domain.session_repo import SessionRepository
    from emotion_app.infra.config import load_config
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.web.delivery import SessionDelivery

    ok = True
    config = load_config()
    store = SqliteStore(config.storage.sqlite_path)
    await store.open()
    repo = SessionRepository(store)
    before = await repo.list()
    before_ids = {s.id for s in before}
    print(f"实验前：{len(before)} 条会话 → {[s.title for s in before]}\n")

    made: list[str] = []
    try:
        # ---------- Q1：新会话拿到自己的 umo ----------
        s = await repo.create(title="（探针）主动消息路由")
        made.append(s.id)
        print("Q1 新建会话：")
        print(f"   id      = {s.id}")
        print(f"   user_id = {s.user_id!r}")
        print(f"   umo     = 11:FriendMessage:{s.user_id}")
        if not s.user_id.startswith("sess-"):
            print("   ❌ user_id 不是 sess- 形状（会与 default 混淆）")
            ok = False
        elif len(s.user_id) != len("sess-") + 12:
            print(f"   ❌ 随机段长度不对：{s.user_id}")
            ok = False
        if s.user_id == "default":
            print("   ❌ 新会话拿到的还是 default —— 正是情绪以为的那个问题")
            ok = False
        else:
            print("   ✅ 拿到的是它**独有**的 umo，不是 default")

        umo = f"11:FriendMessage:{s.user_id}"

        # ---------- Q2：走真投递链，看落到哪 ----------
        # 用**真组件**（照插件实际发的形状）：Plain 组件有 .text
        from astrbot_host.compat.core.message.components import Plain

        delivery = SessionDelivery(sessions=repo, settings=None)
        sent = await delivery.send(umo, [Plain("（探针）主动消息正文")])
        print(f"\nQ2 投递（umo={umo}）→ send() 返回 {sent!r}")
        if not sent:
            print("   ❌ 投递返回假 —— 这条 umo 的主动消息发不出去")
            ok = False

        after = {x.id: x for x in await repo.list()}
        # 找到 target 会话
        target = after.get(s.id)
        if target is None:
            print("   ❌ 会话不见了")
            ok = False
        else:
            msgs = await repo.messages(s.id)
            texts = [m.content for m in msgs]
            print(f"   目标会话消息数 = {len(msgs)}：{texts}")
            if not any("（探针）主动消息正文" in str(t) for t in texts):
                print("   ❌ 消息**没有**落进这条会话 —— 那是它落到别处了")
                ok = False
            else:
                print("   ✅ 消息落进了**这条**会话（按 umo 找对了）")
            print(f"   目标会话 unread = {target.unread}")
            if target.unread != 1:
                print(f"   ❌ unread 应为 1（未读徽标语义），实际 {target.unread}")
                ok = False
            else:
                print("   ✅ 只有它 unread +1")

        # 别的会话 unread 不该变
        dirty = [x.title for x in after.values()
                 if x.id != s.id and x.id in before_ids and x.unread != 0]
        print(f"   其他会话 unread 被改动的：{dirty}")
        if dirty:
            print("   ❌ 串到别的会话了")
            ok = False
        else:
            print("   ✅ 别的会话 unread 未受影响（不串）")

        # 不该凭空多出会话（除了我建的那条）
        new_ids = set(after) - before_ids - {s.id}
        print(f"   意外新增会话：{new_ids}")
        if new_ids:
            print("   ❌ 投递路径另造了孤儿会话")
            ok = False
        else:
            print("   ✅ 没有造孤儿会话")

    finally:
        for sid in made:
            try:
                await repo.delete(sid)
            except Exception as exc:  # noqa: BLE001
                print(f"   ⚠️ 清理 {sid} 失败：{exc}")
        final = await repo.list()
        print(f"\n🧹 清理后：{len(final)} 条会话 → {[s.title for s in final]}")
        if {s.id for s in final} != before_ids:
            print("   ❌ 没复原到实验前状态")
            ok = False
        await store.close()

    print("\n" + ("✅ 两个问题都验证通过：新会话有自己的 umo，主动消息按它落到那条会话"
                  if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
