# -*- coding: utf-8 -*-
"""`astrbot.core.provider.sources` —— 具体 provider 实现包（兼容层）。

⚠️ 我们**不实现真正的 provider**（宿主自己的 provider 在
`astrbot_host/provider.py`）。这里提供的类**只作为类型标记**，
用于插件 `isinstance(obj, ProviderOpenAIOfficial)` 这类判断。

🔴 为什么要有它们（2026-10-19，陪 7.0.0 实测）：
   陪伴 `astrbot_host/native.py:18-19` **顶层** import 这两个类，并把它们塞进
   `NativeAdapterTypes(provider=ProviderOpenAIOfficial, ...)`。
   缺了 ⇒ 插件加载直接失败。它只用这两个类做 `isinstance` / 类型登记，
   从不实例化（真正的调用走宿主 provider.py）。
"""
from .providers import (  # noqa: F401
    Provider,
    ProviderFishAudioTTSAPI,
    ProviderGoogleGenAI,
    ProviderOpenAIOfficial,
)
from .request_retry import (  # noqa: F401
    REQUEST_RETRY_ATTEMPTS,
    retry_provider_request,
    retry_provider_request_context,
)

__all__ = [
    "Provider",
    "ProviderFishAudioTTSAPI",
    "ProviderGoogleGenAI",
    "ProviderOpenAIOfficial",
    "REQUEST_RETRY_ATTEMPTS",
    "retry_provider_request",
    "retry_provider_request_context",
]
