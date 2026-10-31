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

#: 🔴 插件的精确类型校验要的就是这个类（见文件末尾 `_mount_implementation` 的说明）。
from astrbot_host.compat.core.provider.sources.providers import (  # noqa: E402
    ProviderOpenAIOfficial,
)


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

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        """构造一个 **类型精确为 `ProviderOpenAIOfficial`** 的实例。

        🔴 **为什么要 `__new__` 偷换类型**（2026-10-29 实测）：
           插件 `companion_v2/adapters/astrbot/provider.py:188` 是**精确类型比较**：

           ```python
           if type(self.native) is not provider_type:   # ProviderOpenAIOfficial
               raise ValueError("provider needs the verified native provider class")
           ```

           `type(x) is C` ⇒ **任何子类都不通**。所以 `Provider(...)` 必须真的返回
           一个 `ProviderOpenAIOfficial` 实例，而不是 `Provider` 实例。

        ⚠️ **`__init__` 必须在这里手动调**：CPython 只在
           `isinstance(返回值, cls)` 时才自动调用 `cls.__init__`。
           我们返回的是**兄弟类**的实例（不是 `Provider` 的子类），
           所以解释器**不会**再调 `__init__` —— 不手动调就会得到一个没初始化的空对象
           （症状是后续属性访问全 AttributeError，极难查）。
        """
        obj = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
        Provider.__init__(obj, *args, **kwargs)
        return obj

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
        self._build_native_client(base_url=base_url, api_key=api_key)

    # ---- 原生 client 面（插件 `AstrBotTextProvider` 直接依赖）----

    def _build_native_client(self, *, base_url: str, api_key: str) -> None:
        """照真实现建 `client` + `default_params`（`openai_source.py:372-395`）。

        🔴 **为什么不复用 `_llm`（我们的 `UpstreamLLMPort`）**：
           插件不是"调用宿主的发送函数"，而是**拿 client 自己拼 payload、自己解响应**
           （`companion_v2/.../model_wire.py` 的 `build_wire` / `decode_response`），
           还会 `with_options(max_retries=0)` 关掉 SDK 重试层。
           所以必须给一个**真的 `AsyncOpenAI` 实例**，形状才与真实现一致。

        ⚠️ 三个如实降级（不伪造 —— 本项目铁律）：
          * `base_url` 为空 → `client=None`（插件在 `_current()` 里会明确报
            `host_provider_changed`，不是静默空回复）；
          * `openai` 包缺失 → `client=None` + 一条 ERROR；
          * `default_params` 用**与真实现同一份算法**取
            （`inspect.signature(client.chat.completions.create).parameters.keys()`），
            不是我猜的固定列表。
        """
        self.client: Any = None
        self.default_params: Any = frozenset()
        if not base_url:
            logger.warning(
                "供应商 %s 没有 base_url —— 不建原生 client，"
                "插件侧模型链路会明确报 host_provider_changed（不是静默降级）",
                self.provider_id,
            )
            return
        try:
            import inspect

            from openai import AsyncOpenAI
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "供应商 %s 无法建原生 client（openai 包不可用：%s）"
                "—— 插件侧模型链路将不可用",
                self.provider_id, exc,
            )
            return
        try:
            self.client = AsyncOpenAI(
                api_key=api_key or "not-needed",
                base_url=base_url,
                timeout=float(self._timeout),
            )
            self.default_params = inspect.signature(
                self.client.chat.completions.create
            ).parameters.keys()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "供应商 %s 建原生 client 失败：%s: %s",
                self.provider_id, type(exc).__name__, exc,
            )
            self.client = None
            self.default_params = frozenset()

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

        from astrbot_host.compat.api.provider import ProviderRequest
        from astrbot_host.provider_content import provider_request_to_messages

        request = ProviderRequest(
            prompt=prompt, contexts=contexts if isinstance(contexts, list) else [],
            image_urls=image_urls or [], audio_urls=audio_urls or [],
            extra_user_content_parts=extra_user_content_parts or [],
            tool_calls_result=tool_calls_result,
        )
        messages = provider_request_to_messages(request, include_system=False)

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

    # ---- 原生 provider 契约：`_prepare_chat_payload` / `_query` ----
    #
    # 🔴🔴 **2026-10-30 补的真缺口（生产实测）**：陪伴 7.0.0 的主动联系/记忆等
    #    后台任务**不走 `text_chat`**，而是直接调用 provider 的**原生私有方法**：
    #
    #        payload, _ = await native._prepare_chat_payload(
    #            prompt=None, contexts=..., model=...)
    #        response = await native._query(payload, tools)
    #
    #    （官方 `companion_v2/adapters/astrbot/provider.py:396` 与 `:444`；
    #      真 AstrBot 实现在 `core/provider/sources/openai_source.py:949` / `:531`）
    #
    #    我们原来**两个都没有** ⇒ 插件抛 `AttributeError` ⇒ 被它归类成
    #    `request_preparation_failed` / `not_started` ⇒ 第 8 步「主动消息测试」
    #    的真实返回就是 `model_not_started`（日志原文：
    #    `模型请求准备失败 provider=cpa/... failure=request_preparation_failed
    #      error_type=AttributeError`），**且页面只显示"模型或投递链路失败"**。
    #
    #    ⚠️ 我们**不伪造**这两步：`_prepare_chat_payload` 只做官方那份"把
    #       contexts/工具结果拼成 messages + 加 model"的纯组装（没有任何网络
    #       动作，所以可以如实实现）；`_query` 则转调 `text_chat`，由它走真实
    #       上游并复用已验证的 `to_llm_response` 解析。

    async def _prepare_chat_payload(
        self,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: Any = None,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """组装聊天 payload（照真实现 `openai_source.py:949` 的语义）。

        返回 `(payloads, context_query)`：
          * `payloads` = `{"messages": [...], "model": ...}` —— 插件随后会往里塞
            `max_tokens` / `response_format` 等字段，再交给 `_query`；
          * `context_query` 是同一个 messages 列表（真实现也返回两份）。

        ⚠️ 与真实现的**唯一差异**：我们不做 `_finally_convert_payload` 那些
           针对特定厂商的字段改写（MiMo reasoning 回传、Gemini think part 等），
           因为我们不掌握上游厂商分支 —— 与其猜错静默改写，不如原样交给上游。
           插件在这里只依赖 `messages` 与 `model` 两个键。
        """
        from astrbot_host.compat.api.provider import ProviderRequest
        from astrbot_host.provider_content import provider_request_to_messages

        request = ProviderRequest(
            prompt=prompt,
            contexts=contexts if isinstance(contexts, list) else [],
            image_urls=image_urls or [],
            audio_urls=audio_urls or [],
            extra_user_content_parts=extra_user_content_parts or [],
            tool_calls_result=tool_calls_result,
        )
        messages = provider_request_to_messages(request, include_system=bool(system_prompt))
        if system_prompt:
            # `include_system=True` 时 provider_content 已把 system 放在最前；
            # 这里再兜一次，避免不同实现路径下漏掉系统提示。
            if not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {"role": "system", "content": system_prompt})

        # 真实现会剔除内部标记键（`_no_save`）；插件可能带着它进来。
        for part in messages:
            part.pop("_no_save", None)

        payloads: dict[str, Any] = {
            "messages": messages,
            "model": model or self.get_model(),
        }
        return payloads, messages

    async def _query(
        self,
        payloads: dict[str, Any],
        tools: Any = None,
        *,
        request_max_retries: int | None = None,
    ) -> Any:
        """执行一次原生模型请求（照真实现 `openai_source.py:531`）。

        🔴 **必须返回 `LLMResponse`**（插件读 `.completion_text` /
           `.tools_call_*`）；转调 `text_chat` 是为了复用**同一条已验证链路**，
           而不是再写一套请求/解析。
        """
        if not isinstance(payloads, dict):
            raise TypeError("payloads must be an object")
        messages = payloads.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("payloads.messages must be a non-empty list")

        # 🔴🔴 `LLMResponse` 必须挂上**真实原始 completion**。
        #    陪伴 7.0 的 `model_wire.decode_response`（`model_wire.py:244`）
        #    直接读 `response.raw_completion.choices[0].message/.finish_reason`：
        #
        #        completion = response.raw_completion
        #        if len(completion.choices) != 1: raise ...
        #        choice = completion.choices[0]
        #
        #    只返回 `LLMResponse`（raw 为空）⇒ `completion_present=False`、
        #    `finish_reason=missing` ⇒ 判成 `response_contract_rejected` /
        #    `model_usage_unknown`（生产实测，2026-10-30）。
        #    真 AstrBot 的 `_query` 也是把 SDK 的 completion 交给 `LLMResponse`
        #    解析后**保留在 `raw_completion`** 上，我们照此办理。
        result = await self._llm.generate(
            prompt=None,
            messages=messages or None,
            system_prompt=None,
            model=payloads.get("model") or self.get_model() or None,
            tools=normalize_tools(tools),
        )
        response = to_llm_response(result, tools=tools)
        # 🔴 `raw_completion` **必须无条件构造**，不能等后端填 `raw`
        #    （我们后端 `ChatResult.raw` 目前为空 ⇒ 只在 raw 非空时才构造
        #      等于永远不构造，插件的 wire 校验就会一直 `completion_present=False`）。
        #    字段取自**已经拿到的真实结果**：content / finish_reason / tool_calls。
        try:
            from types import SimpleNamespace

            calls = []
            for call in (getattr(result, "tool_calls", None) or []):
                function = (call or {}).get("function") or {}
                calls.append(SimpleNamespace(
                    id=(call or {}).get("id"),
                    type=(call or {}).get("type") or "function",
                    function=SimpleNamespace(name=function.get("name"),
                                             arguments=function.get("arguments", "{}")),
                ))
            # ⚠️ 必须补齐官方 `ChatCompletion.Choice.Message` 上**会被读到的**
            #    字段：`refusal`（`explicit_refusal()` 会读它）与
            #    `model_fields_set`（`_extract_reasoning_content` 会读它）。
            #    缺了就是 `AttributeError` ⇒ `native_response_parse_failed`
            #    （生产实测：`error_type=AttributeError`、`completion_present=true`）。
            message = SimpleNamespace(
                content=(getattr(result, "content", "") or None),
                tool_calls=(calls or None),
                refusal=None,
                reasoning_content=(getattr(result, "reasoning_content", "") or None),
            )
            message.model_fields_set = set()
            # ⚠️ 官方 `_parse_openai_completion` 还会读 `completion.usage` /
            #    `completion.id` / `choice.refusal`；插件用的是**官方那个类方法**
            #    （`provider_type._parse_openai_completion`），不是我们的实例方法，
            #    所以这些字段一个都不能少（缺了就是 AttributeError）。
            usage = getattr(result, "usage", None)
            response.raw_completion = SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason=(getattr(result, "finish_reason", "") or None),
                    message=message,
                    refusal=None,
                )],
                id="",
                model=(getattr(result, "model", "") or None),
                usage=SimpleNamespace(
                    prompt_tokens=int((usage or {}).get("prompt_tokens", 0) or 0),
                    completion_tokens=int((usage or {}).get("completion_tokens", 0) or 0),
                    total_tokens=int((usage or {}).get("total_tokens", 0) or 0),
                    prompt_tokens_details=None,
                    completion_tokens_details=None,
                ),
            )
        except Exception:  # noqa: BLE001 — 原始体属于附加信息，失败不该毁掉已拿到的回复
            logger.warning("供应商 %s 无法构造原始 completion（插件将失去原生校验依据）",
                           self.provider_id, exc_info=True)

        # 🔴🔴 **必须回调 `_parse_openai_completion(completion, tools)`**。
        #    真 AstrBot 的 `_query` 在拿到 SDK completion 后就是这一步
        #    （`openai_source.py:585`）：
        #
        #        llm_response = await self._parse_openai_completion(completion, tools)
        #
        #    陪伴 7.0 会把这个方法**替换成它自己的 `parse()`**
        #    （`companion_v2/adapters/astrbot/provider.py:388`），用来
        #    校验 wire 协议、记录 `observed`、并产出最终文本。
        #    漏掉它 ⇒ 它的 `observed` 永远为空 ⇒ `completion_present=False`
        #    ⇒ `response_contract_rejected` / `model_usage_unknown`
        #    （生产实测 2026-10-30；这是第 8 步模型阶段最后一个缺口）。
        parser = getattr(self, "_parse_openai_completion", None)
        if callable(parser):
            try:
                parsed = parser(response.raw_completion, tools)
                if hasattr(parsed, "__await__"):
                    parsed = await parsed
                if parsed is not None:
                    return parsed
            except Exception:
                # 插件自己的 parse 会做严格校验并抛出它要的错；
                # 这里的异常**必须原样抛给插件**，否则它会误以为成功。
                raise
        return response

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


