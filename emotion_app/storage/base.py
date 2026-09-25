# -*- coding: utf-8 -*-
"""存储抽象基类。

设计参照 AstrBot 陪伴插件的 `storage/backend_base.py`（见
docs/REF_ASTRBOT_COMPANION_STACK.md §2.5），但**刻意做减法**：
它 14 个方法，我们起步 9 个，且都围绕"自用单用户"。

⭐ **关键**：`export_store` / `import_store` / `next_revision` 从第一天就有，
   否则将来想换后端会被数据锁死（这是本项目从 aitag-site 学到的教训）。
"""
from __future__ import annotations

import abc
from typing import Any


class StorageError(Exception):
    """存储层错误。"""


class StoreBackend(abc.ABC):
    """存储后端接口。

    实现约定：
      * 所有方法都可能被异步调用；
      * `load_store` 返回**完整**文档（dict）；
      * `save_store` 是**整体覆盖**，`save_sections` 是**局部更新**；
      * `next_revision` 单调递增，用于乐观并发与导出对账。
    """

    # ---- 身份 ----

    @abc.abstractmethod
    def backend_name(self) -> str:
        """返回后端名（`sqlite` / `json`）。"""

    # ---- 读 ----

    @abc.abstractmethod
    async def load_store(self) -> dict[str, Any]:
        """载入完整文档；不存在时返回默认结构。"""

    @abc.abstractmethod
    async def load_sections(self, keys: list[str]) -> dict[str, Any]:
        """只载入指定 section（**不全量读**）。

        为什么需要：会话消息可能很多，`load_store()` 会把所有会话都读进来。
        按需读取让"打开一个会话"是 O(该会话) 而不是 O(全部)。
        """

    @abc.abstractmethod
    async def exists(self) -> bool:
        """存储是否已初始化。"""

    # ---- 写 ----

    @abc.abstractmethod
    async def save_store(self, data: dict[str, Any]) -> None:
        """整体覆盖保存。"""

    @abc.abstractmethod
    async def save_sections(self, sections: dict[str, Any]) -> None:
        """局部更新（只覆盖给定 section 键），避免整库重写。"""

    # ---- 版本 ----

    @abc.abstractmethod
    async def next_revision(self) -> int:
        """取得并自增 revision。用于乐观并发与导出对账。"""

    # ---- 可移植性（从第一天就有）----

    @abc.abstractmethod
    async def export_store(self) -> dict[str, Any]:
        """导出为可移植 dict（含 revision 与 schema 版本）。"""

    @abc.abstractmethod
    async def import_store(self, data: dict[str, Any], *, mode: str = "replace") -> None:
        """导入。mode: `replace` | `merge`。

        ⚠️ `replace` 必须**显式**调用——不做隐式覆盖（防误操作）。
        """

    # ---- 运维 ----

    @abc.abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """返回 `{"ok": bool, "backend": str, ...}`。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """释放连接。"""


#: 导入模式白名单
IMPORT_MODES = ("replace", "merge")


def validate_import_mode(mode: str) -> str:
    if mode not in IMPORT_MODES:
        raise StorageError(f"import mode 仅支持 {IMPORT_MODES}，当前 {mode!r}")
    return mode
