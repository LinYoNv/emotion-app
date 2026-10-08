# -*- coding: utf-8 -*-
"""**离线清理**陪伴插件里那 20 个残留用户（含它们在统一人物注册表里的痕迹）。

## 背景（详见 `docs/归档不可用_根因报告.md`）

「私聊用户」页面里 20 个非 `default` 的用户点删除会报错，根因链：
  1. 插件的 `/page/user/delete` 对**已绑统一人物**的用户**按设计**拒绝直接删，
     要求走「身份与隔离 → 归档」；
  2. 归档要求 `req041_migration_status["state"]` 不在 `{degraded, paused, stopped}`；
  3. 实测 `paused`，而 **`coordinator.resume()` 在全插件非测试代码里零调用**
     ⇒ `paused` 是终态，插件**没有任何**恢复/重放端点。
⇒ 官方路走不通，只能**离线**清理（情绪已批准）。

## 🔴 为什么不用 `UnifiedPersonRegistry` 的归档 API（本脚本会现场取证给你看）

`data/plugin_src/.../unified_person_registry.py` **能离线 import**（它只依赖 stdlib +
同目录三个纯 Python 模块，不依赖 `astrbot`），这一点本脚本第 2 段会实测打印。
但它的**写状态**入口跑不完：

  * `prepare_person_archive()` 只写一条 operation 预览，`changed=False`（**不归档任何东西**）；
  * 真正改状态的是 `finalize_person_archive()`，它的先决条件是**三份外部回执**：
      - `remote_receipt`：来自 **Memory 记忆桥**的 `ScopedStore.tombstone_identity_scopes`
        （只在**插件运行中**由 `main.py:6493` 的回调接上）；
      - `relationship_receipt`：`req041_relationship.db`；
      - `stream_receipt`：`req041_migration_outbox.db`，且 `stream_count` 必须**恰好 2**。
    离线没有记忆桥，第一份回执就拿不到 → `archive_remote_not_confirmed`；
  * `unlink_identity()` 对**只有一个身份**的人按设计拒绝（`ambiguity_count=1`
    → `split_manual_review_required`），本批 19 个人**每个都只有一个身份**；
  * 另一条 `finalize_person_purge()` 要求 `profile_status="deleted"` + 已写 tombstone，
    即**必须先走完上面的归档**。

⇒ 只能退到**最小侵入的等价操作**：删 `users[uid]`，并把该 person 在
`unified_person` 里的**全部**痕迹一并删掉。删除集合**逐字对照**插件自己
`finalize_person_purge()`（`unified_person_registry.py:1849+`）的清理范围，另外补上
它"假定已 detach"所以不管的 `identity_links`。

🔴 **故意不写 `person_tombstones` / `identity_tombstones`**：`create_or_link()`
（`:720`）与 `link_identity()`（`:983`）见到 `identity_tombstones[key]` 会直接返回
`identity_archived` —— 给 `e2e_*` 写墓碑等于**永久封死**我们的 e2e 测试用户名。
而 tombstone 的语义是"走过归档 saga"，我们没走，写它就是伪造。审计留在
**备份目录里的报告文件**，不污染插件状态。
⚠️ 代价（评审指出，知情接受）：不写墓碑 ⇒ 同一个 `platform_subject_id` 再次出现时
**会被正常重建**（对 `e2e_*` 是我们要的；对 3 个 `sess-*` 意味着"用同一个 umo 标签
重开会话"会重新长出用户）。要改成"不可重建"就在这里补写墓碑，别在别处手改。

⚠️ 两处**比插件自己更激进/更保守**的取舍，评审时请对照：
  * `operations` 的判据是「键**或**值里出现该 person_id（子串）」，而插件
    `finalize_person_purge()` 用的是「值**精确等于** person_id 且嵌套深度 ≤6」
    （`_contains_exact_value`，`:198-206`）。本数据集两者结论相同（各删 19 条），
    但换数据集时本工具可能多删 —— 这是为了不给缩写的 operator key 留尾巴。
  * 插件 purge **不碰** `identity_links`（它假定归档阶段已把链接搬进
    `detached_identity_links`）；本工具补删，否则会留下指向已删 person 的活跃链接。

## 用法

    .venv/bin/python tools/plugin_user_cleanup.py            # 默认 = --dry-run，一个字节都不写
    .venv/bin/python tools/plugin_user_cleanup.py --apply    # 真写（先备份 + 事后逐项断言）

⚠️ `--apply` 前**必须停服务**（插件把 `companions.json` 缓存在内存里，运行中改会被覆盖）。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

# 🔴 必须在 import 插件模块之前设：否则 import 会在 data/plugin_src/** 里写 __pycache__
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
COMPANIONS = ROOT / "data/plugins/astrbot_plugin_private_companion/companions.json"
PLUGIN_SRC = ROOT / "data/plugin_src/astrbot_plugin_private_companion"
BACKUP_ROOT = Path("/root/backup")

#: 全局兜底用户 —— **真实在用的那个，绝不能删**
DEFAULT_UID = "default"
DEFAULT_PERSON = "person_023d8c5bbe0637801002a59d"

#: 要清理的 20 个（与 `tools/plugin_user_residue_report.py` 建议集合**逐个对照过**）
TARGET_UIDS = (
    "e2e_3a0f6d_ns",
    "e2e_5288d8_alice", "e2e_5288d8_bob", "e2e_5288d8_carol",
    "e2e_7bd368_alice", "e2e_7bd368_bob", "e2e_7bd368_carol",
    "e2e_8e7b37_alice", "e2e_8e7b37_bob", "e2e_8e7b37_carol",
    "e2e_da8021_alice", "e2e_da8021_bob", "e2e_da8021_carol",
    "e2e_f52d49_alice", "e2e_f52d49_bob", "e2e_f52d49_carol",
    "e2e_probe_x",
    "sess-4db454d764d3", "sess-659b4e15af8b", "sess-68c74ee1126d",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _canonical(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Abort(RuntimeError):
    """前置/后置断言不通过 —— 一律中止，绝不"踩过去"。"""


def _dumps(data) -> bytes:
    """按磁盘上的既有格式序列化（实测逐字节等于 `indent=2` + 末尾换行）。"""
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------- 注册表 API 取证

def probe_registry(doc: dict, targets: list[tuple[str, str]]) -> None:
    """离线跑一遍官方归档 API，把"跑到哪一步、被什么挡住"原样打出来。

    只在**内存副本**上跑，绝不落盘。
    """
    print("=== 2. 官方注册表 API 的离线可行性（现场取证，只在内存副本上跑）===")
    sys.path.insert(0, str(PLUGIN_SRC))
    try:
        import unified_person_registry as upr  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 取证分支
        print(f"  ❌ 连 import 都失败：{type(exc).__name__}: {exc}")
        raise Abort("注册表模块无法离线 import") from exc
    print(f"  ✅ 离线 import 成功：{upr.__file__}")
    print(f"     （它只依赖 stdlib + 同目录 person_context_contract / p4_affinity_confinement /"
          f" identity_namespace，均不依赖 astrbot）")

    probe = copy.deepcopy(doc)
    reg = upr.UnifiedPersonRegistry(probe)
    before = reg.status()
    print(f"  内存副本 status()：{before}")

    sample_uid, sample_pid = next((u, p) for u, p in targets if p)
    op = f"offline-cleanup-probe-{sample_pid[-8:]}"
    prepared = reg.prepare_person_archive(
        sample_pid, operation_id=op, actor_id="offline_cleanup", reason_code="person_archive",
    )
    print(f"  ① prepare_person_archive({sample_pid})")
    print(f"     -> {json.dumps(prepared, ensure_ascii=False)}")
    token = str(prepared.get("confirmation_token") or "")
    confirmed = reg.confirm_person_archive(
        sample_pid, op, token, actor_id="offline_cleanup", reason_code="person_archive",
    )
    print(f"  ② confirm_person_archive() -> {json.dumps(confirmed, ensure_ascii=False)}")
    finalized = reg.finalize_person_archive(
        sample_uid and sample_pid, op, token, {}, {}, {},
        actor_id="offline_cleanup", reason_code="person_archive",
    )
    print(f"  ③ finalize_person_archive(..., remote/relationship/stream 都传空)")
    print(f"     -> {json.dumps(finalized, ensure_ascii=False)}")
    print(f"  ⇒ 结论：prepare 只写 operation 预览（changed={prepared.get('changed')}），"
          f"真正改状态的 finalize 被外部回执挡住（{finalized.get('code')}）。")
    print(f"  ⇒ 记忆桥回执只可能来自**运行中的插件**，而本流程要求服务停着改 ⇒ 官方路离线跑不通。")
    print()

    # 顺带用它的 prepare 当**完整性校验器**：19 个人必须都通过
    print("  用 prepare_person_archive 做完整性预检（证明 identity_keys ↔ 活跃 identity_links 严格一致）：")
    bad = []
    for uid, pid in targets:
        if not pid:
            continue
        got = reg.prepare_person_archive(
            pid, operation_id=f"offline-cleanup-check-{pid[-12:]}",
            actor_id="offline_cleanup", reason_code="person_archive",
        )
        if got.get("code") != "person_archive_prepared":
            bad.append((uid, pid, got.get("code")))
    print(f"     通过 {sum(1 for _u, p in targets if p) - len(bad)}/{sum(1 for _u, p in targets if p)}"
          f"{'' if not bad else '  ❌ 不通过：' + str(bad)}")
    if bad:
        raise Abort(f"注册表完整性预检不通过：{bad}")
    print()


# ---------------------------------------------------------------- 删除集合

def collect_removals(doc: dict, targets: list[tuple[str, str]]) -> dict:
    """算出**将要删除**的全部结构（只读遍历，不修改 doc）。"""
    users = doc.get("users") or {}
    up = doc.get("unified_person") or {}
    pids = {pid for _u, pid in targets if pid}
    keys = set()
    for pid in pids:
        profile = up["profiles"].get(pid) or {}
        for key in profile.get("identity_keys") or []:
            if isinstance(key, str):
                keys.add(key)
        for key, link in (up.get("identity_links") or {}).items():
            if isinstance(link, dict) and link.get("person_id") == pid:
                keys.add(key)

    out = {
        "users": [u for u, _p in targets],
        "profiles": sorted(pids),
        "identity_keys": sorted(keys),
        "identity_links": sorted(
            k for k, v in (up.get("identity_links") or {}).items()
            if isinstance(v, dict) and v.get("person_id") in pids
        ),
        "binding_checkpoints": sorted(
            k for k, v in (up.get("binding_checkpoints") or {}).items()
            if isinstance(v, dict) and v.get("person_id") in pids
        ),
        "detached_identity_links": sorted(
            k for k, v in (up.get("detached_identity_links") or {}).items()
            if isinstance(v, dict) and v.get("person_id") in pids
        ),
        "group_overlays": sorted(
            k for k, v in (up.get("group_overlays") or {}).items()
            if isinstance(v, dict) and v.get("person_id") in pids
        ),
        "audit_events": [
            i for i, e in enumerate(up.get("audit_events") or [])
            if isinstance(e, dict) and e.get("person_id") in pids
        ],
        "operations": sorted(
            k for k, v in (up.get("operations") or {}).items()
            if any(pid in k or pid in _dump(v) for pid in pids)
        ),
        "person_tombstones": sorted(pid for pid in pids if pid in (up.get("person_tombstones") or {})),
        "identity_tombstones": sorted(
            k for k in keys if k in (up.get("identity_tombstones") or {})
        ),
        "p4_people": sorted(
            pid for pid in pids
            if any(
                pid in ((up.get(name) or {}).get("people") or {})
                for name in ("p4_effect", "p4_live")
            )
        ),
    }
    # operations 兜底：键里带 uid 的也算（插件自己的 purge 只按 person_id 过滤）
    for uid, _p in targets:
        for k, v in (up.get("operations") or {}).items():
            if (uid in k or uid in _dump(v)) and k not in out["operations"]:
                out["operations"].append(k)
    out["operations"].sort()
    return out


def apply_removals(doc: dict, plan: dict) -> None:
    """就地执行删除。**只删 plan 里列出的键**，别的一律不动。"""
    users = doc["users"]
    up = doc["unified_person"]
    for uid in plan["users"]:
        users.pop(uid, None)
    for pid in plan["profiles"]:
        up["profiles"].pop(pid, None)
    for key in plan["identity_links"]:
        up["identity_links"].pop(key, None)
    for key in plan["binding_checkpoints"]:
        up["binding_checkpoints"].pop(key, None)
    for key in plan["detached_identity_links"]:
        up["detached_identity_links"].pop(key, None)
    for key in plan["group_overlays"]:
        up["group_overlays"].pop(key, None)
    for pid in plan["person_tombstones"]:
        up["person_tombstones"].pop(pid, None)
    for key in plan["identity_tombstones"]:
        up["identity_tombstones"].pop(key, None)
    for name in ("p4_effect", "p4_live"):
        people = (up.get(name) or {}).get("people")
        if isinstance(people, dict):
            for pid in plan["p4_people"]:
                people.pop(pid, None)
    doomed = set(plan["audit_events"])
    up["audit_events"] = [e for i, e in enumerate(up["audit_events"]) if i not in doomed]
    for key in plan["operations"]:
        up["operations"].pop(key, None)


def post_checks(doc: dict, plan: dict, targets: list[tuple[str, str]], default_hash: str) -> list[str]:
    """改后断言。返回失败列表（空 = 全绿）。

    判据分三层：
      ① `users` + `unified_person`（插件认的两个权威区段）里**一个目标 uid/person 都不许剩**；
      ② **全文**不许再出现任何被删的 identity_key（它是 person 的形状指纹，最容易被漏在别处）；
      ③ **正向孤儿**：留下来的每个 user 的 `unified_person_id` 必须能在 `profiles` 里找到。
    """
    bad: list[str] = []
    users = doc.get("users") or {}
    up = doc.get("unified_person") or {}
    others = {k: v for k, v in doc.items() if k not in {"users", "unified_person"}}

    blob_users, blob_up, blob_others = _dump(users), _dump(up), _dump(others)
    for uid in plan["users"]:
        if uid in users:
            bad.append(f"users 里仍然有 {uid}")
        if uid in blob_users:
            bad.append(f"users 段内仍有 {uid} 的残留引用（例如别的 user 的 alias_user_ids）")
        if uid in blob_up:
            bad.append(f"unified_person 里仍有 {uid} 的残留引用")
    for pid in plan["profiles"]:
        if pid in up["profiles"]:
            bad.append(f"profiles 里仍然有 {pid}")
        if pid in blob_up:
            bad.append(f"unified_person 里仍有 {pid} 的残留引用")
        if pid in blob_others:
            bad.append(f"unified_person / users 之外的区段里仍有 {pid} 的残留引用")
    for key in plan["identity_keys"]:
        if key in blob_up or key in blob_users or key in blob_others:
            bad.append(f"全文仍有 identity_key {key} 的残留引用")
    # 正向孤儿：留下来的 user 指向的 profile 必须存在
    for uid, u in users.items():
        pid = str((u or {}).get("unified_person_id") or "") if isinstance(u, dict) else ""
        if pid and pid not in up["profiles"]:
            bad.append(f"反向孤儿：users[{uid}] 指向不存在的 profile {pid}")
    if _sha(_dump((users.get(DEFAULT_UID) or {}))) != default_hash:
        bad.append("🔴 default 用户记录变了")
    return bad


def report_extras(doc: dict, targets: list[tuple[str, str]]) -> None:
    """打印**越界但相关**的残留（不动它们，只报告）。"""
    users = doc.get("users") or {}
    up = doc.get("unified_person") or {}
    owners = {
        str((u or {}).get("unified_person_id"))
        for u in users.values() if isinstance(u, dict) and (u or {}).get("unified_person_id")
    }
    orphans = [pid for pid in sorted(up["profiles"]) if pid not in owners]
    print("=== 越界观察（**本次不动**，留给情绪拍板）===")
    if orphans:
        for pid in orphans:
            prof = up["profiles"].get(pid) or {}
            print(f"  ⚠️ 无主 profile（users 里没有对应项，也不在本次名单里）：{pid}"
                  f"  display_name={prof.get('display_name')!r}  创建于 {prof.get('updated_at')}")
    else:
        print("  无主 profile：无")
    hit = []
    for sec in ("recent_prompt_injections", "recent_prompt_injection_events"):
        blob = _dump(doc.get(sec))
        for uid, _p in targets:
            if uid in blob:
                hit.append(sec)
                break
    if hit:
        print(f"  剩下这些区段仍含被删 umo 串（是**按 umo 记的滚动日志**，不是身份记录，未动）：{sorted(set(hit))}")
    print()


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="离线清理陪伴插件的 20 个残留用户")
    ap.add_argument("--apply", action="store_true", help="真写（默认只干跑，一个字节都不写）")
    ap.add_argument(
        "--person", action="append", default=[], metavar="person_id",
        help="额外清理的 person_id（**该 person 没有对应的 user 记录**时用；可重复）。"
             "例：无主 profile `person_1ccc16821205f463ed7d8c02`（display_name=umo_probe_1）。",
    )
    ap.add_argument(
        "--persons-only", action="store_true",
        help="只清理 --person 指定的 person，**跳过内置的 20 个 uid 名单**。"
             "给「名单已经清过、现在只剩无主 person 要收尾」那种情况用"
             "（否则内置名单会因为查不到而按守卫中止）。",
    )
    args = ap.parse_args()

    if not COMPANIONS.exists():
        print(f"❌ 找不到 {COMPANIONS}", file=sys.stderr)
        return 2
    raw = COMPANIONS.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    users = doc.get("users")
    if not isinstance(users, dict) or not isinstance(doc.get("unified_person"), dict):
        print("❌ companions.json 结构不对（缺 users / unified_person）", file=sys.stderr)
        return 2

    print("=== 1. 名单核对 ===")
    print(f"文件        : {COMPANIONS}  ({len(raw)} 字节)")
    print(f"users / profiles : {len(users)} / {len(doc['unified_person']['profiles'])}")
    if args.persons_only:
        if not args.person:
            print("❌ --persons-only 至少要给一个 --person", file=sys.stderr)
            return 3
        targets = [("", p) for p in args.person]
        print(f"目标        : {len(targets)} 个 person（--persons-only，**无 user 记录**）")
        for _uid, pid in targets:
            print(f"  - person={pid}")
    else:
        missing = [u for u in TARGET_UIDS if u not in users]
        if missing:
            print(f"❌ 目标名单里有 {len(missing)} 个**不在 users 里**：{missing}", file=sys.stderr)
            print("   → 名单对不上，停下来问，绝不猜。", file=sys.stderr)
            print("   ⚠️ 若这 20 个已经清过了，现在只是收尾无主 person，请改用："
                  "--persons-only --person <id>", file=sys.stderr)
            return 3
        if DEFAULT_UID not in users:
            print(f"❌ 找不到 {DEFAULT_UID} 用户", file=sys.stderr)
            return 3
        targets = [(u, str((users.get(u) or {}).get("unified_person_id") or "")) for u in TARGET_UIDS]
        targets += [("", p) for p in args.person]   # 额外 person：不带 user 记录
        print(f"目标        : {len(targets)} 个用户/person，其中带 unified_person_id 的 "
              f"{len([1 for _u, p in targets if p])} 个")
        for uid, pid in targets:
            print(f"  - {uid or '(无 user 记录)':24} person={pid or '(无 —— 没绑统一人物)'}")
    d_pid = str((users.get(DEFAULT_UID) or {}).get("unified_person_id") or "")
    if d_pid != DEFAULT_PERSON:
        print(f"❌ default 绑的不是预期 person（实得 {d_pid!r}，预期 {DEFAULT_PERSON!r}）", file=sys.stderr)
        return 3
    persons = [p for _u, p in targets if p]
    if DEFAULT_PERSON in persons:
        print("❌ 目标名单里出现了 default 的 person —— 拒绝执行", file=sys.stderr)
        return 3
    print(f"✅ default 守卫：{DEFAULT_UID} -> {d_pid}（**不在清理集合内**）")
    print()

    probe_registry(doc, targets)

    plan = collect_removals(doc, targets)
    plan["users"] = [u for u in plan["users"] if u]   # `--person` 走的是空 uid 占位，别让它进 users 删除集
    print("=== 3. 将删除的结构（逐项）===")
    print(f"  users                      {len(plan['users'])} 条")
    print(f"  unified_person.profiles    {len(plan['profiles'])} 条")
    print(f"  unified_person.identity_links           {len(plan['identity_links'])} 条")
    print(f"  unified_person.binding_checkpoints      {len(plan['binding_checkpoints'])} 条")
    print(f"  unified_person.detached_identity_links  {len(plan['detached_identity_links'])} 条")
    print(f"  unified_person.group_overlays           {len(plan['group_overlays'])} 条")
    print(f"  unified_person.audit_events             {len(plan['audit_events'])} 条")
    print(f"  unified_person.operations               {len(plan['operations'])} 条")
    print(f"  unified_person.person_tombstones        {len(plan['person_tombstones'])} 条"
          f"{'' if not plan['person_tombstones'] else '  ⚠️ 存在墓碑 = 状态与预期不符'}")
    print(f"  unified_person.identity_tombstones      {len(plan['identity_tombstones'])} 条")
    print(f"  unified_person.p4_*/people              {len(plan['p4_people'])} 条")
    total = sum(len(plan[k]) for k in (
        "users", "profiles", "identity_links", "binding_checkpoints",
        "detached_identity_links", "group_overlays", "audit_events",
        "operations", "person_tombstones", "identity_tombstones", "p4_people",
    ))
    print(f"  ⇒ 合计 {total} 条记录")
    print("  说明：这个集合**逐字对照**插件自己的 finalize_person_purge()"
          "（unified_person_registry.py:1849+）的清理范围，")
    print("        另补上它「假定已 detach」所以不管的 identity_links；"
          "**不写任何 tombstone**（写了会永久封死 e2e 用户名）。")
    print()

    default_hash = _sha(_dump(users.get(DEFAULT_UID) or {}))
    expected = copy.deepcopy(doc)
    apply_removals(expected, plan)
    print("=== 4. 守卫 ===")
    print(f"  default 记录哈希（改前）: {default_hash}")
    print(f"  预期改后 users / profiles : {len(expected['users'])} / "
          f"{len(expected['unified_person']['profiles'])}")
    bad = post_checks(expected, plan, targets, default_hash)
    if bad:
        print("  ❌ 内存预演就没过：", file=sys.stderr)
        for item in bad:
            print(f"     - {item}", file=sys.stderr)
        return 4
    print("  ✅ 内存预演：无残留引用、无孤儿、default 未变")
    print()

    if not args.apply:
        print("=== 5. 干跑结束（--dry-run，**一个字节都没写**）===")
        print(f"  磁盘文件哈希仍为 {hashlib.sha256(COMPANIONS.read_bytes()).hexdigest()[:16]}"
              f"（应与开始时 {hashlib.sha256(raw).hexdigest()[:16]} 相同）")
        report_extras(doc, targets)
        return 0

    # ---------------- 真写 ----------------
    print("=== 5. 写入（--apply）===")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / f"plugin-users-cleanup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_file = backup_dir / "companions.json"
    shutil.copy2(COMPANIONS, backup_file)
    before_hash = hashlib.sha256(raw).hexdigest()
    if hashlib.sha256(backup_file.read_bytes()).hexdigest() != before_hash:
        print("❌ 备份校验失败（备份与原文件不一致）", file=sys.stderr)
        return 5
    print(f"  ✅ 备份绝对路径：{backup_file}  （{backup_file.stat().st_size} 字节，sha256={before_hash[:16]}）")

    report = {
        "at": stamp,
        "source": str(COMPANIONS),
        "backup": str(backup_file),
        "sha256_before": before_hash,
        "default_uid": DEFAULT_UID,
        "default_person": DEFAULT_PERSON,
        "default_record_hash_before": default_hash,
        "targets": [{"uid": u, "person_id": p} for u, p in targets],
        "removed": {k: (v if k != "users" else v) for k, v in plan.items()},
        "counts_before": {"users": len(users), "profiles": len(doc["unified_person"]["profiles"])},
    }

    tmp = COMPANIONS.with_suffix(".json.tmp-cleanup")
    mode = os.stat(COMPANIONS).st_mode & 0o777
    tmp.write_bytes(_dumps(expected))
    os.chmod(tmp, mode)
    os.replace(tmp, COMPANIONS)
    print(f"  已写入 {COMPANIONS}  ({COMPANIONS.stat().st_size} 字节)")

    # 写后复核：从磁盘重新读，独立再跑一遍断言
    reloaded = json.loads(COMPANIONS.read_bytes().decode("utf-8"))
    after_bad = post_checks(reloaded, plan, targets, default_hash)
    default_after = _sha(_dump((reloaded.get("users") or {}).get(DEFAULT_UID) or {}))
    report["counts_after"] = {
        "users": len(reloaded["users"]),
        "profiles": len(reloaded["unified_person"]["profiles"]),
    }
    report["default_record_hash_after"] = default_after
    # 最强断言：磁盘上的新内容必须**逐字节等于**"只在改前基础上删掉 plan 那些键"的结果
    same_as_expected = _canonical(reloaded) == _canonical(expected)
    report["matches_expected_pure_delete"] = same_as_expected
    report["post_check_failures"] = after_bad

    print("=== 6. 改后复核（从磁盘重新读）===")
    print(f"  users    : {len(users)} -> {len(reloaded['users'])}")
    print(f"  profiles : {len(doc['unified_person']['profiles'])} -> "
          f"{len(reloaded['unified_person']['profiles'])}")
    print(f"  default 记录哈希（改后）: {default_after}")
    print(f"  default 哈希比对: {'✅ 一致' if default_after == default_hash else '🔴 不一致'}")
    print(f"  与「纯删除预演结果」比对: {'✅ 逐字节一致（没有动到别的任何键）' if same_as_expected else '🔴 不一致'}")

    if after_bad or default_after != default_hash or not same_as_expected:
        print("🔴 复核失败，**立即从备份还原**：", file=sys.stderr)
        for item in after_bad:
            print(f"     - {item}", file=sys.stderr)
        shutil.copy2(backup_file, COMPANIONS)
        report["restored"] = True
        (backup_dir / "cleanup-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  已还原；报告：{backup_dir / 'cleanup-report.json'}", file=sys.stderr)
        return 6
    print("  ✅ 无残留引用、无孤儿、default 逐字节未变")

    report["sha256_after"] = hashlib.sha256(COMPANIONS.read_bytes()).hexdigest()
    (backup_dir / "cleanup-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  ✅ 报告：{backup_dir / 'cleanup-report.json'}")
    print()
    report_extras(reloaded, targets)
    print("=== 完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
