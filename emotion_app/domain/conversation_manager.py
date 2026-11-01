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

原生对话保存在 `plugin_conversations`；内部投影接口将指定 cid 的历史
单向同步到已存在的 `sessions`/`messages` 行，三个 section 同事务提交。

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
from ..storage.base import StorageError
from .message import Message, from_upstream_message
from .session_repo import MESSAGES_SECTION, SESSIONS_SECTION, NativeProjectionReadOnly, SessionNotFound

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


def conversation_messages(history: str | list[dict[str, Any]]) -> list[Message]:
    """从权威原生历史生成模型消息；checkpoint 仅在出口过滤。"""
    parsed = json.loads(history) if isinstance(history, str) else history
    if not isinstance(parsed, list):
        raise ValueError("原生对话 history 必须是消息列表")
    if any(not isinstance(message, dict) for message in parsed):
        raise ValueError("原生对话 history 的每条消息必须是对象")
    return [from_upstream_message(message) for message in parsed
            if message.get("role") != "_checkpoint"]


class Conversation:
    """对话对象（**照真实现 `astrbot.core.db.po.Conversation` 的字段**）。

    ⚠️ `history` 是 **JSON 字符串**（见模块说明①）。插件就是这么读的。
    """

    __slots__ = (
        "platform_id", "user_id", "cid", "history", "title",
        "persona_id", "created_at", "updated_at", "token_usage", "reset_revision",
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
        reset_revision: int = 0,
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
        self.reset_revision = reset_revision

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

    def __init__(self, store: Any, *, persona_name_of: Any = None) -> None:
        """
        Args:
            persona_name_of: 🔴 **人设 id → 名字**的回调（`str -> str`），可为 None。
                为什么需要它（2026-10-31）：AstrBot / 陪伴插件的规范是
                **`persona_id` 就等于人设的 `name`**
                （`_persona_to_v3` 注释原话「`name` 是插件的主键」；
                 `multi_controller.py:243-244` 还会额外校验
                 `native["name"] == binding.persona_id`，不等就抛
                 `host_persona_unavailable`）。
                而我们面板存的是 UUID ⇒ 直接同步过去**必然对不上**。
                ⇒ 写 `plugin_conversations.persona_id` 时先翻译成 name。

                ⚠️ 用回调而不是持有 repo：本模块是**领域层**，
                   不该反向依赖人设仓库的存储形状（单向依赖）。
                   回调返回空串 = 查不到，如实写空（不猜）。
        """
        self._store = store
        self._persona_name_of = persona_name_of if callable(persona_name_of) else None

    # ---- 内部：读写整个 section ----

    async def _load(self) -> dict[str, Any]:
        return self._section(await self._store.load_sections([CONVERSATIONS_SECTION]))

    @staticmethod
    def _section(raw: dict[str, Any]) -> dict[str, Any]:
        section = raw.get(CONVERSATIONS_SECTION)
        if section is None:
            section = {}
        if not isinstance(section, dict):
            # ⚠️ 格式坏掉时**报错**而不是当空 —— 当空会静默丢光用户历史
            raise RuntimeError(
                f"{CONVERSATIONS_SECTION} section 格式错误（期望 dict，得到 "
                f"{type(section).__name__}）—— 拒绝覆盖，请人工检查。"
            )
        return section

    async def _mutate(self, change: Any, *, projection: bool = False) -> Any:
        keys = [CONVERSATIONS_SECTION]
        if projection:
            keys.extend([SESSIONS_SECTION, MESSAGES_SECTION])

        def mutate(raw):
            section = self._section(raw)
            result = change(section, raw)
            if section or CONVERSATIONS_SECTION in raw:
                raw[CONVERSATIONS_SECTION] = section
            return result

        return await self._store.update_sections(keys, mutate)

    @staticmethod
    def _selected(section: dict[str, Any], umo: str) -> dict[str, Any] | None:
        entry = section.get(umo)
        cid = entry.get(_SEL) if isinstance(entry, dict) else None
        rec = section.get(str(cid)) if cid else None
        if isinstance(rec, dict) and rec.get("user_id") == umo:
            return rec
        return None

    @staticmethod
    def _owned(section: dict[str, Any], umo: str, cid: str) -> dict[str, Any]:
        rec = section.get(str(cid))
        if not isinstance(rec, dict):
            raise RuntimeError(f"Conversation with id {cid} not found")
        if rec.get("user_id") != umo:
            raise RuntimeError(f"Conversation with id {cid} does not belong to {umo}")
        return rec

    @staticmethod
    def _create(
        section: dict[str, Any], umo: str, platform_id: str | None = None,
        content: list[dict[str, Any]] | None = None, title: str | None = None,
        persona_id: str | None = None,
    ) -> dict[str, Any]:
        parts = umo.split(":", 2)
        now = int(time.time())
        cid = str(uuid.uuid4())
        rec = {
            "cid": cid, "platform_id": platform_id or (parts[0] if len(parts) >= 3 else "unknown"),
            "user_id": umo, "content": list(content or []), "title": title,
            "persona_id": persona_id, "token_usage": 0, "created_at": now, "updated_at": now,
            "history_revision": 0, "reset_revision": 0,
        }
        section[cid] = rec
        entry = section.get(umo)
        if not isinstance(entry, dict):
            entry = {}
            section[umo] = entry
        entry[_SEL] = cid
        return rec

    @staticmethod
    def _trim(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(content) <= MAX_MESSAGES:
            return content
        start = len(content) - MAX_MESSAGES
        boundary = start
        while start < len(content) and content[start].get("role") in ("tool", "_checkpoint"):
            start += 1
        log.warning(
            "对话历史超过上限，丢弃最早的 %d 条（上限 %d）",
            boundary, MAX_MESSAGES,
        )
        if start > boundary:
            log.warning("为保持工具协议完整，额外丢弃截断块残余的 %d 条", start - boundary)
        return content[start:]

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
            reset_revision=int(rec.get("reset_revision") or 0),
        )

    # ---- 真契约的 5 个方法（与 `ConversationManagerPort` 一一对应）----

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        """该 umo 当前选中的有效对话 id；没有则 None，不自动创建。"""
        rec = self._selected(await self._load(), str(unified_msg_origin))
        return str(rec["cid"]) if rec is not None else None

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> Conversation | None:
        """取该来源的对话；不存在时按 create_if_not_exists 决定是否新建。"""
        umo = str(unified_msg_origin)
        if create_if_not_exists:
            def change(section, raw):
                rec = section.get(str(conversation_id))
                if not isinstance(rec, dict):
                    rec = self._create(section, umo)
                return self._to_conv(self._owned(section, umo, rec["cid"]))

            return await self._mutate(change)
        section = await self._load()
        rec = section.get(str(conversation_id))
        if not isinstance(rec, dict) or rec.get("user_id") != umo:
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
        """新建对话并原子切换当前 cid；位置参数顺序与原生契约一致。"""
        def change(section, raw):
            return self._create(section, str(unified_msg_origin), platform_id, content, title, persona_id)["cid"]

        return await self._mutate(change)

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: int | None = None,
    ) -> None:
        """在同一事务里解析当前 cid 并更新对话。"""
        umo = str(unified_msg_origin)

        def change(section, raw):
            if conversation_id:
                rec = section.get(str(conversation_id))
            else:
                rec = self._selected(section, umo)
            if not isinstance(rec, dict):
                log.debug("update_conversation 跳过：umo=%s cid=%s 不存在", umo, conversation_id)
                return
            self._owned(section, umo, rec["cid"])
            if history is not None:
                rec["content"] = self._trim(list(history))
                rec["history_revision"] = int(rec.get("history_revision", 0)) + 1
                rec["reset_revision"] = rec["history_revision"]
            if title is not None:
                rec["title"] = title
            if persona_id is not None:
                rec["persona_id"] = persona_id
            if token_usage is not None:
                rec["token_usage"] = int(token_usage)
            rec["updated_at"] = int(time.time())

        await self._mutate(change)

    async def add_message_pair(
        self,
        cid: str,
        user_message: Any,
        assistant_message: Any,
    ) -> None:
        """往 cid 原子追加一对消息；对话不存在要抛异常。"""
        def change(section, raw):
            rec = section.get(str(cid))
            if not isinstance(rec, dict):
                raise RuntimeError(f"Conversation with id {cid} not found")
            self._append_pair(rec, user_message, assistant_message)

        await self._mutate(change)

    def _append_pair(self, rec: dict[str, Any], user_message: Any, assistant_message: Any) -> None:
        content = rec.get("content")
        if content is None:
            content = []
        if not isinstance(content, list):
            raise StorageError("原生对话 content 格式错误")
        content = list(content)
        content.extend([_as_dict(user_message), _as_dict(assistant_message)])
        rec["content"] = self._trim(content)
        rec["history_revision"] = int(rec.get("history_revision", 0)) + 1
        rec["updated_at"] = int(time.time())

    async def get_or_create_conversation(
        self, umo: str, platform_id: str | None = None, persona_id: str | None = None,
    ) -> Conversation:
        def change(section, raw):
            rec = self._selected(section, str(umo))
            if rec is None:
                rec = self._create(section, str(umo), platform_id=platform_id, persona_id=persona_id)
            return self._to_conv(rec)

        return await self._mutate(change)

    def _project(self, raw: dict[str, Any], rec: dict[str, Any], session_id: str) -> None:
        sessions = raw.get(SESSIONS_SECTION, {})
        messages = raw.get(MESSAGES_SECTION, {})
        if not isinstance(sessions, dict) or not isinstance(messages, dict):
            raise StorageError("会话投影 section 格式错误")
        metadata = sessions.get(session_id)
        if not isinstance(metadata, dict):
            raise SessionNotFound(session_id)
        content = rec.get("content", [])
        if not isinstance(content, list):
            raise StorageError("原生对话 content 格式错误")
        projected = conversation_messages(content)
        previous = messages.get(session_id, [])
        if not isinstance(previous, list):
            raise StorageError("会话投影 messages 格式错误")
        common = 0
        for old, new in zip(previous, projected):
            if not isinstance(old, dict):
                raise StorageError("会话投影消息必须是对象")
            stored = Message.from_dict(old)
            if stored.to_upstream() != new.to_upstream():
                break
            new.timestamp = stored.timestamp
            common += 1
        origin, cid = str(rec["user_id"]), str(rec["cid"])
        old_origin = str(metadata.get("native_origin") or "")
        old_cid = str(metadata.get("native_conversation_id") or "")
        linked = bool(old_origin or old_cid)
        if linked and (old_origin != origin or old_cid != cid):
            raise NativeProjectionReadOnly()
        native_revision = int(rec.get("history_revision", 0))
        reset_revision = int(rec.get("reset_revision", 0))
        reset_changed = reset_revision != int(metadata.get("native_reset_revision", 0))
        content_changed = common != len(previous) or len(previous) != len(projected)
        if not linked or content_changed or native_revision != int(metadata.get("native_history_revision", 0)):
            metadata["history_revision"] = int(metadata.get("history_revision", 0)) + 1
        if not linked or common != len(previous) or reset_changed:
            metadata["summary"] = ""
            metadata["summarized_upto"] = 0
        # 🔴🔴 **把人设同步进插件能读的地方**（2026-10-31，方案 A）。
        #
        #   AstrBot 只有一套会话存储：面板改 `persona_id`、插件读同一份 ⇒ 改完即生效。
        #   我们有两套（`sessions` vs `plugin_conversations`）且**从来没打通**，
        #   于是面板绑了人设，QQ 那边完全无感。
        #
        #   陪伴插件读它的优先级（`native.py:364-415`）：
        #     ① 窗口快照 ② 插件 session settings ③ 唯一角色映射
        #     ④ `conversations.persona_id` → `resolve_selected_persona(conversation_persona_id)`
        #   ⇒ 我们写第 ④ 层**不动** ①②③，所以**不会冲掉插件已绑好的角色**；
        #      只在"还没绑"时提供兜底。这正是方案 A 安全的原因。
        #
        #   ⚠️ **必须写 name 而不是 UUID**：插件校验
        #      `native["name"] == binding.persona_id`（`multi_controller.py:244`），
        #      而 `_persona_to_v3` 也写明「`name` 是插件的主键」。
        #      写 UUID 会被它判成 `host_persona_unavailable`。
        #   ⚠️ 查不到映射时**写空串**（不猜、不保留旧值 —— 旧值可能指向已删的人设，
        #      那会让插件拿到一个不存在的角色）。
        wanted = str(metadata.get("persona_id") or "").strip()
        if wanted and self._persona_name_of is not None:
            try:
                resolved = self._persona_name_of(wanted)
            except Exception:  # noqa: BLE001 — 翻译失败不该让整个投影失败
                log.warning("人设 id → name 翻译失败（%s），本轮不写 persona_id", wanted, exc_info=True)
                resolved = None
            if resolved is not None:
                rec["persona_id"] = str(resolved or "")
        elif not wanted:
            # 面板明确清空了绑定 ⇒ 插件侧也要清（否则它会继续用一个旧角色）
            rec["persona_id"] = ""

        metadata["native_origin"] = origin
        metadata["native_conversation_id"] = cid
        metadata["native_history_revision"] = native_revision
        metadata["native_reset_revision"] = reset_revision
        metadata["message_count"] = len(projected)
        metadata["updated_at"] = time.time()
        messages[session_id] = [message.to_dict() for message in projected]
        raw[SESSIONS_SECTION] = sessions
        raw[MESSAGES_SECTION] = messages

    async def project_conversation(self, umo: str, cid: str, session_id: str) -> Conversation:
        def change(section, raw):
            rec = self._owned(section, str(umo), str(cid))
            selected = self._selected(section, str(umo))
            if selected is None or selected["cid"] != str(cid):
                raise RuntimeError(f"Selected conversation changed for {umo}")
            self._project(raw, rec, str(session_id))
            return self._to_conv(rec)

        return await self._mutate(change, projection=True)

    async def record_reply(
        self, umo: str, cid: str, session_id: str, user_message: Any, assistant_message: Any,
        *, expected_reset_revision: int | None = None,
    ) -> bool:
        def change(section, raw):
            rec = self._owned(section, str(umo), str(cid))
            if (expected_reset_revision is not None
                    and int(rec.get("reset_revision") or 0) != expected_reset_revision):
                return False
            self._append_pair(rec, user_message, assistant_message)
            self._project(raw, rec, str(session_id))
            return True

        return await self._mutate(change, projection=True)


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
