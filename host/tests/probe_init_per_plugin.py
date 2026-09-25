# -*- coding: utf-8 -*-
"""探针②：**逐插件**调用 `initialize()`，每个都带超时 —— 定位到底是谁挂住。

⭐ 为什么必须逐插件：

    探针①（整体 `svc.start()`）挂住了，但只告诉我们"某个插件的 initialize() 不返回"。
    而这条路径**就是真实服务的启动路径**（`before_serving` → `svc.start()`），
    也就是说：**任何一个插件的 initialize() 挂住 = 整个 Web 服务起不来**。
    必须点名是哪个插件，并量出它到底要多久。

🔴 判据：**不许"跳过"**。每个插件要么「按时完成」要么「超时失败」，都如实打印。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = APP_ROOT / "host"
for p in (str(APP_ROOT), str(HOST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PER_PLUGIN_TIMEOUT = 12.0


async def main() -> int:
    import shutil
    import tempfile
    import time

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.loader import PluginLoader

    tmp = Path(tempfile.mkdtemp(prefix="probe2-"))
    set_data_root(tmp / "plugin_data")
    (tmp / "plugin_data").mkdir(parents=True, exist_ok=True)

    # 🔴 直接在 **PluginLoader** 上逐个调 initialize() —— 这与宿主
    #    `auto_initialize=True` 的顺序、对象**完全一致**，只是加了逐个超时。
    #
    #    ⚠️ **必须传 mirror_root**：插件会回写自己的"源码目录"
    #       （comfyui_pro 的 `_conf_schema.json`），而下面这个目录是别人
    #       AstrBot 的生产目录 —— 不镜像就会污染它（实测踩过）。
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    loader = PluginLoader(
        plugin_dirs=["/opt/astrbot/data/plugins"],
        mirror_root=tmp / "plugin_src",
    )
    report = await loader.load_all(ports=HostPorts(config={}))

    results = []
    for rec in report.loaded:
        name = rec.dir_name
        t0 = time.monotonic()
        status = "ok"
        detail = ""
        try:
            await asyncio.wait_for(rec.instance.initialize(), timeout=PER_PLUGIN_TIMEOUT)
        except asyncio.TimeoutError:
            status = "TIMEOUT"
            detail = f">{PER_PLUGIN_TIMEOUT}s 未返回"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            detail = f"{type(exc).__name__}: {exc}"
        dt = time.monotonic() - t0
        results.append({"plugin": name, "status": status, "sec": round(dt, 2), "detail": detail})
        print(f"  [{status:8s}] {name:42s} {dt:6.2f}s {detail}", flush=True)

    print("PROBE2_JSON_BEGIN")
    print(
        json.dumps(
            {
                "loaded": report.loaded,
                "per_plugin": results,
                "timeouts": [r["plugin"] for r in results if r["status"] == "TIMEOUT"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("PROBE2_JSON_END")

    try:
        await asyncio.wait_for(loader.terminate_all(), timeout=20)
        print("stop_ok=True", flush=True)
    except asyncio.TimeoutError:
        print("stop_ok=TIMEOUT", flush=True)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
