# -*- coding: utf-8 -*-
"""`astrbot.core.agent.tool` 兼容层 —— `FunctionTool` / `ToolSet` / `ToolSchema`。

⭐⭐ **为什么必须补这一块**（2026-09-21 实测，真 bug）：

陪伴插件用 `ToolSet` 把工具交给模型做 function calling，例如
`page_api.py:4199`（参考图试跑）、`page_api.py:4217` 构造 `ToolSet([trial_tool])`。

我们**没有这个模块**时的真实表现（对**真服务**发一次请求拍到的）：

```json
{"error": "No module named 'astrbot.core.agent.tool'",
 "error_stage": "tool_decision", "tool_status": "model_error"}
```

⚠️ 插件把它包在 `except Exception` 里 → **只记一行 info 日志**，
面板上表现为"模型判断失败"，**完全指不到根因**
（看起来像模型/网络问题，实际是宿主缺模块）。
→ 这正是本项目最贵的那类 bug：**兼容层缺口靠读源码会漏，
   必须把插件真跑起来看行为**（AGENTS.md 已记录的通则）。

## 照真实现抄的三个形状（`astrbot/core/agent/tool.py`）

| 类 | 关键契约 | 出处 |
|---|---|---|
| `ToolSchema` | 字段 `name` / `description` / `parameters`（JSON Schema） | `:19-34` |
| `FunctionTool` | 继承 `ToolSchema`，加 `handler` / `handler_module_path` / `active` / `is_background_task` | `:36-71` |
| `ToolSet` | 字段 `tools: list`；`openai_schema()` 产出**上游要的**格式 | `:78-217` |

🔴 **`openai_schema()` 的输出格式必须逐字对齐**（`:203-217`）：

```python
{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
```

* `description` **为空则整个键不出现**（不是空串）；
* `parameters` 为 `None` → 不出现；`omit_empty_parameter_field=True`
  且 `properties` 为空 → 也不出现。

⚠️ 这个格式直接进上游请求体的 `tools` 字段 —— 差一个键就是**上游 400**
或模型收不到工具（功能静默失效）。

## 与真实现的**有意差异**（如实记录，不假装一致）

* 真实现用 `pydantic.dataclasses` + `jsonschema` 校验 `parameters`
  （`:29-33` `validate_parameters`）。我们**不做 JSON Schema 校验** ——
  理由：①引入 `jsonschema` 依赖只是为了校验**我们自己**传的 schema；
  ②真实现校验失败会抛异常，而插件传的 schema 是它自己写死的常量，
  在校验上花成本收益低。**但参数形状照收**（`parameters` 必须是 dict）。
* 真实现 `FunctionTool.call()` 走 `handler`；我们保留同名方法，
  在没有 handler 时**如实抛 `NotImplementedError`**（照真实现 `:69-73`）
  —— 插件那个试跑工具**故意**传 `handler=None`（它只要模型"说出"要调什么，
  不要真执行），所以这里抛不抛都不影响它。
"""
from __future__ import annotations

import copy
from typing import Any

__all__ = ["FunctionTool", "ToolSchema", "ToolSet"]


class ToolSchema:
    """工具 schema（字段照真实现 `tool.py:19-34`）。

    ⚠️ 用**普通类**而不是 pydantic dataclass：插件只读写这三个属性，
       真实现那层校验对我们没有增益（见模块 docstring 的"有意差异"）。
    """

    def __init__(
        self,
        name: str = "",
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        #: JSON Schema 形状。真实现要求它合法；我们只要求它是 dict 或 None。
        self.parameters = parameters

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"description={self.description!r})"
        )


class FunctionTool(ToolSchema):
    """可调用的工具（字段照真实现 `tool.py:36-71`）。"""

    def __init__(
        self,
        name: str = "",
        description: str = "",
        parameters: dict[str, Any] | None = None,
        handler: Any = None,
        handler_module_path: str | None = None,
        active: bool = True,
        is_background_task: bool = False,
    ) -> None:
        super().__init__(name=name, description=description, parameters=parameters)
        #: 真正执行的协程；`None` = 只声明不执行（插件试跑就是这种）
        self.handler = handler
        self.handler_module_path = handler_module_path
        self.active = active
        self.is_background_task = is_background_task

    async def call(self, context: Any = None, **kwargs: Any) -> Any:
        """执行工具（`handler` 优先）。

        ⚠️ 无 handler 时**如实抛 `NotImplementedError`**（照真实现 `:69-73`）
           —— 不返回空串假装成功（那会让上层以为"工具跑完了但没输出"）。
        """
        if callable(self.handler):
            return await self.handler(context=context, **kwargs)
        raise NotImplementedError(
            "FunctionTool.call() must be implemented by subclasses or set a handler."
        )


