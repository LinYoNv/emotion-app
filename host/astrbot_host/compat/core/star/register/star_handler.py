# -*- coding: utf-8 -*-
"""`astrbot.core.star.register` 兼容层 —— **14 个装饰器**（宿主机制面）。

⭐⭐ 这是宿主**最基础**的一块：API 可以逐个补，
   **但装饰器不齐，插件【加载即失败】**（照 `PLATFORM_POSITIONING.md` §3.3）。

⭐ 依据：真 AstrBot `core/star/register/star_handler.py`（755 行）。
   这里实现陪伴实际用到的 14 个（实测清单见 §3.3）。

🔴 `register_llm_tool` 的语义**必须照真实现**：
   它**解析函数 docstring** 生成 JSON Schema（`docstring_parser.parse`），
   参数缺类型注释会**抛 ValueError** —— 这是插件的编码约定，不能放宽。
   我们没有 `docstring_parser` 依赖，所以自己写了一个最小解析器（见 `_parse_docstring`），
   **只支持真实现要求的格式**（Google 风格 `Args:` 段）。
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot_host.registry import (
    PY_TO_JSON_TYPE,
    SUPPORTED_TYPES,
    EventType,
    StarHandlerMetadata,
    llm_tools,
    star_handlers_registry,
)

from astrbot_host.compat.core.star.star_handler import (  # noqa: F401
    star_handlers_registry as _registry,
)

# ---------------------------------------------------------------------------
# Handler 注册基础设施（照真实现 get_handler_or_create）
# ---------------------------------------------------------------------------


def get_handler_full_name(awaitable: Callable[..., Any]) -> str:
    """`f"{module}_{name}"`（照真实现）。"""
    return f"{awaitable.__module__}_{awaitable.__name__}"


def get_handler_or_create(
    handler: Callable[..., Any],
    event_type: EventType,
    dont_add: bool = False,
    **kwargs: Any,
) -> StarHandlerMetadata:
    """取已有 Handler 或新建（照真实现）。

    ⚠️ **幂等**：同一函数重复注册返回同一个 metadata —— 插件可能被重载，
       不做幂等会导致 handler 重复执行（真实现也是这么处理的）。
    """
    full_name = get_handler_full_name(handler)
    md = star_handlers_registry.get_handler_by_full_name(full_name)
    if md is not None:
        return md

    md = StarHandlerMetadata(
        event_type=event_type,
        handler_full_name=full_name,
        handler_name=handler.__name__,
        handler_module_path=handler.__module__,
        handler=handler,
        event_filters=[],
    )
    if handler.__doc__:
        md.desc = handler.__doc__.strip()
    if "desc" in kwargs:
        md.desc = kwargs.pop("desc")
    md.extras_configs = kwargs

    if not dont_add:
        star_handlers_registry.append(md)
    return md


def _attach_filter(md: StarHandlerMetadata, flt: Any) -> None:
    """给 handler 挂一个过滤器（多个过滤器是 **AND** 关系，照真实现）。"""
    md.event_filters.append(flt)


# ---------------------------------------------------------------------------
# docstring 解析（替代 docstring_parser 依赖）
# ---------------------------------------------------------------------------

#: 匹配 Google 风格参数行：`    name(type): 描述` 或 `    name (type): 描述`
_PARAM_RE = re.compile(
    r"^\s*(\w+)\s*\(\s*([\w\[\]]+)\s*\)\s*:\s*(.*)$"
)


def _parse_docstring(doc: str) -> tuple[str, list[dict[str, str]]]:
    """解析 docstring → (描述, [{name, type_name, description}])。

    ⚠️ 只支持真实现要求的 Google 风格 `Args:` 段 —— 因为真实现用
       `docstring_parser` 且要求参数**必须有类型注释**，
       **缺类型就抛错**。我们的解析器要能识别同样的错误情况并**明确报错**，
       不能静默产出一个缺 schema 的工具（那会让模型调用失败且难排查）。
    """
    lines = doc.splitlines()
    if not lines:
        return "", []

    # 描述 = `Args:` 之前的非空内容
    desc_lines: list[str] = []
    for line in lines:
        if re.match(r"^\s*(Args|Arguments|参数)\s*:\s*$", line):
            break
        desc_lines.append(line)
    description = "\n".join(desc_lines).strip()

    params: list[dict[str, str]] = []
    in_args = False
    for line in lines:
        if re.match(r"^\s*(Args|Arguments|参数)\s*:\s*$", line):
            in_args = True
            continue
        if not in_args:
            continue
        # 到下一个段（Returns/Yields/...）就停
        if re.match(r"^\s*(Returns|Yields|Raises|Examples?|Note)\s*:", line):
            break
        m = _PARAM_RE.match(line)
        if m:
            params.append({
                "name": m.group(1),
                "type_name": m.group(2),
                "description": m.group(3).strip(),
            })
    return description, params


# ---------------------------------------------------------------------------
# 14 个装饰器
# ---------------------------------------------------------------------------


def register_llm_tool(name: str | None = None, **kwargs: Any):
    """注册 function-calling 工具（**陪伴用得最多：31 次**）。

    照真实现语义：
      * 工具名默认取函数名；
      * 从 docstring 的 `Args:` 解析参数（**类型必填，否则报错**）；
      * 注册进全局 LLM 工具表 + 事件表（`EventType.OnCallingFuncToolEvent`）。
    """

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name if name else awaitable.__name__
        description, params = _parse_docstring(awaitable.__doc__ or "")

        args: list[dict[str, Any]] = []
        for p in params:
            type_name = p["type_name"]
            sub_type_name = None
            if not type_name:
                raise ValueError(
                    f"LLM 函数工具 {awaitable.__module__}_{tool_name} 的参数 "
                    f"{p['name']} 缺少类型注释。"
                )
            m = re.match(r"(\w+)\[(\w+)\]", type_name)
            if m:
                type_name, sub_type_name = m.group(1), m.group(2)
            type_name = PY_TO_JSON_TYPE.get(type_name, type_name)
            if sub_type_name:
                sub_type_name = PY_TO_JSON_TYPE.get(sub_type_name, sub_type_name)

            if type_name not in SUPPORTED_TYPES or (
                sub_type_name and sub_type_name not in SUPPORTED_TYPES
            ):
                raise ValueError(
                    f"LLM 函数工具 {awaitable.__module__}_{tool_name} "
                    f"不支持的参数类型：{p['type_name']}"
                )

            schema: dict[str, Any] = {
                "type": type_name,
                "name": p["name"],
                "description": p["description"],
            }
            if sub_type_name and type_name == "array":
                schema["items"] = {"type": sub_type_name}
            args.append(schema)

        md = get_handler_or_create(awaitable, EventType.OnCallingFuncToolEvent, **kwargs)
        llm_tools.add_func(tool_name, args, description, md.handler)
        return awaitable

    return decorator


def _simple_event_decorator(event_type: EventType):
    """生成一个"只标事件类型"的装饰器（多数 `on_*` 都是这种）。"""

    def register(**kwargs: Any):
        def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
            get_handler_or_create(awaitable, event_type, **kwargs)
            return awaitable

        return decorator

    return register


#: 消息事件（`@filter.event_message_type(...)` 挂过滤器）
def register_event_message_type(event_message_type: Any = None, **kwargs: Any):
    """注册消息事件 handler（照真实现，可带消息类型过滤）。"""
    from astrbot_host.compat.core.star.filter.event_message_type import (
        EventMessageTypeFilter,
    )

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        md = get_handler_or_create(awaitable, EventType.AdapterMessageEvent, **kwargs)
        if event_message_type is not None:
            _attach_filter(md, EventMessageTypeFilter(event_message_type))
        return awaitable

    return decorator


def register_command(
    command_name: str | None = None,
    sub_command: str | None = None,
    alias: set | None = None,
    **kwargs: Any,
):
    """注册指令（**照真实现 `register/star_handler.py:75-126` 的结构重写**）。

    🔴 本轮修掉的两个错（都在这一个函数里，且互相掩盖）：

    **错误 A：判断子指令用的属性不存在** —— 我原来写
        `hasattr(command_name, "add_sub_command_filter")`
    但 `command_name` 是 `RegisteringCommandable`，**它没有这个方法**
    （方法在 `command_name.parent_group` 上）→ 守卫恒为 False
    → **`CommandFilter` 根本没被创建**。
    真实现是 `isinstance(command_name, RegisteringCommandable)`
    （`:84`），再 `command_name.parent_group.add_sub_command_filter(...)`（`:96`）。

    **错误 B：创建了也只挂父组、不挂 handler** —— 真实现 `:116-123` 的尾部是
    **裸指令与子指令共用**的：
        handler_md = get_handler_or_create(...)
        if new_command:
            new_command.init_handler_md(handler_md)
            handler_md.event_filters.append(new_command)
    我原来在子指令分支提前 `return` 掉了。

    ⚠️ **后果不是"每条消息都触发"，而是该指令【永远不执行】**：
       真实现派发时 `waking_check/stage.py:182` 是
       `if len(handler.event_filters) == 0: continue` —— 空过滤器被**跳过**。
       实测受害：`prompt_tools` 的 **16 个子指令**（`prompt presets` / `prompt view` …）。

    ⚠️ 照抄真实现的另一处细节：子指令的父名取自
       **`command_name.parent_group.get_complete_command_names()`**（`:87-89`），
       不是 `command_name.get_complete_command_names()` —— 两者在嵌套指令组下不同。
    """
    from astrbot_host.compat.core.star.filter.command import CommandFilter

    new_command: Any = None
    add_to_event_filters = False

    if isinstance(command_name, RegisteringCommandable):
        # ---- 子指令（真实现 :84-101）----
        if sub_command is not None:
            parent_command_names = command_name.parent_group.get_complete_command_names()
            new_command = CommandFilter(
                sub_command, alias, None, parent_command_names=parent_command_names
            )
            command_name.parent_group.add_sub_command_filter(new_command)
        else:
            logger.warning(
                "No sub_command argument was provided while registering a "
                "subcommand for %s.",
                command_name,
            )
    elif command_name is None:
        logger.warning(
            "No command_name argument was provided while registering a bare command."
        )
    else:
        # ---- 裸指令（真实现 :107-109）----
        new_command = CommandFilter(command_name, alias, None)
        add_to_event_filters = True

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        if not add_to_event_filters:
            # 真实现 :113-115 注释：打标记表示这是子指令，
            # 唤醒阶段这个 handler 将由父指令接管（用于 WebUI 命令列表归类）。
            kwargs["sub_command"] = True
        md = get_handler_or_create(
            awaitable, EventType.AdapterMessageEvent, **kwargs
        )
        # 🔴 裸指令与子指令**共用**的尾部（真实现 :121-123）——
        #    `init_handler_md` 先灌签名（`print_types`/`validate_and_convert_params`
        #    依赖 `handler_params`，漏了会 AttributeError），
        #    再把**同一个** filter 对象挂到 handler 的 event_filters。
        if new_command is not None:
            new_command.init_handler_md(md)
            _attach_filter(md, new_command)
        return awaitable

    return decorator


def register_command_group(
    command_group_name: Any = None,
    sub_command: str | None = None,
    alias: set | None = None,
    **kwargs: Any,
):
    """注册一个 CommandGroup（照真实现）。

    🔴 **关键语义（实测踩到，4 个插件加载失败）**：
       真实现返回的**不是** `obj`，而是 **`RegisteringCommandable(new_group)`** ——
       一个带 `.command()` / `.group()` / `.custom_filter()` 的**可级联对象**。

       插件写法是：
           @filter.command_group("记忆")
           def mem_group(): ...

           @mem_group.command("状态")      # ← 这里！返回的函数要有 .command
           async def mem_status(...): ...

       我第一版返回 `obj`（装饰器里的函数原样返回）→
       `AttributeError: 'function' object has no attribute 'command'`
       → `astrbot_plugin_memory_companion` / `prompt_tools` 直接加载失败。

    ⚠️ 另一处照真实现：装饰的函数本身**也要**注册成 handler
       （`get_handler_or_create(obj, ...)` + 挂 group 过滤器）。
    """
    from astrbot_host.compat.core.star.filter.command_group import CommandGroupFilter

    new_group: CommandGroupFilter | None = None
    if isinstance(command_group_name, RegisteringCommandable):
        # 子指令组（照真实现 —— 挂在父组下）
        if sub_command is None:
            logger.warning(
                "No sub_command was specified for a subgroup of command group %s.",
                command_group_name,
            )
        else:
            new_group = CommandGroupFilter(
                sub_command, alias,
                parent_group=command_group_name.parent_group,
            )
            command_group_name.parent_group.add_sub_command_filter(new_group)
    elif command_group_name is None:
        logger.warning("No name was specified for the root command group.")
    else:
        new_group = CommandGroupFilter(command_group_name, alias)

    def decorator(obj: Any) -> Any:
        if new_group is None:
            raise ValueError("注册指令组失败。")
        md = get_handler_or_create(obj, EventType.AdapterMessageEvent, **kwargs)
        md.event_filters.append(new_group)
        return RegisteringCommandable(new_group)

    return decorator


class RegisteringCommandable:
    """用于指令组级联注册（照真实现）。

    ⚠️ 类属性故意指向**模块级函数**（照真实现）——
       这样 `@grp.command(...)` / `@grp.group(...)` 都能继续级联。
    """

    group: Any = None       # 在文件末尾绑定（避免前向引用问题）
    command: Any = None
    custom_filter: Any = None

    def __init__(self, parent_group: Any) -> None:
        self.parent_group = parent_group


def register_permission_type(permission_type: Any = None, raise_error: bool = True, **kwargs: Any):
    """注册权限要求（照真实现）。"""
    from astrbot_host.compat.core.star.filter.permission import PermissionTypeFilter

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        md = get_handler_or_create(awaitable, EventType.AdapterMessageEvent, **kwargs)
        if permission_type is not None:
            _attach_filter(md, PermissionTypeFilter(permission_type, raise_error))
        return awaitable

    return decorator


def register_platform_adapter_type(
    platform_adapter_type_or_str: Any = None, **kwargs: Any
):
    """按平台适配器类型注册（照真实现）。"""
    from astrbot_host.compat.core.star.filter.platform_adapter_type import (
        PlatformAdapterTypeFilter,
    )

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        md = get_handler_or_create(awaitable, EventType.AdapterMessageEvent, **kwargs)
        if platform_adapter_type_or_str is not None:
            _attach_filter(
                md, PlatformAdapterTypeFilter(platform_adapter_type_or_str)
            )
        return awaitable

    return decorator


def register_regex(regex: str | None = None, **kwargs: Any):
    """按正则匹配注册（照真实现）。"""
    from astrbot_host.compat.core.star.filter.regex import RegexFilter

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        md = get_handler_or_create(awaitable, EventType.AdapterMessageEvent, **kwargs)
        if regex:
            _attach_filter(md, RegexFilter(regex))
        return awaitable

    return decorator


def register_custom_filter(custom_filter: Any = None, **kwargs: Any):
    """自定义过滤器（照真实现）。"""

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        md = get_handler_or_create(awaitable, EventType.AdapterMessageEvent, **kwargs)
        if custom_filter is not None:
            _attach_filter(md, custom_filter)
        return awaitable

    return decorator


# ---- 生命周期与 LLM 钩子（都只是"标事件类型"）----

register_on_astrbot_loaded = _simple_event_decorator(EventType.OnAstrBotLoadedEvent)
register_on_platform_loaded = _simple_event_decorator(EventType.OnPlatformLoadedEvent)
register_on_waiting_llm_request = _simple_event_decorator(
    EventType.OnWaitingLLMRequestEvent
)
register_on_llm_request = _simple_event_decorator(EventType.OnLLMRequestEvent)
register_on_llm_response = _simple_event_decorator(EventType.OnLLMResponseEvent)
register_on_agent_begin = _simple_event_decorator(EventType.OnAgentBeginEvent)
register_on_agent_done = _simple_event_decorator(EventType.OnAgentDoneEvent)
register_on_decorating_result = _simple_event_decorator(
    EventType.OnDecoratingResultEvent
)
register_on_using_llm_tool = _simple_event_decorator(EventType.OnUsingLLMToolEvent)
register_on_llm_tool_respond = _simple_event_decorator(
    EventType.OnLLMToolRespondEvent
)
register_after_message_sent = _simple_event_decorator(
    EventType.OnAfterMessageSentEvent
)
register_on_plugin_error = _simple_event_decorator(EventType.OnPluginErrorEvent)
register_on_plugin_loaded = _simple_event_decorator(EventType.OnPluginLoadedEvent)
register_on_plugin_unloaded = _simple_event_decorator(EventType.OnPluginUnloadedEvent)


def register_agent(
    name: str,
    instruction: str,
    tools: list[Any] | None = None,
    run_hooks: Any = None,
):
    """注册一个 Agent（照真实现签名）。

    ⚠️ 真实现构造 `Agent` 对象并登记；我们的宿主**还没有 agent 抽象**，
       所以这里**显式报错**而不是假装成功 —— 假装成功会让插件以为
       agent 已就绪，实际调用时在别处失败，更难排查。
    """
    raise NotImplementedError(
        f"宿主兼容层尚未实现 `register_agent`（agent={name!r}）。"
        "陪伴的功能若依赖 Agent，请先在宿主侧接入 agent 运行器。"
    )


def register_star(star: Any = None, **kwargs: Any):
    """注册插件（Star）本身 —— 真实现里给 `register` 用。"""
    if star is None:
        def decorator(cls: Any) -> Any:
            return cls

        return decorator
    return star


# 照真实现：RegisteringCommandable 的类属性指向模块级函数（级联注册）
RegisteringCommandable.group = register_command_group
RegisteringCommandable.command = register_command
RegisteringCommandable.custom_filter = register_custom_filter


__all__ = [
    "RegisteringCommandable",
    "get_handler_full_name",
    "register_agent",
    "get_handler_or_create",
    "register_after_message_sent",
    "register_command",
    "register_command_group",
    "register_custom_filter",
    "register_event_message_type",
    "register_llm_tool",
    "register_on_agent_begin",
    "register_on_agent_done",
    "register_on_astrbot_loaded",
    "register_on_decorating_result",
    "register_on_llm_request",
    "register_on_llm_response",
    "register_on_llm_tool_respond",
    "register_on_platform_loaded",
    "register_on_plugin_error",
    "register_on_plugin_loaded",
    "register_on_plugin_unloaded",
    "register_on_using_llm_tool",
    "register_on_waiting_llm_request",
    "register_permission_type",
    "register_platform_adapter_type",
    "register_regex",
    "register_star",
]
