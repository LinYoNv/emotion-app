# -*- coding: utf-8 -*-
"""`astrbot.api` —— 公开 API 入口。

⭐ 依据：真 AstrBot `api/__init__.py`。
   插件按 `from astrbot.api import AstrBotConfig` / `from astrbot.api import logger` import
   （实测各 18 / 25 个文件）。
"""
import logging as _logging

from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig

#: 全局 logger（真实现里 `from astrbot.api import logger` 是**极常用**的用法）
logger = _logging.getLogger("astrbot")

# ⚠️ 真实现里 `astrbot.api` 也 re-export 常用装饰器（meme_manager 直接
#    `from astrbot.api import llm_tool`）—— 少一个就加载失败。
from astrbot_host.compat.core.star.register import register_llm_tool as llm_tool

__all__ = ["AstrBotConfig", "llm_tool", "logger"]
