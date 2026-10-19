# -*- coding: utf-8 -*-
"""`ImageResolver` —— 把图片引用解析成模型可投喂的形式。

⭐ 依据：真 AstrBot `core/utils/quoted_message/image_resolver.py`（v4.27.5）。

## 契约（陪伴 `core_reply_media.py:126`）

```python
resolved = await ImageResolver(event, client).resolve_for_llm(image_refs)
# → list[str]，长度可能**小于**输入（无法解析的会被跳过）
```

⚠️ **跳过是允许的，但必须留日志**。真实现静默过滤无法解析的引用 ——
那就是"用户引用了一张图、模型看不到、谁也不知道为什么"的经典静默降级。
我们照做"跳过"，但**每跳一个都 warning 一次**（带原因）。

## 解析顺序（照真实现 `:73-96`）

1. `base64://...` → 原样保留（模型侧能直接吃 data url 的调用方自己处理）
2. `http(s)://...` → 原样保留
3. 其它（`file://`、裸文件名、OneBot 的 `file` 字段）→ 尝试走 OneBot
   `get_image` 换成可访问地址

## 与真实现的差异

真实现有一套"媒体白名单 + 大小上限"策略（`settings`）。我们**不用** ——
自用场景，图都是自己的；加了白名单反而会把某些适配器的地址挡掉。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot")

__all__ = ["ImageResolver"]


class ImageResolver:
    """图片引用解析器（照真实现 `image_resolver.py:64-120`）。"""

    def __init__(self, event: Any = None, client: Any = None, settings: Any = None) -> None:
        self._event = event
        self._client = client
        self._settings = settings

    async def resolve_for_llm(self, image_refs: list[str]) -> list[str]:
        """逐个解析（照真实现 `:73-96`）。"""
        out: list[str] = []
        for ref in image_refs or []:
            if not isinstance(ref, str) or not ref.strip():
                continue
            try:
                resolved = await self._resolve_one(ref.strip())
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "引用图片解析异常（已跳过）：ref=%.80s error_type=%s %s",
                    ref, type(error).__name__, error,
                )
                continue
            if resolved:
                out.append(resolved)
            else:
                # ⚠️ 不静默：这正是"引用图看不见"的现场
                logger.warning(
                    "引用图片无法解析，已跳过（模型将看不到这张图）：ref=%.80s", ref
                )
        return out

    async def _resolve_one(self, image_ref: str) -> str | None:
        """单个引用 → 可投喂地址（照真实现 `:97-120`）。"""
        if image_ref.startswith(("http://", "https://", "base64://", "data:")):
            return image_ref

        # 其余形状交给 OneBot 的 get_image 换一次可访问地址
        client = self._client
        if client is None:
            return None
        payload = await client.call("get_image", file=image_ref)
        if not isinstance(payload, dict):
            return None
        for key in ("url", "file", "path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
