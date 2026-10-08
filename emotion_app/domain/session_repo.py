# -*- coding: utf-8 -*-
"""会话仓库：会话元数据 + 消息存储。

存储布局（`store` 的两个 section）：
  * `sessions`  —— `{session_id: 会话元数据}`（含策略、摘要、计数）
  * `messages`  —— `{session_id: [消息,...]}`

⚠️ 为什么消息单独一个 section：会话列表只需要元数据，
   不该为了列个列表就把所有会话的全文读进内存。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..infra.logging_setup import get_logger
from ..storage.base import StorageError, StoreBackend
from .context import ContextPolicy
from .message import Message

log = get_logger(__name__)

SESSIONS_SECTION = "sessions"
MESSAGES_SECTION = "messages"

#: 单会话消息上限（防御性；超出时保留最近 N 条并记 dropped）
MAX_MESSAGES_PER_SESSION = 5000


class SessionNotFound(KeyError):
    """会话不存在。"""


@dataclass(slots=True)
class Session:
    """一个会话。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = ""
    persona_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    #: 🔴 **该会话在插件世界里的"用户 id"**（= umo 的第三段 = `sender_id`）。
    #:
    #: 为什么要有它（情绪 2026-09-20 定的方案 B）：
    #:
    #: 场景「我同时和三个会话聊天，只有一个想开主动消息」。
    #: 陪伴插件把**每个用户**的数据（记忆/关系/主动消息开关）挂在
    #: `data["users"][<user_id>]` 下，而 `user_id` 来自 **`event.get_sender_id()`**
    #: （实测 `main.py:16577`），在真实现里就是 **umo 的第三段**
    #: （`sender_id` == `message_obj.sender.user_id` == `build_umo(user_id)` 那段）。
    #:
    #: ⚠️ 实测生产 `companions.json`：`users` 的**键是纯 `3412642082`**（不含冒号），
    #:    而 umo / `bound_delivery_umo` 等是**它内部的字段**。即：
    #:    **user_id 是主体，umo 是挂在它下面的投递地址**。
    #:
    #: → 所以「让每个会话有独立的主动消息开关」的正解是
    #:   **让每个会话有自己的 `user_id`**（方案 B），而不是所有会话共用一个 `"default"`。
    #:
    #: ⚠️ 留空 = 回落到全局配置（`plugins.umo_user_id`）——
    #:    单会话/未配置时行为与以前**完全一致**（向后兼容）。
    user_id: str = ""

    #: 上下文策略（每个会话可独立设）
    policy: ContextPolicy = field(default_factory=ContextPolicy)

    #: 摘要（rolling/semantic 模式用）
    summary: str = ""

    #: 已被总结进 summary 的消息条数（游标，防重复总结）
    summarized_upto: int = 0

    #: 消息计数（避免为了显示条数而读全文）
    message_count: int = 0

    #: 🔴 **未读消息数**（情绪 2026-09-26 要的「聊天旁边那个圆圈 3」）。
    #:
    #: 语义（**只有一种来源会 +1**，这一点是刻意的）：
    #:   * **插件主动消息**（`SessionDelivery`）落进某个会话 → +1。
    #:     那是"用户不在场时进来的东西"，也正是这个提示存在的理由；
    #:   * 用户自己发起的聊天（`ChatService.record`）→ **不 +1**。
    #:     他此刻就在这个页面上看着，把它算成"未读"是假阳性。
    #:     ⚠️ 服务端**无法知道**用户当前打开的是哪个会话（那是纯前端状态），
    #:        所以判据只能落在"这条消息是不是用户自己触发的"上 ——
    #:        宁可少报（用户自己聊的不提示），也不要多报（每次聊天都蹦一个红点）。
    #:
    #: 清零路径（唯一）：面板打开该会话时调 `mark_read()`。
    unread: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "persona_id": self.persona_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "policy": self.policy.to_dict(),
            "summary": self.summary,
            "summarized_upto": self.summarized_upto,
            "message_count": self.message_count,
            "unread": self.unread,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        return cls(
            id=d.get("id") or uuid.uuid4().hex,
            title=str(d.get("title") or ""),
            persona_id=str(d.get("persona_id") or ""),
            # ⚠️ 旧数据没有这个字段 → 空串 = 回落全局（向后兼容，见字段注释）
            user_id=str(d.get("user_id") or ""),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
            policy=ContextPolicy.from_dict(d.get("policy") or {}),
            summary=str(d.get("summary") or ""),
            summarized_upto=int(d.get("summarized_upto", 0)),
            message_count=int(d.get("message_count", 0)),
            # ⚠️ 旧数据没有这个字段 → 0（"没有未读"，不是"不知道"）：
            #    存量会话本来就没有"用户没看过的主动消息"这个概念，
            #    给它们补一个非 0 的初值反而会在面板上凭空冒红点。
            unread=max(0, int(d.get("unread", 0) or 0)),
        )


