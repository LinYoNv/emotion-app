# -*- coding: utf-8 -*-
"""`AiocqhttpMessageEvent` —— QQ / OneBot 平台事件（兼容层）。

⭐ 依据：真 AstrBot `core/platform/sources/aiocqhttp/aiocqhttp_message_event.py`
（v4.27.5，只读参考）。

## 在本项目里的用途：**类型标记**

陪伴 `astrbot_host/native.py:17` 顶层导入它，登记进
`NativeAdapterTypes(event=AiocqhttpMessageEvent)`，用于判断
"当前事件是不是 OneBot 私聊事件"。它**不实例化**这个类 ——
真正的事件由宿主 `astrbot_host/dispatch.py` 构造。

## 与真实现的差异（如实记录）

真实现的这个类有 200+ 行，做的是**平台协议转换**：
- `_from_segment_to_dict()`：把 `Image`/`Record` 段转成 base64 dict
- `send()`：按 `message_type` 走 `send_private_msg` / `send_group_msg`
- `get_group_id()` / `is_private_chat()` 等平台判定

我们**继承宿主的 `AstrMessageEvent`**（那是本项目事件的真实实现，
`astrbot_host/compat/core/platform/astr_message_event.py`），
只补真实现里**这个子类特有**的字段与方法：

| 成员 | 用途 | 出处（真实现） |
|---|---|---|
| `self.bot` | OneBot 连接（`__init__` 第 5 个参数） | `:29` |
| `_from_segment_to_dict()` | 段 → dict | `:34-60` |

🔴 **为什么不重写 `send()`**：宿主的投递走 `dispatch.py` / `delivery` 端口，
事件对象上的 `send` 会绕过那条链（丢授权校验、丢未知结果保护）。
真实现之所以在事件上有 `send`，是因为它同时是 "event + 平台客户端"；
我们的分层是"事件只描述发生什么"，投递另有端口 ⇒ **不伪造 `send`**，
真有人调用会走宿主 `AstrMessageEvent` 已有的实现（若没有则 AttributeError，
这正是我们想要的"显式缺失"）。
"""
from __future__ import annotations

from typing import Any

from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

__all__ = ["AiocqhttpMessageEvent"]


class AiocqhttpMessageEvent(AstrMessageEvent):
    """OneBot 平台事件（照真实现 `:22-32` 的构造签名）。"""

    def __init__(
        self,
        message_str: str = "",
        message_obj: Any = None,
        platform_meta: Any = None,
        session_id: str = "",
        bot: Any = None,
        **kwargs: Any,
    ) -> None:
        # ⚠️ 真实现把 bot 放在**第 5 个位置参数**（`:29`）—— 顺序必须一致，
        #    插件可能按位置传参。
        super().__init__(
            message_str=message_str,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
            **kwargs,
        )
        self.bot = bot

    @staticmethod
    async def _from_segment_to_dict(segment: Any) -> dict:
        """把一个消息段转成 OneBot 的 dict 形状（照真实现 `:34-60`）。

        ⚠️ 真实现对 `Image` / `Record` 会**转 base64**（`convert_to_base64()`）。
        我们照做 —— 但只在段对象真的提供该方法时；否则退化成 `to_dict()`。
        静默退化的风险是"图发成了链接"而不是"没发出去"，可以接受；
        但**失败要能被看到**，所以拿不到任何形状时抛 `ValueError`。
        """
        seg_type = getattr(segment, "type", "")
        if isinstance(seg_type, str):
            seg_type = seg_type.lower()

        if seg_type in ("image", "record"):
            convert = getattr(segment, "convert_to_base64", None)
            if callable(convert):
                bs64 = await convert()
                return {"type": seg_type, "data": {"file": f"base64://{bs64}"}}

        to_dict = getattr(segment, "to_dict", None)
        if callable(to_dict):
            result = await to_dict()
            if isinstance(result, dict):
                return result

        # 兜底：按已知字段拼（真实现也有类似分支）
        if seg_type:
            return {"type": seg_type, "data": {}}
        raise ValueError(f"无法把消息段转成 OneBot dict：{type(segment).__name__}")
