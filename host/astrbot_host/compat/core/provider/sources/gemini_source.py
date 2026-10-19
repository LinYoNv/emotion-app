# -*- coding: utf-8 -*-
"""`ProviderGoogleGenAI` 的导入路径（照真实现模块名）。

⚠️ 类体在 `providers.py`（三个类型的实现放一处便于对照真实现差异）。
真实现的类就在 `gemini_source.py`，所以**导入路径必须一字不差** ——
陪伴 `native.py:19` 写的就是 `from astrbot.core.provider.sources.gemini_source
import ProviderGoogleGenAI`。
"""
from __future__ import annotations

from .providers import Provider, ProviderGoogleGenAI

__all__ = ["ProviderGoogleGenAI", "Provider"]
