# -*- coding: utf-8 -*-
"""`astrbot.api.message_components` —— 消息段（**138 个文件用，最广的 API**）。

⭐ 依据：真 AstrBot `api/message_components.py` —— 实测该文件**全文只有 1 行**：
       `from astrbot.core.message.components import *`
   真实定义全在 `core/message/components.py`。

⚠️ 所以我们照抄这个结构（一层 re-export），
   这样 `from astrbot.api.message_components import Plain` 与
   `from astrbot.core.message.components import Plain` **拿到同一个类**
   —— `isinstance` 判断才不会跨模块失败。
"""
from astrbot_host.compat.core.message.components import *  # noqa: F403
from astrbot_host.compat.core.message.components import __all__ as _all

__all__ = list(_all)
