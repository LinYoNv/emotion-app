# -*- coding: utf-8 -*-
"""`astrbot.core.tools.registry` 兼容层 —— `builtin_tool` 装饰器。

⭐ 依据：真 AstrBot `core/tools/registry.py`。

⚠️ 只补插件**可能间接碰到的**最小面。真实现里 `builtin_tool` 做的是
"把工具登记进内置工具表 + 注入默认 config"，我们**没有内置工具表**
（宿主的工具来自插件注册，见 `astrbot_host/registry.py`）。

所以这里的 `builtin_tool` 是**直通装饰器**：给类打一个 `_builtin_tool` 标记
就返回原类。这样如果某个插件照抄了 AstrBot 内置工具的写法，不会因为
"没有这个符号"而 import 失败。

🔴 **它不注册任何东西** —— 别以为用了它就"进了模型可见的工具集"。
宿主的工具注册走 `astrbot.api.star.register` / `@filter.llm_tool`。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["builtin_tool"]


def builtin_tool(_cls: Any = None, *, config: Any = None, **kwargs: Any) -> Callable:
    """直通装饰器（见模块 docstring）。支持 `@builtin_tool` 与 `@builtin_tool(config=…)` 两种写法。"""

    def wrap(cls: Any) -> Any:
        try:
            setattr(cls, "_builtin_tool", True)
            setattr(cls, "_builtin_tool_config", config)
        except (AttributeError, TypeError):
            # 不可变对象（极少数）→ 不阻断，装饰器语义仍是"原样返回"
            pass
        return cls

    if _cls is not None:
        return wrap(_cls)
    return wrap
