# -*- coding: utf-8 -*-
"""`astrbot` 兼容层 —— 行为等价的宿主端口。

⚠️ 这个包**只做协议转换**，不写业务规则（照作者 §7 验收判据）。
⚠️ **领域层绝不能 import 它**（否则违反「便携层不依赖宿主」）。
"""
from __future__ import annotations

__all__ = ["api", "core"]