#: 插件用户 id 里**不允许**出现的字符 —— 冒号是 umo 的分隔符，空白会造出
#: "看着一样、实际匹配不上"的键（`data["users"]` 的键相等比较是逐字节的）。
_USER_ID_MAX = 120


def _normalize_user_id(raw: Any, *, session_id: str = "") -> str:
    """规整插件用户 id（方案 B）。**只 trim + 查非法字符，不改内容**。

    🔴 为什么必须在这里做（而不是只靠前端 trim）：
       这个值是插件 `data["users"]` 的**键**，相等比较是**逐字节**的。
       尾随一个空格 → 看着一样、实际永远匹配不上 →
       主动消息"找不到用户"，而**日志里两串看起来完全相同**，极难排查。
       前端 trim 挡不住 API 直调/脚本/表单自动填充。

    ⚠️ **冒号直接拒绝**（不是替换）：`umo` 是 `A:B:C` 三段，
       用户 id 里带冒号会让 `parse_umo` 把第三段**截断**
       （`split(":", 2)`）→ 又是"看起来对、实际错位"。

    ⚠️ 空串是**合法**的：语义是"跟随全局设置"（向后兼容），不是错误。
    """
    uid = str(raw or "").strip()
    if not uid:
        return ""
    if ":" in uid:
        raise ValueError(
            f"插件用户 ID 不能含冒号（会破坏 umo 的 A:B:C 结构）：{uid!r}"
        )
    if len(uid) > _USER_ID_MAX:
        raise ValueError(f"插件用户 ID 过长（>{_USER_ID_MAX}）：{uid[:40]!r}…")
    if any(c.isspace() for c in uid):
        raise ValueError(f"插件用户 ID 不能含空白字符：{uid!r}")
    return uid


def _new_umo_user_id() -> str:
    """给**新会话**生成一个独有的插件用户 id（= umo 的第三段）。

    🔴 为什么每个会话都要有自己的（情绪 2026-09-26 的原话）：
        「每个会话都带一个唯一标签 umo，这样我就能自己到陪伴那边
          添加"用户"和测试主动消息等东西了，**陪伴插件不就是靠 umo 去识别用户的嘛**」

       陪伴把**每个用户**的数据（记忆/关系/主动消息开关）挂在
       `data["users"][<这个值>]` 下 —— 所以"一个会话一个 umo"就等于
       "陪伴面板里一个会话一个用户"，用户可以直接按这个 umo 去建档、开主动消息。

    ⚠️ **取值必须挑过，不能随便**（三条都是插件源码实测出来的）：

    ① **不能是全数字** —— `page_api.py:17719` 用 `user_id_text.isdigit()`
       判"是不是 QQ 用户"，命中就走 onebot 那套（`platform_kind="onebot"`）。
       我们是 webchat，冒充 QQ 会让插件的能力判定走错分支。

    ② **不能含冒号 / 空白** —— umo 是 `A:B:C` 三段，第三段带冒号会被
       `split(":", 2)` 语义之外的解析截断（见 `_normalize_user_id`）。

    ③ **不能出现 `scheduler`** —— 插件 `tts_tool_sanitizer.py:69` 会把
       `sender_name == "scheduler"` 当成"定时任务伪事件"（静默降级、文本可能发两次）；
       `host/astrbot_host/umo.py` 的 `build_inbound_event` 也会**当场抛错**。
       我们这里的随机段是十六进制，理论上撞不上，但"理论上"不是判据。

    形如 `sess-a1b2c3d4e5f6`（12 位十六进制 = 48 bit）。
    ⚠️ 用**随机**而不是"会话 id 前 12 位"：会话 id 本身也是随机十六进制，
       但两者同源会让"用户 id 反推会话 id"变成可能，且将来会话 id 规则一变
       就会**集体换库**（插件侧数据全对不上）。随机值与它解耦。
    """
    return f"sess-{uuid.uuid4().hex[:12]}"


