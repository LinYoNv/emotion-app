# -*- coding: utf-8 -*-
"""事件总线：`EventType` / `StarHandlerMetadata` / `StarHandlerRegistry`。

⭐⭐ **语义照真 AstrBot 实现**（不是猜的）——依据：
   `/root/.local/share/uv/tools/astrbot/.../astrbot/core/star/star_handler.py`

为什么要有这一层（见 `爱语/docs/PLATFORM_POSITIONING.md` §3.0）：
   作者的设计里 AstrBot 只是「**首个宿主适配器**」，`embedded_app` 是一等部署模式。
   所以我们要提供的是**行为等价的宿主**，而不是把 AstrBot 抄一遍。

🔴 移植时的差异（**故意保留的**）：
   * 不依赖 AstrBot 的任何内部对象（`logger`/`html_renderer`/`FunctionTool`…）；
   * `priority` 排序语义**与真实现一致**（数值越大越先执行，见 `append`）；
   * `__lt__` 保留 —— 真实现用它支持优先队列，插件可能依赖。
"""
from __future__ import annotations

import copy
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

#: Handler 的可调用形态（异步函数或异步生成器）
H = TypeVar("H", bound=Callable[..., Any])


class EventType(enum.Enum):
    """AstrBot 内部事件类型。

    ⚠️ **成员名必须与真实现逐字一致** —— 插件会 `from ... import EventType` 并按名比较。
       真实现共 16 个（实测），这里全部保留。
    """

    OnAstrBotLoadedEvent = enum.auto()      # AstrBot 加载完成
    OnPlatformLoadedEvent = enum.auto()     # 平台加载完成
    AdapterMessageEvent = enum.auto()       # 收到适配器发来的消息
    OnWaitingLLMRequestEvent = enum.auto()  # 等待调用 LLM（获取锁之前，仅通知）
    OnLLMRequestEvent = enum.auto()         # 收到 LLM 请求
    OnLLMResponseEvent = enum.auto()        # LLM 响应后
    OnAgentBeginEvent = enum.auto()         # Agent 开始运行
    OnAgentDoneEvent = enum.auto()          # Agent 运行完成
    OnDecoratingResultEvent = enum.auto()   # 发送消息前
    OnCallingFuncToolEvent = enum.auto()    # 调用函数工具
    OnUsingLLMToolEvent = enum.auto()       # 使用 LLM 工具
    OnLLMToolRespondEvent = enum.auto()     # 调用函数工具后
    OnAfterMessageSentEvent = enum.auto()   # 发送消息后
    OnPluginErrorEvent = enum.auto()        # 插件处理消息异常时
    OnPluginLoadedEvent = enum.auto()       # 插件加载完成
    OnPluginUnloadedEvent = enum.auto()     # 插件卸载完成


@dataclass
class StarHandlerMetadata(Generic[H]):
    """描述一个被注册的 Handler（照真实现的字段）。"""

    event_type: EventType
    handler_full_name: str
    """格式为 f"{handler.__module__}_{handler.__name__}"（照真实现）"""
    handler_name: str
    handler_module_path: str
    handler: H
    event_filters: list[Any] = field(default_factory=list)
    desc: str = ""
    extras_configs: dict = field(default_factory=dict)
    enabled: bool = True

    def __lt__(self, other: StarHandlerMetadata) -> bool:
        """照真实现：支持优先队列（priority 小的在前）。"""
        return self.extras_configs.get("priority", 0) < other.extras_configs.get(
            "priority", 0
        )

    #: 是否是被禁用的 handler（真实现用 `enabled` 控制）
    def is_enabled(self) -> bool:
        return bool(self.enabled)


