# -*- coding: utf-8 -*-
"""`astrbot.core.star.filter.command` 兼容层 —— 标准指令过滤器。

⭐⭐ **本文照真 AstrBot `core/star/filter/command.py`（220 行）逐条移植**。

🔴 **为什么重写**（子代理差分验证抓到的真问题）：
   我第一版是**"幻影实现"** —— 只按"看起来该有什么"写了 `command_name`/`alias`
   和一个简化 `filter()`，**缺了真实现的核心机制**：
   `print_types` / `init_handler_md` / `handler_params` /
   `validate_and_convert_params` / `GreedyStr` / `unwrap_optional` /
   `custom_filter_ok` / `equals`。

   后果很实际：
   * `CommandGroupFilter.print_cmd_tree()` 调 `print_types()` → **AttributeError**
   * `register_command` 按真实现传 `handler_md=` → 我旧版参数名是 `cmd_handler` → **TypeError**

   → 教训：**叶子类也不能"照感觉写"** —— 真源码就在磁盘上，必须读。
     （项目铁律「工具的空输出/想象 ≠ 事实」的又一例。）
"""
from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any

from astrbot_host.compat.core.star.filter import HandlerFilter

if typing.TYPE_CHECKING:
    from astrbot_host.compat.core.star.star_handler import StarHandlerMetadata

    from .custom_filter import CustomFilter


class GreedyStr(str):
    """标记指令完成其他参数接收后的所有剩余文本（照真实现）。"""


def unwrap_optional(annotation: Any) -> tuple:
    """去掉 `Optional[T]` / `Union[T, None]` / `T|None`，返回 `T`（照真实现）。"""
    args = typing.get_args(annotation)
    non_none_args = [a for a in args if a is not type(None)]  # noqa: E721
    if len(non_none_args) == 1:
        return (non_none_args[0],)
    if len(non_none_args) > 1:
        return tuple(non_none_args)
    return ()


