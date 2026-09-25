# -*- coding: utf-8 -*-
"""`astrbot.core.platform.platform_metadata` 兼容层。

⭐ 依据：真 AstrBot `core/platform/platform_metadata.py`（纯 dataclass，全量照抄）。
   实测 15 个文件用它。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlatformMetadata:
    """平台元数据（照真实现字段）。"""

    name: str
    """平台的名称（类型），如 aiocqhttp / discord / slack"""
    description: str
    """平台的描述"""
    id: str
    """平台的唯一标识符，用于配置中识别特定平台"""

    default_config_tmpl: dict | None = None
    """平台的默认配置模板"""
    adapter_display_name: str | None = None
    """WebUI 配置页显示名；空则用 name"""
    logo_path: str | None = None
    """适配器 logo 路径（相对插件目录）"""

    support_streaming_message: bool = True
    """是否支持真实流式传输"""
    support_proactive_message: bool = True
    """是否支持主动消息推送（非用户触发）"""

    module_path: str | None = None
    """注册该适配器的模块路径（热重载清理用）"""
    i18n_resources: dict[str, dict] | None = None
    """国际化资源"""
    config_metadata: dict | None = None
    """配置项元数据（WebUI 表单用）"""


__all__ = ["PlatformMetadata"]