class StarHandlerRegistry(Generic[H]):
    """Handler 注册表 + 事件查询。

    🔴 **排序语义必须与真实现一致**（实测 `star_handler.py:append`）：
       注册时把 `priority` 默认成 0，然后按 **`-priority`** 排序 ——
       即 **priority 越大越先执行**。
       ⚠️ 注意 `__lt__` 是升序定义，而 `append` 用的是 `-priority`；
          两者方向**不同**，这里照抄以免语义漂移。
    """

    def __init__(self) -> None:
        self.star_handlers_map: dict[str, StarHandlerMetadata] = {}
        self._handlers: list[StarHandlerMetadata] = []

    def append(self, handler: StarHandlerMetadata) -> None:
        """添加一个 Handler，并保持按优先级有序（照真实现）。"""
        if "priority" not in handler.extras_configs:
            handler.extras_configs["priority"] = 0

        self.star_handlers_map[handler.handler_full_name] = handler
        self._handlers.append(handler)
        # ⚠️ 真实现是 `sort(key=lambda h: -priority)` = 降序（大者先）
        self._handlers.sort(key=lambda h: -h.extras_configs["priority"])

    def get_handler_by_full_name(
        self, full_name: str
    ) -> StarHandlerMetadata | None:
        return self.star_handlers_map.get(full_name)

    def get_handlers_by_event_type(
        self,
        event_type: EventType,
        only_activated: bool = True,
        plugins_name: list[str] | None = None,
    ) -> list[StarHandlerMetadata]:
        """按事件类型取 handler（照真实现签名）。

        Args:
            only_activated: 只返回启用的（真实现默认 True）。
            plugins_name: 只看这些插件的 handler（None = 全部）。
        """
        out = []
        for h in self._handlers:
            if h.event_type != event_type:
                continue
            if only_activated and not h.is_enabled():
                continue
            if plugins_name is not None:
                # 真实现按模块路径的顶层包名过滤
                if h.handler_module_path.split(".")[0] not in plugins_name:
                    continue
            out.append(h)
        return out

    def get_handlers_by_module_name(
        self,
        module_name: str,
    ) -> list[StarHandlerMetadata]:
        """按**模块路径精确匹配**取 handler（真实现 `star_handler.py:191-199`）。

        ⚠️ 是**精确相等**，不是前缀匹配 —— 别和我们自己的 `remove_plugin`
           （前缀匹配，用于卸载整插件）混用。语义不同，用途也不同。
        """
        return [
            handler
            for handler in self._handlers
            if handler.handler_module_path == module_name
        ]

    # ---- 魔法方法：真实现有，插件会直接用 ----
    # 🔴 实测教训：我们原来只有自造的 `all_handlers()`，
    #    而真实现里根本没有这个方法名 —— 插件不会去调它。
    #    真 AstrBot 自己就多处**直接迭代**注册表：
    #      * `core/platform/sources/telegram/tg_adapter.py:344`
    #      * `core/platform/sources/discord/discord_platform_adapter.py:412`
    #      * `core/star/command_management.py:256` / `:381`
    #    缺 `__iter__` 的后果**实测到了**：服务启动日志里出现
    #      `修复热更新残留回调绑定失败: 'StarHandlerRegistry' object is not iterable`
    #    —— 插件用 try/except 兜住了，所以**不报错、只降级**（又一类静默失败）。

    def __iter__(self):
        """迭代全部 handler（照真实现 —— 直接暴露内部列表）。"""
        return iter(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)

    # ---- 运维/诊断（宿主要能看清自己注册了什么）----

    def all_handlers(self) -> list[StarHandlerMetadata]:
        """取全部 handler 的**副本**（自造诊断方法，真实现无此名）。"""
        return list(self._handlers)

    def count_by_event(self) -> dict[str, int]:
        """按事件类型统计 —— 用来验证"插件真的注册上了"。"""
        out: dict[str, int] = {}
        for h in self._handlers:
            out[h.event_type.name] = out.get(h.event_type.name, 0) + 1
        return out

    def remove(self, handler: "StarHandlerMetadata") -> None:
        """移除**单个** handler（照真实现 `star_handler.py:205-207`）。

        🔴 **2026-10-19 补：这是陪伴 7.0.0 加载失败的直接原因**

        `astrbot_host/import_lifecycle.py:28-32` 在**模块顶层**执行：

            for handler in tuple(star_handlers_registry):
                if handler.handler_module_path == entry_name:
                    star_handlers_registry.remove(handler)

        它的意图是"重新 import 前先清掉上一次注册的 handler"
        （注释原文：*A fresh import must replace those handlers before
        the decorators register again*）。

        我们原来**只有 `remove_plugin(前缀)`、没有 `remove(handler)`**
        ⇒ 报 `AttributeError: 'StarHandlerRegistry' object has no attribute 'remove'`
        ⇒ **整个插件加载失败**。

        ⚠️ 与 `remove_plugin` 的区别：那个按**模块前缀**批量删，
        这个删**特定那个对象**。两者都保留 —— 批量删是我们自己的便利方法，
        `remove(handler)` 是真实现在用的契约，**不能互相替代**
        （按前缀删会把同模块的其它 handler 一起干掉）。
        """
        # 照真实现：先从 map 里按 full_name 摘掉，再按**身份**过滤列表
        # ⚠️ 用 `!=`（真实现是 `h != handler`）—— dataclass 的默认相等比较，
        #    与真实现行为一致；不是 `is not`。
        self.star_handlers_map.pop(getattr(handler, "handler_full_name", None), None)
        self._handlers = [h for h in self._handlers if h != handler]

    def remove_plugin(self, module_path_prefix: str) -> int:
        """移除某插件的全部 handler（重载/卸载用）。返回移除数量。"""
        before = len(self._handlers)
        self._handlers = [
            h for h in self._handlers
            if not h.handler_module_path.startswith(module_path_prefix)
        ]
        for name in [
            n for n, h in self.star_handlers_map.items()
            if h.handler_module_path.startswith(module_path_prefix)
        ]:
            self.star_handlers_map.pop(name, None)
        return before - len(self._handlers)

    def clear(self) -> None:
        self._handlers.clear()
        self.star_handlers_map.clear()


