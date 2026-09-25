# -*- coding: utf-8 -*-
"""`astrbot.core` —— 宿主核心（私有 API 面）。

⚠️ 真实现里 `astrbot.core` 是**内部**包，但陪伴插件有 54 个私有依赖落在它下面
   （实测）。我们的策略：**照实际用到的补**，并且**集中适配**（见 `event_dispatch.py`
   那 14 个 import 天然就是一层的例子）。

⚠️ 真实现 `from astrbot.core import file_token_service` / `logger` 这类
   "从包根取子模块"的写法，靠 `__init__.py` 里显式 import 满足。
"""
import logging as _logging

logger = _logging.getLogger("astrbot")

from .star import Context, Star, StarMetadata, StarTools, star_map, star_registry

__all__ = [
    "Context",
    "Star",
    "StarMetadata",
    "StarTools",
    "logger",
    "star_map",
    "star_registry",
]
