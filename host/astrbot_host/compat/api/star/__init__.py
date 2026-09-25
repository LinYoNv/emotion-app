# -*- coding: utf-8 -*-
"""`astrbot.api.star` —— 插件最常用的 import 入口之一（**78 个文件用**）。

⭐ 依据：真 AstrBot `api/star/__init__.py`（实测 7 行）：
       from astrbot.core.star import Context, Star, StarTools
       from astrbot.core.star.config import *
       from astrbot.core.star.register import register_star as register
"""
from astrbot_host.compat.core.star import Context, Star, StarTools
from astrbot_host.compat.core.star.config import *  # noqa: F403
from astrbot_host.compat.core.star.register import register_star as register

__all__ = ["Context", "Star", "StarTools", "register"]