#: 全局注册表（真实现也是模块级单例，插件可能直接 import 它）
star_handlers_registry: StarHandlerRegistry = StarHandlerRegistry()


# ---------------------------------------------------------------------------
# LLM 工具表（`@filter.llm_tool` 注册到这里）
# ---------------------------------------------------------------------------


@dataclass
class FunctionToolSpec:
    """一个 function-calling 工具的描述（由 docstring 解析而来）。

    ## 🔴 2026-09-22 补两个真实现字段（`active` / `handler_module_path`）

    真实现的 `FuncTool` 就是 `FunctionTool`（`agent/tool.py:36-71`），因此**有**
    `active` 与 `handler_module_path`，并且 `star_manager.py:1273-1299` 靠它们做两件事：

    ① **按模块归属绑定插件实例** —— 遍历 `llm_tools.func_list`，
       对 `handler.__module__ == 插件模块` 的项把 `handler` 换成
       `functools.partial(raw_handler, 插件实例)`；
    ② **按启用状态置 `active`** —— `ft.active = (not plugin_disabled
       and ft.name not in inactivated_llm_tools)`。

    ⚠️ 我们**两个都缺**时的真实后果（2026-09-22 实测，读我们自己的源码看不出来）：
      * handler 是**裸函数**（签名 `(self, event, ...)`）→ 谁调它都必错
        （实测 `llm_tools.get_func("pc_generate_photo").handler` 是裸函数）；
      * 没有 `active` → 无法表达"这个工具被停用了"，
        真实现的 `get_full_tool_set()` + 插件侧过滤都读它。
    """

    name: str
    #: 🔴 **JSON Schema dict**（照真实现 `FunctionTool.parameters`）。
    #: ⚠️ 2026-10-19 前这里存的是 `list[dict]`（参数描述列表），已改；
    #: `to_openai_schema()` 仍兼容 list，见 `json_schema()` 的 docstring。
    parameters: dict[str, Any] | list[dict[str, Any]]
    description: str
    handler: Callable[..., Any]
    #: 是否启用（照真实现 `FunctionTool.active`，默认 True）
    active: bool = True
    #: handler 所属插件模块（照真实现，用于**幂等**重绑时按归属匹配）
    handler_module_path: str | None = None

    def json_schema(self) -> dict[str, Any]:
        """参数部分的 **JSON Schema**（= 真实现 `FunctionTool.parameters` 的形状）。

        ⚠️ **2026-10-19 改：`self.parameters` 现在也是 JSON Schema dict**

        原来这里写的是"`self.parameters` 是参数描述列表，`json_schema()` 产出
        JSON Schema"。那个设计是错的 —— 真实现的 `FunctionTool.parameters`
        **直接就是** JSON Schema dict（`astrbot/core/agent/tool.py:36-71` +
        `func_tool_manager.py:343-363` 的 `spec_to_func()`）：
        插件、`openai_schema()`、工具执行器的参数过滤**全都按 dict 读**。

        保留 list 的实际代价（2026-10-19 实测）：`memory_companion` 的
        `initialize()` 崩在 `tool.parameters.get(...)` —— 一个插件初始化失败，
        而报错完全指不到根因。

        ⇒ 现在 `FunctionToolSpec.parameters` **恒为**
        `{"type": "object", "properties": {...}}`。
        本方法与 `to_openai_schema()` **兼容两种形状**：既接受老的 list
        （外部构造 / 历史判据），也接受新的 dict（所有正常注册路径）。
        """
        return self.to_openai_schema()["function"]["parameters"]

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI `tools` 格式 —— 宿主的模型调用需要它。

        ⚠️ **两种形状都接受**（见 `json_schema()` 的 docstring）：
          * `parameters` 是 **dict**（正常注册路径，`add_func` 已转换）→ 直接用；
          * `parameters` 是 **list**（外部构造 / 历史判据）→ 就地转成 schema。
        """
        parameters = self.parameters
        if isinstance(parameters, dict):
            # 已是 JSON Schema：补一个 `required`（真实现要求每个参数都必填，
            # 见 `spec_to_func()` —— 它把 func_args 的每一项都当必填）
            schema = copy.deepcopy(parameters)
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            if "required" not in schema and schema["properties"]:
                schema["required"] = list(schema["properties"].keys())
        else:
            props: dict[str, Any] = {}
            required: list[str] = []
            for p in parameters or []:
                if not isinstance(p, dict):
                    raise TypeError(
                        f"工具 {self.name} 的 parameters 里出现了非 dict 项："
                        f"{type(p).__name__} —— 这是注册方写错了，不是格式偏好问题"
                    )
                entry: dict[str, Any] = {"type": p.get("type", "string")}
                if p.get("description"):
                    entry["description"] = p["description"]
                if p.get("items"):
                    entry["items"] = p["items"]
                name = p.get("name")
                if name:
                    props[name] = entry
                    required.append(name)
            schema = {
                "type": "object",
                "properties": props,
                "required": required,
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class FunctionToolManager:
    """LLM 工具管理器（对真实现 `FunctionToolManager` 的最小等价物）。

    ⚠️ 真实现还有 `activate/deactivate` 等；这里只实现**陪伴实际用到**的部分，
       缺的能力**显式报错**而不是静默返回空（照项目铁律「静默失败比报错贵」）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, FunctionToolSpec] = {}

    def add_func(
        self,
        name: str,
        args: list[dict[str, Any]],
        desc: str,
        handler: Callable[..., Any],
    ) -> None:
        """注册一个 function-calling 工具（**语义照真实现 `add_func`**）。

        🔴 **2026-10-19 修的一个真 bug（`memory_companion` 初始化崩溃的根因）**

        原来这里写的是 `parameters=args` —— 把 `@filter.llm_tool` 的
        **参数列表**原样塞进了 `parameters`。而真实现的 `add_func`
        （`core/provider/func_tool_manager.py:365-390`）会先走
        `spec_to_func()`（`:343-363`）把它转成 JSON Schema：

            params = {"type": "object", "properties": {}}
            for param in func_args:
                p = copy.deepcopy(param); p.pop("name", None)
                params["properties"][param["name"]] = p

        **为什么这是真 bug 而不是格式偏好**：
          * 模型看到的 `tools[].function.parameters` 会是一个 **list**，
            而 OpenAI 协议要求它是 **object** ⇒ 严格上游直接 400；
          * 插件侧会按 dict 读 `tool.parameters.get("properties", {})`
            —— 实测 `astrbot_plugin_memory_companion/core/query_session.py:550`
            就崩在 `'list' object has no attribute 'get'`，
            表现成「插件 initialize() 失败」，而根因离它三层；
          * `openai_schema()` 里 `parameters.get("properties")` 同样会炸。

        ⚠️ 与真实现一致的两处细节：
          ① **`name` 键要从属性里删掉**（它只是我们的内部占位，不属于 JSON Schema）；
          ② 用 `copy.deepcopy`，不要改调用方传进来的那批 dict
             （真实现也 deepcopy —— 否则插件复用同一个参数表会互相污染）。
        """
        params: dict[str, Any] = {"type": "object", "properties": {}}
        for param in args or []:
            p = copy.deepcopy(param)
            # 照真实现：`name` 只用于作 properties 的键，不出现在值里
            key = p.pop("name", None)
            if key:
                params["properties"][key] = p
        self._tools[name] = FunctionToolSpec(
            name=name, parameters=params, description=desc, handler=handler
        )

    def get_func(self, name: str) -> FunctionToolSpec | None:
        return self._tools.get(name)

    def get_all(self) -> list[FunctionToolSpec]:
        return list(self._tools.values())

    @property
    def func_list(self) -> list[FunctionToolSpec]:
        """照真实现 `func_list`（`func_tool_manager.py:290`）。

        🔴 **为什么需要它**：真实现 `star_manager.py:1273-1299` 是
        **遍历 `llm_tools.func_list`** 给每个工具绑插件实例的。
        没有这个属性 → 绑定的那一步根本没法照抄 → handler 永远是裸函数。
        """
        return list(self._tools.values())

    def get_tool(self, name: str) -> FunctionToolSpec | None:
        """照真实现的方法名（`get_func` 是它的旧名，我们两个都留）。"""
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def remove_func(self, name: str) -> None:
        """照真实现 `:392-397`（`add_func` 前会先调它做去重）。"""
        self._tools.pop(name, None)

    def get_full_tool_set(self) -> Any:
        """完整工具集（照真实现 `func_tool_manager.py:496-513`）。

        真实现用 `ToolSet.add_tool` 逐个填充（同名去重规则见 `ToolSet.add_tool`），
        并对非内置工具套一层权限守卫 —— 我们**如实不实现权限层**
        （单用户自用，没有多用户权限模型；`_PermissionGuardedTool` 对我们是空壳）。

        ⚠️ **只收 `active=True` 的工具**？**不** —— 真实现是**全收**，
        由调用方（`astr_main_agent.py:606-609`）再按 `active` 过滤删除。
        这里照抄"全收"，否则调用方的过滤逻辑就成了死代码。
        """
        from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

        tool_set = ToolSet()
        for spec in self._tools.values():
            tool_set.add_tool(
                FunctionTool(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.json_schema(),
                    handler=spec.handler,
                    handler_module_path=spec.handler_module_path,
                    active=spec.active,
                )
            )
        return tool_set

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def clear(self) -> None:
        self._tools.clear()


#: 全局工具表（真实现里 `llm_tools` 也是模块级单例）
llm_tools: FunctionToolManager = FunctionToolManager()


# ---------------------------------------------------------------------------
# 类型映射（照真实现 `PY_TO_JSON_TYPE`）
# ---------------------------------------------------------------------------

#: Python 类型名 → JSON Schema 类型名（docstring 里写的类型）
PY_TO_JSON_TYPE: dict[str, str] = {
    "str": "string",
    "string": "string",
    "int": "number",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "dict": "object",
    "object": "object",
}

#: 支持的 JSON Schema 类型（照真实现 `SUPPORTED_TYPES`）
SUPPORTED_TYPES: tuple[str, ...] = (
    "string", "number", "object", "array", "boolean",
)
