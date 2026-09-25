# -*- coding: utf-8 -*-
"""`astrbot.core.platform.platform` 兼容层 —— 平台抽象与状态。

⭐ 依据：真 AstrBot `core/platform/platform.py`。
   实测 `PlatformStatus` 17 个文件用它（陪伴会判断"平台是否在运行"）。

⚠️ 真实现的 `Platform` 是 `abc.ABC` 且方法很多（消息发送/媒体上传/事件轮询…）。
   我们**只提供抽象骨架**：我们这个宿主的"平台"就是 Web 端本身，
   不通过 Platform 子类接入。插件若真的实例化 Platform 子类，
   会走 `abc` 的抽象方法报错（**显式**，不静默）。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PlatformStatus(Enum):
    """平台运行状态（照真实现 —— 值逐字一致）。"""

    PENDING = "pending"    # 待启动
    RUNNING = "running"    # 运行中
    ERROR = "error"        # 发生错误
    STOPPED = "stopped"    # 已停止


@dataclass
class PlatformError:
    """平台错误（照真实现）。"""

    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    traceback: str | None = None


class Platform(abc.ABC):
    """平台适配器抽象基类（照真实现的最小骨架）。

    ⚠️ 真实现有 `send_by_session` / `run` / `terminate` 等一堆抽象方法。
       我们**不逐个复刻** —— 因为我们的宿主不通过它接入。
       保留这个类是为了 `isinstance` 判断与类型注解能通过。
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._status = PlatformStatus.PENDING

    @property
    def status(self) -> PlatformStatus:
        return self._status

    def set_status(self, status: PlatformStatus) -> None:
        self._status = status

    def meta(self) -> Any:  # noqa: F821  (真实现返回 PlatformMetadata)
        raise NotImplementedError(
            "宿主兼容层的 `Platform.meta()` 未实现 —— 本宿主不通过 Platform 子类接入。"
        )

    async def run(self) -> None:
        raise NotImplementedError(
            "宿主兼容层的 `Platform.run()` 未实现 —— 本宿主不通过 Platform 子类接入。"
        )

    async def terminate(self) -> None:
        self._status = PlatformStatus.STOPPED


from typing import Any  # noqa: E402  (放末尾避免与上面注解冲突)

__all__ = ["Platform", "PlatformError", "PlatformStatus"]