# ======================================================================
# 🔴🔴 让 `ProviderOpenAIOfficial` 本身成为可实例化的真实现类
# ======================================================================
#
# ## 为什么必须这么做（2026-10-29，实测踩过一次）
#
# 陪伴 7.0.0 的 `companion_v2/adapters/astrbot/provider.py:188` 用的是
# **精确类型比较**，不是 `isinstance`：
#
# ```python
# self.native = context.get_provider_by_id(provider_id)
# if type(self.native) is not provider_type:      # provider_type = ProviderOpenAIOfficial
#     raise ValueError("provider needs the verified native provider class")
# ```
#
# `type(x) is C` 要求 **`type(x)` 精确等于 `C`** ⇒ 任何子类都通不过：
#
#   * ❌ `class Provider(ProviderOpenAIOfficial)` → `type()` 是 `Provider` → 失败
#   * ✅ 直接实例化 `ProviderOpenAIOfficial` → `type()` 精确相等 → 通过
#
# 这与真 AstrBot 完全一致：真实现里 `ProviderOpenAIOfficial`（`openai_source.py:51`）
# 就是"既有类型标记、又有完整 text_chat 实现"的类，ProviderManager 装的就是它的实例。
#
# ## 为什么是"注入方法"而不是"继承"
#
# `Provider` 这个类在本项目里有**自己的历史**：它的构造签名是 keyword-only
# （`provider_id=` / `llm=` …），跟真实现 `(provider_config, provider_settings)`
# 完全不同，而且 `host/tests/test_provider.py` 有 20+ 处按这个签名在构造。
# 为了不推翻这些，做法是：**把 `Provider` 的实现方法整体拷贝到
# `ProviderOpenAIOfficial` 上**，再让 ProviderManager 实例化后者。
#
# ⚠️ 副作用（有意为之）：`Provider` 与 `ProviderOpenAIOfficial` 现在共享同一批
#    方法实现，但**类型不同**。插件只认后者（因为它做精确比较），
#    我们自己的测试只认前者。这样两边都不用改。

