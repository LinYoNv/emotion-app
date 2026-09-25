# -*- coding: utf-8 -*-
"""`astrbot.core.star` —— Star 体系导出面。

⭐ 依据：真 AstrBot `core/star/__init__.py`。
   插件按 `from astrbot.core.star import Context, Star, StarTools` import（实测核心用法）。
"""
from .base import Star, StarMetadata, star_map, star_registry
from .context import Context, HostPorts
from .star_tools import StarTools

__all__ = [
    "Context",
    "HostPorts",
    "Star",
    "StarMetadata",
    "StarTools",
    "star_map",
    "star_registry",
]
