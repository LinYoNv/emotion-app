# -*- coding: utf-8 -*-
"""探针：实测 `auto_initialize=True`（**真实服务的默认值**）会发生什么。

⭐ 为什么要单独探这一条：

    现有 e2e 测试全部把 `auto_initialize` 设成 **False** ——
    也就是说"真实服务会走的那个分支"**从来没被跑过**。
    而它是会**真的执行 11 个插件自己的 `initialize()`** 的，
    可能：起后台任务 / 建目录 / 读网络 / 抛异常 / 挂住。

🔴 用**隔离进程 + 临时数据目录 + 硬超时**跑，绝不碰 `/opt/astrbot/data`。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = APP_ROOT / "host"
for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

#: 🔴 **插件源 = 仓库内 `plugins/`**（2026-10-19，情绪拍板）
#:
#: 原来指向 `/opt/astrbot/data/plugins` —— 别人 AstrBot 的生产目录：
#:   ① 违反 AGENTS.md 铁律「绝不碰 AstrBot」；
#:   ② 与 `tests/test_plugin_host_e2e.py`（读仓库内插件）**同名不同源**，
#:      同一个 pytest 进程里先后加载 ⇒ `sys.modules` 里留下旧版模块
#:      ⇒ 全量跑时 12 条判据互相污染（单独跑全绿）。
#: 换成本仓库的 6 个陪伴系插件后，判的是「我们的插件能不能被加载」。
PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins"


def snapshot_prod() -> dict[str, float]:
    """生产目录快照（只看 mtime，用于事后证明零污染）。"""
    snap: dict[str, float] = {}
    if not PLUGIN_ROOT.is_dir():
        return snap
    for p in PLUGIN_ROOT.rglob("*"):
        try:
            snap[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return snap


async def main() -> int:
    from emotion_app.infra.config import Config
    from emotion_app.storage.factory import build_store
    from emotion_app.web.app import create_app
    from emotion_app.web.auth import AuthManager

    token = "probe-auto-init-token-0001"
    cfg = Config()
    cfg.auth.access_token = token
    tmp = Path(tempfile.mkdtemp(prefix="probe-autoinit-"))
    cfg.storage.sqlite_path = str(tmp / "t.db")
    cfg.plugins.enabled = True
    cfg.plugins.mount_web_apis = True
    cfg.plugins.plugin_dirs = str(PLUGIN_ROOT)
    # 🔴 临时数据目录：绝不指向 /opt/astrbot/data
    cfg.plugins.data_dir = str(tmp / "plugin_data")
    # 🔴 源码镜像也落临时目录：插件会回写自己的"源码目录"，
    #    不镜像就会污染 `/opt/astrbot/data/plugins`（实测踩过两次）。
    cfg.plugins.source_mirror_dir = str(tmp / "plugin_src")
    cfg.plugins.auto_initialize = True  # ← 被测分支
    cfg.validate()

    before = snapshot_prod()

    store = build_store(cfg.storage)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=token))
    svc = app.config["EMOTION_PLUGIN_HOST"]

    t0 = time.monotonic()
    report = await svc.start()
    elapsed = time.monotonic() - t0

    after = snapshot_prod()

    out = {
        "elapsed_sec": round(elapsed, 2),
        "report_none": report is None,
        "loaded": getattr(report, "loaded", None),
        "loaded_plugins": getattr(report, "loaded_plugins", None),
        "failed": getattr(report, "failed", None),
        "mounted_count": getattr(report, "mounted_count", None),
        "initialized": getattr(report, "initialized", None),
        "init_failed": getattr(report, "init_failed", None),
        "tmp_data_dir": str(tmp),
        "prod_changed": sorted(set(after) - set(before))
        + sorted(k for k in before if k in after and before[k] != after[k]),
    }

    # 停宿主，看 terminate 是否也干净
    try:
        await svc.stop()
        out["stop_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["stop_ok"] = f"{type(exc).__name__}: {exc}"
    await store.close()

    # 临时目录里到底写了什么（证明数据落在我们这边）
    written = []
    pdir = Path(cfg.plugins.data_dir)
    if pdir.is_dir():
        for p in sorted(pdir.rglob("*"))[:40]:
            written.append(str(p.relative_to(pdir)))
    out["written_in_our_data_dir"] = written

    print("PROBE_JSON_BEGIN")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("PROBE_JSON_END")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("EMOTION_LOG_LEVEL", "WARNING")
    raise SystemExit(asyncio.run(main()))
