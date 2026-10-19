# -*- coding: utf-8 -*-
"""`OneBotClient` —— 走事件的 OneBot 连接发 action（照真实现语义）。

⭐ 依据：真 AstrBot `core/utils/quoted_message/onebot_client.py`（v4.27.5）。

## 契约（陪伴 `core_reply_media.py` 依赖的部分）

```python
OneBotClient(event)                          # 构造
OneBotClient._resolve_call_action(event)     # 🔴 **静态方法**，用来校验"还是同一个连接"
client.get_msg(message_id)                   # 取被引用消息
```

🔴 **`_resolve_call_action` 必须是静态方法**：陪伴在两次调用之间用它比对
`quote.client._call_action != 另一处取的 call_action` ⇒ 判定
「引用来自**另一个**账号/连接」并拒绝（`host_quote_account_changed`）。
如果我们每次新建一个 bound method，这两个对象**永远不相等** ⇒ 引用功能**全废**。

## 与真实现的差异

* 真实现有 `_call_action_try_params` 的多参数形状重试（不同 OneBot 实现对
  `get_msg` 的参数名不一致）。这里同样做 —— 但**简化为两种形状**
  （`message_id` 位置/关键字），并在全部失败时返回 `None` + warning。
* 真实现支持 `settings.warn_on_action_failure` 控制告警。我们**总是告警**：
  静默失败是项目明令禁止的。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger("astrbot")

__all__ = ["CallAction", "OneBotClient"]


class CallAction(Protocol):
    def __call__(self, action: str, **params: Any) -> Awaitable[Any] | Any: ...


def _unwrap_action_response(ret: Any) -> dict[str, Any]:
    """把 action 的返回规整成 dict（照真实现 `:12-18`）。

    OneBot 实现有两种返回形状：裸 `{...}` 或 `{"status":"ok","data":{...}}`。
    ⚠️ 两种都要能读 —— 只认一种会让"某个适配器下引用图永远为空"。
    """
    if not isinstance(ret, dict):
        return {}
    data = ret.get("data")
    if isinstance(data, dict):
        return data
    return ret


class OneBotClient:
    """绑定到一个事件的 OneBot action 调用器。"""

    def __init__(self, event: Any, settings: Any = None) -> None:
        self._call_action = self._resolve_call_action(event)
        self._settings = settings

    # ------------------------------------------------------------------
    # 🔴 静态方法：陪伴用它判断"还是不是同一个连接"
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_call_action(event: Any) -> CallAction | None:
        """从事件里取 `bot.api.call_action`（照真实现 `:34-41`）。

        ⚠️ 取不到时返回 `None`（**不抛**）：引用是可选能力，
        没有 OneBot 连接只是"引用取不到"，不该让整个回复失败。
        """
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        return call_action if callable(call_action) else None

    # ------------------------------------------------------------------
    # 通用 action
    # ------------------------------------------------------------------

    async def call(self, action: str, **params: Any) -> dict[str, Any] | None:
        """发一个 OneBot action（无重试形状）。"""
        if self._call_action is None:
            return None
        try:
            ret = self._call_action(action, **params)
            if inspect.isawaitable(ret):
                ret = await ret
        except Exception as error:  # noqa: BLE001 — 网络/适配器异常都要降级成 None
            logger.warning(
                "OneBot action 失败：action=%s error_type=%s %s",
                action, type(error).__name__, error,
            )
            return None
        return _unwrap_action_response(ret)

    async def _call_action_compat(
        self, action: str, params_list: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """按多种参数形状依次尝试（照真实现 `:43-84` 的思路）。"""
        if self._call_action is None:
            return None
        last_error: Exception | None = None
        for params in params_list:
            try:
                ret = self._call_action(action, **params)
                if inspect.isawaitable(ret):
                    ret = await ret
            except Exception as error:  # noqa: BLE001
                last_error = error
                continue
            return _unwrap_action_response(ret)
        if last_error is not None:
            logger.warning(
                "OneBot action 全部参数形状都失败：action=%s error_type=%s %s",
                action, type(last_error).__name__, last_error,
            )
        return None

    # ------------------------------------------------------------------
    # 具体 action
    # ------------------------------------------------------------------

    async def get_msg(self, message_id: str | int) -> dict[str, Any] | None:
        """取一条消息（照真实现 `:120-122`）。"""
        mid = str(message_id)
        return await self._call_action_compat(
            "get_msg",
            [{"message_id": mid}, {"message_id": int(mid) if mid.isdigit() else mid}],
        )

    async def get_forward_msg(self, forward_id: str | int) -> dict[str, Any] | None:
        """取合并转发内容（照真实现 `:123-125`）。"""
        fid = str(forward_id)
        return await self._call_action_compat(
            "get_forward_msg",
            [{"id": fid}, {"message_id": fid}, {"forward_id": fid}],
        )
