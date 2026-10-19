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

    @property
    def meta(self) -> Any:
        """provider 元信息（真实现返回 `ProviderMeta`）。"""
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
    """OpenAI 官方 provider（真实现 `openai_source.py:51`）。"""

    PROVIDER_TYPE = "openai_chat_completion"


class ProviderFishAudioTTSAPI(Provider):
    """FishAudio TTS provider（真实现 `fishaudio_tts_api_source.py:46`）。"""

    PROVIDER_TYPE = "text_to_speech"

    async def get_audio(self, text: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "ProviderFishAudioTTSAPI.get_audio() 在本宿主里不提供：它是类型标记。"
            "真正的语音合成走宿主的 TTS 端口。"
        )
