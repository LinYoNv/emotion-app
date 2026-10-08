# -*- coding: utf-8 -*-
"""**只读**清单：陪伴插件里那些"删不掉"的用户到底是谁、占了哪些数据。

## 为什么需要它

情绪报障原话：「私聊用户那有很多不知道什么东西的用户**点击删除还报错**」。

查证结论（详见 `docs/归档不可用_根因报告.md`）：
  * 那些用户是**我们自己的测试工具留下的**（`tools/proactive_multisession_check.py`
    自己在文档里承认会留；另有两次一次性探针）；
  * "删不掉"**不是 bug** —— 插件的删除按钮对**已绑统一人物**（`unified_person_id`）的用户
    **按设计**拒绝直接删，要求走「身份与隔离 → 归档」；
  * 归档目前不可用（迁移 `state=degraded`，而插件的 `coordinator.resume()` 全代码零调用
    ⇒ `paused` 是终态），**所以那条路走不通**；
  * ⇒ 唯一剩下的路是**离线**改 `companions.json`，但那会碰到 `unified_person` 注册表的
    一致性（profile / identity_links / relation_policies / tombstones / audit），
    **手改 JSON 有破坏一致性风险** —— 所以先出这份清单给人看，不擅自动手。

## 🔴 本脚本【只读】

只读两个文件，**不写任何东西**、不连服务、不改插件：
  * `data/plugins/astrbot_plugin_private_companion/companions.json`
  * `data/emotion.db` 的 `store` 表（`section='sessions'`）

用法：
    .venv/bin/python tools/plugin_user_residue_report.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPANIONS = ROOT / "data/plugins/astrbot_plugin_private_companion/companions.json"
EMOTION_DB = ROOT / "data/emotion.db"

#: 测试工具用的前缀（`tools/proactive_multisession_check.py:47`）
TEST_PREFIX = "e2e_"
#: 全局兜底用户 —— **真实在用的那个，绝不能删**
KEEP = {"default"}


def load_sessions() -> dict[str, dict]:
    """读我们自己库里的会话（**只读**）→ `{session_id: session}`。"""
    if not EMOTION_DB.exists():
        return {}
    conn = sqlite3.connect(f"file:{EMOTION_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload FROM store WHERE section='sessions'"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def registry_mentions(unified_person: dict, person_id: str) -> dict[str, int]:
    """统计 `unified_person` 各子结构里提到该 person 的次数（只读遍历）。"""
    out: dict[str, int] = {}
    for section, payload in unified_person.items():
        if not isinstance(payload, (dict, list)):
            continue
        blob = json.dumps(payload, ensure_ascii=False)
        n = blob.count(person_id)
        if n:
            out[section] = n
    return out


def main() -> int:
    if not COMPANIONS.exists():
        print(f"❌ 找不到 {COMPANIONS}", file=sys.stderr)
        return 2
    data = json.loads(COMPANIONS.read_text(encoding="utf-8"))
    users = data.get("users") or {}
    unified_person = data.get("unified_person") or {}
    sessions = load_sessions()
    by_uid: dict[str, list[str]] = {}
    for sid, s in sessions.items():
        if isinstance(s, dict) and s.get("user_id"):
            by_uid.setdefault(str(s["user_id"]), []).append(sid)

    print("=== 只读清单（**本脚本不写任何文件**）===")
    print(f"companions.json : {COMPANIONS}  ({COMPANIONS.stat().st_size} 字节)")
    print(f"插件用户总数    : {len(users)}")
    print(f"我们会话总数    : {len(sessions)}")
    print()

    rows = []
    for uid, u in sorted(users.items()):
        person_id = str((u or {}).get("unified_person_id") or "") if isinstance(u, dict) else ""
        bound = by_uid.get(uid, [])
        if uid in KEEP:
            kind = "**真实用户（保留）**"
        elif uid.startswith(TEST_PREFIX):
            kind = "测试残留"
        elif bound:
            kind = "会话在用"
        else:
            kind = "孤儿（无会话引用）"
        rows.append((uid, kind, person_id, bound, u))

    for uid, kind, person_id, bound, u in rows:
        inbound = (u or {}).get("inbound_count") if isinstance(u, dict) else None
        last = (u or {}).get("last_user_message") if isinstance(u, dict) else None
        nkeys = len(u) if isinstance(u, dict) else 0
        print(f"— {uid}")
        print(f"    类型        : {kind}")
        print(f"    unified_person_id : {person_id or '(无)'}")
        print(f"    inbound_count={inbound!r}  字段数={nkeys}  last_user_message={str(last)[:24]!r}")
        print(f"    被我们会话引用: {bound or '（无）'}")
        if person_id:
            mentions = registry_mentions(unified_person, person_id)
            print(f"    注册表提及  : {mentions or '（无）'}  ⚠️ 粗指标 = 该段 JSON 里出现 person_id 的次数，不是精确条数")

    print()
    print("=== 建议的清理集合（**待情绪确认，尚未执行**）===")
    target = [
        (uid, person_id, kind)
        for uid, kind, person_id, _b, _u in rows
        if uid not in KEEP
    ]
    print(f"共 {len(target)} 个：")
    for uid, person_id, kind in target:
        print(f"  - {uid:26} person={person_id or '(无)':30} {kind}")
    print()
    print("⚠️ 直接删 `users[uid]` 会留下孤儿 `person_*`（profiles / identity_links /")
    print("   relation_policies / audit_events 都还指向它）。真要清，得走插件自己的")
    print("   `UnifiedPersonRegistry.prepare_person_archive()` 那套（会写 tombstones + audit），")
    print("   而不是手改 JSON —— 这正是本脚本只出清单、不动手的原因。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
