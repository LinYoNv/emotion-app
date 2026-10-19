# -*- coding: utf-8 -*-
"""`astrbot.builtin_stars` 兼容层 —— AstrBot 内置插件包。

⚠️ 真 AstrBot 这里有一整套内置命令插件（`builtin_commands` 等）。
**我们不是要复刻它们**，只是让插件的 import 能走通。

见 `builtin_commands/commands/conversation.py` 的模块 docstring：
陪伴 7.0.0 会 import 它来**包裹 `/reset` 命令**，以观察"上下文重置完成"
这一事件（`astrbot_host/conversation_reset.py:26`）。
"""
from __future__ import annotations

__all__ = ["builtin_commands"]
