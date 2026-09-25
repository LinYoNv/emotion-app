# -*- coding: utf-8 -*-
"""可替换实现：把具体协议封在 Protocol 后面。

🔴 原则（情绪 2026-09-18）：「**接口别写死了**」。
   本文件提供**多套实现**，靠配置切换，领域层不感知。

| Protocol | 本文件的实现 |
|---|---|
| `ChatBackend` | `OpenAIChatBackend`（当前；new-api 系实测兼容） |
| `ImageBackend` | `OpenAIImageBackend`（爱语同款；可换 NAI/ComfyUI） |
| `ToolDispatch` | `FunctionCallDispatch`（默认，实测可用）<br>`TextTagDispatch`（**退路**，照爱语的标签方式） |
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..infra.logging_setup import get_logger
from .image_gen import (
    DEFAULT_SIZE,
    KIND_EDIT,
    ImageClient,
    ImageError,
    ImageTarget,
    parse_image_response,
    reference_data_uri,
)
from .message import Message
from .tools import ToolCall, ToolResult, ToolSpec

log = get_logger(__name__)


# ===========================================================================
# ChatBackend
# ===========================================================================


class OpenAIChatBackend:
    """OpenAI 兼容的对话后端。

    ⭐ 实测：`api.syuan.org` 是 new-api 系，完全兼容 OpenAI，无需适配层。
       **但领域层不依赖这个事实** —— 换别的实现只要满足 `ChatBackend` Protocol。
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(60.0, connect=10.0),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OpenAIChatBackend 未 open()")
        return self._client

    @staticmethod
    def _url(base_url: str, path: str = "chat/completions") -> str:
        """拼出完整的请求地址。

        🔴 **base_url 为空必须响亮报错**（2026-09-20 实测踩到）：
           原来直接 `f"{base_url.rstrip('/')}/..."`，空串会**静默**拼出一个
           **相对地址** `/chat/completions` —— httpx 接过去才抛
           `ValueError: unknown url type: '/chat/completions'`，
           错误现场离根因（"这条源的 base_url 没配"）很远，极难排查。
           → 在这里就拦住，并把"缺 base_url"说清楚。
        """
        base = (base_url or "").strip()
        if not base:
            raise ValueError(
                "上游 base_url 为空：无法拼出请求地址"
                "（检查该供应商源是否填了 API Base URL）"
            )
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        return h

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        from .upstream import ChatResult, build_body, parse_chat_response

        body = build_body(messages, model=model, stream=False, extra=extra, tools=tools)
        client = self._require()
        try:
            resp = await client.post(
                self._url(base_url), headers=self._headers(api_key), json=body, timeout=timeout
            )
        except httpx.HTTPError as exc:
            from .upstream import UpstreamError

            raise UpstreamError(f"上游连接失败：{exc}") from exc

        if resp.status_code >= 400:
            from .upstream import UpstreamError

            raise UpstreamError(
                f"上游返回 {resp.status_code}", status=resp.status_code, body=resp.text[:500]
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            from .upstream import UpstreamError

            raise UpstreamError("上游返回的不是 JSON", body=resp.text[:500]) from exc

        return parse_chat_response(data, fallback_model=model)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> Any:
        """插件宿主用的**裸模型调用**入口（`UpstreamLLMPort` 期望的形状）。

        🔴 为什么要有它（不是重复造轮子）：
           插件的 `ProviderManager` 端口在**启动路径**上拿到的就是这个类，
           而它原来只有 `chat()` / `chat_stream()`。`UpstreamLLMPort.generate()`
           调 `complete()` → `AttributeError` → 插件一用模型就炸
           （详见 `web/provider_bridge.py` 里那段注释）。
           → 这里补一个**同义**入口，让两条路都能通，互为保险。

        🔴 `base_url`/`api_key`/`timeout` **必须收下并转发**（2026-09-20 实测踩到）：
           插件用的是**它自己那条源**的连接信息（可能与面板主聊天不是同一条）。
           我第一版漏了这三个参数 → `base_url` 落成空串 → `_url()` 拼出
           相对地址 `/chat/completions` → 报 `unknown url type`，
           现场离根因很远。**端到端判据当场抓住了它**
           （`test_startup_path_backend_can_actually_generate`）。

        ⚠️ 与 `chat()` 的分工：`chat()` 收领域层 `Message` 列表；
           本方法收 **OpenAI 风格的 dict** 列表（插件就是这么给的），在此转换。
           **不吞错**：上游错误原样抛。
        """
        from .message import Message as _Msg

        domain_msgs = [
            _Msg(role=str(m.get("role") or "user"), content=str(m.get("content") or ""))
            for m in (messages or [])
            if isinstance(m, dict)
        ]
        target = model or kwargs.get("fallback_model") or ""
        return await self.chat(
            domain_msgs,
            model=target,
            base_url=base_url or kwargs.get("base_url", ""),
            api_key=api_key or kwargs.get("api_key", ""),
            timeout=float(timeout or kwargs.get("timeout", 60.0)),
            tools=tools,
            # 🔴 `extra` 必须转发（2026-09-20 实测踩到，与 `UpstreamLLMPort` 同一类 bug）：
            #    插件通过它传 `max_tokens` 等 OpenAI 兼容参数控成本；
            #    本方法签名收了 `**kwargs` 却**不转发 `extra`** → 静默丢弃，
            #    上游按默认长度生成，插件面板显示的预算与实际成本对不上，且无任何报错。
            extra=kwargs.get("extra"),
        )

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        from .upstream import UpstreamError, build_body, parse_sse_line

        body = build_body(messages, model=model, stream=True, extra=extra, tools=tools)
        client = self._require()
        try:
            async with client.stream(
                "POST", self._url(base_url), headers=self._headers(api_key),
                json=body, timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise UpstreamError(
                        f"上游返回 {resp.status_code}",
                        status=resp.status_code, body=text[:500],
                    )
                async for line in resp.aiter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except httpx.HTTPError as exc:
            raise UpstreamError(f"上游连接失败：{exc}") from exc


# ===========================================================================
# ImageBackend
# ===========================================================================


@dataclass
class ImageBackendResult:
    """生图结果（与 `image_gen.ImageResult` 兼容的最小形状）。"""

    images: list[Any] = field(default_factory=list)
    model: str = ""

    @property
    def count(self) -> int:
        return len(self.images)


class OpenAIImageBackend:
    """OpenAI 兼容的生图后端（`/images/generations`）。

    ⭐ 依据：爱语实测用的就是这个（11 个配置键 `key_image_gen_custom_*`）。
    ⚠️ 但**不是唯一选择** —— 陪伴插件还支持 NAI / ComfyUI / SD WebUI。
       换后端只需实现 `ImageBackend` Protocol，领域层不改。
    """

    name = "openai-images"

    def __init__(
        self,
        client: ImageClient,
        target: ImageTarget,
        *,
        enabled: bool = True,
        default_size: str = DEFAULT_SIZE,
    ) -> None:
        self._client = client
        self._target = target
        self.enabled = enabled
        self.default_size = default_size

    def is_configured(self) -> bool:
        return bool(self._target.base_url)

    async def open(self) -> None:
        await self._client.open()

    async def close(self) -> None:
        await self._client.close()

    @property
    def raw_client(self) -> httpx.AsyncClient | None:
        return getattr(self._client, "_client", None)

    def _resolve_target(self, *, base_url: str, api_key: str, model: str) -> ImageTarget:
        """允许**按次覆盖**端点（人设级生图后端 / 用户自定义）。"""
        return ImageTarget(
            base_url=base_url or self._target.base_url,
            api_key=api_key or self._target.api_key,
            model=model or self._target.model,
        )

    async def generate(
        self,
        *,
        prompt: str,
        kind: str = "text2img",
        size: str = "",
        reference_image: str = "",
        n: int = 1,
        extra: dict[str, Any] | None = None,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
    ) -> ImageBackendResult:
        target = self._resolve_target(base_url=base_url, api_key=api_key, model=model)
        size_value = size or self.default_size

        # 🔴 **参考图必须真的发给上游**（2026-09-25 修）。
        #    此前这里只打一行「后端不支持参考图，已忽略」就把它丢了 ——
        #    正是本项目最贵的那类 bug（参考图静默丢弃），用户只能凭感觉报
        #    「好像没用上参考图」。现在两条路都实测可用：
        #      · `edit`   → 真 img2img（`/images/edits`，实测 200 出图）
        #      · `selfie` → 精准参考（`director_reference_*`，实测 200 出图）
        if reference_image:
            # ⚠️ 读不到**响亮报错**，绝不降级成"悄悄不带参考图"
            uri = reference_data_uri(reference_image)
            if kind == KIND_EDIT:
                log.info(
                    "生图带参考图",
                    extra={"data": {"backend": self.name, "mode": "img2img",
                                    "ref_bytes": len(uri)}},
                )
                payload = await self._client.edit(
                    target, prompt=prompt, image=uri, size=size_value,
                    n=n, extra=extra,
                )
            else:
                log.info(
                    "生图带参考图",
                    extra={"data": {"backend": self.name, "mode": "director_reference",
                                    "ref_bytes": len(uri)}},
                )
                payload = await self._client.generate(
                    target, prompt=prompt, size=size_value, n=n, extra=extra,
                    reference_images=[uri],
                )
        else:
            # `edit` 的语义就是"改这张图" —— 没图就不该走到这儿
            #    （`image_tool` 已提前挡住，这里是纵深防御）
            if kind == KIND_EDIT:
                raise ImageError("edit 缺少参考图", code="reference_unreadable")
            payload = await self._client.generate(
                target, prompt=prompt, size=size_value, n=n, extra=extra
            )

        return ImageBackendResult(
            images=list(payload.images), model=payload.model or target.model
        )


# ===========================================================================
# ToolDispatch
# ===========================================================================


class FunctionCallDispatch:
    """**原生 function calling** 下发（默认）。

    ⭐ 实测可用（`docs/IMAGE_TOOL_DISPATCH.md` §5.6）：
       非流式 `finish_reason=tool_calls`；流式分片可拼装；枚举参数模型能理解。
    """

    name = "function_call"

    def __init__(self, *, tool_choice: str = "auto") -> None:
        self.tool_choice = tool_choice

    def build_payload(self, tools: list[ToolSpec]) -> dict[str, Any]:
        if not tools:
            return {}
        payload: dict[str, Any] = {"tools": [t.to_openai() for t in tools]}
        if self.tool_choice:
            payload["tool_choice"] = self.tool_choice
        return payload

    def extract_calls(self, response: Any) -> list[ToolCall]:
        raw = getattr(response, "tool_calls", None) or []
        out: list[ToolCall] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") or {}
            out.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{len(out)}"),
                    name=str(fn.get("name") or ""),
                    arguments=str(fn.get("arguments") or ""),
                )
            )
        return [c for c in out if c.name]

    def extract_stream_calls(self, fragments: list[Any]) -> list[ToolCall]:
        from .upstream import assemble_tool_calls

        merged = assemble_tool_calls([f for f in fragments if isinstance(f, dict)])
        return [
            ToolCall(
                id=str(m.get("id") or "call_0"),
                name=str((m.get("function") or {}).get("name") or ""),
                arguments=str((m.get("function") or {}).get("arguments") or ""),
            )
            for m in merged
        ]

    def render_result(self, result: ToolResult, *, call_id: str = "") -> Message:
        """function calling 的结果走 **`role="tool"`** 消息。"""
        return Message(role="tool", content=result.to_payload(), tool_call_id=call_id)

    def prompt_sections(self, tools: list[ToolSpec]) -> list[Any]:
        """function calling 不需要把工具说明塞进提示词（走 tool schema）。

        ⚠️ 但**使用规则仍要注入**（防幻觉规则），那是另一处（`image_tool.TOOL_USAGE_RULES`）。
        """
        return []


