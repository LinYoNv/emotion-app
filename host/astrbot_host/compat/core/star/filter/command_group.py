# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.command_group` 兼容层 —— 指令组过滤器。

⭐ 依据：真 AstrBot `core/star/filter/command_group.py`（实测 135 行）。

⚠️ **为什么必须是独立文件**（真实现把 command 与 command_group 分成两个模块）：
   插件按 `from astrbot.core.star.filter.command_group import CommandGroupFilter`
   独立 import；把它并进 `command.py` 会让那条 import 直接失败。

⚠️ **相对真实现省略了什么**：
   * 只把 `CustomFilter` 的 import 收到 `TYPE_CHECKING`。理由：真实现里它在
     **运行时并不需要** —— 只出现在 `list[CustomFilter]` 注解里，实际调用是
     鸭子类型的 `custom_filter.filter(event, cfg)`（见 `custom_filter_ok`）。
     而 `CommandFilter` 必须**运行时** import：`print_cmd_tree` 用
     `isinstance(sub_filter, CommandFilter)` 分流，换成鸭子类型会让
     「指令」与「指令组」的遍历分支走错。
   * 逻辑零删减：`_cmpl_cmd_names` 缓存、`get_complete_command_names` 里
     「根节点**不写缓存**」的写法、`print_cmd_tree` 的树形前缀、
     `filter()` 里「只输入组名 → 抛 ValueError 列出子指令」的报错，
     全部照抄 —— 这些正是插件作者依赖的可观测行为。

⚠️ 两个**照抄而不是"修正"**的地方（改了就是语义漂移）：
   * `filter()` 读的是 `event.message_str`（属性），
     而 `CommandFilter.filter` 读的是 `event.get_message_str()`（方法）——
     真实现两处不一致，**不要**顺手统一。
   * `filter()` 末尾 `return self.startswith(event.message_str)` **没有 strip**，
     而上一行比 `equals` 时 strip 了。保留原样。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import HandlerFilter
from .command import CommandFilter

if TYPE_CHECKING:  # pragma: no cover - 仅给类型检查器看
    from astrbot_host.compat.core.config.astrbot_config import AstrBotConfig
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent

    from .custom_filter import CustomFilter


# 指令组受到 wake_prefix 的制约。
class CommandGroupFilter(HandlerFilter):
    def __init__(
        self,
        group_name: str,
        alias: set | None = None,
        parent_group: CommandGroupFilter | None = None,
    ) -> None:
        self.group_name = group_name
        self.alias = alias if alias else set()
        self._original_group_name = group_name
        self.sub_command_filters: list[CommandFilter | CommandGroupFilter] = []
        self.custom_filter_list: list[CustomFilter] = []
        self.parent_group = parent_group

        # Cache for complete command names list
        self._cmpl_cmd_names: list | None = None

    def add_sub_command_filter(
        self,
        sub_command_filter: CommandFilter | CommandGroupFilter,
    ) -> None:
        self.sub_command_filters.append(sub_command_filter)

    def add_custom_filter(self, custom_filter: CustomFilter) -> None:
        self.custom_filter_list.append(custom_filter)

    def get_complete_command_names(self) -> list[str]:
        """遍历父节点获取完整的指令名。

        新版本 v3.4.29 采用预编译指令，不再从指令组递归遍历子指令，因此这个方法是返回包括别名在内的整个指令名列表。
        """
        if self._cmpl_cmd_names is not None:
            return self._cmpl_cmd_names

        parent_cmd_names = (
            self.parent_group.get_complete_command_names() if self.parent_group else []
        )

        if not parent_cmd_names:
            # 根节点
            return [self.group_name] + list(self.alias)

        result = []
        candidates = [self.group_name] + list(self.alias)
        for parent_cmd_name in parent_cmd_names:
            for candidate in candidates:
                result.append(parent_cmd_name + " " + candidate)
        self._cmpl_cmd_names = result
        return result

    # 以树的形式打印出来
    def print_cmd_tree(
        self,
        sub_command_filters: list[CommandFilter | CommandGroupFilter],
        prefix: str = "",
        event: AstrMessageEvent | None = None,
        cfg: AstrBotConfig | None = None,
    ) -> str:
        parts = []
        for sub_filter in sub_command_filters:
            if isinstance(sub_filter, CommandFilter):
                custom_filter_pass = True
                if event and cfg:
                    custom_filter_pass = sub_filter.custom_filter_ok(event, cfg)
                if custom_filter_pass:
                    cmd_th = sub_filter.print_types()
                    line = f"{prefix}├── {sub_filter.command_name}"
                    if cmd_th:
                        line += f" ({cmd_th})"
                    else:
                        line += " (无参数指令)"

                    if sub_filter.handler_md and sub_filter.handler_md.desc:
                        line += f": {sub_filter.handler_md.desc}"

                    parts.append(line + "\n")
            elif isinstance(sub_filter, CommandGroupFilter):
                custom_filter_pass = True
                if event and cfg:
                    custom_filter_pass = sub_filter.custom_filter_ok(event, cfg)
                if custom_filter_pass:
                    parts.append(f"{prefix}├── {sub_filter.group_name}\n")
                    parts.append(
                        sub_filter.print_cmd_tree(
                            sub_filter.sub_command_filters,
                            prefix + "│   ",
                            event=event,
                            cfg=cfg,
                        )
                    )

        return "".join(parts)

    def custom_filter_ok(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        for custom_filter in self.custom_filter_list:
            if not custom_filter.filter(event, cfg):
                return False
        return True

    def startswith(self, message_str: str) -> bool:
        return message_str.startswith(tuple(self.get_complete_command_names()))

    def equals(self, message_str: str) -> bool:
        return message_str in self.get_complete_command_names()

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if not event.is_at_or_wake_command:
            return False

        # 判断当前指令组的自定义过滤器
        if not self.custom_filter_ok(event, cfg):
            return False

        if self.equals(event.message_str.strip()):
            tree = (
                self.group_name
                + "\n"
                + self.print_cmd_tree(self.sub_command_filters, event=event, cfg=cfg)
            )
            raise ValueError(
                f"参数不足。{self.group_name} 指令组下有如下指令，请参考：\n" + tree,
            )

        return self.startswith(event.message_str)


__all__ = ["CommandGroupFilter"]
