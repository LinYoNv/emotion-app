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


# 🔴 `astrbot.core.sp` —— 共享偏好单例（2026-10-19 补，陪伴 7.0.0 需要）
#
# 真实现 `astrbot/core/__init__.py:56` 就把它建在这里（`sp = SharedPreferences(...)`），
# 插件写 `from astrbot.core import sp`。陪伴 `native.py:15` 正是这么用的。
#
# ⚠️ 惰性创建：真实现在 import 时就建库，我们要等宿主把 ASTRBOT_ROOT 设好
#    （否则库会落到进程 cwd 下的 `data/`，重启方式一变数据就"消失"）。
#    用模块级 `__getattr__`（PEP 562）满足 `from astrbot.core import sp` 的语义。
_sp_instance = None


def get_shared_preferences():
    """取（惰性创建）共享偏好单例。"""
    global _sp_instance
    if _sp_instance is None:
        from .utils.shared_preferences import SharedPreferences

        _sp_instance = SharedPreferences()
    return _sp_instance


def __getattr__(name):
    if name == "sp":
        return get_shared_preferences()
    if name == "logger":
        return logger
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Context",
    "Star",
    "StarMetadata",
    "StarTools",
    "logger",
    "get_shared_preferences",
    "star_map",
    "star_registry",
]
