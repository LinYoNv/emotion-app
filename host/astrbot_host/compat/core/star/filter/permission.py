# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.permission` 兼容层 —— 管理员权限过滤器。

⭐ 依据：真 AstrBot `core/star/filter/permission.py`（实测 31 行）。

⚠️ **相对真实现省略了什么**：
   * 真实现 `from astrbot.core.platform.astr_message_event import AstrMessageEvent`
     与 `from astrbot.core.config import AstrBotConfig` 是**运行时 import**；
     这里改成 `TYPE_CHECKING` 下的类型注解。**为什么**：这两个模块由别的兼容
     文件提供，运行时 import 会让「只想 import 过滤器」的调用方被整条依赖链拖住
     （配合 `astrbot_host/shadow.py` 逐个注册子模块的装载顺序，容易假失败）。
     配合 `from __future__ import annotations`，注解**不参与求值**，签名与真实现
     逐字一致，行为零差异。
   * 没有任何逻辑删减：`PermissionType` 两个成员、`filter()` 的判定分支、
     连真实现里被注释掉的两行（`event.stop_event()` / `raise ValueError`）都保留，
     因为那两行解释了「**为什么**这里只返回 False 而不抛异常」——报错责任在
     handler 层，过滤器只负责「过 / 不过」。
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from . import HandlerFilter

if TYPE_CHECKING:  # pragma: no cover - 仅给类型检查器看
    from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent


class PermissionType(enum.Flag):
    """权限类型。当选择 MEMBER，ADMIN 也可以通过。"""

    ADMIN = enum.auto()
    MEMBER = enum.auto()


class PermissionTypeFilter(HandlerFilter):
    def __init__(
        self, permission_type: PermissionType, raise_error: bool = True
    ) -> None:
        self.permission_type = permission_type
        self.raise_error = raise_error

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """过滤器"""
        if self.permission_type == PermissionType.ADMIN:
            if not event.is_admin():
                # event.stop_event()
                # raise ValueError(f"您 (ID: {event.get_sender_id()}) 没有权限操作管理员指令。")
                return False

        return True


__all__ = ["PermissionType", "PermissionTypeFilter"]
