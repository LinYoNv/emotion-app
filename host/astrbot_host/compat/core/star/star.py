# -*- coding: utf-8 -*-
"""`astrbot.core.star.star` 兼容层 —— StarMetadata 与全局注册表。

⭐ 依据：真 AstrBot `core/star/star.py`（`StarMetadata` 定义 + `star_map` /
   `star_registry` 两个模块级单例）。

🔴 **为什么必须单独一个模块**：真实现把 `StarMetadata` 定义在 `star.py`，
   `base.py` 再 `from .star import StarMetadata, star_map, star_registry`。
   插件按 `from astrbot.core.star.star import star_map` import
   （实测 `final_response_persistence.py:16`）—— 路径少一层就 ImportError。

⚠️ 我们的 `StarMetadata` 实体定义在 `base.py`（避免循环 import），
   这里做 **re-export**：保证 `astrbot.core.star.star.StarMetadata`
   与 `astrbot.core.star.base.StarMetadata` 是**同一个类**
   （否则 `isinstance` 会跨模块失败）。
"""
from __future__ import annotations

from astrbot_host.compat.core.star.base import (
    Star,
    StarMetadata,
    star_map,
    star_registry,
)

__all__ = ["Star", "StarMetadata", "star_map", "star_registry"]