# 标准指令受到 wake_prefix 的制约（照真实现注释）
class CommandFilter(HandlerFilter):
    """标准指令过滤器（照真实现）。"""

    def __init__(
        self,
        command_name: str,
        alias: set | None = None,
        handler_md: StarHandlerMetadata | None = None,
        parent_command_names: list[str] | None = None,
    ) -> None:
        self.command_name = command_name
        self.alias = alias if alias else set()
        self._original_command_name = command_name
        self.parent_command_names = (
            parent_command_names if parent_command_names is not None else [""]
        )
        if handler_md:
            self.init_handler_md(handler_md)
        self.custom_filter_list: list[CustomFilter] = []
        #: 完整指令名缓存（照真实现）
        self._cmpl_cmd_names: list | None = None

    def print_types(self) -> str:
        """打印参数签名（`CommandGroupFilter.print_cmd_tree` 依赖它）。"""
        parts = []
        for k, v in self.handler_params.items():
            if isinstance(v, type):
                parts.append(f"{k}({v.__name__}),")
            elif isinstance(v, types.UnionType) or typing.get_origin(v) is typing.Union:
                parts.append(f"{k}({v}),")
            else:
                parts.append(f"{k}({type(v).__name__})={v},")
        return "".join(parts).rstrip(",")

    def init_handler_md(self, handle_md: StarHandlerMetadata) -> None:
        """从 handler 签名提取参数表（照真实现 —— 跳过前两个参数 `self`/`event`）。"""
        self.handler_md = handle_md
        signature = inspect.signature(self.handler_md.handler, eval_str=True)
        self.handler_params = {}      # 参数名 → 参数类型；有默认值则为默认值
        idx = 0
        for k, v in signature.parameters.items():
            if idx < 2:
                idx += 1
                continue
            if v.default == inspect.Parameter.empty:
                self.handler_params[k] = v.annotation
            else:
                self.handler_params[k] = v.default

    def get_handler_md(self) -> StarHandlerMetadata:
        return self.handler_md

    def add_custom_filter(self, custom_filter: CustomFilter) -> None:
        self.custom_filter_list.append(custom_filter)

    def custom_filter_ok(self, event: Any, cfg: Any) -> bool:
        for custom_filter in self.custom_filter_list:
            if not custom_filter.filter(event, cfg):
                return False
        return True

    def validate_and_convert_params(
        self,
        params: list[Any],
        param_type: dict[str, type],
    ) -> dict[str, Any]:
        """把参数列表按类型转换成参数字典（照真实现）。"""
        result: dict[str, Any] = {}
        param_items = list(param_type.items())
        for i, (param_name, param_type_or_default_val) in enumerate(param_items):
            is_greedy = param_type_or_default_val is GreedyStr

            if is_greedy:
                # GreedyStr 必须是最后一个参数
                if i != len(param_items) - 1:
                    raise ValueError(
                        f"参数 '{param_name}' (GreedyStr) 必须是最后一个参数。",
                    )
                result[param_name] = " ".join(params[i:])
                break

            if i >= len(params):
                if (
                    isinstance(param_type_or_default_val, type | types.UnionType)
                    or typing.get_origin(param_type_or_default_val) is typing.Union
                    or param_type_or_default_val is inspect.Parameter.empty
                ):
                    raise ValueError(
                        f"必要参数缺失。该指令完整参数: {self.print_types()}",
                    )
                result[param_name] = param_type_or_default_val
            else:
                try:
                    if param_type_or_default_val is None:
                        if params[i].isdigit():
                            result[param_name] = int(params[i])
                        else:
                            result[param_name] = params[i]
                    elif isinstance(param_type_or_default_val, str):
                        result[param_name] = params[i]
                    elif isinstance(param_type_or_default_val, bool):
                        lower_param = str(params[i]).lower()
                        if lower_param in ["true", "yes", "1"]:
                            result[param_name] = True
                        elif lower_param in ["false", "no", "0"]:
                            result[param_name] = False
                        else:
                            raise ValueError(
                                f"参数 {param_name} 必须是布尔值（true/false, yes/no, 1/0）。",
                            )
                    elif isinstance(param_type_or_default_val, int):
                        result[param_name] = int(params[i])
                    elif isinstance(param_type_or_default_val, float):
                        result[param_name] = float(params[i])
                    else:
                        origin = typing.get_origin(param_type_or_default_val)
                        if origin in (typing.Union, types.UnionType):
                            nn_types = unwrap_optional(param_type_or_default_val)
                            if len(nn_types) == 1:
                                result[param_name] = nn_types[0](params[i])
                            else:
                                result[param_name] = params[i]
                        else:
                            result[param_name] = param_type_or_default_val(params[i])
                except ValueError:
                    raise ValueError(
                        f"参数 {param_name} 类型错误。完整参数: {self.print_types()}",
                    ) from None
        return result

    def get_complete_command_names(self) -> list[str]:
        """完整指令名（父链 × 别名 的笛卡尔积，照真实现 —— 结果带缓存）。"""
        if self._cmpl_cmd_names is not None:
            return self._cmpl_cmd_names
        self._cmpl_cmd_names = [
            f"{parent} {cmd}" if parent else cmd
            for cmd in [self.command_name] + list(self.alias)
            for parent in self.parent_command_names or [""]
        ]
        return self._cmpl_cmd_names

    def equals(self, message_str: str) -> bool:
        return any(message_str == full for full in self.get_complete_command_names())

    def filter(self, event: Any, cfg: Any) -> bool:
        """是否命中该指令（照真实现 —— 并把解析结果写进 `event.parsed_params`）。"""
        if not event.is_at_or_wake_command:
            return False

        if not self.custom_filter_ok(event, cfg):
            return False

        message_str = re.sub(r"\s+", " ", event.get_message_str().strip())
        ok = False
        for full_cmd in self.get_complete_command_names():
            if message_str.startswith(f"{full_cmd} ") or message_str == full_cmd:
                ok = True
                message_str = message_str[len(full_cmd):].strip()
        if not ok:
            return False

        ls = [param for param in message_str.split(" ") if param]
        try:
            params = self.validate_and_convert_params(ls, self.handler_params)
        except ValueError as e:
            raise e

        event.set_extra("parsed_params", params)
        return True


__all__ = ["CommandFilter", "GreedyStr", "unwrap_optional"]
