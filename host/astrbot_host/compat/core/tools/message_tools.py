# -*- coding: utf-8 -*-
"""`astrbot.core.tools` 兼容层 —— 内置工具命名空间。

⭐ 依据：真 AstrBot `core/tools/`（v4.27.5，只读参考）。

## 为什么必须补（2026-10-19，陪伴 7.0.0 实测）

两个调用点，性质不同：

1. **硬依赖（启动链）**：`astrbot_host/core_reply_native_send.py:7` 顶层
   `from astrbot.core.tools.message_tools import SendMessageToUserTool`，
   而 `core_reply_native_send` 被 `main.py:38` 直接导入
   ⇒ 缺它**整个插件加载失败**。

2. **惰性依赖（运行时才有）**：`astrbot_host/news.py:516` 在 `fetch()` 里
   `from astrbot.core.tools import web_search_tools as search_tools`。
   这条**不在启动路径上**，但资讯主动消息会用到 —— 缺了它只是"资讯搜不到"，
   不会让插件加载失败。**按项目铁律：能力缺失要显式报出来，不能静默降级**
   （见 `web_search_tools` 的实现说明）。

## `SendMessageToUserTool` 的关键契约（照真实现 `core/tools/message_tools.py:78-200`）

陪伴 `core_reply_native_send.py:163` 有两处**依赖具体字段**的判断：

```python
if isinstance(original, SendMessageToUserTool) and original.handler is None:
```

* 必须是**类**（`isinstance` 判定）—— 所以它是类不是函数；
* **`handler` 必须是 `None`**（这是真实现里 `FunctionTool` 的默认值）——
  陪伴靠它区分"宿主原生的发送工具（需要它接管投递）"和
  "别的插件注册的同名工具（不该接管）"。
  ⚠️ 如果这里给 `handler` 塞一个实现，陪伴就**不会**接管投递 ⇒ 语音/表情/
  分段这些能力**全部静默失效**（不报错，只是"功能没了"）。

* 构造签名要能吃 `name=` / `description=` / `parameters=` / `active=` /
  `handler_module_path=` —— 陪伴 `CurrentReplySendTool.__init__` 就是这么传的。

## 与真实现的差异（如实记录）

* 真实现的 `call()` 会做：路径校验、沙箱下载、组件构建、`session` 解析、
  `MessageChain` 投递。我们**不在这里实现投递** —— 宿主的投递端口是
  `astrbot_host/dispatch.py` / `delivery`，而陪伴**本来就会接管**
  （它把 `handler is None` 的工具替换成自己的 `CurrentReplySendTool`）。
  ⇒ 这里的 `call()` 走 `handler`；没有 handler 时**如实抛 NotImplementedError**
  （照真实现基类语义，`core/agent/tool.py:69-73`）。
* `parameters` 的 JSON Schema **逐字照抄真实现**：模型看的就是这份 schema，
  少一个字段就少一种消息类型（例如丢了 `mention_user` 就再也不能 @ 人）。
"""
from __future__ import annotations

import logging
from typing import Any

from astrbot_host.compat.core.agent.tool import FunctionTool

logger = logging.getLogger("astrbot")

__all__ = ["SendMessageToUserTool", "SEND_MESSAGE_PARAMETERS"]


def _send_parameters() -> dict[str, Any]:
    """`send_message_to_user` 的参数 schema（**逐字照真实现**
    `core/tools/message_tools.py:88-144`）。

    ⚠️ 组件类型清单是模型的行为边界：`plain/image/record/video/file/mention_user`
    六种少一个，模型就学不会发那种消息。
    """
    return {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "description": (
                    "An ordered list of message components to send. "
                    "`mention_user` type can be used to mention the user."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": (
                                "Component type. One of: "
                                "plain, image, record, video, file, mention_user. "
                                "Record is voice message."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": "Text content for `plain` type.",
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "File path for `image`, `record`, `video`, or `file` types. "
                                "Both local path and sandbox path are supported."
                            ),
                        },
                        "url": {
                            "type": "string",
                            "description": (
                                "URL for `image`, `record`, `video`, or `file` types."
                            ),
                        },
                        "mention_user_id": {
                            "type": "string",
                            "description": "User ID to mention for `mention_user` type.",
                        },
                    },
                    "required": ["type"],
                },
            },
            "session": {
                "type": "string",
                "description": (
                    "Optional. Leave empty for the current session. "
                    "Use 'platform_id:message_type:session_id' to target another session."
                ),
            },
        },
        "required": ["messages"],
    }


#: 暴露成常量，方便判据直接比对（不复制一份字面量）
SEND_MESSAGE_PARAMETERS = _send_parameters()


class SendMessageToUserTool(FunctionTool):
    """宿主原生发送工具（照真实现 `message_tools.py:78-200` 的**字段与语义**）。

    🔴 **`handler` 必须保持 `None`** —— 见模块 docstring。陪伴靠它判断
    "这是不是宿主原生工具、要不要接管投递"。给了实现就静默丢掉语音/表情/分段。
    """

    def __init__(
        self,
        name: str = "send_message_to_user",
        description: str = (
            "Send message to the user. "
            "Supports various message types including `plain`, `image`, `record`, "
            "`video`, `file`, and `mention_user`. "
            "Use this tool to send media files (`image`, `record`, `video`, `file`), "
            "or when you need to proactively message the user(such as cron job). "
            "For other normal text replies, you can output directly and no need to use this tool."
        ),
        parameters: dict[str, Any] | None = None,
        handler: Any = None,
        handler_module_path: str | None = None,
        active: bool = True,
        is_background_task: bool = False,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            # ⚠️ 传 None 时用**真实现那份 schema**（不是空 dict）——
            #    陪伴 `CurrentReplySendTool` 会 deepcopy 它，模型靠它知道能发什么
            parameters=parameters if parameters is not None else _send_parameters(),
            handler=handler,
            handler_module_path=handler_module_path,
            active=active,
            is_background_task=is_background_task,
        )
