"""Bridge one admitted provider request to its event-scoped tools and response hooks."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any

from ..domain.chat import LLMRequestCancelled, PreparedLLMRequest
from ..domain.message import Message, build_upstream_messages
from ..domain.tools import ToolCall, ToolResult
from ..domain.upstream import ChatResult
from ..infra.logging_setup import get_logger

log = get_logger(__name__)


def _arguments(value: Any) -> dict[str, Any]:
    parsed = json.loads(value or "{}") if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return parsed


def _require_native_call(call: ToolCall, arguments: dict[str, Any], messages: list[dict]) -> None:
    completed = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    if not call.id or call.id in completed:
        raise ValueError("Tool call has no pending native call id")
    latest = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
    for native in latest.get("tool_calls") or []:
        function = native.get("function") or {}
        if (native.get("type") == "function" and native.get("id") == call.id
                and function.get("name") == call.name
                and _arguments(function.get("arguments", "{}")) == arguments):
            return
    raise ValueError("Tool call is absent from the actual assistant tool_calls")


async def _call_handler(handler: Any, event: Any, arguments: dict[str, Any]) -> Any:
    signature = inspect.signature(handler)
    try:
        signature.bind(event, **arguments)
    except TypeError:
        if "event" in signature.parameters:
            signature.bind(event=event, **arguments)
            result = handler(event=event, **arguments)
        else:
            signature.bind(**arguments)
            result = handler(**arguments)
    else:
        result = handler(event, **arguments)
    return await result if inspect.isawaitable(result) else result


def _tool_result(value: Any) -> ToolResult:
    from mcp.types import CallToolResult

    if isinstance(value, ToolResult):
        return value
    if isinstance(value, CallToolResult):
        text = "\n".join(part.text for part in value.content if part.type == "text")
        if not text and value.structuredContent is not None:
            text = json.dumps(value.structuredContent, ensure_ascii=False)
        if value.isError:
            raise ValueError(text or "MCP tool returned isError=True")
        if not text:
            raise ValueError("MCP tool returned no text or structured content")
        return ToolResult(message=text)
    if isinstance(value, str):
        return ToolResult(message=value)
    if isinstance(value, (dict, list, bool, int, float)):
        return ToolResult(message=json.dumps(value, ensure_ascii=False))
    raise TypeError(f"Unsupported tool result type: {type(value).__name__}")


def prepare_llm_turn(
    messages: list[Message], provider_request: Any, event: Any, context: Any = None,
) -> PreparedLLMRequest:
    from astrbot_host.compat.core.agent.tool import FunctionTool
    from astrbot_host.response_bridge import normalize_tools

    func_tool = provider_request.func_tool
    tools = normalize_tools(func_tool)
    if tools:
        tools = [
            item for item in tools
            if item.get("type") == "function"
            and isinstance(item.get("function"), dict)
            and (tool := func_tool.get_tool(item["function"].get("name"))) is not None
            and getattr(tool, "active", True)
        ] or None
    offered = {item["function"]["name"] for item in tools or []}
    wrapper = SimpleNamespace(
        context=SimpleNamespace(event=event, context=context), messages=[],
    )

    async def validate_context() -> None:
        getter = getattr(event, "get_extra", None)
        guard = getter("_emotion_native_history_guard") if callable(getter) else None
        if callable(guard):
            await guard()

    async def execute_tool(call: ToolCall, messages: list[Message]) -> ToolResult:
        from jsonschema import validate

        await validate_context()
        try:
            async with asyncio.timeout(120):
                if call.name not in offered:
                    raise ValueError(f"Tool {call.name} is not offered by this request")
                tool = func_tool.get_tool(call.name)
                if tool is None or not getattr(tool, "active", True):
                    raise ValueError(f"Tool {call.name} is unavailable or inactive")
                arguments = _arguments(call.arguments)
                validate(arguments, getattr(tool, "parameters", None) or {"type": "object"})
                wrapper.messages = build_upstream_messages(messages)
                _require_native_call(call, arguments, wrapper.messages)
                caller = getattr(tool, "call", None)
                custom_call = callable(caller) and (
                    not isinstance(tool, FunctionTool)
                    or getattr(type(tool), "call", None) is not FunctionTool.call
                )
                if custom_call:
                    value = caller(wrapper, **arguments)
                    if inspect.isawaitable(value):
                        value = await value
                else:
                    handler = getattr(tool, "handler", None)
                    if not callable(handler):
                        raise TypeError(f"Tool {call.name} has no executable handler")
                    value = await _call_handler(handler, event, arguments)
                return _tool_result(value)
        except LLMRequestCancelled:
            raise
        except Exception as exc:
            log.error("请求工具 %s 执行失败", call.name, exc_info=True)
            return ToolResult(
                status="error", success=False, message="工具执行失败",
                actual_error=f"{type(exc).__name__}: {exc}", must_not_claim_sent=True,
                final_response_instruction="如实说明这次没有成功，不要编造执行或发送结果。",
            )

    async def response_hook(result: ChatResult) -> ChatResult:
        from astrbot_host.llm_hooks import run_llm_response_hooks
        from astrbot_host.response_bridge import to_llm_response

        await validate_context()
        response = to_llm_response(result, tools=func_tool)
        await run_llm_response_hooks(event, response)
        result.content = (
            response.result_chain.get_plain_text()
            if response.result_chain is not None else response.completion_text
        )
        result.reasoning_content = response.reasoning_content or ""
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("_emotion_llm_response", response)
        return result

    model = getattr(provider_request, "model", "") or ""
    message_id = getattr(event, "get_message_id", None)
    log.info(
        "LLM 请求已准备",
        extra={"data": {
            "umo": str(getattr(event, "unified_msg_origin", "") or ""),
            "message_id": str(message_id() or "") if callable(message_id) else "",
            "system_segments": [
                {"length": len(message.content),
                 "sha256": hashlib.sha256(message.content.encode("utf-8")).hexdigest()[:16]}
                for message in messages if message.role == "system"
            ],
            "model": model, "tools": sorted(offered), "response_same_event": True,
        }},
    )
    return PreparedLLMRequest(
        messages=messages, tools=tools,
        execute_tool=execute_tool if func_tool is not None else None,
        response_hook=response_hook, model=model, validate_context=validate_context,
    )
