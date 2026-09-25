# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.custom_filter` 兼容层。

⭐ 依据：真 AstrBot `core/star/filter/custom_filter.py`。
   真实现提供 `CustomFilter` / `CustomFilterAnd` / `CustomFilterOr` 三个类。
"""
from __future__ import annotations

from typing import Any

from . import HandlerFilter


class CustomFilter(HandlerFilter):
    """自定义过滤器基类（照真实现）。"""

    def filter(self, event: Any, cfg: Any) -> bool:
        raise NotImplementedError


class CustomFilterAnd(CustomFilter):
    """多个过滤器**全部**满足（照真实现）。"""

    def __init__(self, filters: list[HandlerFilter] | None = None) -> None:
        self.filters = filters or []

    def filter(self, event: Any, cfg: Any) -> bool:
        return all(f.filter(event, cfg) for f in self.filters)


class CustomFilterOr(CustomFilter):
    """多个过滤器**任一**满足（照真实现）。"""

    def __init__(self, filters: list[HandlerFilter] | None = None) -> None:
        self.filters = filters or []

    def filter(self, event: Any, cfg: Any) -> bool:
        return any(f.filter(event, cfg) for f in self.filters)


__all__ = ["CustomFilter", "CustomFilterAnd", "CustomFilterOr"]
