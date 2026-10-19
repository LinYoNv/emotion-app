# -*- coding: utf-8 -*-
"""`builtin_commands.main` —— 内置命令插件入口（兼容层）。

⭐ 依据：真 AstrBot `builtin_stars/builtin_commands/main.py`（v4.27.5）。

## 陪伴为什么要 import 它（`astrbot_host/conversation_reset.py:30-36`）

```python
if callable(getattr(commands, "reset", None)):
    bindings = ((commands, "reset"), (commands, "new_conv"))
else:
    from astrbot.builtin_stars.builtin_commands.main import Main
    bindings = ((Main, "reset"), (commands, "new_conv"))
```

这是**跨版本兼容分支**：老版 AstrBot 把 `/reset` 挂在 `Main` 上，
新版挂在 `ConversationCommands` 上。我们的 `ConversationCommands` 有 `reset`
⇒ 走上面那支，`Main` 只是**兜底 import 存在性**。

⚠️ 所以 `Main` 必须存在且**有 `reset` 方法**（真实现 `main.py:16-21`），
否则那个兜底分支一旦被走到就是 `AttributeError`。
"""
from __future__ import annotations

from typing import Any

from .commands.conversation import ConversationCommands

__all__ = ["Main"]


class Main:
    """内置命令插件（照真实现 `main.py:16-21` 的最小形状）。"""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.conversation_c = ConversationCommands(context)

    async def reset(self, message: Any) -> None:
        """老版 AstrBot 的入口（转给 `ConversationCommands.reset`）。"""
        await self.conversation_c.reset(message)

    async def new_conv(self, message: Any) -> None:
        await self.conversation_c.new_conv(message)
