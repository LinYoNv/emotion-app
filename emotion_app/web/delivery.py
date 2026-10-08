# -*- coding: utf-8 -*-
"""投递端口：把**插件主动发出的消息**写进情绪app 的会话历史。

## 它解决什么

在此之前，`HostPorts.delivery` **一直是 `None`**（`plugin_host.py` 里写着
"故意传 None"）。后果是插件每一条主动消息都走不通：

| 路径 | 插件的调用 | 我们的结局 |
|---|---|---|
| A | `context.send_message(umo, chain)` | `NotImplementedError`（端口为空） |
| B | `platform.send_by_session(session, chain)` | 返回 `False`（如实失败） |

→ 陪伴的**主动消息**（"她想你了"那一类）**一条也发不出来**。

## 🔴 边界（刻意划清，照项目铁律）

**本模块只把消息写进【我们自己的会话存储】**，不接任何外部推送
（不调 APNs/邮件/webhook）。理由：
  1. 单独一个宿主服务不该偷偷往外发东西 —— 那是最需要显式授权的动作；
  2. 我们的面板就是用户看消息的地方，写进会话历史即"用户能看到"；
  3. 将来要加推送，是**再加一个消费者**，不是改这里。

⚠️ **不伪造送达**（作者验收判据③）：
   写不进去就返回 `False` / 抛错，**绝不假装成功** ——
   插件据此走它自己的降级路径，而它本来就有。

## umo 怎么映射到会话

插件的每个用户对应**一个固定的 umo**（`11:FriendMessage:<umo_user_id>`），
所以这里做**一对一**：**一个 umo 一个会话**（会话 id 由 umo 确定性派生）。

⚠️ 为什么用**确定性派生**而不是随机 uuid：
   进程重启后必须还能找回**同一个**会话，否则主动消息会散落在无数个新会话里。

⚠️ 为什么现在不做"注入到用户当前打开的会话"：
   那需要 umo↔多会话的映射 + "发给哪一个"的策略（多会话时是真设计问题），
   而插件只知道 umo。**先做一对一，语义清晰且可验证**；
   "注入当前会话"是后续增强，不改变本模块的契约。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger("emotion_app.plugins.delivery")

#: 主动消息会话的 id 前缀 —— 便于在会话列表里一眼认出"这是插件发的"
PROACTIVE_SESSION_PREFIX = "plugin:"

#: 会话标题（插件主动消息落到新会话时用）
PROACTIVE_SESSION_TITLE = "陪伴（主动消息）"


def umo_user_id(umo: str) -> str:
    """umo → 插件用户 id（= **第三段**）。

    🔴 为什么是第三段（实测契约）：
       插件把每个用户的数据放在 `data["users"][<user_id>]`，而 `user_id`
       来自 `event.get_sender_id()`（`main.py:16577`）；
       我们的 `build_inbound_event(text, user_id)` 里
       **`sender` = `MessageMember(user_id)`、umo = `build_umo(user_id)`** ——
       所以 **`sender_id` == `user_id` == umo 第三段**。

    ⚠️ 实测生产 `companions.json`：`users` 的**键是纯 `3412642082`**（不含冒号），
       umo / `bound_delivery_umo` 是它**内部的字段** —— 印证了这个取法。

    ⚠️ 解析不出来（umo 格式不对）→ 返回**原串**而不是空：
       宁可拿一个不匹配的键（表现为"找不到该用户的会话"，会留日志），
       也不要静默把不同用户**混成一个空键**。
    """
    raw = str(umo or "")
    parts = raw.split(":", 2)          # 照真实现 `split(":", 2)`：第三段可含冒号
    if len(parts) == 3 and parts[2]:
        return parts[2]
    return raw


def session_id_for_umo(umo: str) -> str:
    """umo → 会话 id（**确定性**，重启后不变）。

    ⚠️ **这是兜底路径**：优先用 `SessionRepository.by_user_id()` 找**用户自己的会话**
       （方案 B）；只有找不到时才用这个派生 id 新建一个，
       免得主动消息因为"用户还没建过会话"而丢掉。

    ⚠️ 用 sha256 而不是明文：umo 含 `:` 等字符，直接当会话 id 会与路由冲突；
       摘要同时保证"同一个 umo 永远是同一个会话"。前缀保留可读性。
    """
    digest = hashlib.sha256(str(umo or "").encode("utf-8")).hexdigest()[:16]
    return f"{PROACTIVE_SESSION_PREFIX}{digest}"


def chain_to_text(chain: Any) -> str:
    """消息链 → 纯文本。

    ⚠️ **降级必须可见**（照作者「能力不对等要如实 degraded」）：
       我们目前只呈现文本；图片/语音/文件等组件**降级成 `[类型]` 标记**，
       并记一条 WARNING —— 绝不静默丢掉用户本该看到的内容
       （本项目最贵的那类 bug：「参考图被静默丢弃」）。
    """
    if chain is None:
        return ""
    # `MessageChain` 有 `get_plain_text`；裸 list 就自己遍历
    getter = getattr(chain, "get_plain_text", None)
    if callable(getter):
        return str(getter() or "")

    parts: list[str] = []
    degraded: list[str] = []
    for comp in chain if isinstance(chain, (list, tuple)) else []:
        text = getattr(comp, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            name = type(comp).__name__
            degraded.append(name)
            parts.append(f"[{name}]")
    if degraded:
        logger.warning(
            "主动消息里有 %d 个非文本组件被降级成标记：%s（用户看不到原内容）",
            len(degraded),
            ", ".join(sorted(set(degraded))),
        )
    return "".join(parts)


class SessionDelivery:
    """把消息链投递成**我们的会话消息**（`DeliveryPort` 的实现）。

    用法（装配层）::

        delivery = SessionDelivery(sessions=sessions, settings=settings)
        build_platform_manager(delivery=delivery)
        HostPorts(delivery=delivery, ...)
    """

    def __init__(self, *, sessions: Any, settings: Any = None) -> None:
        #: `SessionRepository`
        self._sessions = sessions
        #: `SettingsService`（可选）—— 用来给新会话挂上"当前人设"。
        #: ⚠️ 当前人设的**权威来源是 Settings**（`active_persona_id` 是 property），
        #:    不是 `PersonaRepository`（那里没有"哪个是当前"的概念）。
        self._settings = settings
        #: 投递记录（**只留最近 N 条**，仅供诊断；不是第二份消息正本）
        self._recent: list[dict[str, Any]] = []

    # ---- DeliveryPort 契约 ----

    async def send(self, umo: str, chain: Any) -> Any:
        """把消息链写进 `umo` 对应的会话。

        Returns:
            `True` = **真的写进去了**；`False` = 写不进去（如实失败）。

        ⚠️ 返回语义照 `OurPlatform.send_by_session`：
           插件的判定是 `if result is not False` —— 返回 `False` 会让它
           走"回退核心发送"那条路（它本来就有）。
           **我们绝不在没写成功时返回 True。**
        """
        text = chain_to_text(chain)
        if not text.strip():
            # 空链：真实现里插件用 `if not chain` 自己判过，走到这里说明是异常情况。
            # ⚠️ 不当成功（否则插件以为发出去了）。
            logger.warning("投递收到空消息链（umo=%s）→ 如实返回 False", umo)
            return False

        if self._sessions is None:
            logger.error("投递端口没有会话仓库（装配缺失）→ 如实返回 False")
            return False

        session_id = await self._resolve_session_id(umo)
        try:
            from emotion_app.domain.message import ROLE_ASSISTANT, Message

            # 会话不存在就建一个（插件的主动消息可能是第一句话）
            persona_id = self._current_persona_id()
            session = await self._sessions.ensure(
                session_id,
                title=PROACTIVE_SESSION_TITLE,
                persona_id=persona_id,
                # 🔴 建会话时**把 user_id 绑上**（方案 B）：
                #    这样它和"面板里那个会话"是同一个插件用户，
                #    而不是孤儿会话（原来用 sha256 造 id，谁都找不到它）。
                user_id=umo_user_id(umo),
            )
            # ⚠️ `ensure` 返回的会话 id 可能与我们请求的不同：若这个 umo
            #    已经绑在某条**用户自己的会话**上，`_resolve_session_id` 会先找到它
            #    （那条路径返回的就是它的 id，一般一致）；但仓库实现若做了
            #    规整/迁移，我们要以**它返回的那个**为准，否则消息会写进一条
            #    刚刚被 ensure 判定为"不存在"的 id（写不进去，静默丢消息）。
            real_sid = str(getattr(session, "id", "") or session_id)
            await self._sessions.append(
                real_sid,
                [Message(role=ROLE_ASSISTANT, content=text)],
                # 🔴 **未读 +1**：这是"用户不在场时进来的东西"，
                #    正是侧边栏那个红点存在的理由（见 `Session.unread` 的注释）。
                #    ⚠️ 用户自己发起的聊天**不 +1**（他此刻就在页面上看着）——
                #       那条路径走 `ChatService.record`，压根不传这个参数。
                unread_delta=1,
            )
        except Exception as exc:  # noqa: BLE001
            # 🔴 记 ERROR 并**如实失败** —— 绝不吞掉后返回 True（伪造送达）
            logger.error(
                "主动消息写入会话失败（umo=%s, session=%s）：%s: %s",
                umo,
                session_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return False

        self._remember(umo=umo, session_id=real_sid, text=text)
        logger.info(
            "主动消息已投递：umo=%s → 会话 %s（%d 字）",
            umo,
            real_sid,
            len(text),
        )
        return True

    async def _resolve_session_id(self, umo: str) -> str:
        """umo → 该落到**哪个会话**（方案 B：优先用户自己的会话）。

        🔴 两条路的优先级（这是本节的核心）：

        1. **按 `user_id` 找会话** —— `umo` 第三段就是插件的 user_id，
           `SessionRepository.by_user_id()` 找到**用户在面板里用的那个会话**。
           → 主动消息落进**他看得见的会话**，与他的记忆/人格/上下文一致。
        2. 找不到（用户还没建过会话）→ 用 `session_id_for_umo()` 派生的 id
           **新建一个**，并绑上该 `user_id`。

        ⚠️ **为什么必须有第 1 条**：原来只有第 2 条 —— 每个 umo 造一个
           `plugin:<sha256>` 的**孤儿会话**，用户在面板里根本找不到它，
           而他自己那个会话**永远收不到主动消息**。
        """
        uid = umo_user_id(umo)
        if uid:
            try:
                found = await self._sessions.by_user_id(uid)
            except Exception:  # noqa: BLE001 — 查找失败不该让投递整个失败
                logger.error(
                    "按 user_id=%r 找会话失败，回落到派生 id", uid, exc_info=True
                )
                found = None
            if found is not None:
                return found.id
        return session_id_for_umo(umo)

    # ---- 诊断 ----

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近投递记录（**不含正文**以外的东西，供面板/诊断看）。"""
        return list(self._recent[-limit:])

    def _remember(self, *, umo: str, session_id: str, text: str) -> None:
        self._recent.append(
            {"umo": umo, "session_id": session_id, "chars": len(text)}
        )
        # 只留最近 200 条 —— 这是诊断缓存，不是第二份消息正本
        if len(self._recent) > 200:
            del self._recent[:-200]

    def _current_persona_id(self) -> str:
        """给主动消息会话挂上"当前人设"（拿不到就空着，不报错）。

        ⚠️ 权威来源是 `SettingsService.active_persona_id`（**property**，不是方法）——
           照 `ChatService.resolve_persona`（`domain/chat.py:259`）的取法。
           拿不到就返回空串：会话不带人设**不影响消息可见**，
           不该因为这一步失败就让整条主动消息投递失败。
        """
        if self._settings is None:
            return ""
        try:
            return str(getattr(self._settings, "active_persona_id", "") or "")
        except Exception:  # noqa: BLE001
            logger.warning("取当前人设失败（主动消息会话将不带人设）", exc_info=True)
            return ""


__all__ = [
    "umo_user_id",
    "PROACTIVE_SESSION_PREFIX",
    "PROACTIVE_SESSION_TITLE",
    "SessionDelivery",
    "chain_to_text",
    "session_id_for_umo",
]
