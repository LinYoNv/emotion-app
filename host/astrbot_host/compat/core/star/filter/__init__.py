# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter` 兼容层 —— Handler 过滤器基类。

⭐ 依据：真 AstrBot `astrbot/core/star/filter/__init__.py`（实测 15 行）。
"""
from __future__ import annotations

import abc
from typing import Any


class HandlerFilter(abc.ABC):
    """所有过滤器的基类（照真实现）。"""

    @abc.abstractmethod
    def filter(self, event: Any, cfg: Any) -> bool:
        """是否应当被过滤。"""
        raise NotImplementedError


__all__ = ["HandlerFilter"]