class TextTagDispatch:
    """**文本标签**下发（退路）。

    ⚠️ 为什么保留：情绪指出爱语的标签方式「容易被截断」，
       但**万一上游将来不支持 function calling**（换端点/换模型），
       我们需要一条退路，而**不必改动领域层**。

    改进（相对爱语）：
      * **不在提示词里要求"独占一行"** —— 爱语的正则 `^<...>$` 强制独占整行，
        模型稍一发挥就失配；
      * 解析时**容忍前后文**（只要标签完整闭合就认）；
      * **解析失败要留日志**（爱语的失败是静默的）。
    """

    name = "text_tag"

    #: 宽容解析：不要求独占行，只要 `<name:...>` 闭合
    _TAG_RE = re.compile(r"<(?P<name>[a-z_]+):(?P<body>[^<>]*)>", re.IGNORECASE)

    def __init__(self, *, tag_name: str = "generate_image") -> None:
        self.tag_name = tag_name

    def build_payload(self, tools: list[ToolSpec]) -> dict[str, Any]:
        # 文本标签方式不改请求体 —— 工具说明走提示词
        return {}

    def prompt_sections(self, tools: list[ToolSpec]) -> list[Any]:
        """把工具说明写成提示词片段（标签方式必须这样做）。"""
        from .persona import PromptSection

        if not tools:
            return []
        lines = ["你可以用以下标签调用能力（标签必须完整闭合）："]
        for t in tools:
            params = "、".join(p.name for p in t.params)
            lines.append(f"- `<{t.name}:内容>` —— {t.description}（参数：{params}）")
        return [
            PromptSection(
                title="可用工具（标签方式）",
                content="\n".join(lines),
                order=800,
                source="module",
            )
        ]

    def extract_calls(self, response: Any) -> list[ToolCall]:
        """从**回复正文**里抠标签。"""
        content = str(getattr(response, "content", "") or "")
        calls: list[ToolCall] = []
        for i, m in enumerate(self._TAG_RE.finditer(content)):
            name = m.group("name").lower()
            if name != self.tag_name:
                continue
            body = m.group("body").strip()
            calls.append(
                ToolCall(
                    id=f"tag_{i}",
                    name=name,
                    arguments=json.dumps({"prompt": body}, ensure_ascii=False),
                )
            )
        if not calls and "<" in content and ":" in content:
            # ⚠️ 有标签痕迹但没解析出来 —— **必须留日志**（爱语的失败是静默的）
            log.warning(
                "疑似工具标签但解析失败（可能被截断）",
                extra={"data": {"len": len(content)}},
            )
        return calls

    def extract_stream_calls(self, fragments: list[Any]) -> list[ToolCall]:
        # 流式下需要把正文拼起来再解析
        text = "".join(
            str(f.get("delta") or "") for f in fragments if isinstance(f, dict)
        )
        if not text:
            return []
        return self.extract_calls(type("R", (), {"content": text})())

    def render_result(self, result: ToolResult, *, call_id: str = "") -> Message:
        """标签方式没有 `tool` 角色 —— 用 **user 消息**回填结果。"""
        return Message(
            role="user",
            content=(
                f"[工具 {call_id or 'result'} 返回]\n{result.to_payload()}\n"
                "（这是系统返回的工具结果，不要在回复里复述此段）"
            ),
        )


#: 可用的下发方式（供配置选择）
DISPATCH_BACKENDS: dict[str, type] = {
    "function_call": FunctionCallDispatch,
    "text_tag": TextTagDispatch,
}


def build_dispatch(name: str, **kwargs: Any) -> Any:
    """按名字造下发方式。

    ⚠️ 未知名字 → **明确报错**（不静默退回默认，免得"配了没生效"）。
    """
    cls = DISPATCH_BACKENDS.get(str(name or "").strip().lower())
    if cls is None:
        raise ValueError(
            f"未知工具下发方式：{name!r}，可用 {sorted(DISPATCH_BACKENDS)}"
        )
    return cls(**kwargs)
