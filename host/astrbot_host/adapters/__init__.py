# -*- coding: utf-8 -*-
"""消息平台适配器（OneBot v11 等）。

⚠️ 这个包是**宿主侧**代码（`astrbot_host`），不是插件。它负责
"外部消息平台 ↔ 我们的派发链路"，插件对它一无所知 —— 插件只看到
`platform_manager` 里的平台实例（那是 `platforms.PlatformSpec` 提供的）。

模块分工：

| 文件 | 职责 |
|---|---|
| `onebot.py` | OneBot v11 协议实现（反向 WS、消息段转换、发送） |
| `runtime.py` | 起停编排、入站接派发、出站取 sender |

配置存储（`adapters.json`）在 `emotion_app/infra/adapters_store.py` ——
**故意放在 emotion_app 侧**：它是"用户配置"，不属于宿主管兼容的范围。
"""

from astrbot_host.adapters.runtime import AdapterRuntime

__all__ = ["AdapterRuntime"]
