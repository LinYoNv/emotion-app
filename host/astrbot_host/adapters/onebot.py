# -*- coding: utf-8 -*-
"""OneBot v11 适配器 —— 照抄真 AstrBot 的 `aiocqhttp` 适配器。

## 为什么是"反向 WebSocket"（照抄的依据）

真 AstrBot 的 `core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:44`
用的是：

```python
self.bot = CQHttp(use_ws_reverse=True, import_name="aiocqhttp",
                  api_timeout_sec=180, access_token=...)
```

即**我们起 WS 服务，协议端（NapCat / LLOneBot / go-cqhttp）连过来**。
`aiocqhttp` 库在 `/ws`、`/ws/event`、`/ws/api` 三条路径上挂了反向 WS 处理器
（见 `CQHttp.__init__`），另在 `/` 上挂了 HTTP POST —— 所以两种协议端都能接。

⚠️ 方向别搞反：不是我们去连协议端，而是**等它连我们**。
   这决定了"启动适配器"只是**起一个 HTTP 服务**，协议端没连上来时
   服务照样在跑（只是没有事件）。所以"适配器已启动"≠"QQ 已连通"，
   判据必须看 `connected`（见 `OneBotAdapter.status()`）。

## 与真实现逐条对齐的地方（照抄，不是猜）

| 真实现 | 本文件 | 说明 |
|---|---|---|
| `convert_message()` | `convert_message()` | 消息/通知/请求三类事件 → `AstrBotMessage` |
| `_convert_handle_message_event()` | 同名 | 消息段逐类适配，含 `reply` 递归解析 |
| `send_by_session()` | `send_by_session()` | 群聊取 `session_id.split("_")[-1]` |
| `create_event()` | `create_event()` | 返回 `AiocqhttpMessageEvent` |
| `run()` | `start()` | `bot.run_task(host, port, shutdown_trigger=...)` |
| `terminate()` | `stop()` | 关闭反向 WS 连接 |

## 我们额外做的两件事（真实现没有，但本宿主必须有）

1. **白名单**：真 AstrBot 有完整的权限体系（管理员、群白名单等）；
   我们这套是单用户自用，用**QQ 号白名单**是最小可用且可解释的做法。
   被拒的消息**记 INFO 日志**（"静默丢弃是最贵的 bug"是项目铁律）。
2. **`status()`**：暴露 `running / connected / last_error`，
   让面板能显示"适配器在跑但 QQ 没连上"这种状态 —— 否则用户只能看着
   一个绿色的"已启用"发呆。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 消息段 → 组件类 的映射，**照抄**真实现 `aiocqhttp_platform_adapter.py:239`
#: 里 `ComponentTypes` 的形状。真实现从 `astrbot.api.message_components` 取，
#: 我们从兼容层的同名模块取（那就是插件看到的同一批类）。
def _component_types() -> dict[str, Any]:
    from astrbot_host.compat.api.message_components import (  # noqa: PLC0415
        At,
        Contact,
        Face,
        File,
        Forward,
        Image,
        Json,
        Music,
        Node,
        Nodes,
        Poke,
        Plain,
        Record,
        Reply,
        Shake,
        Video,
    )

    return {
        "text": Plain,
        "image": Image,
        "record": Record,
        "video": Video,
        "file": File,
        "at": At,
        "reply": Reply,
        "face": Face,
        "poke": Poke,
        "json": Json,
        "music": Music,
        "contact": Contact,
        "shake": Shake,
        "forward": Forward,
        "node": Node,
        "nodes": Nodes,
    }


class OneBotAdapter:
    """OneBot v11 适配器（反向 WebSocket）。

    Args:
        adapter_id: 适配器 id（来自 `adapters.json`）。它进 umo，**必须稳定**。
        config: 已校验的配置（`ws_reverse_host` / `ws_reverse_port` /
            `ws_reverse_token` / `whitelist_enabled` / `access_whitelist`）。
        on_message: 收到一条可派发的消息时的回调
            `async (event) -> None`；由调用方（运行时管理器）接到派发链路。
    """

    #: 平台名（进 umo 第一段）。照真实现 `PlatformMetadata(name="aiocqhttp")`。
    platform_name = "aiocqhttp"

    def __init__(
        self,
        *,
        adapter_id: str,
        config: dict[str, Any],
        on_message: Any = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.config = dict(config)
        self.on_message = on_message

        self.host = str(self.config.get("ws_reverse_host") or "0.0.0.0")
        self.port = int(self.config.get("ws_reverse_port") or 6199)
        self.token = str(self.config.get("ws_reverse_token") or "")
        self.whitelist_enabled = bool(self.config.get("whitelist_enabled"))
        self.access_whitelist = tuple(str(x) for x in (self.config.get("access_whitelist") or ()))

        self._bot: Any = None
        self._task: asyncio.Task | None = None
        self._shutdown: asyncio.Event | None = None
        self._running = False
        self._last_error: str | None = None
        #: 最近一次收到事件的时刻（用于判断"协议端连上来了没有"）
        self._last_event_at: float | None = None
        #: 收到的消息计数（诊断用；面板显示"已收到 N 条"比"已连接"更可信）
        self._received = 0
        self._sent = 0
        self._rejected = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """起反向 WS 服务。**幂等**：已在跑就直接返回。

        🔴 **协议端连上来时必须带 `X-Client-Role` 头**（值 `universal` /
           `event` / `api` 之一）。这是 OneBot v11 反向 WS 的规范要求，
           `aiocqhttp` 的 `_handle_wsr()` 会读它来分流：

           ```python
           role = websocket.headers['X-Client-Role'].lower()
           ```

           **缺这个头会怎样**：不是"连不上"，而是握手阶段被回 **HTTP 400**
           —— 实测（2026-10-23）我用 `websockets.connect` 不带该头，
           客户端报 `InvalidStatus: server rejected WebSocket connection: HTTP 400`，
           看起来像"我们的服务坏了"，其实**是客户端没按规范发头**。

           ⚠️ 排查提示：如果协议端连不上，先抓它的握手请求看有没有
           `X-Client-Role`；NapCat / go-cqhttp / LLOneBot 都会自动带，
           自己写的测试客户端最容易漏。
        """
        if self._running:
            return
        from aiocqhttp import CQHttp  # noqa: PLC0415

        bot = CQHttp(
            use_ws_reverse=True,
            import_name="aiocqhttp",
            api_timeout_sec=180,
            # ⚠️ 空字符串要传 None，否则库会把它当成"要求 token 为空串"的严格校验
            access_token=self.token or None,
        )
        self._bot = bot
        self._register_handlers(bot)

        self._shutdown = asyncio.Event()

        async def _trigger() -> None:
            assert self._shutdown is not None
            await self._shutdown.wait()

        # `run_task` 返回协程；用 create_task 挂到当前事件循环上
        self._task = asyncio.create_task(
            bot.run_task(host=self.host, port=self.port, shutdown_trigger=_trigger),
            name=f"onebot-adapter-{self.adapter_id}",
        )
        self._running = True
        self._last_error = None
        logger.info(
            "OneBot 适配器已启动：%s 监听 %s:%s（等协议端连上来）",
            self.adapter_id,
            self.host,            self.port,
        )
        # 端口被占 / bind 失败不会立刻抛，要等一会儿才能看出来 —— 所以这里
        # **不阻塞**，由 `status()` 反映；同时挂个回调把异常记下来。
        self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        """服务任务结束时把异常**如实记下来**（不吞）。"""
        self._running = False
        if task.cancelled():
            logger.info("OneBot 适配器已停止：%s", self.adapter_id)
            return
        exc = task.exception()
        if exc is not None:
            self._last_error = f"{type(exc).__name__}: {exc}"
            # 🔴 端口被占是最常见的启动失败，必须让用户看到可操作的提示
            hint = ""
            text = str(exc)
            if "address already in use" in text.lower() or "errno 98" in text.lower():
                hint = (
                    f"（端口 {self.port} 被占用 —— 换个端口，"
                    "或先停掉占用它的程序）"
                )
            logger.error(
                "OneBot 适配器异常退出：%s → %s%s", self.adapter_id, exc, hint
            )
        else:
            logger.info("OneBot 适配器服务已结束：%s", self.adapter_id)

    async def stop(self) -> None:
        """停服务并断开所有反向 WS 连接。**幂等**。"""
        if self._shutdown is not None:
            self._shutdown.set()
        await self._close_reverse_ws_connections()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 照真实现 `_close_reverse_ws_connections()`：主动关掉协议端连接，
        # 否则协议端会一直以为连着（它自己重连要等超时）。
        self._bot = None
        self._shutdown = None
        self._running = False
        logger.info("OneBot 适配器已停止：%s", self.adapter_id)

    async def _close_reverse_ws_connections(self) -> None:
        """照抄真实现 `aiocqhttp_platform_adapter.py:452-490`。"""
        bot = self._bot
        if bot is None:
            return
        api_clients = getattr(bot, "_wsr_api_clients", None)
        event_clients = getattr(bot, "_wsr_event_clients", None)
        ws_clients: set[Any] = set()
        if isinstance(api_clients, dict):
            ws_clients.update(api_clients.values())
        if isinstance(event_clients, set):
            ws_clients.update(event_clients)
        closers = []
        for ws in ws_clients:
            close_func = getattr(ws, "close", None)
            if not callable(close_func):
                continue
            try:
                result = close_func(code=1000, reason="Adapter shutdown")
            except TypeError:
                result = close_func()
            except Exception:  # noqa: BLE001
                continue
            if asyncio.iscoroutine(result):
                closers.append(result)
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)

    def status(self) -> dict[str, Any]:
        """面板显示用的状态。**如实反映**，不美化。"""
        connected = False
        bot = self._bot
        if bot is not None:
            api_clients = getattr(bot, "_wsr_api_clients", None)
            event_clients = getattr(bot, "_wsr_event_clients", None)
            count = 0
            if isinstance(api_clients, dict):
                count += len(api_clients)
            if isinstance(event_clients, set):
                count += len(event_clients)
            connected = count > 0
        return {
            "id": self.adapter_id,
            "type": "onebot_v11",
            "running": self._running,
            "connected": connected,
            "last_event_at": self._last_event_at,
            "received": self._received,
            "sent": self._sent,
            "rejected": self._rejected,
            "last_error": self._last_error,
            "listen": f"{self.host}:{self.port}",
        }

    # ------------------------------------------------------------------
    # 事件注册（照抄真实现的四类：request / notice / message(group|private)）
    # ------------------------------------------------------------------

    def _register_handlers(self, bot: Any) -> None:
        async def _dispatch(event: Any) -> None:
            try:
                await self._handle_onebot_event(event)
            except Exception as exc:  # noqa: BLE001
                # 🔴 不吞：一条事件处理失败不能拖垮整个连接，但必须留痕
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "OneBot 事件处理失败（%s）：%s", self.adapter_id, exc
                )

        @bot.on_request()
        async def _on_request(event: Any) -> None:
            await _dispatch(event)

        @bot.on_notice()
        async def _on_notice(event: Any) -> None:
            await _dispatch(event)

        @bot.on_message("group")
        async def _on_group(event: Any) -> None:
            await _dispatch(event)

        @bot.on_message("private")
        async def _on_private(event: Any) -> None:
            await _dispatch(event)

        # 心跳/生命周期：只用来刷新"协议端连上来了"的时刻
        @bot.on_meta_event("heartbeat")
        async def _on_heartbeat(event: Any) -> None:
            self._last_event_at = time.time()

    async def _handle_onebot_event(self, event: Any) -> None:
        self._last_event_at = time.time()
        abm = await self.convert_message(event)
        if abm is None:
            return

        # ---- 白名单（我们额外加的一道闸）----
        if not self._allowed(abm):
            self._rejected += 1
            logger.info(
                "白名单拒绝：%s 的消息（QQ %s）不在允许名单内，已忽略。"
                "要放行请在面板「适配器」里把它加进白名单。",
                self.adapter_id,
                getattr(getattr(abm, "sender", None), "user_id", "?"),
            )
            return

        self._received += 1
        if self.on_message is not None:
            await self.on_message(self, abm)

    def _allowed(self, abm: Any) -> bool:
        """白名单判定。

        ⚠️ **空白名单 + 启用白名单 = 谁都进不来**（fail-closed）。
           反过来（空白名单 = 全放行）会让"我忘了填"变成"对全网开放"，
           那是会烧模型额度的静默事故。
        """
        if not self.whitelist_enabled:
            return True
        sender = getattr(abm, "sender", None)
        user_id = str(getattr(sender, "user_id", "") or "")
        if not user_id:
            return False
        return user_id in self.access_whitelist

    # ------------------------------------------------------------------
    # 消息转换（照抄真实现 `convert_message` 系列）
    # ------------------------------------------------------------------

    async def convert_message(self, event: Any) -> Any:
        """OneBot 事件 → `AstrBotMessage`。不认识的事件返回 `None`。"""
        post_type = event.get("post_type")
        if post_type == "message":
            abm = await self._convert_message_event(event)
            # 照真实现：屏蔽 QQ 管家（2854196310）的骚扰消息
            sender = getattr(abm, "sender", None)
            if str(getattr(sender, "user_id", "")) == "2854196310":
                logger.debug("已屏蔽 QQ 管家的消息")
                return None
            return abm
        if post_type == "notice":
            return await self._convert_notice_event(event)
        if post_type == "request":
            return await self._convert_request_event(event)
        return None

    async def _convert_request_event(self, event: Any) -> Any:
        """请求类事件（加好友 / 加群）。照真实现 `_convert_handle_request_event`。"""
        from astrbot_host.compat.core.platform.astrbot_message import (  # noqa: PLC0415
            AstrBotMessage,
            MessageMember,
            MessageType,
        )

        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        abm.sender = MessageMember(
            user_id=str(event.user_id), nickname=str(event.user_id)
        )
        abm.type = MessageType.OTHER_MESSAGE
        if event.get("group_id"):
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(event.group_id)
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )
        abm.message_str = ""
        abm.message = []
        abm.timestamp = int(time.time())
        abm.message_id = uuid.uuid4().hex
        abm.raw_message = event
        return abm

    async def _convert_notice_event(self, event: Any) -> Any:
        """通知类事件（群成员增减 / 撤回 / 戳一戳）。照真实现同名方法。"""
        from astrbot_host.compat.core.platform.astrbot_message import (  # noqa: PLC0415
            AstrBotMessage,
            MessageMember,
            MessageType,
        )

        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        abm.sender = MessageMember(
            user_id=str(event.user_id), nickname=str(event.user_id)
        )
        abm.type = MessageType.OTHER_MESSAGE
        if event.get("group_id"):
            abm.group_id = str(event.group_id)
            abm.type = MessageType.GROUP_MESSAGE
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )
        abm.message_str = ""
        abm.message = []
        abm.raw_message = event
        abm.timestamp = int(time.time())
        abm.message_id = uuid.uuid4().hex

        if event.get("sub_type") == "poke" and event.get("target_id"):
            try:
                from astrbot_host.compat.api.message_components import (  # noqa: PLC0415
                    Poke,
                )

                abm.message.append(Poke(id=str(event["target_id"])))
            except Exception:  # noqa: BLE001
                logger.debug("戳一戳消息段构造失败，已忽略该段")
        return abm

    async def _convert_message_event(self, event: Any, *, get_reply: bool = True) -> Any:
        """消息类事件。**逐段照抄**真实现 `_convert_handle_message_event`。"""
        from astrbot_host.compat.core.platform.astrbot_message import (  # noqa: PLC0415
            AstrBotMessage,
            MessageMember,
            MessageType,
        )

        component_types = _component_types()

        abm = AstrBotMessage()
        abm.self_id = str(event.self_id)
        sender_payload = event.get("sender") or {}
        abm.sender = MessageMember(
            str(sender_payload.get("user_id", "")),
            sender_payload.get("card") or sender_payload.get("nickname", "N/A"),
        )
        if event.get("message_type") == "group":
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(event.group_id)
            try:
                from astrbot_host.compat.core.platform.astrbot_message import Group  # noqa: PLC0415

                abm.group = Group(str(event.group_id))
                abm.group.group_name = event.get("group_name", "N/A")
            except Exception:  # noqa: BLE001
                logger.debug("群信息构造失败，已跳过 group 字段")
        else:
            abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = (
            str(event.group_id)
            if abm.type == MessageType.GROUP_MESSAGE
            else abm.sender.user_id
        )
        abm.message_id = str(event.get("message_id", ""))
        abm.message = []

        raw_message = event.get("message")
        if not isinstance(raw_message, list):
            # 🔴 照真实现：**不能静默**。post_format 不是 array 时，
            #    整条消息都解析不了，必须告诉用户怎么改。
            err = (
                "OneBot: 无法识别的消息类型（消息段不是数组）。"
                "如果你在用 go-cqhttp / NapCat，请把配置里的 "
                "`message.post-format`（NapCat 是 `messagePostFormat`）改成 `array`。"
            )
            logger.error(err)
            raise ValueError(err)

        message_str = ""
        routing_params = {"self_id": event.self_id} if event.self_id else {}

        for seg_type, group in itertools.groupby(raw_message, key=lambda x: x.get("type")):
            if seg_type == "text":
                text = "".join(m.get("data", {}).get("text", "") for m in group).strip()
                if not text:
                    continue
                message_str += text
                abm.message.append(component_types["text"](text=text))

            elif seg_type == "reply":
                for m in group:
                    if not get_reply:
                        abm.message.append(component_types["reply"](**m.get("data", {})))
                        continue
                    try:
                        reply_data = await self._bot.call_action(
                            action="get_msg",
                            message_id=int(m["data"]["id"]),
                            **routing_params,
                        )
                        reply_data["post_type"] = "message"
                        from aiocqhttp import Event  # noqa: PLC0415

                        new_event = Event.from_payload(reply_data)
                        if not new_event:
                            logger.error("无法从引用消息数据构造事件：%r", reply_data)
                            continue
                        abm_reply = await self._convert_message_event(
                            new_event, get_reply=False
                        )
                        Reply = component_types["reply"]
                        abm.message.append(
                            Reply(
                                id=abm_reply.message_id,
                                chain=abm_reply.message,
                                sender_id=abm_reply.sender.user_id,
                                sender_nickname=abm_reply.sender.nickname,
                                time=abm_reply.timestamp,
                                message_str=abm_reply.message_str,
                                text=abm_reply.message_str,
                                qq=abm_reply.sender.user_id,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        # 照真实现：拿不到引用就**退化成普通 reply 段**，不丢消息
                        logger.error("获取引用消息失败：%s。已退化为普通引用段。", exc)
                        abm.message.append(component_types["reply"](**m.get("data", {})))

            elif seg_type == "at":
                at_parts: list[str] = []
                first_at_self_done = False
                for m in group:
                    data = m.get("data", {})
                    qq = str(data.get("qq", ""))
                    if qq == "all":
                        abm.message.append(component_types["at"](qq="all", name="全体成员"))
                        continue
                    nickname = ""
                    try:
                        info = await self._bot.call_action(
                            action="get_group_member_info",
                            group_id=event.get("group_id"),
                            user_id=int(qq),
                            no_cache=False,
                            **routing_params,
                        )
                        if info:
                            nickname = info.get("card", "")
                            if not nickname:
                                info = await self._bot.call_action(
                                    action="get_stranger_info",
                                    user_id=int(qq),
                                    no_cache=False,
                                    **routing_params,
                                )
                                nickname = (info or {}).get("nick", "") or (info or {}).get("nickname", "")
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("@ 用户信息获取失败（%s），用空昵称继续", exc)
                    abm.message.append(component_types["at"](qq=qq, name=nickname))
                    is_at_self = qq in {abm.self_id, "all"}
                    if is_at_self and not first_at_self_done:
                        first_at_self_done = True
                    else:
                        at_parts.append(f" @{nickname}({qq}) ")
                message_str += "".join(at_parts)

            elif seg_type == "file":
                for m in group:
                    data = m.get("data", {})
                    url = data.get("url") or ""
                    if url.startswith("http"):
                        name = data.get("file_name") or data.get("name") or data.get("file") or "file"
                        abm.message.append(component_types["file"](name=name, url=url))
                        continue
                    # NapCat 的 file 段没有 url，要用 file_id 换
                    try:
                        file_id = data.get("file_id") or data.get("file")
                        if abm.type == MessageType.GROUP_MESSAGE:
                            ret = await self._bot.call_action(
                                action="get_group_file_url",
                                file_id=file_id,
                                group_id=event.get("group_id"),
                                **routing_params,
                            )
                        else:
                            ret = await self._bot.call_action(
                                action="get_private_file_url",
                                file_id=file_id,
                                **routing_params,
                            )
                        if ret and ret.get("url"):
                            name = (
                                ret.get("file_name")
                                or ret.get("name")
                                or data.get("file")
                                or "file"
                            )
                            abm.message.append(
                                component_types["file"](name=name, url=ret["url"])
                            )
                        else:
                            logger.error("获取文件失败，返回值里没有 url：%r", ret)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("获取文件失败：%s，该消息段已忽略。", exc)

            elif seg_type == "mface":
                # 照真实现：mface（商城表情）直接跳过
                continue

            elif seg_type == "markdown":
                for m in group:
                    data = m.get("data", {})
                    text = data.get("markdown") or data.get("content", "")
                    abm.message.append(component_types["text"](text=text))
                    message_str += text

            else:
                # 其余类型：能构造就构造，不能就**记 warning 后跳过**（不静默）
                for m in group:
                    cls = component_types.get(seg_type)
                    if cls is None:
                        logger.warning(
                            "不支持的消息段类型，已忽略：%s，data=%r",
                            seg_type,
                            m.get("data"),
                        )
                        continue
                    try:
                        abm.message.append(cls(**m.get("data", {})))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "消息段解析失败，已忽略：type=%s data=%r（%s）",
                            seg_type,
                            m.get("data"),
                            exc,
                        )

        abm.timestamp = int(time.time())
        abm.message_str = message_str
        abm.raw_message = event
        return abm

    # ------------------------------------------------------------------
    # 事件构造（照抄真实现 `AiocqhttpAdapter.create_event`）
    # ------------------------------------------------------------------

    def create_event(self, abm: Any) -> Any:
        """`AstrBotMessage` → `AiocqhttpMessageEvent`（插件 handler 的入参）。

        🔴 为什么用 `AiocqhttpMessageEvent` 而不是基类：它带着 `bot` 引用，
           插件的「引用回复」「戳一戳」等能力会调 `event.bot.call_action(...)`。
           用基类会让那些能力在运行期 `AttributeError` ——
           **而插件通常把它 catch 成一行 info 日志**，于是表现为
           "功能不对但没报错"（本项目踩过同类的静默 bug）。
        """
        from astrbot_host.compat.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (  # noqa: PLC0415
            AiocqhttpMessageEvent,
        )
        from astrbot_host.platforms import PlatformSpec  # noqa: PLC0415
        from astrbot_host.umo import build_platform_metadata  # noqa: PLC0415

        spec = PlatformSpec(
            id=self.adapter_id,
            name=self.platform_name,
            description="QQ 机器人",
            label=self.adapter_id,
        )
        return AiocqhttpMessageEvent(
            message_str=getattr(abm, "message_str", ""),
            message_obj=abm,
            platform_meta=build_platform_metadata(spec=spec),
            session_id=getattr(abm, "session_id", ""),
            bot=self._bot,
        )

    # ------------------------------------------------------------------
    # 发送（照抄真实现 `send_by_session` + `AiocqhttpMessageEvent.send_message`）
    # ------------------------------------------------------------------

    async def send_by_session(self, session: Any, message_chain: Any) -> bool:
        """把一条消息链发到 QQ。返回是否成功（调用方据此如实报错）。

        ⚠️ 照真实现 `send_by_session`：**群聊的 session_id 要取最后一段**
           （真实现 `session.session_id.split("_")[-1]`，因为群里 session_id
            形如 `<群号>_<群号>`）。
        """
        from astrbot_host.compat.core.platform.astrbot_message import (  # noqa: PLC0415
            MessageType,
        )

        bot = self._bot
        if bot is None or not self._running:
            logger.error("OneBot 适配器未运行，消息发送失败：%s", self.adapter_id)
            return False

        is_group = getattr(session, "message_type", None) == MessageType.GROUP_MESSAGE
        session_id = str(getattr(session, "session_id", ""))
        if is_group:
            session_id = session_id.split("_")[-1]

        try:
            payload = await self._chain_to_onebot(message_chain)
            if payload is None:
                return False
            if is_group:
                await bot.call_action(
                    action="send_group_msg", group_id=session_id, message=payload
                )
            else:
                await bot.call_action(
                    action="send_private_msg", user_id=session_id, message=payload
                )
            self._sent += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error("OneBot 发送失败（%s）：%s", self.adapter_id, exc)
            return False

    async def _chain_to_onebot(self, message_chain: Any) -> list[dict[str, Any]] | None:
        """消息链 → OneBot 消息段数组。

        🔴 图片/语音/文件要走 **base64**（照真实现
           `AiocqhttpMessageEvent._from_segment_to_dict`）：协议端不一定能访问
           我们本机的文件路径。
        """
        chain = getattr(message_chain, "chain", None)
        if chain is None:
            chain = list(message_chain or [])
        segments: list[dict[str, Any]] = []
        for seg in chain:
            kind = str(getattr(seg, "type", "") or "").lower()
            if kind == "plain":
                text = getattr(seg, "text", "")
                if text:
                    segments.append({"type": "text", "data": {"text": text}})
            elif kind in ("image", "record"):
                # 照真实现：转 base64
                try:
                    bs64 = await seg.convert_to_base64()
                    segments.append(
                        {"type": kind, "data": {"file": f"base64://{bs64}"}}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("消息段转 base64 失败（%s）：%s", kind, exc)
            elif kind == "at":
                qq = getattr(seg, "qq", "")
                segments.append({"type": "at", "data": {"qq": str(qq)}})
            elif kind == "reply":
                seg_id = getattr(seg, "id", "")
                if seg_id:
                    segments.append({"type": "reply", "data": {"id": str(seg_id)}})
            else:
                # 其余类型尽力转 dict；失败就记日志跳过（不静默）
                try:
                    data = await seg.to_dict()
                    if isinstance(data, dict) and data.get("type"):
                        segments.append(data)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("消息段 %s 无法序列化，已跳过：%s", kind, exc)
        if not segments:
            return None
        return segments


__all__ = ["OneBotAdapter"]
