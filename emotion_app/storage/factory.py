# -*- coding: utf-8 -*-
"""存储后端工厂。

照 AstrBot 陪伴插件 `storage/factory.py` 的模式：**按名字造后端，调用方不关心实现**。
"""
from __future__ import annotations

from typing import Any

from ..infra.config import StorageConfig
from .base import StorageError, StoreBackend
from .sqlite_store import SqliteStore


def build_store(cfg: StorageConfig, *, default_factory: Any = None) -> StoreBackend:
    """按配置造存储后端。

    ⚠️ 只支持 `sqlite`（默认）。`json` 是**预留**的可插拔位——
    第一版不实现，因为情绪选了 SQLite；但接口在，将来加一个类即可。
    """
    name = str(cfg.backend or "sqlite").strip().lower()

    if name == "sqlite":
        return SqliteStore(cfg.sqlite_path, default_factory=default_factory)

    if name == "json":
        raise StorageError(
            "json 后端尚未实现（当前版本只提供 sqlite）。"
            "接口已预留：新增 JsonStore(StoreBackend) 并在本工厂注册即可。"
        )

    raise StorageError(f"未知存储后端：{name!r}")
