# -*- coding: utf-8 -*-
"""`astrbot.core.star.base` —— `Star` 基类（所有插件的父类）。

⭐⭐ **语义照真 AstrBot `core/star/base.py`**（实测 114 行，已逐条比对）。

🔴 三个**必须保留**的行为（插件会依赖）：
   ① `__init_subclass__` 把子类登记进 `star_map` / `star_registry`
      —— 加载器靠它发现插件类；
   ② `__init__` 里 `star_map.get(self.__class__.__module__)` 解析插件名后建 logger
      —— 插件定义了只读 `logger` property 时要**放过**（真实现用 AttributeError 兜住）；
   ③ `.context` 属性 —— 个别插件走 `context.context.context.send_message(...)`
      （实测 `tts_tool_sanitizer.py:447`，全库仅 1 处三层访问）。

⚠️ 我们**不实现** `text_to_image` / `html_render` 的真实渲染
   （真实现依赖 `html_renderer` + 浏览器）；
   调用时**显式抛 NotImplementedError**，不静默返回假值。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot")

#: 模块路径 → 插件元数据（照真实现 `star_map`）
star_map: dict[str, "StarMetadata"] = {}
#: 全部已注册插件（有序，照真实现 `star_registry`）
star_registry: list["StarMetadata"] = []


@dataclass
class StarMetadata:
    """插件元数据（照真实现的核心字段）。"""

    star_cls_type: type | None = None
    module_path: str = ""
    #: 🔴 插件**实例**（不是类！类在 `star_cls_type`）—— 照真实现
    #: `core/star/star.py:42`。插件靠它**跨插件互调**：实测陪伴的
    #: `_memory_companion_bridge()` 会读 `get_all_stars()` 里的
    #: `metadata.star_cls.memory_companion`（`memory_companion_adapter.py:538`），
    #: 记忆插件面板桥也读它（`companion_page_bridge.py:786`）。
    #: ⚠️ **插件被禁用/实例化失败时为 `None`** —— 照真实现
    #: `star_manager.py:1240`（`else: metadata.star_cls = None`），
    #: 别填假对象：插件对此有明确语义（`presence["loaded"] is False`）。
    star_cls: Any | None = None
    #: 🔴 插件**模块对象** —— 照真实现 `core/star/star.py:44` +
    #: 赋值处 `star_manager.py:1243`（`metadata.module = module`）。
    #: 插件从它取桥：`getattr(metadata.module, "get_memory_companion_bridge")`
    #: （`memory_companion_adapter.py:892`）。
    module: Any | None = None
    name: str = ""
    author: str = ""
    desc: str = ""
    version: str = ""
    #: 插件目录（加载器填）
    root_dir_name: str = ""
    #: 展示名（面板用）
    display_name: str = ""
    activated: bool = True
    reserved: dict[str, Any] = field(default_factory=dict)


class CommandParserMixin:
    """指令解析 mixin（真实现来自 `utils/command_parser.py`）。

    ⚠️ 真实现实现了 `wc_with_prefix` 等解析能力；这里只提供**最小等价物**，
       因为陪伴插件没有直接调用它的方法（实测依赖面里没出现）。
       保持类存在是为了**继承链一致** —— 有些插件会 `isinstance` 判断。
    """


class PluginKVStoreMixin:
    """插件键值存储 mixin（真实现来自 `utils/plugin_kv_store.py`）。

    ⚠️ 同理：保留继承链，能力按需补。
    """


class Star(CommandParserMixin, PluginKVStoreMixin):
    """所有插件（Star）的父类。

    🔴 **`__init_subclass__` 是关键**：任何继承 `Star` 的类一被定义，
       就自动进 `star_map` —— 插件的 `main.py` 只要 import 完，
       加载器就能从这张表里发现它。
    """

    author: str
    name: str
    context: Any
    logger: logging.Logger

    def __init__(self, context: Any, config: dict | None = None) -> None:
        self.context = context
        # ⚠️ **`config` 形参被刻意丢弃** —— 照真实现（`core/star/base.py:28-52`）。
        #    实测确认：真 `Star.__init__` 的函数体**从不使用**这个参数，
        #    且 `Star` **没有 `config` 属性**。
        #    → 我第一版"顺手"存了 `self.config`，那是**加行为不是等义**，
        #      已按契约调研结论移除（`docs/REAL_ASTRBOT_CONTRACT.md` §3）。
        #      插件要拿配置走 `context.get_config()`。
        del config
        # 先按"模块已注册的元数据"取名（与 AstrBot 面板一致），
        # 再退回类属性 `name`（加载器会注入一个清洗过的名字）。
        metadata = star_map.get(self.__class__.__module__)
        plugin_name = (metadata.name if metadata else None) or getattr(
            self, "name", None
        )
        try:
            self.logger = (
                logging.getLogger(f"astrbot.plugin.{plugin_name}")
                if plugin_name
                else logging.getLogger("astrbot")
            )
        except AttributeError:
            # 插件把 `logger` 定义成只读 property → 尊重它自己的（照真实现）
            pass

    def _get_context_config(self) -> Any:
        """照真实现：优先 `context.get_config()`，退回 `context._config`。"""
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            try:
                return get_config()
            except Exception as exc:  # noqa: BLE001
                logger.debug("get_config() 失败：%s", exc)
                return None
        return getattr(self.context, "_config", None)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """子类一被定义就登记（照真实现）。"""
        super().__init_subclass__(**kwargs)
        if not star_map.get(cls.__module__):
            metadata = StarMetadata(
                star_cls_type=cls,
                module_path=cls.__module__,
            )
            star_map[cls.__module__] = metadata
            star_registry.append(metadata)
        else:
            star_map[cls.__module__].star_cls_type = cls
            star_map[cls.__module__].module_path = cls.__module__

    # ---- 真实现有、但我们**尚未实现**的能力：显式报错 ----

    async def text_to_image(self, text: str, return_url: bool = True) -> str:
        """⚠️ 未实现（真实现依赖 `html_renderer` + 浏览器渲染）。"""
        raise NotImplementedError(
            "宿主兼容层尚未实现 `Star.text_to_image`（需要 Renderer 能力）。"
            "插件若依赖它，请先在宿主侧补 Renderer 适配器。"
        )

    async def html_render(
        self,
        tmpl: str,
        data: dict,
        return_url: bool = True,
        options: dict | None = None,
    ) -> str:
        """⚠️ 未实现（同上）。"""
        raise NotImplementedError(
            "宿主兼容层尚未实现 `Star.html_render`（需要 Renderer 能力）。"
        )

    # ---- 生命周期（真实现是空方法，插件按需覆盖）----

    async def initialize(self) -> None:
        """插件被激活时调用。"""

    async def terminate(self) -> None:
        """插件被禁用/重载时调用。"""


__all__ = [
    "CommandParserMixin",
    "PluginKVStoreMixin",
    "Star",
    "StarMetadata",
    "star_map",
    "star_registry",
]