#: `compat` 里**特意重写**、不允许被 `Provider` 的实现覆盖的成员。
#: 这三项在 `providers.py` 里都有更贴合插件契约的版本（含注释与如实降级）。
_KEEP_TARGET_IMPLEMENTATION = frozenset({
    "get_model",
    "set_model",
    "_query_stream",
})


def _mount_implementation(target: type) -> None:
    """把 `Provider` 的实现方法挂到 `target` 上（不覆盖 target 已有的同名属性）。

    Args:
        target: 目的类（这里就是 `ProviderOpenAIOfficial`）。

    ⚠️ **不覆盖已有的**：`ProviderOpenAIOfficial` 自己在 compat 里定义了
       `get_model` / `set_model` / `_query_stream`（那三项是我们特意写的，
       与 `Provider` 的实现等价但注释更贴合插件契约）。
       覆盖会让"哪份实现生效"变得不可预测 —— 那是本项目最忌讳的隐式行为。
    """
    for name, member in vars(Provider).items():
        if name.startswith("__") and name not in ("__init__", "__repr__"):
            continue
        if name in ("__dict__", "__weakref__", "__doc__", "__module__"):
            continue
        # 🔴 只跳过 compat 里**特意重写**的那三项（它们与 Provider 的实现在语义上
        #    等价，但注释更贴合插件契约）。其余一律用 Provider 的实现覆盖 ——
        #    compat 的 `Provider` 基类里有一份 "text_chat 抛 NotImplementedError"
        #    的**占位实现**，不覆盖它 ⇒ 宿主的 provider 永远调不通
        #    （实测症状：9 个 provider 测试全红，报 "这些类只是类型标记"）。
        if name in _KEEP_TARGET_IMPLEMENTATION:
            continue
        try:
            setattr(target, name, member)
        except (AttributeError, TypeError) as exc:  # noqa: BLE001
            logger.warning(
                "把 Provider.%s 挂到 %s 上失败（%s）—— 该能力将不可用",
                name, target.__name__, exc,
            )
    # 🔴 `__init__` 必须挂：它的 keyword-only 签名是这个项目的历史契约。
    setattr(target, "__init__", Provider.__init__)


