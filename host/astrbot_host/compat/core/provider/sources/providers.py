# -*- coding: utf-8 -*-
"""Provider 类型标记（`ProviderGoogleGenAI` / `ProviderOpenAIOfficial` / TTS）。

⭐ 依据：真 AstrBot `core/provider/sources/{gemini_source,openai_source,
fishaudio_tts_api_source}.py`（v4.27.5，只读参考）。

## 这些类在本项目里的**唯一用途**：类型标记

陪伴 `astrbot_host/native.py:18-19` 在**模块顶层**导入它们，并登记进
`NativeAdapterTypes(provider=ProviderOpenAIOfficial, video_provider_type=ProviderGoogleGenAI)`，
用于回答"宿主用的是哪种 provider"。它**从不实例化**这些类，也不调用它们的方法。

⚠️ 所以这里**只实现类型与最小元信息**，不实现 `text_chat()` 等真正发请求的方法。
如果有人真的 `ProviderOpenAIOfficial(...)` 然后 `await text_chat(...)`，
会得到 **`NotImplementedError` 且带明确说明** —— 而不是一个
"看起来能用但静默返回空"的假 provider（那是最坏的降级）。

## 为什么继承链要对齐

真实现：`ProviderGoogleGenAI(Provider)`、`ProviderOpenAIOfficial(Provider)`。
本项目的 `Provider` 端口在 `astrbot_host/compat/core/provider/__init__.py`
（`ProviderMeta` / `ProviderType` 一族）。这里提供一个**最小 `Provider` 基类**，
只为让 `isinstance(x, Provider)` 这类判断有确定答案。

🔴 **不要**把宿主的 `astrbot_host/provider.py` 里的类挂到这里 ——
那是**宿主实现**，插件不该看见它（会破坏"插件只依赖 AstrBot 契约"的边界）。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "Provider",
    "ProviderGoogleGenAI",
    "ProviderOpenAIOfficial",
    "ProviderFishAudioTTSAPI",
]


class Provider:
    """最小 chat provider 基类（真实现 `core/provider/provider.py:66`）。

    ⚠️ 只保留**构造与元信息**，不实现发请求的方法（见模块 docstring）。
    """

    #: provider 类型标签（真实现由子类覆盖，用于面板分组）
    PROVIDER_TYPE: str = "chat_completion"

    def __init__(
        self,
        provider_config: dict | None = None,
        provider_settings: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self.provider_config: dict = dict(provider_config or {})
        self.provider_settings: dict = dict(provider_settings or {})

    # ---- 元信息（真实现有；插件可能读）----

    def meta(self) -> Any:
        """provider 元信息（真实现 `provider/provider.py:43`）。

        🔴 **必须是方法，不是 property**（2026-10-29 修）：
           真实现是 `def meta(self) -> ProviderMeta`，调用方写 `provider.meta()`。
           我们原来写成了 `@property` ⇒ `provider.meta()` 抛
           `TypeError: 'ProviderMeta' object is not callable`。
           插件（及我们自己的 provider 端口）都按**方法**调，所以这里改回方法。
        """
        from astrbot_host.compat.core.provider.entities import ProviderMeta

        cfg = self.provider_config
        return ProviderMeta(
            id=str(cfg.get("id") or ""),
            model=str(cfg.get("model") or ""),
            type=str(cfg.get("type") or self.PROVIDER_TYPE),
        )

    def get_current_key(self) -> str:
        keys = self.get_keys()
        return keys[0] if keys else ""

    def get_keys(self) -> list[str]:
        keys = self.provider_config.get("key")
        if isinstance(keys, list):
            return [str(k) for k in keys if str(k)]
        if isinstance(keys, str) and keys:
            return [keys]
        return [""]

    def set_key(self, key: str) -> None:
        self.provider_config["key"] = [key] if key else []

    async def get_models(self) -> list[str]:
        model = self.provider_config.get("model")
        return [str(model)] if model else []

    # ---- 未实现（**明确报错，不静默**）----

    async def text_chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__}.text_chat() 在本宿主里不提供：这些类只是"
            "**AstrBot provider 的类型标记**，真正发请求的是宿主的 "
            "`astrbot_host/provider.py`。若你确实需要直接调用，请改走宿主 provider 端口。"
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.provider_config.get('id')!r}, "
            f"model={self.provider_config.get('model')!r})"
        )


class ProviderGoogleGenAI(Provider):
    """Google Gemini provider（真实现 `gemini_source.py:46`）。"""

    PROVIDER_TYPE = "google_gemini"


class ProviderOpenAIOfficial(Provider):
    """OpenAI 官方 provider（真实现 `openai_source.py:51`）。

    🔴 **2026-10-29：这个类从"纯类型标记"升级为"可实例化的真实现类"**。
       原因是一条**精确类型校验**（不是 `isinstance`）：

       ```python
       # companion_v2/adapters/astrbot/provider.py:188
       if type(self.native) is not provider_type:      # provider_type = ProviderOpenAIOfficial
           raise ValueError("provider needs the verified native provider class")
       ```

       `type(x) is C` 要求 **`type(x)` 精确等于 `C`** ⇒ 任何子类都不通过。
       所以宿主**必须直接实例化这个类本身**（与真 AstrBot 一致：真实现里
       `ProviderOpenAIOfficial` 就是那个既有类型又有实现的类，
       宿主的 provider 管理器装的就是它的实例）。

    ⚠️ `get_model` / `client` / `default_params` / `_query_stream` 这四项
       由宿主在 `register_provider_native()` 里注入（**不在这里写死**），
       因为它们的值与上游配置（base_url / api_key / model）绑定，
       而本模块属于"插件只依赖 AstrBot 契约"的边界，不该知道我们的配置结构。
    """

    PROVIDER_TYPE = "openai_chat_completion"

    #: 当前模型名（真实现 `provider.py:39` `get_model()` 读 `self.model_name`）。
    #: 宿主装配时注入；未注入 ⇒ 空串（插件据此判"没配"，不是静默给假值）。
    model_name: str = ""

    #: OpenAI SDK 实例（真实现 `openai_source.py:384`）。
    #: 宿主装配时注入；未注入 ⇒ None（插件在 `_current()` 里明确报
    #: `host_provider_changed`，**不是**静默空回复）。
    client: Any = None

    #: `client.chat.completions.create` 的签名参数名集合
    #: （真实现 `openai_source.py:392-394` `inspect.signature(...).parameters.keys()`）。
    default_params: Any = frozenset()

    def get_model(self) -> str:
        """当前模型名（插件 `provider.py:192` 直接读它）。"""
        return str(self.model_name or self.provider_config.get("model") or "")

    def set_model(self, model: str) -> None:
        self.model_name = str(model or "")
        self.provider_config["model"] = self.model_name

    async def _query_stream(self, *args: Any, **kwargs: Any) -> Any:
        """流式传输入口（真实现 `openai_source.py` 的流式方法）。

        ⚠️ 插件只在 `streaming=True` 时检查它是否存在
           （`provider.py:197` `if streaming and not callable(...)`）。
           存在但抛 NotImplementedError 是**如实**的：
           比"检查能过、真调时静默返回空"好得多。
        """
        raise NotImplementedError(
            "本宿主的 ProviderOpenAIOfficial 不支持流式传输（streaming=True）；"
            "请在插件设置里把 model.streaming 关掉。"
        )


    async def _parse_openai_completion(self, completion: Any, tools: Any = None) -> Any:
        """把原始 completion 解析成 `LLMResponse`（真实现 `openai_source.py:833`）。

        🔴🔴 **2026-10-30 补的真缺口（生产实测）**：
           陪伴 7.0 在模型回复处理阶段会**显式调用这个类方法**：

               parsed = await self.provider_type._parse_openai_completion(
                   native, completion, offered)

           （`companion_v2/adapters/astrbot/provider.py:376`，
             `provider_type` 就是我们这个 `ProviderOpenAIOfficial`）。

           它替换的只是**实例属性**；类上若没有这个方法 ⇒ 插件回落到
           类方法时抛 `AttributeError` ⇒ `native_response_parse_failed`
           ⇒ `model_usage_unknown`（第 8 步「主动消息测试」的最后一环）。

        实现取真实现的**核心语义**：从 `choices[0].message` 取文本与
        reasoning，从 `tools_call_*` 取工具调用；厂商特有的 payload 改写
        （`_finally_convert_payload`）**不做** —— 我们不在这一步猜测改写，
        交给上游即可。
        """
        # 🔴 **不在这里手拼 `LLMResponse`**：本项目硬约束是「只有一个构造入口」
        #    （`host/tests/test_response_bridge.py:391` 的源码级扫描判据）。
        #    统一走 `response_bridge.to_llm_response`。
        #    ⚠️ 它按**属性**读正文（`content` / `text` / `completion_text`），
        #       所以这里先把 completion 的 `choices[0].message` **摊平**成
        #       它认识的对象，再交出去 —— 不在本文件重复一份解析逻辑。
        from types import SimpleNamespace

        from astrbot_host.response_bridge import to_llm_response

        def read(value: Any, name: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(name, default)
            return getattr(value, name, default)

        choices = read(completion, "choices", ()) or ()
        if not choices:
            raise ValueError("model completion has no choices")
        choice = choices[0]
        message = read(choice, "message")
        content = read(message, "content")
        if isinstance(content, list):
            content = "".join(
                str(read(part, "text", "") or "")
                for part in content
                if read(part, "type") == "text" or isinstance(part, str)
            )
        flat = SimpleNamespace(
            content=None if content is None else str(content),
            reasoning_content=read(message, "reasoning_content"),
            tool_calls=read(message, "tool_calls"),
            usage=read(completion, "usage"),
            finish_reason=read(choice, "finish_reason"),
            model=read(completion, "model"),
        )
        response = to_llm_response(flat, tools=tools)
        try:
            response.raw_completion = completion
        except Exception:  # noqa: BLE001 — 原始体是附加信息
            pass
        return response


class ProviderFishAudioTTSAPI(Provider):
    """FishAudio TTS provider（真实现 `fishaudio_tts_api_source.py:46`）。"""

    PROVIDER_TYPE = "text_to_speech"

    async def get_audio(self, text: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "ProviderFishAudioTTSAPI.get_audio() 在本宿主里不提供：它是类型标记。"
            "真正的语音合成走宿主的 TTS 端口。"
        )
