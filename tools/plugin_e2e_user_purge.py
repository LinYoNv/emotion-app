# -*- coding: utf-8 -*-
"""离线清理陪伴插件里的**测试用户残留**（默认 dry-run；`--apply` 才写）。

## 为什么需要它
`tools/proactive_multisession_check.py` 这类判据会造 `e2e_<tag>_*` 用户，
而**插件自己的删除接口删不掉它们**：
  * `page_api_users_groups.py:1196` 对"已绑统一人物"的用户**按设计**拒绝直删，
    要求走「身份与隔离 → 归档」；
  * 归档又要求统一身份迁移不在 `{degraded,paused,stopped}` —— 当前正是 `paused`
    （`req041_migration_control.db` 里 `error_code=relationship_legacy_event_invalid`）。
⇒ 每跑一次判据就多留 3 条**永久残留**，面板上看着像"一堆删不掉的测试用户"。

## 用法
    # 先停服务（插件把 companions.json 缓存在内存里，运行中改会被覆盖）
    .venv/bin/python tools/plugin_e2e_user_purge.py                  # dry-run
    .venv/bin/python tools/plugin_e2e_user_purge.py --apply          # 真写
    .venv/bin/python tools/plugin_e2e_user_purge.py --apply --prefix=e2e_

## 清理范围（两条同时满足才动）
  * `user_id == 'default'`，或 `user_id` 以 `--prefix` 开头（默认 `e2e_`），
    或 `nickname == '你'`（历史测试产物）；
  * **绝不碰** `KEEP` 里的 uid（糯米）。

除了 `users`，还会清 `unified_person` 等 **18 个顶层结构**里指向这些用户的
`person_id` 引用（2026-10-18 实测：只清 `users` 会留下大量悬挂引用）。

## 这一轮踩到的四个坑（全部写进注释，别再犯）
① dry-run 必须**在副本上真删一遍**再校验 —— 否则"看到残留"是必然的，等于没验。
② 判断必须在**递归之前**做 —— 先递归会把内层 `person_id` 删掉，
   外层再判就 False ⇒ 整条留成空壳。
③ `profiles`/`identity_links`/… 是**混合容器**（不同 person 混在一个 dict 里）
   ⇒ 必须在"一条记录"这层判断。往上抬一层 = 整个容器被删
   （实测 profiles 16 → 0，**糯米的也没了**）。
④ `unified_person` 是**结构层**（它的 key 是 `profiles` 等结构名，不是记录），
   而其他顶层结构（`proactive_audit_log` 等）的直接子元素就是记录
   ⇒ 两者必须分开处理，不能一刀切。
"""
import json, shutil, sqlite3, sys, copy
from pathlib import Path

APPLY = '--apply' in sys.argv
PREFIX = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--prefix=')), 'e2e_')
PLUG = Path('data/plugins/astrbot_plugin_private_companion')
MEM  = Path('data/plugins/astrbot_plugin_memory_companion')
KEEP = {'sess-a85c907bea5c'}          # 糯米 —— 绝不动

cj = PLUG / 'companions.json'
orig = json.loads(cj.read_text(encoding='utf-8'))
d = copy.deepcopy(orig)
users = d.get('users') or {}
kill = [u for u, v in users.items()
        if isinstance(v, dict) and u not in KEEP
        and (u == 'default' or u.startswith(PREFIX) or str(v.get('nickname') or '') == '你')]
killp = {str((users[k] or {}).get('unified_person_id') or '') for k in kill} - {''}
keep_p = {str((users[k] or {}).get('unified_person_id') or '') for k in KEEP} - {''}
assert not (killp & keep_p), f"🔴 目标 person 与保留用户共用：{killp & keep_p}"
TARGETS = set(kill) | set(killp)

print(f"{'【真写】' if APPLY else '【dry-run：副本模拟】'} "
      f"{len(kill)} 用户 / {len(killp)} person")

def hits(o) -> bool:
    s = json.dumps(o, ensure_ascii=False)
    return any(t in s for t in TARGETS)

def drop_records(container) -> int:
    """逐条判断并删除（container 的**直接子元素** = 一条记录）。返回删了几条。"""
    n = 0
    if isinstance(container, dict):
        doomed = [k for k, v in container.items()
                  if any(t in str(k) for t in TARGETS) or hits(v)]
        for k in doomed:
            container.pop(k, None); n += 1
    elif isinstance(container, list):
        keep = [x for x in container if not hits(x)]
        n = len(container) - len(keep)
        container[:] = keep
    return n

# ---------- ① users ----------
for u in kill:
    users.pop(u, None)
d['users'] = users
report = {'users': len(orig['users']) - len(d['users'])}

# ---------- ② unified_person（结构层 → 逐个子容器清理）----------
up = d.get('unified_person')
if isinstance(up, dict):
    for sub, container in list(up.items()):
        if isinstance(container, (dict, list)):
            n = drop_records(container)
            if n: report[f'unified_person.{sub}'] = n

# ---------- ③ 其他顶层结构（直接子元素 = 记录）----------
for k in list(d.keys()):
    if k in ('users', 'unified_person'):
        continue
    box = d[k]
    if isinstance(box, (dict, list)):
        n = drop_records(box)
        if n: report[k] = n

print("\n=== 清理量 ===")
for k, n in report.items():
    print(f"  {k:38s} {n} 条")

# ---------- ④ 校验（删后副本）----------
s = json.dumps(d, ensure_ascii=False)
print("\n=== 校验 ===")
print(f"  残留目标用户: {sorted(set(d['users']) & set(kill)) or '无 ✅'}")
print(f"  保留用户仍在: {sorted(set(d['users']) & KEEP) or '🔴 糯米没了！'}")
print(f"  残留目标 id:  {[t for t in sorted(TARGETS) if t in s] or '无 ✅'}")
# 🔴 必须显式验糯米**没被误删** —— 这正是上一版差点造成的破坏
ok_nuo = all(keep_p and p in s for p in keep_p)
print(f"  糯米 person 保留: {'✅' if ok_nuo else '🔴 被误删！'}")

# ---------- ⑤ 数据库 ----------
SQL_PLAN = [
    (MEM / 'memory_companion.db',  [('memories','subject_id'), ('timeline','subject_id')]),
    (PLUG / 'req041_migration_control.db', [('migration_identities','identity_id'),
                                            ('migration_read_leases','identity_id')]),
]
ids = kill + sorted(killp)
ph = ','.join('?' * len(ids))
print("\n=== 数据库行 ===")
for db, specs in SQL_PLAN:
    if not db.exists(): continue
    c = sqlite3.connect(db)
    for t, col in specs:
        try:
            n = c.execute(f"select count(*) from {t} where {col} in ({ph})", ids).fetchone()[0]
        except sqlite3.OperationalError: continue
        if not n: continue
        print(f"  {db.name:28s} {t:24s} {n} 行")
        if APPLY:
            c.execute(f"delete from {t} where {col} in ({ph})", ids); c.commit()
    c.close()

# ---------- ⑥ 写盘 ----------
if APPLY:
    if not ok_nuo:
        raise SystemExit("🔴 糯米 person 会被误删，拒绝写入")
    tmp = cj.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.move(str(tmp), str(cj))
    print(f"\n✅ 已写入 {cj}")
else:
    print("\n（dry-run，未写入）")

print("\n=== 保护确认 ===")
print("  人设工厂库:", (PLUG / 'persona_profiles' / 'default.db').exists())
