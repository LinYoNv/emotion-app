# -*- coding: utf-8 -*-
"""pytest 公共配置：把 host 目录放进 sys.path。

⚠️ 不在这里安装影子包 —— 因为安装是**进程级副作用**，
   某些测试需要验证"未安装时的行为"，交给各测试自己管。
"""
from __future__ import annotations

import sys
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))
