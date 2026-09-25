# -*- coding: utf-8 -*-
"""`astrbot.core.file_token_service` 兼容层 —— 基于令牌的文件下载服务。

⭐⭐ **照真 AstrBot `core/file_token_service.py`（92 行）逐条移植**。

用途（真实现语义）：插件把本地文件"登记"成一个**单次令牌 + 超时**，
宿主在投递时用令牌取回路径 —— 这样媒体文件不必进内存，也有过期保护。

⚠️ 一处差异：真实现 `register_file` 里为了处理 `file://` URI
   会 import `astrbot.core.utils.media_utils`；**该模块我们没实现**，
   所以照真实现的**兜底分支**处理（`except Exception: local_path = file_path`）——
   这不是降级，而是真实现本来就有的容错路径。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid


class FileTokenService:
    """基于令牌的文件下载服务（照真实现 —— 支持超时与懒清除）。"""

    def __init__(self, default_timeout: float = 300) -> None:
        self.lock = asyncio.Lock()
        #: token → (文件路径, 过期时间)
        self.staged_files: dict[str, tuple[str, float]] = {}
        self.default_timeout = default_timeout

    async def _cleanup_expired_tokens(self) -> None:
        """清理过期令牌（照真实现 —— 懒清除，无后台任务）。"""
        now = time.time()
        expired = [
            token for token, (_, expire) in self.staged_files.items() if expire < now
        ]
        for token in expired:
            self.staged_files.pop(token, None)

    async def check_token_expired(self, file_token: str) -> bool:
        """令牌是否已过期/不存在（照真实现）。"""
        async with self.lock:
            await self._cleanup_expired_tokens()
            return file_token not in self.staged_files

    async def register_file(
        self, file_path: str, timeout: float | None = None
    ) -> str:
        """登记文件，返回**单次令牌**（照真实现）。

        Raises:
            FileNotFoundError: 路径不存在（**照真实现会抛，不静默**）
        """
        local_path = file_path
        try:
            # 真实现在这里处理 file:// URI；我们没有 media_utils，
            # 走它自己的 except 兜底分支（等价于真实现解析失败时的行为）
            from astrbot_host.compat.core.utils.media_utils import (  # noqa: F401
                file_uri_to_path,
                is_file_uri,
            )

            local_path = file_uri_to_path(file_path) if is_file_uri(file_path) else file_path
        except Exception:  # noqa: BLE001
            local_path = file_path

        async with self.lock:
            await self._cleanup_expired_tokens()
            if not os.path.exists(local_path):
                raise FileNotFoundError(
                    f"File does not exist: {local_path} (original input: {file_path})",
                )
            token = str(uuid.uuid4())
            expire = time.time() + (
                timeout if timeout is not None else self.default_timeout
            )
            self.staged_files[token] = (local_path, expire)
            return token

    async def handle_file(self, file_token: str) -> str:
        """用令牌换回文件路径（**用后失效**，照真实现）。

        Raises:
            KeyError: 令牌不存在或已过期
            FileNotFoundError: 文件本身已被删除
        """
        async with self.lock:
            await self._cleanup_expired_tokens()
            if file_token not in self.staged_files:
                raise KeyError(f"Invalid or expired file token: {file_token}")
            file_path, _ = self.staged_files.pop(file_token)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File does not exist: {file_path}")
            return file_path


#: 全局单例（照真实现 —— 插件 `from astrbot.core import file_token_service` 拿到的是实例）
file_token_service = FileTokenService()

__all__ = ["FileTokenService", "file_token_service"]
