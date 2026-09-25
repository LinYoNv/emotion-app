# -*- coding: utf-8 -*-
"""上游 AI 客户端（OpenAI 兼容）。

实测依据（见 `docs/REPRODUCTION.md` §1）：
  * 上游是 **new-api 系、完全兼容 OpenAI、无需适配层**；
  * 首选 `api.syuan.org`（备选 `api-api.syuan.org`；`aiapi` 是 404，弃用）；
  * 非流式 `POST {base}/chat/completions`，流式同路径 + SSE。

⭐ 借鉴陪伴插件的一点（`model_routing.py`）：
   **拒答话术触发换模型重试** —— 比塞在提示词里可配置、可审计。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..infra.logging_setup import get_logger
from .message import build_upstream_messages, Message

log = get_logger(__name__)


class UpstreamError(Exception):
    """上游调用失败。"""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


#: 上游拒答话术（借鉴陪伴插件的 `DEFAULT_SENSITIVE_REPLACEMENT_KEYWORDS`）。
#: 命中后**换模型重试** —— 这类拒答往往与具体模型的内容策略有关。
DEFAULT_REFUSAL_KEYWORDS = (
    "很抱歉，我无法",
    "很抱歉,我无法",
    "我有我自己的底线",
    "我们可以聊聊别的",
    "我无法满足",
    "没办法提交这个请求",
    "这个请求没办法提交",
    "The prompt could not be submitted",
    "prompt could not be submitted",
    "The request could not be submitted",
    "request could not be submitted",
)


def looks_like_refusal(text: str, keywords: tuple[str, ...] = DEFAULT_REFUSAL_KEYWORDS) -> bool:
    """文本是否像上游拒答。"""
    if not text:
        return False
    low = text.casefold()
    return any(k.casefold() in low for k in keywords)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UpstreamTarget:
    """一次调用的目标（人设可覆盖全局）。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0

    def url(self, path: str = "chat/completions") -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


@dataclass(slots=True)
class ChatResult:
    """非流式结果。"""

    content: str = ""
    model: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    reasoning_content: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: 模型发起的工具调用（原样保留，由上层解析）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: 工具产出的**可展示资源**（如生成图片的 URL）。
    #: ⚠️ OpenAI 协议里没有这个字段，是我们的扩展 ——
    #:    标准客户端会忽略它；我们的面板会渲染。
    attachments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class StreamChunk:
    """流式增量。"""

    delta: str = ""
    reasoning_delta: str = ""
    finish_reason: str = ""
    done: bool = False
    #: 工具调用的**原始分片**（照 OpenAI 协议：首个带 id/name，后续增量 arguments）
    tool_call_deltas: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def build_body(
    messages: list[Message],
    *,
    model: str,
    stream: bool,
    thinking_field: str = "reasoning_content",
    inject_system_markers: bool = True,
    extra: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "",
) -> dict[str, Any]:
    """构造请求体。

    ⭐ 实测还原（`dr.java` L211–293 + `t10.d()`）：
       `{model, messages, stream}` + 用户自定义参数（支持 `.` 嵌套路径）。
       `role == "reasoning"` 的消息**不发**；
       system 的 content 包 `<|SYSTEM_INJECT|>…<|END_INJECT|>`。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": build_upstream_messages(
            messages,
            thinking_field=thinking_field,
            inject_system_markers=inject_system_markers,
        ),
        "stream": bool(stream),
    }
    # ⭐ function calling（实测上游完整支持，见 docs/IMAGE_TOOL_DISPATCH.md §5.6）
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    if extra:
        body.update(extra)
    return body


def apply_nested_params(body: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """支持 `.` 分隔的嵌套路径写入（爱语自定义参数的行为）。

    >>> apply_nested_params({}, {"a.b": 1})
    {'a': {'b': 1}}
    """
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            continue
        parts = key.split(".")
        cursor = body
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
    return body


# ⚠️ 原先这里有个 `UpstreamClient` 类。
#    S6 起它被 `backends.OpenAIChatBackend` 取代（后者满足 `ports.ChatBackend` Protocol）。
#    保留本模块的**纯函数工具**（build_body / parse_chat_response / parse_sse_line /
#    assemble_tool_calls），因为它们与协议解析有关且被多方复用。
#    **判据**：删掉重复实现，避免"两套客户端行为不一致"这种隐蔽 bug。


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_chat_response(data: dict[str, Any], *, fallback_model: str = "") -> ChatResult:
    """解析非流式响应。"""
    choices = data.get("choices") or []
    if not choices:
        raise UpstreamError("上游响应缺少 choices")

    first = choices[0] or {}
    msg = first.get("message") or {}
    raw_calls = msg.get("tool_calls")
    calls = [c for c in raw_calls if isinstance(c, dict)] if isinstance(raw_calls, list) else []
    return ChatResult(
        content=str(msg.get("content") or ""),
        reasoning_content=str(msg.get("reasoning_content") or ""),
        model=str(data.get("model") or fallback_model),
        finish_reason=str(first.get("finish_reason") or ""),
        usage=dict(data.get("usage") or {}),
        raw=data,
        tool_calls=calls,
    )


def parse_sse_line(line: str) -> StreamChunk | None:
    """解析一行 SSE。

    返回 None 表示这行不含数据（空行/注释/非 data 行）。
    """
    if not line:
        return None
    text = line.strip()
    if not text.startswith("data:"):
        return None

    payload = text[len("data:"):].strip()
    if payload == "[DONE]":
        return StreamChunk(done=True)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # 坏行不该炸掉整个流 —— 跳过并继续
        log.warning("跳过无法解析的 SSE 行", extra={"data": {"len": len(payload)}})
        return None

    choices = data.get("choices") or []
    if not choices:
        return None
    first = choices[0] or {}
    delta = first.get("delta") or {}

    raw_tc = delta.get("tool_calls")
    tcs = [c for c in raw_tc if isinstance(c, dict)] if isinstance(raw_tc, list) else []

    return StreamChunk(
        delta=str(delta.get("content") or ""),
        reasoning_delta=str(delta.get("reasoning_content") or ""),
        finish_reason=str(first.get("finish_reason") or ""),
        done=False,
        tool_call_deltas=tcs,
    )


def assemble_tool_calls(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把流式的 tool_calls **分片**拼成完整调用。

    ⭐ 实测（`docs/IMAGE_TOOL_DISPATCH.md` §5.6）：上游流式下
       首个 chunk 带 `{"index":0,"id":"call_xxx","function":{"name":"generate_image","arguments":""}}`，
       后续 chunk **不再带 id/name**，只有 `{"index":0,"function":{"arguments":"..."}}` 增量。
       → 必须按 `index` 聚合，且 **arguments 是字符串拼接**，不是 JSON 合并。
    """
    merged: dict[int, dict[str, Any]] = {}

    for frag in fragments:
        if not isinstance(frag, dict):
            continue
        idx = frag.get("index")
        idx = int(idx) if isinstance(idx, int) else 0
        entry = merged.setdefault(idx, {"id": "", "name": "", "arguments": ""})

        if frag.get("id"):
            entry["id"] = str(frag["id"])
        fn = frag.get("function")
        if isinstance(fn, dict):
            if fn.get("name"):
                entry["name"] = str(fn["name"])
            if fn.get("arguments"):
                entry["arguments"] += str(fn["arguments"])

    out: list[dict[str, Any]] = []
    for idx in sorted(merged):
        e = merged[idx]
        if not e["name"]:
            continue
        out.append({
            "id": e["id"] or f"call_{idx}",
            "type": "function",
            "function": {"name": e["name"], "arguments": e["arguments"]},
        })
    return out
