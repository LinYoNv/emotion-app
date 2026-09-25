# -*- coding: utf-8 -*-
"""模型供应商（Provider）—— 宿主侧的 provider 层（让插件能"选模型"）。

⭐⭐ **为什么需要这个**：AstrBot 插件普遍按**供应商 ID** 选模型，而不是写死一个上游。
实测陪伴插件的 `_conf_schema.json` 里有 **20+ 个 `*_PROVIDER_ID` 配置项**
（`FAST_RESPONSE_PROVIDER_ID` / `COMPLEX_REASONING_PROVIDER_ID` /
`CREATIVE_MODEL_PROVIDER_ID` / `LLM_PROVIDER_ID` / `DAILY_PLAN_PROVIDER_ID` …），
面板上就是截图里那三张卡片（快速响应 / 复杂推理 / 创作）。

🔴 **不实现它的真实后果**（实测日志）：
```
WARNING [Token预算] 无法解析默认对话 Provider：get_using_provider 与注册表兜底都没有结果；
        未显式配置 provider 的模型任务将退化为模板兜底
WARNING [Token预算] LLM 调用失败: task=daily_plan error=未找到可用的 AstrBot 默认模型 Provider
```
即：插件的**记忆抽取/总结、日程生成、关系分析**等全部**退化成固定模板** ——
不报错、功能静默降级（本项目最贵的那类 bug）。

## 契约来源（都是实测读出来的，不是猜的）

| 需要什么 | 真实现出处 | 插件用法 |
|---|---|---|
| `context.get_provider_by_id(id)` | `star/context.py` | `main.py:19204`、`private_image.py:1204` |
| `context.get_using_provider(umo)` | `star/context.py:457` | `main.py:18199`、`page_api.py:22109` |
| `context.get_all_providers()` | `star/context.py` | `page_api.py:22097`（面板列供应商） |
| `context.provider_manager.inst_map` | `provider/manager.py:34` | `page_api.py:22104`（取值的**第二路径**） |
| `provider.text_chat(...)` | `provider/provider.py:96` | `service.py:7519`、`summarizer.py:77`、`audit.py:334` |
| `provider.get_model()` | `provider/sources/openai_source.py` | `page_api.py:23020` |
| `provider.provider_config["id"/"model"]` | `provider/provider.py:33` | `page_api.py:22930`、`:23017` |

⭐ **`text_chat` 是我们必须实现的那一个** —— 它是插件真正用来"让模型干活"的入口。
   我们的 `LLMPort.generate()` 已经能调上游，所以 `text_chat` 是**一层薄转换**。
"""
from __future__ import annotations

import logging
from typing import Any

from astrbot_host.response_bridge import (
    extract_text,
    normalize_tools,
    to_llm_response,
)

logger = logging.getLogger("astrbot")

#: **适配器类型**（`provider_sources[].type`）—— 描述"用哪种协议去调"。
#: 🔴 与下面的 `PROVIDER_TYPE_CHAT` **不是一回事**（实测真实 AstrBot：
#:    一个对话供应商是 `type="openai_chat_completion"` + `provider_type="chat_completion"`）。
#: 我们只有一个 OpenAI 兼容上游，所以**如实**报这个。
ADAPTER_TYPE_OPENAI_CHAT = "openai_chat_completion"

#: **供应商分类**（`provider_type`）—— 描述"这是干什么用的"。
#: ⚠️ 我们只真正支持 `chat_completion`（对话）；
#:    其余（embedding / text_to_speech / …）**不假装支持**（作者判据③）。
PROVIDER_TYPE_CHAT = "chat_completion"


