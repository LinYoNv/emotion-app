# -*- coding: utf-8 -*-
"""`conversation` —— `/reset` / `/new` 命令的**观察点**（兼容层）。

⭐ 依据：真 AstrBot `builtin_stars/builtin_commands/commands/conversation.py`
（v4.27.5，只读参考）。

## 陪伴拿它做什么（`astrbot_host/conversation_reset.py:26-60`）

```python
native = conversation            # 本模块
commands = native.ConversationCommands
original_clear = native._clear_third_party_agent_runner_state
bindings = ((commands, "reset"), (commands, "new_conv"))   # 或 ((Main,"reset"),…)
# …然后：
#   native._clear_third_party_agent_runner_state = clear     ← 包一层
#   setattr(owner, name, decorate(original))                 ← 也包一层
```

**它要的是"命令执行成功之后"的那个时刻**：真 AstrBot 的 `reset` 在清完上下文后
没有留下"我成功了"的标记，陪伴就靠包裹这两个函数来补这个标记
（成功 → `event.set_extra(SUCCESS_MARKER, True)`；权限拒绝/清理失败则在之前就返回，
不会到那一步）。

## 差异（如实记录，**这一点必须让用户知道**）

真实现的 `reset()` / `new_conv()` 是**完整的命令实现**：
读配置、判权限、按 `agent_runner_type` 走第三方 runner、清 sp、清对话历史、
回一句「会话已重置」。

我们**不实现命令**（宿主的对话重置走自己的 Web API 与 UI，不走 QQ 命令）。
所以这里的 `reset` / `new_conv` 做的是**真实发生过的事**：

1. 取 `context.conversation_manager`；
2. 为这个 umo 新建一个对话（等价于"AstrBot 重置会话"的效果）；
3. 调 `_clear_third_party_agent_runner_state()`（那是陪伴包裹的目标点）。

⚠️ **不伪造的部分**：真实现会**按权限拒绝**非管理员（`reset` 里有
`is_admin` 判断）。我们**照做** —— 没权限就抛 `PermissionError`，
而不是"看起来重置了其实没动"。这就是项目铁律说的"别让能力缺失变成静默成功"。

⚠️ 另一个差异：真实现用的是 `message`（AstrMessageEvent）；
这里的形参名保持 `message`，语义不变。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")

__all__ = [
    "ConversationCommands",
    "_clear_third_party_agent_runner_state",
    "THIRD_PARTY_AGENT_RUNNER_KEY",
]

#: 第三方 agent runner → sp 里存会话状态的键
#: （照真实现 `conversation.py`；陪伴的 `clear()` 包裹点不读它，但保留以对齐形状）
THIRD_PARTY_AGENT_RUNNER_KEY: dict[str, str] = {
    "deerflow": "third_party_agent_runner_deerflow_session_key",
}


async def _clear_third_party_agent_runner_state(
    context: Any,
    umo: str,
    agent_runner_type: str,
) -> None:
    """清掉第三方 runner 的会话状态（照真实现 `:87-104`）。

    ⚠️ **这是陪伴包裹的目标点**（`conversation_reset.py:27`）——
    它的签名与语义必须保持一致：`(context, umo, agent_runner_type)`。

    没有 sp（共享偏好）时**直接返回**（真实现里 `session_key` 为空也直接返回，
    形状一致），但**记一条 debug** 而不是完全静默。
    """
    session_key = THIRD_PARTY_AGENT_RUNNER_KEY.get(agent_runner_type)
    if not session_key:
        logger.debug(
            "第三方 runner 无会话键，跳过清理：agent_runner_type=%s", agent_runner_type
        )
        return
    try:
        from astrbot_host.compat.core import get_shared_preferences
    except ImportError:
        logger.debug("共享偏好不可用，跳过第三方 runner 状态清理")
        return
    try:
        await get_shared_preferences().remove_async(
            scope="umo", scope_id=str(umo), key=session_key
        )
    except Exception as error:  # noqa: BLE001
        # 真实现这里是裸 await（异常会上抛）—— 照做，但先把原因说清楚
        logger.warning(
            "清理第三方 runner 状态失败：umo=%s key=%s error_type=%s %s",
            umo, session_key, type(error).__name__, error,
        )
        raise


class ConversationCommands:
    """`/reset` 与 `/new` 命令（照真实现的**类形状**，见模块 docstring）。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def reset(self, message: Any) -> None:
        """重置会话（照真实现 `:124` 的位置与用途）。

        🔴 **权限语义照真实现**：非管理员**拒绝**，不静默当成功。
        """
        if getattr(message, "is_admin", True) is False:
            raise PermissionError(
                "只有管理员可以重置会话（照真 AstrBot 的 /reset 权限规则）"
            )
        await self._reset_window(message, reason="reset")

    async def new_conv(self, message: Any) -> None:
        """新建对话（照真实现 `:222`）。"""
        await self._reset_window(message, reason="new_conv")

    async def _reset_window(self, message: Any, *, reason: str) -> None:
        """真正动手的那一步：清第三方状态 + 开新对话。"""
        umo = getattr(message, "unified_msg_origin", "") or ""
        agent_runner_type = ""
        try:
            cfg = self.context.get_config(umo=umo)
            agent_runner_type = (cfg.get("provider_settings", {}) or {}).get(
                "agent_runner_type", ""
            )
        except Exception:  # noqa: BLE001 — 配置读不到不是"不能重置"
            logger.debug("读取 provider_settings 失败，按空 runner 处理")

        await _clear_third_party_agent_runner_state(
            self.context, umo, agent_runner_type
        )

        manager = getattr(self.context, "_ports", None)
        manager = getattr(manager, "conversation_manager", None) if manager else None
        if manager is None:
            # ⚠️ 不伪造：没有会话端点就**明确说**，别让用户以为重置了
            raise RuntimeError(
                "宿主未装配 `conversation_manager`，无法新建对话。"
                "请在 `HostPorts(conversation_manager=...)` 里注入它。"
            )
        if umo:
            await manager.new_conversation(umo)
        logger.info("会话已重置（原因=%s，umo=%s）", reason, umo or "(空)")
