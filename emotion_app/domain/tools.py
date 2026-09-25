# -*- coding: utf-8 -*-
"""工具（function calling）定义与执行框架。

⭐ 设计依据（`docs/IMAGE_TOOL_DISPATCH.md`）：
   实测结论 —— **用 function calling，不用文本标签**。
   爱语用 `<generate_image:描述>` 文本标签，被情绪指出「容易被截断」，
   实测确认是**结构性问题**（正则带 `^…$` 锚点、流式半截失效、失败无反馈）。

⭐ 借鉴陪伴插件的三条硬规则（原文见文档 §5.7）：
   ① **每轮最多调一次**同个工具（实测我们上游也会重复调用！）
   ② 工具返回必须带 `must_not_claim_sent`（防"没生成却说到手了"）
   ③ 失败时给 `final_response_instruction`（告诉模型下一步，不让它编）
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..infra.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# 定义
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolParam:
    """一个工具参数。"""

    name: str
    type: str = "string"
    description: str = ""
    enum: tuple[str, ...] = ()
    required: bool = False

    def to_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            out["enum"] = list(self.enum)
        return out


@dataclass(slots=True)
class ToolSpec:
    """一个工具。"""

    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)

    def to_openai(self) -> dict[str, Any]:
        """转成 OpenAI `tools` 数组的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p.name: p.to_schema() for p in self.params
                    },
                    "required": [p.name for p in self.params if p.required],
                },
            },
        }


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。

    ⚠️ **这个结构是防幻觉的关键**（照陪伴插件的契约）：
       * `must_not_claim_sent` —— 明确告诉模型"别谎称发出去了"
       * `final_response_instruction` —— 失败时告诉模型该说什么
    """

    status: str = "success"           # success / skipped / error / sent
    success: bool = True
    sent: bool = False
    generated: bool = False
    message: str = ""
    actual_error: str = ""            # 给日志，不给用户
    must_not_claim_sent: bool = False
    final_response_instruction: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    #: 视为"成功"的 status（其余都要求 success=False）
    _OK_STATUSES = ("success", "skipped", "already_sent")

    def __post_init__(self) -> None:
        """⚠️ 一致性校验：`status` 与 `success` **不能自相矛盾**。

        起因：实测写测试时 `ToolResult(status="error")` 的 `success` 仍是默认 `True`
        —— 结果对象自相矛盾，下游（如附件提取）会按 `success` 放行。
        **判据：宁可在此处显式报错，也不要让矛盾的 result 流下去。**
        """
        if self.status in self._OK_STATUSES:
            return
        if self.success:
            raise ValueError(
                f"ToolResult 自相矛盾：status={self.status!r} 但 success=True"
            )

    def to_payload(self) -> str:
        """给模型看的 JSON（**不含** actual_error —— 那是日志用的）。"""
        out: dict[str, Any] = {
            "status": self.status,
            "success": self.success,
            "generated": self.generated,
            "sent": self.sent,
        }
        if self.message:
            out["message"] = self.message
        if self.must_not_claim_sent:
            out["must_not_claim_sent"] = True
        if self.final_response_instruction:
            out["final_response_instruction"] = self.final_response_instruction
        out.update(self.data)
        return json.dumps(out, ensure_ascii=False)


@dataclass(slots=True)
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any]:
        """解析 arguments。坏 JSON 返回空 dict（并记日志，不炸）。"""
        if not self.arguments:
            return {}
        try:
            data = json.loads(self.arguments)
        except json.JSONDecodeError:
            log.warning(
                "工具参数不是合法 JSON，按空参数处理",
                extra={"data": {"tool": self.name, "len": len(self.arguments)}},
            )
            return {}
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


class ToolRegistry:
    """工具注册与执行。

    ⭐ **每轮最多调一次**（`max_calls_per_turn`）——
       实测我们的上游模型**会重复调用工具**（把结果回填后它又调一次），
       所以这条防护是必需的，不是可选优化。
    """

    def __init__(self, *, max_calls_per_turn: int = 1) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self.max_calls_per_turn = max_calls_per_turn

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具重名：{spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)
        self._handlers.pop(name, None)

    def clear(self) -> None:
        self._specs.clear()
        self._handlers.clear()

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def to_openai(self) -> list[dict[str, Any]]:
        """给上游的 `tools` 参数。"""
        return [s.to_openai() for s in self._specs.values()]

    def has(self, name: str) -> bool:
        return name in self._specs

    async def execute(self, call: ToolCall, *, call_counts: dict[str, int] | None = None) -> ToolResult:
        """执行一次工具调用。

        Args:
            call: 模型发起的调用。
            call_counts: 本轮各工具已调用次数（用于"每轮最多一次"限流）。
        """
        handler = self._handlers.get(call.name)
        if handler is None:
            # 未知工具：明确报错，**不静默忽略**
            return ToolResult(
                status="unknown_tool",
                success=False,
                message=f"不存在名为 {call.name} 的工具",
                must_not_claim_sent=True,
                final_response_instruction="不要声称已执行；按当前语境自然回复。",
            )

        counts = call_counts if call_counts is not None else {}
        used = counts.get(call.name, 0)
        if used >= self.max_calls_per_turn:
            log.warning(
                "工具本轮已调用过，拒绝重复调用",
                extra={"data": {"tool": call.name, "used": used}},
            )
            return ToolResult(
                status="already_called",
                success=False,
                message="本轮已经调用过该工具，不重复执行",
                must_not_claim_sent=True,
                final_response_instruction=(
                    "不要再次调用该工具；如果上一次已成功，就自然收尾；"
                    "如果上一次失败，如实告知用户，等用户下一轮明确要求再重试。"
                ),
            )

        counts[call.name] = used + 1

        try:
            args = call.parsed_arguments()
            return await handler(args)
        except Exception as exc:  # noqa: BLE001 — 工具异常必须变成结构化结果
            log.exception("工具执行异常", extra={"data": {"tool": call.name}})
            return ToolResult(
                status="error",
                success=False,
                message="工具执行出错",
                actual_error=str(exc),
                must_not_claim_sent=True,
                final_response_instruction="如实说明这次没有成功，不要编造结果。",
            )
