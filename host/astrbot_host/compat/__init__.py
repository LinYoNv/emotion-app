# -*- coding: utf-8 -*-
"""`astrbot` 兼容层 —— 行为等价的宿主端口。

⚠️ 这个包**只做协议转换**，不写业务规则（照作者 §7 验收判据）。
⚠️ **领域层绝不能 import 它**（否则违反「便携层不依赖宿主」）。
"""
from __future__ import annotations

# 🔴 `__version__` —— 插件拿它做**宿主契约校验**，必须报真实值
#
# 陪伴 7.0.0 `astrbot_host/native.py:14` 顶层 `from astrbot import __version__`，
# 然后 `companion_v2/adapters/astrbot/native.py:29` 用它比对白名单：
#
#     SUPPORTED_VERSIONS = frozenset(("4.27.2", "4.28.2"))
#
# ⚠️ **本机真 AstrBot 是 4.27.5 —— 不在白名单里**。所以：
#   我们报 `4.27.2`（白名单里最接近、且本机源码可对照的那个契约），
#   并**按 4.27.2 的行为实现兼容层**（真 4.27.5 的那些增量接口我们没实现，
#   报 4.27.5 会变成"声称支持却没做到"，比报低更危险）。
#
# ⚠️ 这个常量**必须与 `astrbot_host/compat/core/config/default.py` 的 VERSION 一致**
#   （真实现 `core/config/default.py:9` 就是 `VERSION = __version__`）。
__version__ = "4.27.2"

__all__ = ["api", "core", "__version__"]