class SessionRepository:
    """会话与其消息的读写。"""

    def __init__(self, store: StoreBackend) -> None:
        self._store = store

    # ---- 会话 ----

    async def _load_sessions(self) -> dict[str, Session]:
        raw = await self._store.load_sections([SESSIONS_SECTION])
        section = raw.get(SESSIONS_SECTION) or {}
        if not isinstance(section, dict):
            raise StorageError(f"{SESSIONS_SECTION} section 格式错误")
        return {sid: Session.from_dict(d) for sid, d in section.items() if isinstance(d, dict)}

    async def _save_sessions(self, sessions: dict[str, Session]) -> None:
        await self._store.save_sections(
            {SESSIONS_SECTION: {sid: s.to_dict() for sid, s in sessions.items()}}
        )

    async def list(self) -> list[Session]:
        items = list((await self._load_sessions()).values())
        return sorted(items, key=lambda s: s.updated_at, reverse=True)

    async def get(self, session_id: str) -> Session:
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)
        return sessions[session_id]

    async def exists(self, session_id: str) -> bool:
        return session_id in (await self._load_sessions())

    async def create(
        self,
        *,
        title: str = "",
        persona_id: str = "",
        user_id: str = "",
        policy: ContextPolicy | None = None,
        session_id: str = "",
    ) -> Session:
        """新建会话。

        🔴 **`user_id` 留空 = 自动分配一个该会话专属的 umo 标签**
           （`sess-xxxxxxxxxxxx`，见 `_new_umo_user_id` 的三条取值约束）。

        为什么默认自动分配（情绪 2026-09-26「每个会话都带一个唯一标签 umo」）：
          留空时老行为是"所有会话共用全局 `plugins.umo_user_id`（默认 `default`）"
          —— 三个会话在插件世界里是**同一个用户**，记忆/关系/主动消息开关
          全是同一份，既不能单独开主动消息，也没法在陪伴面板里按会话建档。

        ⚠️ **存量会话不会被自动改写**（只有 `create` 走自动分配）：
           把一条已经和插件用户绑定的会话悄悄换成新 umo，会让插件那边的
           记忆/关系**当场对不上**（等于静默换库）。存量会话要换标签，
           由用户在面板上显式点「生成专用 umo」——那是一个可见的动作。
        """
        sessions = await self._load_sessions()
        uid = _normalize_user_id(user_id, session_id=session_id)
        if not uid:
            # 走与 `new_umo_user_id()` **同一份**规则（唯一性检查也在那里）
            uid = await self.new_umo_user_id()
        s = Session(
            id=session_id or uuid.uuid4().hex,
            title=title or "新会话",
            persona_id=persona_id,
            user_id=uid,
            policy=policy or ContextPolicy(),
        )
        s.policy.validate()
        sessions[s.id] = s
        await self._save_sessions(sessions)
        log.info("会话已创建", extra={"data": {"id": s.id, "user_id": s.user_id}})
        return s

    async def new_umo_user_id(self) -> str:
        """生成一个**没被任何会话占用**的新 umo 标签（面板「换一个」按钮用）。

        ⚠️ 与 `create()` 里的自动分配是**同一份规则**（`_new_umo_user_id`）——
           两处各写一套的话，将来约束一变就会漂移：面板生成的值可能违反
           某条约束，而插件那边**静默**按别的用户处理。
        """
        sessions = await self._load_sessions()
        taken = {s.user_id for s in sessions.values()}
        for _ in range(8):
            candidate = _new_umo_user_id()
            if candidate not in taken:
                return candidate
        raise StorageError(
            "生成会话 umo 标签失败（8 次都撞上已有值）—— 随机源异常，已中止"
        )

    async def by_user_id(self, user_id: str) -> Session | None:
        """按**插件用户 id** 找会话（方案 B：umo 第三段 → 会话）。

        🔴 用途：插件的主动消息带的是 **umo**，而 umo 的第三段就是
           `user_id`（`build_umo(user_id)`）。收到主动消息时，
           要靠它找到**该落到哪个会话** —— 而不是另造一套映射。

        ⚠️ 多个会话绑了同一个 user_id 时返回**创建最早的那个**，
           并记 WARNING：那说明配置有歧义，用户应当自己改掉其中一个。

        ⚠️ **不能用 `list()` 的顺序**（2026-09-20 实测踩到）：
           `list()` 按 `updated_at` **倒序**（最近用过的在前）——
           直接取第一个会拿到**最近**改过的会话，与"最早"的语义
           以及日志文案**都不符**（判据当场抓到了这个不一致）。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return None
        matches = [s for s in (await self._load_sessions()).values() if s.user_id == uid]
        if not matches:
            return None
        # 明确的"最早"：按 created_at，再按 id 兜底（同一毫秒内保证确定性）
        earliest = sorted(matches, key=lambda s: (s.created_at, s.id))[0]
        if len(matches) > 1:
            log.warning(
                "有 %d 个会话绑了同一个 user_id=%r，主动消息只会落到最早的那个（%s）",
                len(matches),
                uid,
                earliest.id,
            )
        return earliest

    async def ensure(self, session_id: str, **kwargs: Any) -> Session:
        """取会话；不存在就建（聊天时用，省一次往返）。"""
        if await self.exists(session_id):
            return await self.get(session_id)
        return await self.create(session_id=session_id, **kwargs)

    async def update(self, session_id: str, patch: dict[str, Any]) -> Session:
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)

        s = sessions[session_id]
        allowed = {"title", "persona_id", "user_id", "summary", "summarized_upto"}
        for k, v in patch.items():
            if k == "policy":
                if not isinstance(v, dict):
                    raise ValueError("policy 必须是对象")
                s.policy = ContextPolicy.from_dict(v)
                s.policy.validate()
            elif k in allowed:
                if k == "user_id":
                    v = _normalize_user_id(v, session_id=session_id)
                setattr(s, k, v if not isinstance(v, str) else str(v))

        s.updated_at = time.time()
        sessions[session_id] = s
        await self._save_sessions(sessions)
        return s

    async def delete(self, session_id: str) -> None:
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)
        sessions.pop(session_id)

        msgs = await self._load_messages()
        msgs.pop(session_id, None)

        await self._store.save_sections(
            {
                SESSIONS_SECTION: {sid: s.to_dict() for sid, s in sessions.items()},
                MESSAGES_SECTION: msgs,
            }
        )
        log.info("会话已删除", extra={"data": {"id": session_id}})

    async def clear_messages(self, session_id: str) -> None:
        """清空某会话的消息，但保留会话本身（含策略与当前人设）。"""
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)

        msgs = await self._load_messages()
        msgs[session_id] = []

        s = sessions[session_id]
        s.message_count = 0
        s.summary = ""
        s.summarized_upto = 0
        s.updated_at = time.time()

        await self._store.save_sections(
            {
                SESSIONS_SECTION: {sid: x.to_dict() for sid, x in sessions.items()},
                MESSAGES_SECTION: msgs,
            }
        )

    # ---- 消息 ----

    async def _load_messages(self) -> dict[str, list[dict[str, Any]]]:
        raw = await self._store.load_sections([MESSAGES_SECTION])
        section = raw.get(MESSAGES_SECTION) or {}
        if not isinstance(section, dict):
            raise StorageError(f"{MESSAGES_SECTION} section 格式错误")
        return {
            sid: [m for m in arr if isinstance(m, dict)]
            for sid, arr in section.items()
            if isinstance(arr, list)
        }

    async def messages(self, session_id: str) -> list[Message]:
        """取会话消息。

        🔴 **会话不存在时抛 SessionNotFound，而不是返回空列表**。
           实测踩到：最初直接 `store.get(sid, [])` → 不存在的会话静默返回 `[]`，
           调用方无法区分"会话空"与"会话不存在"，会把 bug 藏起来。
           （判据：本项目铁律 —— 静默返回空数据比报错更贵。）
        """
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)
        store = await self._load_messages()
        return [Message.from_dict(d) for d in store.get(session_id, [])]

    async def append(
        self,
        session_id: str,
        new_messages: list[Message],
        *,
        unread_delta: int = 0,
    ) -> Session:
        """追加消息并更新会话元数据（一次写入，避免两次持久化）。

        Args:
            unread_delta: 未读数增量。**只允许 +1 或 0**（见 `Session.unread`
                的字段注释：只有"插件主动消息"那条路会 +1）。
                ⚠️ 做一个**夹取**而不是原样加：传负数会把计数压成负数，
                那是个"看着像 0、其实是坏的"状态。要清零走 `mark_read()`。
        """
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)

        store = await self._load_messages()
        arr = store.setdefault(session_id, [])
        arr.extend(m.to_dict() for m in new_messages)

        dropped = 0
        if len(arr) > MAX_MESSAGES_PER_SESSION:
            dropped = len(arr) - MAX_MESSAGES_PER_SESSION
            del arr[:dropped]
            log.warning(
                "会话消息超上限，已丢弃最旧的",
                extra={"data": {"session": session_id, "dropped": dropped}},
            )

        s = sessions[session_id]
        s.message_count = len(arr)
        s.updated_at = time.time()
        # 首条用户消息自动做标题（省得用户手填）
        if not s.title or s.title == "新会话":
            for m in new_messages:
                if m.role == "user" and m.content.strip():
                    s.title = m.content.strip()[:30]
                    break

        # 游标要跟着丢弃量平移，否则总结会错位
        if dropped:
            s.summarized_upto = max(0, s.summarized_upto - dropped)

        if unread_delta > 0:
            s.unread += int(unread_delta)

        await self._store.save_sections(
            {
                SESSIONS_SECTION: {sid: x.to_dict() for sid, x in sessions.items()},
                MESSAGES_SECTION: store,
            }
        )
        return s

    async def mark_read(self, session_id: str) -> Session:
        """把某会话的未读数清零（面板真正**打开**这个会话时调）。

        🔴 为什么"清零"必须是**显式**一个方法，而不是 `update({"unread": 0})`：
           `update()` 的字段白名单是"用户能编辑的东西"，
           而未读数是**服务端状态**（由投递/打开这两条路径维护）。
           混进白名单就等于让前端可以随便把它设成任意值 —— 那时候
           `unread` 就不再是"有多少条没看"，而只是一个前端数字。
        """
        sessions = await self._load_sessions()
        if session_id not in sessions:
            raise SessionNotFound(session_id)
        s = sessions[session_id]
        if s.unread:
            s.unread = 0
            await self._save_sessions(sessions)
        return s

    async def unread_total(self) -> int:
        """全部会话的未读总数（面板侧边栏那个圆圈的数字）。"""
        return sum(s.unread for s in (await self._load_sessions()).values())