_mount_implementation(ProviderOpenAIOfficial)


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
        #: 🔴 人格管理器（真实现 `manager.py:130` 的 `persona_mgr`）。
        #: 由宿主装配时注入；未注入 ⇒ `personas` 返回空列表（如实"没有人格"）。
        self.persona_manager: Any = None

    # ---- 人格转发（2026-10-19 补，真实现 `core/provider/manager.py:136-138`）----
    #
    # 🔴 **真实现的 `personas` 是 property，转发给 persona_mgr**：
    #
    #     @property
    #     def personas(self) -> list:
    #         return self.persona_mgr.personas_v3
    #
    # 我们**有** persona_manager（`astrbot_host/persona_manager.py`，它的
    # `personas` / `personas_v3` 同源同形），但 `ProviderManager` 之前没转发
    # ⇒ 插件写 `context.provider_manager.personas` 直接 AttributeError。
    #
    # 实测现场（2026-10-19）：`meme_manager/main.py:821`
    # `personas = self.context.provider_manager.personas` → 插件加载失败。
    # 那是**别人的插件**，但它暴露的是**我们真实的兼容缺口**（真 AstrBot 有这属性）。
    #
    # ⚠️ **没注入 persona_manager 时返回空列表**（不是抛错）：
    #    真实现里 `persona_mgr` 总是存在；我们没有时"没有人格"是如实状态，
    #    而插件对空列表有处理路径。**但这里绝不编造人格**。

    def _persona_source(self) -> Any:
        """找人格来源：优先显式注入的，其次从 Context 拿。"""
        if self.persona_manager is not None:
            return self.persona_manager
        return None

    @property
    def personas(self) -> list:
        """动态获取最新 personas（照真实现 `manager.py:136-138`）。"""
        source = self._persona_source()
        value = getattr(source, "personas_v3", None) if source is not None else None
        if isinstance(value, list):
            return list(value)
        return []

    @property
    def persona_configs(self) -> list:
        """动态获取最新 persona 配置（照真实现 `manager.py:131-134`）。"""
        source = self._persona_source()
        value = getattr(source, "persona_v3_config", None) if source is not None else None
        if isinstance(value, list):
            return list(value)
        # 真实现里 `persona_v3_config` 与 `personas_v3` 同源，退化到它
        return self.personas

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
