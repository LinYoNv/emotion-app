# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.platform_adapter_type` 兼容层 —— 按平台适配器类型过滤。

⭐ 依据：真 AstrBot `core/star/filter/platform_adapter_type.py`（实测 74 行）。

⚠️ **为什么整张表都要搬（哪怕宿主只实现 1 个平台）**：
   `PlatformAdapterType` 是 `enum.Flag`，插件会写
   `PlatformAdapterType.AIOCQHTTP | PlatformAdapterType.TELEGRAM` 这种**组合值**。
   少一个成员 → 插件在**类定义/装饰器求值阶段**就 `AttributeError`，
   表现为「插件加载即失败」，而不是运行到某个分支才出问题。
   所以 21 个成员与 20 条 `ADAPTER_NAME_2_TYPE` 映射**逐字照搬**，
   宿主没实现的平台只是**永远匹配不上**（返回 False），不是缺失。

⚠️ **相对真实现省略了什么**：
   * `AstrMessageEvent` / `AstrBotConfig` 两个运行时 import 改为 `TYPE_CHECKING`
     注解（同 `permission.py` 的理由：避免把整条依赖链拖进 import 期）。
   * 过滤逻辑零删减，含真实现那个**容易看漏但必须保留**的短路：
     `self.platform_type & PlatformAdapterType.ALL` 为真就直接放行。
     注意 `ALL` 是 `enum.auto()` 的第 21 位（不是「全 1 掩码」）——
     它只跟**同样含 ALL 位**的组合值相交，**不是**「匹配所有平台」的开关；
     照抄以免语义漂移。
"""
from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from . import HandlerFilter

if TYPE_CHECKING:  # pragma: no cover - 仅给类型检查器看
    from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent


class PlatformAdapterType(enum.Flag):
    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    QQOFFICIAL_WEBHOOK = enum.auto()
    TELEGRAM = enum.auto()
    WECOM = enum.auto()
    WECOM_AI_BOT = enum.auto()
    LARK = enum.auto()
    DINGTALK = enum.auto()
    DISCORD = enum.auto()
    SLACK = enum.auto()
    KOOK = enum.auto()
    VOCECHAT = enum.auto()
    WEIXIN_OFFICIAL_ACCOUNT = enum.auto()
    SATORI = enum.auto()
    MISSKEY = enum.auto()
    LINE = enum.auto()
    MATRIX = enum.auto()
    WEIXIN_OC = enum.auto()
    MATTERMOST = enum.auto()
    WEBCHAT = enum.auto()
    ALL = enum.auto()


ADAPTER_NAME_2_TYPE = {
    "aiocqhttp": PlatformAdapterType.AIOCQHTTP,
    "qq_official": PlatformAdapterType.QQOFFICIAL,
    "qq_official_webhook": PlatformAdapterType.QQOFFICIAL_WEBHOOK,
    "telegram": PlatformAdapterType.TELEGRAM,
    "wecom": PlatformAdapterType.WECOM,
    "wecom_ai_bot": PlatformAdapterType.WECOM_AI_BOT,
    "lark": PlatformAdapterType.LARK,
    "dingtalk": PlatformAdapterType.DINGTALK,
    "discord": PlatformAdapterType.DISCORD,
    "slack": PlatformAdapterType.SLACK,
    "kook": PlatformAdapterType.KOOK,
    "vocechat": PlatformAdapterType.VOCECHAT,
    "weixin_official_account": PlatformAdapterType.WEIXIN_OFFICIAL_ACCOUNT,
    "satori": PlatformAdapterType.SATORI,
    "misskey": PlatformAdapterType.MISSKEY,
    "line": PlatformAdapterType.LINE,
    "matrix": PlatformAdapterType.MATRIX,
    "weixin_oc": PlatformAdapterType.WEIXIN_OC,
    "mattermost": PlatformAdapterType.MATTERMOST,
    "webchat": PlatformAdapterType.WEBCHAT,
}


class PlatformAdapterTypeFilter(HandlerFilter):
    def __init__(self, platform_adapter_type_or_str: PlatformAdapterType | str) -> None:
        if isinstance(platform_adapter_type_or_str, str):
            self.platform_type = ADAPTER_NAME_2_TYPE.get(platform_adapter_type_or_str)
        else:
            self.platform_type = platform_adapter_type_or_str

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if (
            self.platform_type is not None
            and self.platform_type & PlatformAdapterType.ALL
        ):
            return True

        adapter_name = event.get_platform_name()
        if adapter_name in ADAPTER_NAME_2_TYPE and self.platform_type is not None:
            return bool(ADAPTER_NAME_2_TYPE[adapter_name] & self.platform_type)
        return False


__all__ = [
    "ADAPTER_NAME_2_TYPE",
    "PlatformAdapterType",
    "PlatformAdapterTypeFilter",
]
