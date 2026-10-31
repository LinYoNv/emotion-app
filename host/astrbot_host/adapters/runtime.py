# -*- coding: utf-8 -*-
"""适配器运行时 —— 把 `adapters.json` 里的配置变成**活着的**消息通道。

## 职责边界（三件事，不多做）

1. **按配置起停适配器**（`reload()` 是唯一入口，幂等）。
2. **把入站消息接到派发链路**（`dispatch` → 插件 handler）。
3. **把出站消息交给对应适配器**（`sender_for(platform_id)`）。

## 🔴 为什么"启动"和"连通"必须分开报

OneBot 是**反向** WebSocket：我们起一个服务**等协议端连过来**。
所以"适配器已启动"只是"端口在监听"，**协议端可能根本没连**。
把这两件事合成一个 `enabled` 布尔值，用户会看着绿色的"已启用"发呆 ——
这是"静默失败"的典型形态。⇒ `status()` 必须同时给 `running` 和 `connected`。

## 🔴 为什么入站要自己做"事件 → AstrMessageEvent"的构造

`dispatch()` 接受的是 `AstrMessageEvent`（插件 handler 的入参），
而适配器拿到的是 `AstrBotMessage`。中间这一步在真 AstrBot 里是
`Platform.create_event()` → `commit_event()` → 平台管理器的事件队列。
我们这里没有那条队列，所以由本模块直接把 `AstrBotMessage` 包成事件后
**同步派发**（不排队：QQ 私聊的并发量极低，排队只会引入"消息延迟"这种
更难查的问题；而且我们的 dispatch 本来就是 await 完成的）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AdapterRuntime:
    """所有适配器的运行时容器（**单例式**，由服务层持有一份）。"""

    def __init__(self, *, on_dispatch: Any = None) -> None:
        #: 派发回调：`async (event) -> None`。
        #: 由服务层注入（它才知道插件宿主、上下文、配置）。
        self.on_dispatch = on_dispatch
        #: 平台实例 id → 适配器实例（**只含启用的**）
        self._adapters: dict[str, Any] = {}
        #: 平台实例 id → 该平台的 `PlatformSpec`（供注册表用）
        self._specs: dict[str, Any] = {}
        #: 适配器 id → 最近一次错误（诊断用）
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def reload(self, records: list[Any]) -> None:
        """按配置重建全部适配器（**幂等**：先全停，再按启用项起）。

        ⚠️ 全停再起而不是"增量对比"：增量对比在"改了端口/类型"时
           很容易漏停一个，留下孤儿监听（端口占用是最难查的故障之一）。
           私聊量级下重启的代价可以忽略。
        """
        await self.stop_all()
        for record in records:
            if not getattr(record, "enabled", False):
                continue
            adapter_type = getattr(record, "type", "")
            if adapter_type == "webchat":
                # 内置 Web 端不需要"启动"：它就是本服务自己的面板。
                # 但要给它一条**如实失败**的投递路径（不能是 None，
                # 否则插件会以为"平台不可用"而回退到别的路径）。
                self._specs[record.id] = self._spec_for(record)
                continue
            if adapter_type != "onebot_v11":
                logger.warning(
                    "适配器 %s 的类型 %r 暂不支持启动，已跳过（配置保留）",
                    getattr(record, "id", "?"),
                    adapter_type,
                )
                continue
            await self._start_onebot(record)

    async def _start_onebot(self, record: Any) -> None:
        from astrbot_host.adapters.onebot import OneBotAdapter  # noqa: PLC0415

        adapter = OneBotAdapter(
            adapter_id=record.id,
            config=dict(getattr(record, "config", {}) or {}),
            on_message=self._handle_inbound,
        )
        try:
            await adapter.start()
        except Exception as exc:  # noqa: BLE001
            self._errors[record.id] = f"{type(exc).__name__}: {exc}"
            # 🔴 如实记 ERROR，不阻止服务启动（一个适配器起不来 ≠ 整个服务不可用）
            logger.error(
                "OneBot 适配器启动失败（%s）：%s", record.id, exc, exc_info=True
            )
            return
        self._adapters[record.id] = adapter
        self._specs[record.id] = self._spec_for(record)

    def _spec_for(self, record: Any) -> Any:
        """适配器 → `PlatformSpec`（umo 第一段 + 插件看到的三元组）。"""
        from astrbot_host.platforms import PlatformSpec  # noqa: PLC0415

        if getattr(record, "type", "") == "onebot_v11":
            # 🔴 name 必须是 `aiocqhttp`：插件靠它把平台判成 onebot 类型。
            #    ⚠️ description **不能**提别的平台名（约束②），
            #    所以这里只写人类可读的「QQ 机器人」。
            return PlatformSpec(
                id=record.id,
                name="aiocqhttp",
                description="QQ 机器人",
                label=getattr(record, "name", "") or "QQ 机器人",
            )
        # 内置 Web 端：与默认平台同构
        from astrbot_host.platforms import DEFAULT_PLATFORM  # noqa: PLC0415

        return DEFAULT_PLATFORM

    async def stop_all(self) -> None:
        """停掉所有适配器（**幂等**）。"""
        adapters, self._adapters = self._adapters, {}
        self._specs = {}
        for adapter_id, adapter in adapters.items():
            try:
                await adapter.stop()
            except Exception as exc:  # noqa: BLE001
                logger.error("停止适配器 %s 失败：%s", adapter_id, exc)
        if adapters:
            logger.info("已停止 %d 个适配器", len(adapters))

    # ------------------------------------------------------------------
    # 入站：适配器 → 派发链路
    # ------------------------------------------------------------------

    async def _handle_inbound(self, adapter: Any, abm: Any) -> None:
        """适配器收到一条消息 → 构造事件 → 派发。"""
        if self.on_dispatch is None:
            logger.warning(
                "收到消息但派发回调未注入（适配器 %s），消息已丢弃：%s",
                getattr(adapter, "adapter_id", "?"),
                getattr(abm, "message_str", "")[:40],
            )
            return
        try:
            event = adapter.create_event(abm)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "构造事件失败（适配器 %s）：%s", getattr(adapter, "adapter_id", "?"), exc,
                exc_info=True,
            )
            return
        try:
            await self.on_dispatch(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "派发消息失败（适配器 %s）：%s", getattr(adapter, "adapter_id", "?"), exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # 出站：给平台管理器提供 sender
    # ------------------------------------------------------------------

    def sender_for(self, platform_id: str) -> Any:
        """按平台实例 id 取发送器；没有就返回 None（走 delivery）。"""
        adapter = self._adapters.get(platform_id)
        if adapter is None:
            return None

        async def _send(session: Any, message_chain: Any) -> bool:
            return await adapter.send_by_session(session, message_chain)

        return _send

    def client_for(self, platform_id: str) -> Any:
        """按平台实例 id 取**协议端客户端**（`CQHttp` 实例）。

        为什么需要（2026-10-29 实测）：陪伴 7.0.0 的
        `astrbot_host/multi_controller.py:118` `startup_readiness()` 里写了

        ```python
        client = platform.get_client()
        if not getattr(client, "_wsr_api_clients", {}).get(account.self_id):
            return "host_account_unavailable"
        ```

        真 AstrBot 的 `AiocqhttpPlatformAdapter.get_client()` 直接
        `return self.bot`（`aiocqhttp_platform_adapter.py:512`）。
        我们原来**没有这个方法** ⇒ 插件一调就 AttributeError
        ⇒ 被 catch 成 `host_platform_unavailable` ⇒ 面板上显示
        「共享宿主配置、原生路由或权限无法校验」这种**和真实原因无关**的话。

        ⚠️ 返回 None 表示"没有可用客户端"，插件会据此判 `host_account_unavailable`
           —— 这是**如实**的状态，不是静默给假对象。
        """
        adapter = self._adapters.get(platform_id)
        if adapter is None:
            return None
        return getattr(adapter, "_bot", None)

    def specs(self) -> list[Any]:
        """当前启用的平台规格（供 `PlatformRegistry` 注册）。"""
        return list(self._specs.values())

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """面板显示用：每个适配器的真实状态。"""
        out: list[dict[str, Any]] = []
        for adapter_id, adapter in self._adapters.items():
            try:
                out.append(adapter.status())
            except Exception as exc:  # noqa: BLE001
                out.append(
                    {
                        "id": adapter_id,
                        "running": False,
                        "connected": False,
                        "last_error": f"状态读取失败：{exc}",
                    }
                )
        for adapter_id, error in self._errors.items():
            if adapter_id not in self._adapters:
                out.append(
                    {
                        "id": adapter_id,
                        "running": False,
                        "connected": False,
                        "last_error": error,
                    }
                )
        return out


__all__ = ["AdapterRuntime"]
