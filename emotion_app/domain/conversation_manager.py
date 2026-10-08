# -*- coding: utf-8 -*-
"""把「对话历史」接给插件 —— `HostPorts.conversation_manager` 的实现。

## 🔴 为什么必须有它（2026-09-27 第八轮续，情绪明确要求修）

真机日志（`logs/serve.out`，主动消息每一轮都出现）：

    [主动消息] 主动消息主链生成失败: 宿主未接入会话历史能力
                （`HostPorts.conversation_manager` 为空）
    [主动消息] 主动消息写入会话历史失败: 同一原因

**影响**：主动消息**发得出去**（有兜底），但插件**存不下自己刚说过的话**
（`proactive_message.py:18844` 那条 warning）⇒ 下一轮生成时读不到上文
⇒ **可能重复话题**（情绪原话：「这个很重要哦要修」）。

## 根因不在"忘了注入"，而在**契约写错了**

`ConversationManagerPort` 原来只有 `get_history()` / `append()` 两个方法
—— 那是**按我们以为插件会怎么用猜的**。实测（15 个调用点 + 真源码）：

* 插件的调用点用的是 `get_curr_conversation_id` / `get_conversation` /
  `new_conversation` / `update_conversation` / `add_message_pair`；
* 原来那两个方法**全库 0 调用点**。

⇒ 契约写错 = **谁也没法实现它**（这就是它一直是空的真原因）。
已按真实现（`astrbot/core/conversation_mgr.py`）重写 Protocol，本模块是它的实现。

## ⚠️ 三个最容易写错的数据形状（照真实现，不是我们发明的）

1. **`history` 必须是 JSON 字符串**（真实现 `_convert_conv_from_v2_to_v1`
   里 `json.dumps(...)`）。插件侧就有一条 `isinstance(raw_history, str)`
   的分支在读它（`proactive_message.py:18813`）—— 返回 list 会让那边
   **静默**当成空历史（不报错、只是上一句白说）。
2. **`new_conversation` 的第一个位置参数是 `platform_id`**（不是 title）。
   插件为此写了两层 `TypeError` 兜底（`proactive_message.py:4935-4944`），
   说明作者见过别人实现错。我们照真实现，别让它走兜底。
3. **`cid` 是 uuid 字符串**；`add_message_pair` 对不存在的 cid
   **要抛异常**（真实现 `raise Exception(f"Conversation with id {cid} not found")`）
   —— 静默丢弃会让插件的"存档成功"变成假象（本项目最忌）。

## 存储落点

新开一个 section `plugin_conversations`（**不动** `sessions`/`messages`：
那是我们自己的会话，与插件的"对话"是两个概念 —— 真 AstrBot 里
`conversations` 与 `platform_message_history` 也是分开的表）。

    {
      "<umo>":  {"sel": "<cid>"},                 # 该 umo 当前选中的对话
      "<cid>":  {"platform_id": ..., "user_id": "<umo>", "content": [...],
                 "title": ..., "persona_id": ..., "token_usage": 0,
                 "created_at": ..., "updated_at": ...}
    }

⚠️ `sel` 与 `cid` 同在一个 dict 里，用**前缀键**区分；cid 是 uuid，
   不会与字面量 `"sel"` 撞车。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..infra.logging_setup import get_logger

log = get_logger(__name__)

#: store 里的 section 名（新开一个 —— 见模块说明）
CONVERSATIONS_SECTION = "plugin_conversations"

#: 该 umo 当前选中对话的键（真实现把这个映射存在 `session_conversations`
#: 内存字典 + `sp.session_get(umo, "sel_conv_id")`；我们持久化到同一个 section）。
_SEL = "sel"

#: 一条对话最多保留多少条消息。真 AstrBot 不截断（它靠 LLM 侧压上下文），
#: 但**我们是自用后端，不能让插件把库写爆**：
#: ⚠️ 超限时**从头丢最早的**（保留最近），并在日志里说一次 —— 不静默。
MAX_MESSAGES = 400


class Conversation:
    """对话对象（**照真实现 `astrbot.core.db.po.Conversation` 的字段**）。

    ⚠️ `history` 是 **JSON 字符串**（见模块说明①）。插件就是这么读的。
    """

    __slots__ = (
        "platform_id", "user_id", "cid", "history", "title",
        "persona_id", "created_at", "updated_at", "token_usage",
    )

    def __init__(
        self,
        *,
        platform_id: str = "",
        user_id: str = "",
        cid: str = "",
        history: str = "[]",
        title: str | None = "",
        persona_id: str | None = "",
        created_at: int = 0,
        updated_at: int = 0,
        token_usage: int = 0,
    ) -> None:
        self.platform_id = platform_id
        self.user_id = user_id
        self.cid = cid
        self.history = history
        self.title = title
        self.persona_id = persona_id
        self.created_at = created_at
        self.updated_at = updated_at
        self.token_usage = token_usage

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Conversation cid={self.cid!r} umo={self.user_id!r} title={self.title!r}>"


class StoreConversationManager:
    """`HostPorts.conversation_manager` 的实现（挂在我们的 store 上）。

    🔴 **不持有长期状态**：每次操作都从 store 读、写完就存。
       理由：插件会跨进程/跨重载调用，缓存在内存里一旦与磁盘不一致，
       就会出现"插件以为存过了、其实没落盘"（本项目踩过同类：
       应用进程里改 `companions.json` 被插件内存态覆盖）。
       自用规模（一个用户几十条对话），这点开销可以忽略。
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    # ---- 内部：读写整个 section ----

    async def _load(self) -> dict[str, Any]:
        raw = await self._store.load_sections([CONVERSATIONS_SECTION])
        section = raw.get(CONVERSATIONS_SECTION) or {}
        if not isinstance(section, dict):
            # ⚠️ 格式坏掉时**报错**而不是当空 —— 当空会静默丢光用户历史
            raise RuntimeError(
                f"{CONVERSATIONS_SECTION} section 格式错误（期望 dict，得到 "
                f"{type(section).__name__}）—— 拒绝覆盖，请人工检查。"
            )
        return section

    async def _save(self, section: dict[str, Any]) -> None:
        await self._store.save_sections({CONVERSATIONS_SECTION: section})

    @staticmethod
    def _trim(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(content) <= MAX_MESSAGES:
            return content
        log.warning(
            "对话历史超过上限，丢弃最早的 %d 条（上限 %d）",
            len(content) - MAX_MESSAGES, MAX_MESSAGES,
        )
        return content[-MAX_MESSAGES:]

    @staticmethod
    def _to_conv(rec: dict[str, Any]) -> Conversation:
        content = rec.get("content") or []
        return Conversation(
            platform_id=str(rec.get("platform_id") or ""),
            user_id=str(rec.get("user_id") or ""),
            cid=str(rec.get("cid") or ""),
            # 🔴 JSON 字符串（模块说明①）
            history=json.dumps(content, ensure_ascii=False),
            title=rec.get("title"),
            persona_id=rec.get("persona_id"),
            created_at=int(rec.get("created_at") or 0),
            updated_at=int(rec.get("updated_at") or 0),
            token_usage=int(rec.get("token_usage") or 0),
        )

    # ---- 真契约的 5 个方法（与 `ConversationManagerPort` 一一对应）----

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        """该 umo 当前选中的对话 id；没有则 `None`（**不自动创建**，照真实现）。"""
        section = await self._load()
        entry = section.get(str(unified_msg_origin))
        if not isinstance(entry, dict):
            return None
        cid = entry.get(_SEL)
        # 只有这个 cid 的对话记录**真的还在**才算数 ——
        # 否则返回一个悬空 id，插件 `get_conversation` 会拿到 None，
        # 而它会以为"有对话但取不到"（比直接说"没有"更难排查）。
        if cid and isinstance(section.get(str(cid)), dict):
            return str(cid)
        return None

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> Conversation | None:
        """取对话对象；不存在时按 `create_if_not_exists` 决定是否新建（照真实现）。"""
        section = await self._load()
        rec = section.get(str(conversation_id))
        if not isinstance(rec, dict):
            if not create_if_not_exists:
                return None
            cid = await self.new_conversation(str(unified_msg_origin))
            section = await self._load()
            rec = section.get(cid)
            if not isinstance(rec, dict):
                return None
        return self._to_conv(rec)

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        """新建对话并把该 umo 的"当前对话"切到它；返回新 cid（照真实现）。

        ⚠️ **第一个位置参数是 `platform_id`**（模块说明②）。没给时从 umo
           里解析（`platform:type:id` 的第一段）—— 真实现也是这么做的。
        """
        umo = str(unified_msg_origin)
        if not platform_id:
            parts = umo.split(":")
            platform_id = parts[0] if len(parts) >= 3 else "unknown"
        now = int(time.time())
        cid = str(uuid.uuid4())
        section = await self._load()
        section[cid] = {
            "cid": cid,
            "platform_id": platform_id,
            "user_id": umo,
            "content": list(content or []),
            "title": title,
            "persona_id": persona_id,
            "token_usage": 0,
            "created_at": now,
            "updated_at": now,
        }
        section.setdefault(umo, {})
        if isinstance(section[umo], dict):
            section[umo][_SEL] = cid
        else:
            section[umo] = {_SEL: cid}
        await self._save(section)
        return cid

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: int | None = None,
    ) -> None:
        """更新对话（cid 为空时取该 umo 当前的；照真实现）。"""
        umo = str(unified_msg_origin)
        cid = conversation_id or await self.get_curr_conversation_id(umo)
        if not cid:
            # 真实现这里是 `if conversation_id:` 静默跳过。我们**不改语义**
            # （插件依赖"传空就不更新"），但留一行 debug 便于排查。
            log.debug("update_conversation 跳过：umo=%s 没有当前对话", umo)
            return
        section = await self._load()
        rec = section.get(str(cid))
        if not isinstance(rec, dict):
            # 真实现此时也是静默无操作（`db.update_conversation` 找不到就返回）
            log.debug("update_conversation 跳过：cid=%s 不存在", cid)
            return
        if history is not None:
            rec["content"] = self._trim(list(history))
        if title is not None:
            rec["title"] = title
        if persona_id is not None:
            rec["persona_id"] = persona_id
        if token_usage is not None:
            rec["token_usage"] = int(token_usage)
        rec["updated_at"] = int(time.time())
        section[str(cid)] = rec
        await self._save(section)

    async def add_message_pair(
        self,
        cid: str,
        user_message: Any,
        assistant_message: Any,
    ) -> None:
        """往 `cid` 追加一对消息（照真实现；**对话不存在要抛异常**）。"""
        section = await self._load()
        rec = section.get(str(cid))
        if not isinstance(rec, dict):
            # 照真实现抛 —— 静默会让插件的"存档成功"变成假象
            raise RuntimeError(f"Conversation with id {cid} not found")
        content = rec.get("content")
        content = list(content) if isinstance(content, list) else []
        content.append(_as_dict(user_message))
        content.append(_as_dict(assistant_message))
        rec["content"] = self._trim(content)
        rec["updated_at"] = int(time.time())
        section[str(cid)] = rec
        await self._save(section)


def _as_dict(msg: Any) -> dict[str, Any]:
    """把消息对象/字典统一成 dict（照真实现 `add_message_pair`）。

    真实现接受 `UserMessageSegment` / `AssistantMessageSegment`（pydantic）
    或普通 dict —— 它用 `isinstance(..., UserMessageSegment)` 判，
    然后 `model_dump()`。我们没有那两个类（不是必须），所以**按鸭子类型**：
    有 `model_dump()` 就调它，否则当 dict，再否则**显式报错**
    （不静默塞个空 dict —— 那会让历史里出现空消息，还查不出来）。
    """
    if isinstance(msg, dict):
        return msg
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        out = dump()
        if isinstance(out, dict):
            return out
    raise TypeError(
        f"add_message_pair 收到不支持的消息类型：{type(msg).__name__}"
        "（期望 dict 或带 model_dump() 的对象）"
    )
