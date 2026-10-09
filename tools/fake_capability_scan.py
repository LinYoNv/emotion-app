#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫「**有 UI / 能保存 / 但零消费点**」的配置字段（假能力）。

## 为什么要这个工具
本项目已第 6 次踩「同一概念多条写入路径」，而这一轮的形态更隐蔽：
**字段在 schema 里、在界面上、能改能存、还有校验 —— 就是没有任何代码读它。**
它不报错、不告警；用户改完发现"没反应"，只会以为自己没找对地方。

第九轮的 `ConversationManagerPort` 契约写错、`capabilities()` 写好了不调用，
都是同一家族。**通则：写好了不调用 = 没有。**

## 判据口径（三条，缺一会误报）
  ① **定义处**：`config_profile.SPECS` 里的字段名 —— **排除**，那是"声明"不是"消费"
  ② **schema/序列化/校验的机器搬运**：`to_dict`/`from_storage`/字段白名单这类
     **泛型遍历**也**排除**（它们对**所有**字段一视同仁，不构成"这个字段被用了"）
  ③ 剩下的引用才算**真实消费点**

⚠️ 扫的是「点号访问 + 字符串键」两种写法，且要**搜索两棵测试树之外的全部源码**
（`emotion_app/` + `host/`）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def specs() -> dict[str, list[str]]:
    """取出所有 schema 字段名（按 section 分组）。"""
    from emotion_app.domain.config_profile import SPECS

    out: dict[str, list[str]] = {}
    for section, fields in SPECS.items():
        out[section] = [f.key for f in fields]
    return out


#: 泛型搬运文件（对**所有**字段一视同仁，不算"某字段被消费"）
GENERIC_FILES = {
    "emotion_app/domain/config_profile.py",   # 声明 + 泛型 to_dict/校验
    "emotion_app/domain/config_profile_store.py",
    "emotion_app/web/api_admin.py",           # 泛型 PATCH 搬运
}
#: 这些目录/文件不算"消费"（测试、工具、前端）
SKIP_PARTS = ("tests/", "host/tests/", "tools/", ".venv/", "__pycache__/", "static/")


def scan() -> list[tuple[str, str, int]]:
    files: list[Path] = []
    for base in ("emotion_app", "host"):
        for p in (ROOT / base).rglob("*.py"):
            rel = str(p.relative_to(ROOT))
            if any(s in rel for s in SKIP_PARTS):
                continue
            files.append(p)

    results: list[tuple[str, str, int]] = []
    for section, names in specs().items():
        for name in names:
            hits = 0
            for p in files:
                rel = str(p.relative_to(ROOT))
                if rel in GENERIC_FILES:
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                # 两种写法：字符串键 "name" / 'name'，以及属性访问 .name
                if re.search(rf"""["']{re.escape(name)}["']""", text):
                    hits += 1
                elif re.search(rf"\.{re.escape(name)}\b", text):
                    hits += 1
            results.append((section, name, hits))
    return results


def main() -> int:
    rows = scan()
    fake = [(s, n) for s, n, h in rows if h == 0]
    live = [(s, n, h) for s, n, h in rows if h > 0]

    print(f"schema 字段总数：{len(rows)}")
    print(f"  有消费点：{len(live)}")
    print(f"  🔴 **零消费点（假能力）**：{len(fake)}\n")

    if fake:
        print("=" * 78)
        print("零消费点字段（界面上能改能存，但没有任何代码读它）")
        print("=" * 78)
        cur = None
        for s, n in fake:
            if s != cur:
                print(f"\n  [{s}]")
                cur = s
            print(f"    - {n}")

    print("\n" + "=" * 78)
    print("有消费点的字段（供对照）")
    print("=" * 78)
    cur = None
    for s, n, h in live:
        if s != cur:
            print(f"\n  [{s}]")
            cur = s
        print(f"    - {n}  （{h} 个文件引用）")

    # 退出码：有假能力 = 非 0（这样它能当 CI 门禁；但本工具是诊断用，
    # 所以默认仍返回 0，只有 --strict 才用退出码表达）
    return 1 if ("--strict" in sys.argv and fake) else 0


if __name__ == "__main__":
    sys.exit(main())
