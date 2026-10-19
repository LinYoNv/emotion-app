# -*- coding: utf-8 -*-
"""`astrbot.core.platform.sources` —— 各平台适配器包（兼容层）。

⚠️ 我们**只有一个平台适配器**要提供：aiocqhttp（QQ/OneBot）。
其它平台（telegram/discord…）本项目不用，**不放假实现** ——
缺了就是缺了，显式 ImportError 好过"有个类但调不通"。
"""
from __future__ import annotations

__all__ = ["aiocqhttp"]