class Provider:
    """一个模型供应商（照真实现的最小可用面）。

    真实现是一整套 `Provider` 抽象基类 + 各厂商子类（openai/ollama/gemini…）。
    我们**只有一个"OpenAI 兼容"上游**，所以这里是**单实现**，但对外形状与真实现一致：
    插件按属性/方法取用时不会分叉。

    🔴 三处**必须照抄**的形状（否则插件读不到值 → 面板显示空）：
      1. `provider_config` 是**字典**，`id`/`model`/`type` 都在里面
         （`page_api.py:22930` 的 `_provider_config_value` 先查它）；
      2. `get_model()` 是**方法**（`:23020` 优先调它，失败才退回属性）；
      3. `provider_id` 属性存在（`:22931` 第二顺位）。
    """

    def __init__(
        self,
        *,
        provider_id: str,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        name: str = "",
        timeout: float = 60.0,
        llm: Any = None,
    ) -> None:
        self.provider_id = provider_id
        #: 🔴 插件读配置**首先**看这个 dict（照真实现 `provider.py:33`）
        self.provider_config: dict[str, Any] = {
            "id": provider_id,
            # 🔴 照真实契约把两者分开（`page_api.py:23029` 读的是 `type` **或**
            #    `provider_type`，两个都给才与真实现一致）
            "type": ADAPTER_TYPE_OPENAI_CHAT,
            "provider_type": PROVIDER_TYPE_CHAT,
            "model": model,
            "api_base": base_url,
            # ⚠️ `key` 照真实现是**列表**（`provider.py:83` `provider_config.get("key", [""])`）
            "key": [api_key] if api_key else [],
            "name": name or provider_id,
            "timeout": timeout,
        }
        self.name = name or provider_id
        self.model_name = model
        self._llm = llm
        self._timeout = timeout

    # ---- 插件会读/调的方法 ----

    def get_model(self) -> str:
        """当前模型名（`page_api.py:23020` 会调它）。"""
        return str(self.provider_config.get("model") or self.model_name or "")

    def meta(self) -> Any:
        """供应商元信息（照真实现返回一个带 `id`/`model` 的对象）。

        ⚠️ 用 `types.SimpleNamespace` 而不是自造类：插件只按属性读，
           这样即便真实现将来加了字段，我们缺的会**显式 AttributeError**
           （比静默返回 None 好排查）。
        """
        from types import SimpleNamespace

        return SimpleNamespace(
            id=self.provider_id,
            model=self.get_model(),
            # ⚠️ 这里报**分类**（`chat_completion`）—— 真实 provider 对象的 `meta().type`
            #    用于"这是什么用途"，而非适配器协议名。
            type=PROVIDER_TYPE_CHAT,
        )

    def set_model(self, model: str) -> None:
        self.model_name = model
        self.provider_config["model"] = model

    # ---- 核心：真正"让模型干活"的入口 ----

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: Any = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """文本对话（照真实现 `provider.py:96` 的签名）。

        🔴 **这是插件最依赖的一个方法** —— 记忆抽取/总结、日程生成、关系分析
           全走它（实测调用点：`memory_companion/core/service.py:7519`、
           `summarizer.py:77`、`audit.py:334`）。

        实现方式：**转调宿主的 `LLMPort.generate()`**（我们已经能调上游），
        再把结果包成真实现的 `LLMResponse`。

        ⚠️ 返回**必须是 `LLMResponse`**（不是裸字符串）—— 插件会读
           `.completion_text`（它是 property，见 `compat/core/provider/entities.py`）。
           返回字符串会让插件拿到 `AttributeError` 或静默空值。
        """
        from astrbot_host.compat.core.provider.entities import LLMResponse

        if self._llm is None:
            # 🔴 **不伪造**：没有模型能力就如实抛错，让插件走它自己的降级/报错路径。
            #    返回一个假回复会让"记忆抽取"写进编造的内容（比失败更糟）。
            raise RuntimeError(
                f"供应商 {self.provider_id!r} 未接入模型调用端口（HostPorts.llm 为空）"
            )

        # 组装消息：真实现语义是 prompt 追加到 contexts 之后
        messages: list[dict[str, Any]] = []
        if isinstance(contexts, list):
            messages.extend(contexts)
        # 🔴 **工具调用结果必须拼成消息喂回上游**（2026-09-22 修）。
        #
        #   原来 `tool_calls_result` 在 `_IGNORED_TEXT_CHAT_KWARGS` 里**刻意不转发**
        #   （当时的理由："转发会让上游 400"）—— 但真因不是"不该转发"，
        #   而是**转发的是裸对象**。真实现（`openai_source.py:983-990`）是：
        #
        #       context_query.extend(tool_calls_result.to_openai_messages())
        #
        #   即先转成 **1 条 role="assistant"（带 tool_calls）+ N 条 role="tool"**
        #   再并入消息列表。不转就丢对象 → 上游 400；转了才是对的协议。
        #   → 不转发的实际后果：**工具结果永远回不到模型**，
        #     模型只能重复调同一个工具（或干脆不知道工具跑没跑）。
        for block in _iter_tool_calls_results(tool_calls_result):
            try:
                messages.extend(block.to_openai_messages())
            except Exception:  # noqa: BLE001
                # ⚠️ 不静默：这是"工具闭环断掉"的关键节点
                logger.error(
                    "tool_calls_result 无法转成 OpenAI 消息（本轮工具结果将丢失）",
                    exc_info=True,
                )
        if prompt:
            messages.append({"role": "user", "content": prompt})

        try:
            # 🔴 `tools=` 必须**转成 OpenAI schema**再交给上游。
            #    插件给的是 `ToolSet` **对象**（`page_api.py:4217`），
            #    原样透传会 `TypeError: Object of type ToolSet is not
            #    JSON serializable`，而那个异常还会被下游的
            #    `except TypeError` 误当成"签名不匹配"，最后报成
            #    「上游 base_url 为空」—— **离根因隔了三层**。
            #    详见 `response_bridge.normalize_tools` 的 docstring。
            tools_schema = normalize_tools(func_tool)
            out = await self._llm.generate(
                prompt=prompt,
                messages=messages or None,
                system_prompt=system_prompt,
                model=model or self.get_model() or None,
                tools=tools_schema,
                **{
                    k: v
                    for k, v in kwargs.items()
                    # 过滤掉真实现专有、我们不认识的参数（不静默丢业务语义，
                    # 但也别让它们把上游请求搞坏）
                    if k not in _IGNORED_TEXT_CHAT_KWARGS
                },
            )
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 记 ERROR 后**继续抛**：插件的 `token_budget` 会捕获并记录用量/重试；
            #    吞掉异常会让它以为"调用成功但返回空"（静默失败）。
            logger.error(
                "供应商 %s 调用模型失败：%s", self.provider_id, exc, exc_info=True
            )
            raise

        # ---- 把我们的返回值包成 LLMResponse ----
        #
        # 🔴🔴 **必须走 `response_bridge.to_llm_response`，不许在这里手拼**
        #    （2026-09-21 修的真 bug）。原来这里只搬了文本：
        #
        #        resp = LLMResponse(completion_text=_extract_text(out))
        #
        #    → `ChatResult.tool_calls` 与 `reasoning_content` 被**静默丢弃**。
        #    插件读的是 `resp.tools_call_name` / `tools_call_args`
        #    （实测消费点 `main.py:19298/:19363`、`llm_tool_actions.py:2474`、
        #     `daily_state.py:14764`、`hdsi_experiment.py:573`）→ 恒为 `[]`
        #    ⇒ **插件的 function calling 在这个宿主上完全失效，且零日志**。
        #
        #    ⚠️ `tools=` 必须传下去：真实现在 `tools is None` 时**不解析**
        #       tool_calls（`openai_source.py:881`），我们要照抄那个前提。
        return to_llm_response(out, tools=func_tool)

    async def text_chat_stream(self, *args: Any, **kwargs: Any) -> Any:
        """流式文本对话。

        ⚠️ 我们**只提供非流式**：真实现的流式要逐块 yield，而插件调用它时会
           `async for`。与其假装流式（一次性吐完），不如**如实不支持** ——
           插件的 `token_budget` 对非流式有正常分支，而假流式会让它拿到
           不完整的累积逻辑。判据里有专门一条守这个"如实"。
        """

        async def _single() -> Any:
            yield await self.text_chat(*args, **kwargs)

        return _single()

    def __repr__(self) -> str:
        return (
            f"<Provider id={self.provider_id!r} model={self.get_model()!r} "
            f"type={PROVIDER_TYPE_CHAT}>"
        )