class ToolSet:
    """一组工具（字段与方法照真实现 `tool.py:78-217`）。

    插件用到的面（实测）：`ToolSet([tool])` 构造、`openai_schema()`、
    `get_all()`、`empty()`、`add_tool()`、`get_tool()`。
    """

    def __init__(self, tools: list[Any] | None = None) -> None:
        #: 🔴 名字照真实现：`tools`（不是 `_tools`/`funcs`）
        self.tools: list[Any] = list(tools or [])

    # ---- 集合操作 ----

    def empty(self) -> bool:
        return len(self.tools) == 0

    def add_tool(self, tool: Any) -> None:
        """加一个工具；**同名时**按真实现的规则替换（`:91-108`）：

        * 新的是 active → 覆盖；
        * 否则旧的若 inactive 也覆盖；
        * 否则保留旧的。
        """
        for i, existing in enumerate(self.tools):
            if getattr(existing, "name", None) == getattr(tool, "name", None):
                existing_active = bool(getattr(existing, "active", True))
                new_active = bool(getattr(tool, "active", True))
                if new_active or not existing_active:
                    self.tools[i] = tool
                return
        self.tools.append(tool)

    def remove_tool(self, name: str) -> None:
        self.tools = [t for t in self.tools if getattr(t, "name", None) != name]

    def get_tool(self, name: str) -> Any | None:
        for tool in self.tools:
            if getattr(tool, "name", None) == name:
                return tool
        return None

    def get_all(self) -> list[Any]:
        """照真实现 `:200-201`（插件 `_tools_of` 用它）。"""
        return self.tools

    def names(self) -> list[str]:
        """所有工具名（照真实现 `:344-346`）。

        🔴 为什么必须有：真实现的工具执行器在**找不到工具时**要用它列出
        "Available tools are: ..." 回填给模型（`tool_loop_agent_runner.py:1156-1159`）。
        没有它，那条错误分支会变成 `AttributeError`（新 bug 掩盖老 bug）。
        """
        return [getattr(tool, "name", "") for tool in self.tools]

    def merge(self, other: Any) -> None:
        """合并另一个 `ToolSet`（照真实现 `:348-351`）。

        🔴 **这是"插件注册的工具进不了模型"的最后一块拼图**：
        真实现 `astr_main_agent.py:613-617` 就是用它把
        `context.get_llm_tool_manager().get_full_tool_set()` 并进 `req.func_tool`。
        """
        for tool in getattr(other, "tools", None) or []:
            self.add_tool(tool)

    def __len__(self) -> int:
        return len(self.tools)

    def __bool__(self) -> bool:
        """空集为假（照真实现 `:354-355`）。

        ⚠️ 真实现靠它做 `if req.func_tool:` 判断；**不要**退化成"恒真对象"
           （那会让"没有工具"被当成"有工具"，上游会收到空 tools 数组）。
        """
        return len(self.tools) > 0

    def __iter__(self):
        return iter(self.tools)

    # ---- 轻量变体（真实现有，插件按需用） ----

    def get_light_tool_set(self) -> ToolSet:
        """只留 name/description（照真实现 `:121-138`）。"""
        out = []
        for tool in self.tools:
            if hasattr(tool, "active") and not tool.active:
                continue
            out.append(
                FunctionTool(
                    name=getattr(tool, "name", ""),
                    parameters={"type": "object", "properties": {}},
                    description=getattr(tool, "description", ""),
                    handler=None,
                )
            )
        return ToolSet(out)

    def get_param_only_tool_set(self) -> ToolSet:
        """只留 name/parameters（照真实现 `:140-158`）。"""
        out = []
        for tool in self.tools:
            if hasattr(tool, "active") and not tool.active:
                continue
            params = getattr(tool, "parameters", None)
            out.append(
                FunctionTool(
                    name=getattr(tool, "name", ""),
                    parameters=copy.deepcopy(params)
                    if params
                    else {"type": "object", "properties": {}},
                    description="",
                    handler=None,
                )
            )
        return ToolSet(out)

    # ---- 转上游格式（🔴 最关键的契约） ----

    def openai_schema(self, omit_empty_parameter_field: bool = False) -> list[dict[str, Any]]:
        """转 OpenAI function calling 格式（**逐字照真实现 `:203-217`**）。

        🔴 三条容易写错、写错就静默失效的细节：

        1. `description` 为**空则不出现**该键（不是 `""`）；
        2. `parameters is None` → 不出现该键；
        3. `omit_empty_parameter_field=True` 且 `properties` 为空 → 也不出现。
        """
        result: list[dict[str, Any]] = []
        for tool in self.tools:
            name = getattr(tool, "name", "")
            description = getattr(tool, "description", "")
            parameters = getattr(tool, "parameters", None)
            func_def: dict[str, Any] = {"type": "function", "function": {"name": name}}
            if description:
                func_def["function"]["description"] = description
            if parameters is not None:
                if (parameters and parameters.get("properties")) or (
                    not omit_empty_parameter_field
                ):
                    func_def["function"]["parameters"] = parameters
            result.append(func_def)
        return result

    def get_func_desc_openai_style(
        self, omit_empty_parameter_field: bool = False
    ) -> list[dict[str, Any]]:
        """真实现的旧名（`:332-334` 标了 deprecated，但插件里可能有旧调用）。"""
        return self.openai_schema(omit_empty_parameter_field)

    def __repr__(self) -> str:
        return f"ToolSet(tools={[getattr(t, 'name', None) for t in self.tools]})"
