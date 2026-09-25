# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.regex` 兼容层。

⭐ 依据：真 AstrBot `core/star/filter/regex.py`。
"""
from __future__ import annotations

import re
from typing import Any

from . import HandlerFilter


class RegexFilter(HandlerFilter):
    """按正则匹配消息文本（照真实现）。"""

    def __init__(self, regex: str) -> None:
        self.regex = regex
        self._compiled = re.compile(regex)

    def filter(self, event: Any, cfg: Any) -> bool:
        text = getattr(event, "message_str", "") or ""
        return self._compiled.search(text) is not None


__all__ = ["RegexFilter"]
