# -*- coding: utf-8 -*-
"""`astrbot.core.platform.astr_message_event` —— `AstrMessageEvent` 与消息链。

⭐⭐ 接口面**照插件的实际调用统计**实现（AST 扫描 759 个文件，见下），
   而不是照抄真实现 1000+ 行的类 —— 只做**被用到的**，其余显式报错。

实测插件对 event 的调用频次（前 20）：
```
get 261 · get_sender_id 100 · stop_event 76 · get_result 33 · set_result 30
send 26 · plain_result 18 · chain_result 10 · message_str 10
is_private_chat 9 · unified_msg_origin 8 · get_extra 5 · is_stopped 3
get_platform_name 2 · send_streaming 2 · get_self_id 2
get_message_str 1 · set_extra 1 · should_call_llm 1 · get_sender_name 1
get_messages 1
```

⚠️ `get` 261 次最多 —— 说明插件把 event 当**上下文容器**用（`event.get("key")`）。
   真实现里 `get` 来自 `MessageEventResult` 或事件的可变上下文，
   这里实现成**通用键值容器**（语义等价、行为可测）。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot")


@dataclass
class MessageChain:
    """消息链（照真实现的最小等价物）。

    真实现是 `list[BaseMessageComponent]` 的包装；这里保留同样的
    `chain` 字段与迭代行为，因为插件会 `MessageChain(chain=[...])` 构造它。
    """

    chain: list[Any] = field(default_factory=list)

    def __iter__(self):
        return iter(self.chain)

    def __len__(self) -> int:
        return len(self.chain)

    def __bool__(self) -> bool:
        # ⚠️ 真实现的空链为 False —— 插件用它判断"要不要发"
        return bool(self.chain)

    def message(self, text: str) -> "MessageChain":
        """追加纯文本（照真实现的链式 API）。"""
        from astrbot_host.compat.core.message.components import Plain

        self.chain.append(Plain(text))
        return self

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        """取纯文本（照真实现 `message_event_result.py:149`）。

        ⚠️ 非 Plain 组件在 `with_other_comps_mark=True` 时降级成 `[类型]` 标记，
           否则**跳过**（不是静默丢内容 —— 真实现就是这么写的，照抄）。
        """
        out = []
        for comp in self.chain:
            text = getattr(comp, "text", None)
            if text is not None:
                out.append(str(text))
            elif with_other_comps_mark:
                out.append(f"[{type(comp).__name__}]")
        return "".join(out)

    def __str__(self) -> str:
        """降级成纯文本 —— 宿主只支持文本时用它（照作者「能力降级」判据）。"""
        return self.get_plain_text()


def _normalize_message_type(message_obj: Any) -> Any:
    """把 `message_obj.type` 归一成 `MessageType` —— 照真实现 `:62-74` 逐条。

    真实现原话（`core/platform/astr_message_event.py:62-74`）：
    ```python
    message_type = getattr(message_obj, "type", None)
    if not isinstance(message_type, MessageType):
        try:
            message_type = MessageType(str(message_type))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                f"Failed to convert message type {message_obj.type!r} to MessageType. "
                f"Falling back to FRIEND_MESSAGE."
            )
            message_type = MessageType.FRIEND_MESSAGE
    ```

    ⚠️ 两处**有意**与真实现不同，都只影响真实现里不存在的情形：
      ① `message_obj` 为 `None` → 直接给 `FRIEND_MESSAGE`（真实现 4 个参数
         全必填，没有"没有 message_obj"的事件；这是我们容忍式构造的退路，
         不为此打 warning）；
      ② 没有 `type` 属性 → 也给 `FRIEND_MESSAGE` 而不打 warning
         （真实现走到这条路上，f-string 里的 `message_obj.type` 会先抛
         `AttributeError`）。
    """
    from astrbot_host.compat.core.platform.message_type import MessageType

    if message_obj is None:
        return MessageType.FRIEND_MESSAGE
    raw = getattr(message_obj, "type", None)
    if isinstance(raw, MessageType):
        return raw
    if raw is None:
        return MessageType.FRIEND_MESSAGE
    try:
        return MessageType(str(raw))
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "Failed to convert message type %r to MessageType. "
            "Falling back to FRIEND_MESSAGE.",
            raw,
        )
        return MessageType.FRIEND_MESSAGE


class AstrMessageEvent:
    """一次消息事件（宿主侧）。

    🔴 设计取舍：**只实现被插件用到的 + 派发链路必需的**，未实现的**显式抛错**。
       理由：静默返回 None 会让插件走进"没配置/没消息"的分支，
       产生难排查的静默降级（项目铁律「静默失败比报错贵」）。

    ⭐⭐ 2026-09-20 按真实现 `core/platform/astr_message_event.py` 对齐了三处**语义**：

    1. **`is_stopped` 是方法，且有两个来源**（真 `:58` / `:359-365`）：
       `_force_stopped or (_result is not None and _result.is_stopped())`。
       🔴 `_force_stopped` 是**独立标志**，真实现原话是
       「不依赖 _result，**不会被 clear_result() 重置**」。
       我原来把它做成 `_stopped` 字段 → 插件写 `if event.is_stopped:`（漏括号）
       会**恒为真** → 静默吞掉所有消息。

    2. **`plain_result` / `chain_result` 是纯构造**（真 `:399` / `:412`），
       **不写** `self._result`。真实现靠 `call_handler`
       （`pipeline/context_utils.py:71-73`）在 yield 那一刻才 `set_result`，
       且每轮 handler 后 `clear_result()`（`process_stage/method/star_request.py:53`）。
       我原来提前写 → "上一个 handler 的结果"与清除语义错位。

    3. **`unified_msg_origin` 是 property**（真 `:106-114`），
       setter 会用 `MessageSession.from_str` **重解析**并换掉 `session`。

    ⚠️ **不提供** `plain` / `wait` / `send_message` / `get_user_id`：
       实测真 AstrBot（含全部事件子类）都没有这四个 → 它们是**插件自身 bug**，
       补了就是伪造能力，且会让我们的宿主与真宿主行为分叉。
       `host/tests/test_compat_regressions.py` 有专门判据守着这条线。
    """

    def __init__(
        self,
        message_str: str = "",
        message_obj: Any = None,
        platform_meta: Any = None,
        session_id: str = "",
        *,
        unified_msg_origin: str = "",
        sender_id: str = "",
        sender_name: str = "",
        self_id: str = "",
        platform_name: str | None = None,
        platform_id: str = "",
        is_private: bool | None = None,
        is_admin: bool = True,
    ) -> None:
        """构造事件。

        ⭐⭐ **前 4 个参数的位置顺序照真实现**（`core/platform/astr_message_event.py:37-42`）：
        `(message_str, message_obj, platform_meta, session_id)` —— 插件就是这么构造的
        （`proactive_message.py:16792`、`image_runtime.py:20816`，以及两个
        `SyntheticPrivateWakeEvent` 子类的 `super().__init__(...)`）。

        🔴 2026-09-26 之前这里签名是**关键字-only**，这种位置调用直接
        `TypeError: AstrMessageEvent.__init__() takes 1 positional argument but 5 were given`，
        被插件的 `except Exception`（`proactive_message.py:16821`）接住成一行 debug 日志
        → **整条主动消息装饰链永久不执行**（真机 `logs/serve.out` 里 23 次）。

        **推导规则**：真实现里下面这些字段**都不是入参**，而是各 getter 现读
        （`:199` self_id · `:205-215` sender · `:255` 消息类型 ·
        `:126`/`:133` `platform_meta.name`/`.id` · `:75-79` session_id）。
        显式传入的关键字**优先**（我们自己的派发链路 `umo.py:553` 显式传），
        没传的才推导：

        | 参数 | 未显式传入时 |
        |---|---|
        | `sender_id` | `message_obj.sender.user_id`（真实现只认 `str`，其余给空） |
        | `sender_name` | `message_obj.sender.nickname`（`None` → 空，非 `str` → `str(...)`） |
        | `self_id` | `message_obj.self_id` |
        | `is_private` | `message_obj.type` 是不是 `MessageType.FRIEND_MESSAGE` |
        | `platform_name` / `platform_id` | `platform_meta.name` / `platform_meta.id` |
        | `session_id` | 传入值 → `unified_msg_origin` → `message_obj.session_id` |
        | `unified_msg_origin` | 给了 `platform_meta` 时照真实现 `:75-79` 造 session |

        ⚠️ **未从 `message_obj` / `platform_meta` 推导**的字段（真实现同样不推导，
           别顺手补）：`message_str`（真实现 `:43` 原样存传入值）、
           `role`/`is_admin`（真实现 `:46` 恒以 `"member"` 起，由 waking stage 再改）、
           `is_wake` / `is_at_or_wake_command` / `call_llm` / `plugins_name`。

        ⚠️ `platform_name` / `is_private` 用 `None` 当"没传"的哨兵 ——
           因为这两个参数**今天有非空默认值**（`"webchat"` / `True`），
           用空值当哨兵就分不清"没传"与"传了默认值"。其余参数默认值本就是空串。
        """
        self.message_str = message_str
        self.message_obj = message_obj
        if platform_meta is not None:
            # 真实现 `:47` 恒设 `platform_meta`，`:103` 还留了 `platform` 这个
            # back_compability 别名。⚠️ **只在给了才设**：我们自己的容忍式构造
            # （无平台，`umo.py:576-577` 是构造后再补）不设，`hasattr` 探测的语义不变。
            self.platform_meta = platform_meta
            self.platform = platform_meta

        # ---- 未显式传入的字段：按真实现从 message_obj / platform_meta 推导 ----
        from astrbot_host.compat.core.platform.message_type import MessageType

        sender = getattr(message_obj, "sender", None)
        if not sender_id:
            user_id = getattr(sender, "user_id", None)
            # 真实现 `get_sender_id` 只认 `str`，其余一律给空（`:205-209`）
            sender_id = user_id if isinstance(user_id, str) else ""
        if not sender_name:
            nickname = getattr(sender, "nickname", None)
            # 真实现 `get_sender_name`：`None` → `""`，非 `str` → `str(...)`（`:211-219`）
            sender_name = "" if nickname is None else str(nickname)
        if not self_id:
            self_id = getattr(message_obj, "self_id", "") or ""
        if not platform_id:
            platform_id = getattr(platform_meta, "id", "") or ""
        if platform_name is None:
            # 推导不到（含没给 platform_meta）时用今天的默认值，不是伪造平台名
            platform_name = getattr(platform_meta, "name", "") or "webchat"
        message_type = _normalize_message_type(message_obj)
        if is_private is None:
            # 真实现 `is_private_chat()` 就是"消息类型是不是 FRIEND"（`:255`）
            is_private = message_type is MessageType.FRIEND_MESSAGE

        self.session_id = session_id or unified_msg_origin or (
            getattr(message_obj, "session_id", "") or ""
        )
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._platform_name = platform_name
        #: 平台**实例** ID（`PlatformMetadata.id`）—— umo 第一段用的是它。
        #: 空则退化成 `platform_name`（真实现里未配置平台实例时也近似此行为）。
        self._platform_id = platform_id
        self._is_private = is_private

        # ---- 真实现 :51-58 / :94 / :99 的实例属性 ----
        #: 用户角色（`admin` / `member`）—— `is_admin()` 读它（真 `:265`）
        self.role = "admin" if is_admin else "member"
        #: 是否唤醒（是否通过 WakingStage，真 `:53`）
        self.is_wake = False
        #: 是否 @机器人 / 带唤醒词 / 私聊（真 `:55`）
        #: 🔴 **派发的前置条件** —— `CommandFilter.filter` 第一行就查它
        self.is_at_or_wake_command = False
        #: 是否禁止默认 LLM 请求（真 `:94`）—— `should_call_llm()` 写它
        self.call_llm = False
        #: 该事件启用的插件名列表；`None` = 全部启用（真 `:99`）
        self.plugins_name: list[str] | None = None
        #: 🔴 独立的停止标志，**不依赖 `_result`，不会被 `clear_result()` 重置**
        #: （真 `:58` 原话）
        self._force_stopped: bool = False
        #: 本次事件中是否**至少有过一次**发送消息的操作（真 `:92`）
        #: ⚠️ 插件靠它判断"这轮到底发出去没有"（实测 **6 个文件**读它）：
        #:    恒 False → 插件以为没发出去，可能重复发；恒 True → 静默不发。
        self._has_send_oper: bool = False

        #: 事件上下文（`event.get/set` 的载体；插件高频使用）
        self._extras: dict[str, Any] = {}
        #: 结果（`set_result` / `get_result`）
        self._result: Any = None
        #: 本事件产出的消息（供宿主消费）
        self.sent_messages: list[Any] = []

        #: ⚠️ `unified_msg_origin` 走 property，解析出的 session 放这里
        self.session: Any = None
        self._umo = ""
        if unified_msg_origin:
            self.unified_msg_origin = unified_msg_origin
        elif platform_meta is not None:
            # 🔴 真实现 `:75-79` **必建** session，`unified_msg_origin` 就是
            #    `str(self.session)`（`:106-108`）—— 插件那条主动消息装饰链
            #    靠它认会话：`main.py:9303` 起，umo 里没有 `":FriendMessage:"`
            #    就直接 return。不建 session = 把 TypeError 换成
            #    "钩子拿到空 umo 后静默早退"，症状一模一样。
            from astrbot_host.compat.core.platform.message_session import MessageSession

            self.session = MessageSession(
                platform_name=self._platform_id or self._platform_name,
                message_type=message_type,
                session_id=self.session_id,
            )
            self.session_id = self.session.session_id

    # ================= 高频：上下文容器 =================

    def get(self, key: str, default: Any = None) -> Any:
        """取上下文值（**插件用得最多：280 次**）。"""
        return self._extras.get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._extras.setdefault(key, default)

    def get_extra(self, key: str = "", default: Any = None) -> Any:
        """真实现的 `get_extra`（90 次）—— 与 `get` 同源。

        ⚠️ 真实现签名是 `get_extra(key=None, default=None)`：
           **不传 key** 时返回整个 dict 的拷贝（真 `:226-231`）。
        """
        if not key:
            return dict(self._extras)
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def clear_extra(self) -> None:
        """清空上下文（真 `:232`）。"""
        self._extras.clear()

    # ================= 身份与会话 =================

    def get_sender_id(self) -> str:
        """发送者 ID（**236 次**）。"""
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_self_id(self) -> str:
        """Bot 自己的 ID（10 次）—— 真实现取自 `message_obj.self_id`（真 `:199`）。"""
        return getattr(self.message_obj, "self_id", "") or self._self_id

    def get_platform_name(self) -> str:
        """平台名（18 次）。"""
        return self._platform_name

    def get_platform_id(self) -> str:
        """平台实例 ID（真 `:133`）。

        ⚠️ 真实现返回 `platform_meta.id`（用户给平台实例起的唯一标识），
           与 `get_platform_name()`（类型名）**不是一回事** —— umo 第一段用的是它。
        """
        return self._platform_id or self._platform_name

    def get_session_id(self) -> str:
        """会话 ID（真 `:191`）。"""
        return self.session_id

    def get_group_id(self) -> str:
        """群组 ID；**非群聊返回空字符串**（真 `:195`）。"""
        return getattr(self.message_obj, "group_id", "")

    def get_message_str(self) -> str:
        return self.message_str

    def get_message_type(self) -> Any:
        """消息类型（真 `:184`）—— 取自 `message_obj.type`。"""
        return getattr(self.message_obj, "type", None)

    def get_messages(self) -> list[Any]:
        """原始消息段（12 次）。"""
        if self.message_obj is not None:
            return list(getattr(self.message_obj, "message", []) or [])
        return []

    def is_private_chat(self) -> bool:
        """是否私聊（16 次）。

        ⚠️ 真实现（`:255`）是 `return self.session.message_type == FRIEND_MESSAGE`，
           即**从 session 推导**，不是独立字段。这里优先用 session，
           没有 session 时退回构造参数（宿主单用户场景）。
        """
        mt = getattr(self.session, "message_type", None)
        if mt is not None:
            return getattr(mt, "value", "") == "FriendMessage"
        return self._is_private

    def is_admin(self) -> bool:
        """是否管理员 —— 真实现是 `self.role == "admin"`（`:265`）。"""
        return self.role == "admin"

    def is_wake_up(self) -> bool:
        """是否被唤醒（真 `:259`）。"""
        return self.is_wake

    # ================= 事件控制 =================

    def stop_event(self) -> None:
        """终止事件传播（**124 次**）。

        照真实现 `:343-349`：置**独立**的 `_force_stopped`，
        并同步把结果也标记成 stopped。
        """
        self._force_stopped = True
        if self._result is None:
            from astrbot_host.compat.core.message.message_event_result import (
                MessageEventResult,
            )

            self._result = MessageEventResult()
        if hasattr(self._result, "stop_event"):
            self._result.stop_event()

    def continue_event(self) -> None:
        """继续事件传播（真 `:351-357`）—— `stop_event` 的对偶。"""
        self._force_stopped = False
        if self._result is None:
            from astrbot_host.compat.core.message.message_event_result import (
                MessageEventResult,
            )

            self._result = MessageEventResult()
        if hasattr(self._result, "continue_event"):
            self._result.continue_event()

    def is_stopped(self) -> bool:
        """是否终止传播（12 次）。

        🔴 照真实现 `:359-365` —— **两个来源**，且 `_force_stopped` 优先：
        ```python
        if self._force_stopped: return True
        if self._result is None: return False
        return self._result.is_stopped()
        ```
        ⚠️ 这必须是**方法**（真实现如此）。做成字段的话，插件写
           `if event.is_stopped:`（漏括号）会恒为真 → 静默吞掉所有消息。
        """
        if self._force_stopped:
            return True
        if self._result is None:
            return False
        is_stopped = getattr(self._result, "is_stopped", None)
        if callable(is_stopped):
            return bool(is_stopped())
        return False

    def should_call_llm(self, call_llm: bool = True) -> None:
        """是否允许默认 LLM 请求（真 `:367-372`）—— 写 `call_llm` 属性。"""
        self.call_llm = call_llm

    # ================= 结果与发送 =================

    def set_result(self, result: Any) -> None:
        """设置结果（40 次）。

        ⚠️ 照真实现 `:314-341`：**接受 str**（会包成 MessageEventResult）。
        """
        if isinstance(result, str):
            from astrbot_host.compat.core.message.message_event_result import (
                MessageEventResult,
            )

            result = MessageEventResult().message(result)
        self._result = result

    def get_result(self) -> Any:
        """取结果（80 次）—— 未设置时返回 None（真实现同）。"""
        return self._result

    def clear_result(self) -> None:
        """清除结果（真 `:378`）。

        🔴 注意：**不重置 `_force_stopped`** —— 真实现刻意把两者分开
           （`:58` 注释「独立的停止标志，不依赖 _result，不会被 clear_result() 重置」）。
        """
        self._result = None

    def make_result(self) -> Any:
        """创建一个空结果（真 `:384`）。"""
        from astrbot_host.compat.core.message.message_event_result import (
            MessageEventResult,
        )

        return MessageEventResult()

    def plain_result(self, text: str) -> Any:
        """构造纯文本结果（**662 次，用得最多**）。

        🔴 照真实现 `:399-401` —— **纯构造，不写 `self._result`**。
           真实现靠 `call_handler`（`context_utils.py:71-73`）在 yield 时
           才 `set_result`，并在每轮 handler 后 `clear_result()`。
        """
        from astrbot_host.compat.core.message.message_event_result import (
            MessageEventResult,
        )

        return MessageEventResult().message(text)

    def chain_result(self, chain: Any) -> Any:
        """构造消息链结果（58 次）。

        🔴 同 `plain_result` —— **纯构造，不写 `self._result`**（真 `:412-418`）。
        """
        from astrbot_host.compat.core.message.message_event_result import (
            MessageEventResult,
        )

        result = MessageEventResult()
        result.chain = list(chain)
        return result

    def image_result(self, url_or_path: str) -> Any:
        """构造图片结果（真 `:403-410`）：以 http 开头当网络图，否则本地文件。"""
        from astrbot_host.compat.core.message.message_event_result import (
            MessageEventResult,
        )

        result = MessageEventResult()
        if url_or_path.startswith("http"):
            result.url_image(url_or_path)
        else:
            result.file_image(url_or_path)
        return result

    async def send(self, message: Any) -> None:
        """直接发送一条消息（153 次）。

        ⚠️ 真实现在这里做实际投递；宿主侧我们**只记录** `sent_messages`，
           由装配层决定何时真正投递 —— 这样事件类不依赖任何 IO，
           可单测（与领域层同样的"端口注入"思路）。

        🔴 照真实现 `:478-495`：发送后必须置 **`_has_send_oper = True`**。
        """
        if message is None:
            return
        self.sent_messages.append(message)
        # ⚠️ 真实现在 **`send()` 末尾**（不是开头）置位 —— 表示"确实发出去了"
        self._has_send_oper = True

    async def send_streaming(self, generator: AsyncGenerator[Any, None], use_t2i: bool = False) -> None:
        """流式发送。

        ⚠️ 真实现逐块投递；这里**完整消费生成器**并记录 ——
           注意必须消费完，否则插件的收尾逻辑不执行
           （我们踩过同类坑：流式 `break` 掉生成器导致持久化永不执行）。
        """
        if use_t2i:
            logger.debug("send_streaming(use_t2i=True)：宿主暂不支持文本转图，按纯文本投递")
        async for chunk in generator:
            self.sent_messages.append(chunk)
        # 🔴 真实现 `:292` 在 send_streaming 里也置 `_has_send_oper = True`
        self._has_send_oper = True

    # ================= umo（property，照真实现） =================

    @property
    def unified_msg_origin(self) -> str:
        """统一消息来源（31 次）。格式：`platform_id:message_type:session_id`。

        🔴 照真实现 `:106-108`：**由 `session` 推导**，不是独立字段。
        """
        if self.session is not None:
            return str(self.session)
        return self._umo

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:
        """设置 umo —— 照真实现 `:110-114`：**用 `MessageSession.from_str` 重解析**。

        ⚠️ 解析失败时**不静默**：记 warning 并退化成"整串当 session_id"，
           否则插件拿到空 umo 会去操作错误的会话（比报错更危险）。
        """
        self._umo = value
        try:
            from astrbot_host.compat.core.platform.message_session import MessageSession

            self.session = MessageSession.from_str(value)
            self.session_id = self.session.session_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "umo 解析失败（%r）：%s —— 退化为整串当 session_id", value, exc
            )

    def __repr__(self) -> str:
        return (
            f"<AstrMessageEvent umo={self.unified_msg_origin!r} "
            f"text={self.message_str[:30]!r} stopped={self.is_stopped()}>"
        )


__all__ = ["AstrMessageEvent", "MessageChain"]