#: `text_chat` 上真实现专有、我们**不转发给上游**的参数。
#: ⚠️ 列出来是**刻意**的：静默丢弃业务参数是坏味道，
#:    但这些是"重试策略/回调"之类的宿主侧概念，转发会让上游 400。
#:
#: 🔴 **2026-09-22 移出 `tool_calls_result`**（原来在这里，是错的）。
#:    它不是"宿主侧概念"，而是**要拼成消息喂回上游的协议内容**
#:    （真实现 `openai_source.py:983-990`）—— 见 `text_chat` 里的处理。
_IGNORED_TEXT_CHAT_KWARGS = frozenset(
    {
        "request_max_retries",
        "session_id",
        "image_urls",
        "audio_urls",
        "extra_user_content_parts",
        "tool_choice",
    }
)


def _iter_tool_calls_results(value: Any) -> list[Any]:
    """把 `tool_calls_result` 归一成 `list[ToolCallsResult]`。

    真实现的字段类型是 `list[ToolCallsResult] | ToolCallsResult | None`
    （`entities.py` 的 `ProviderRequest.tool_calls_result`），所以三种都要认。
    认不出的**如实跳过并告警**（不静默丢业务内容）。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None]
    return [value]


def _extract_text(out: Any) -> str:
    """从 `LLMPort.generate()` 的返回值里取文本。

    ⚠️ **保留本函数只为了向后兼容**（外部/旧判据可能 import 它）。
       真正的取值逻辑已收归 `response_bridge.extract_text` ——
       **不许在这里再写一份**，否则又是"同一语义两处实现"。
    """
    return extract_text(out)


class ProviderManager:
    """供应商管理器（照真实现的关键面）。

    🔴 **两个取值路径都要提供**（实测 `page_api.py:22097-22105`）：
       ① `context.get_all_providers()`  → 优先；
       ② `provider_manager.inst_map`    → 兜底（**字典**，值为 Provider）。
    只给一个会让另一条分支拿到空列表 → 面板显示"无可用供应商"。

    ⚠️ 真实现的 `inst_map` 是 `dict[str, Provider]`（`manager.py:219` 那样赋值），
       我们照抄**字典**形状。
    """

    def __init__(self) -> None:
        #: 🔴 名字照真实现：`inst_map`（不是 `providers`/`insts`）
        self.inst_map: dict[str, Provider] = {}
        #: 当前默认对话供应商 id（空 = 未设置 → `get_using_provider` 返回 None）
        self.default_provider_id: str = ""

    # ---- 注册/查询 ----

    def register(self, provider: Provider, *, make_default: bool = False) -> None:
        self.inst_map[provider.provider_id] = provider
        if make_default or not self.default_provider_id:
            self.default_provider_id = provider.provider_id

    def unregister(self, provider_id: str) -> bool:
        gone = self.inst_map.pop(provider_id, None) is not None
        if gone and self.default_provider_id == provider_id:
            # 删掉默认供应商后，把默认让给剩下的第一个（否则会留下悬空 id）
            self.default_provider_id = next(iter(self.inst_map), "")
        return gone

    def get_provider_by_id(self, provider_id: str) -> Provider | None:
        """按 id 取（插件 `main.py:19204` 等用它）。

        ⚠️ id 为空时返回 None（不是"随便给一个"）—— 插件据此判断"没配"。
        """
        pid = str(provider_id or "").strip()
        if not pid:
            return None
        return self.inst_map.get(pid)

    def get_all(self) -> list[Provider]:
        return list(self.inst_map.values())

    def get_using(self, umo: str | None = None) -> Provider | None:
        """当前默认对话供应商（照真实现 `manager.py:281`）。

        ⚠️ 真实现支持"按会话偏好选 provider"；我们**单用户**，
           所以 `umo` 收下但不做区分（**如实**：不假装支持会话隔离）。
        """
        if not self.default_provider_id:
            return None
        return self.inst_map.get(self.default_provider_id)

    def set_default(self, provider_id: str) -> bool:
        """设默认供应商。id 不存在时**不设**（返回 False）——不许留悬空默认值。"""
        if provider_id not in self.inst_map:
            return False
        self.default_provider_id = provider_id
        return True

    def describe(self) -> list[dict[str, Any]]:
        """诊断视图（供面板/状态接口用，**不脱敏 key**——那是调用方的事）。"""
        return [
            {
                "id": p.provider_id,
                "name": p.name,
                "model": p.get_model(),
                "type": PROVIDER_TYPE_CHAT,
                "is_default": p.provider_id == self.default_provider_id,
            }
            for p in self.inst_map.values()
        ]


__all__ = [
    "ADAPTER_TYPE_OPENAI_CHAT",
    "PROVIDER_TYPE_CHAT",
    "Provider",
    "ProviderManager",
]
