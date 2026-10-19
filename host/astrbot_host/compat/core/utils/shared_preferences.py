# -*- coding: utf-8 -*-
"""`astrbot.core.utils.shared_preferences` 兼容层 —— 共享偏好（`SharedPreferences`）。

⭐ 依据：真 AstrBot `core/utils/shared_preferences.py`（v4.27.5，只读参考）。

## 为什么必须补（2026-10-19，陪伴 7.0.0 实测）

`astrbot_host/native.py:15` 顶层 `from astrbot.core import sp`，而 `native.py` 被
`main.py:36` 直接导入 ⇒ 缺它整个插件加载失败。陪伴用它读**每会话的服务设置**
（`session_service_config`），是"模型/语音按会话覆盖"的数据来源。

## 与真实现的差异（如实记录）

| 维度 | 真实现 | 这里 |
|---|---|---|
| 存储 | SQLite `preferences` 表（宿主 db_helper） | **宿主自己的 SQLite**（`data/preferences.db`） |
| 写盘 | FIFO 写队列 + 内存 overlay + 定期 flush | **同步写 + 内存 cache**（自用场景并发低） |
| 持久化等级 | 全局 | 进程内 + 跨重启（同库） |

⚠️ **没有实现的**：真实现的 `temporary_cache`（24h 自动清理）、APScheduler 定时
flush、`deprecated` 装饰的同步 API 全部**保留签名但走同一存储**。
同步 API 用独立连接读 —— 与真实现"同步读不阻塞异步池"的意图一致。

🔴 **`Preference` 的形状必须逐字段对齐**：陪伴 `native.py:238-242` 读的是
`row.scope_id` 与 `row.value["val"]`（**两级**：`value` 是 dict，真正的值在
`["val"]` 里）。少一层包装会让 `services` 恒为 `None` ⇒ 会话设置**静默失效**
（不报错、只是"设了不生效"，这是最贵的 bug 类型）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger("astrbot")

_VT = TypeVar("_VT")
_MISSING = object()

#: 默认库位置（跟随 `ASTRBOT_ROOT/data`，与真实现一致）
_DEFAULT_DB_NAME = "preferences.db"


@dataclass
class Preference:
    """一条偏好记录（照真实现 `core/db/po.py:Preference` 的字段）。

    ⚠️ `value` 是 **dict**，真正的值在 `value["val"]` —— 照真实现的存储形状，
    不是我们的发明。插件按这个形状读，少包一层就静默拿到 `None`。
    """

    scope: str
    scope_id: str
    key: str
    value: dict


def _default_path() -> Path:
    root = os.environ.get("ASTRBOT_ROOT")
    base = Path(os.path.realpath(root)) / "data" if root else Path("data")
    base.mkdir(parents=True, exist_ok=True)
    return base / _DEFAULT_DB_NAME


class SharedPreferences:
    """共享偏好（照真实现的接口形态，调用点代码一字不改）。"""

    def __init__(self, db_helper: object = None, json_storage_path: str | None = None) -> None:
        #: ⚠️ 保留 `db_helper` 只为签名兼容 —— 我们**不用**它，
        #: 用宿主自己的库（避免把偏好塞进插件不该碰的宿主库）。
        self.db_helper = db_helper
        self.path = json_storage_path or str(_default_path())
        self._lock = threading.RLock()
        self._ready = False
        #: 真实现有此字段（`native.py` 不读它，但别的插件可能读）→ 给个空实现
        self.temporary_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS preferences ("
            " scope TEXT NOT NULL,"
            " scope_id TEXT NOT NULL,"
            " key TEXT NOT NULL,"
            " value TEXT NOT NULL,"
            " PRIMARY KEY (scope, scope_id, key))"
        )
        return conn

    async def initialize(self) -> None:
        """建库（真实现也会在首次用时惰性初始化）。"""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            conn = self._connect()
            try:
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    async def flush(self) -> None:
        """真实现用来把写队列刷盘；我们是同步写 → **已经是持久状态**。"""
        return None

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    # 读（陪伴 7.0.0 只用这两个）
    # ------------------------------------------------------------------

    async def get_async(
        self,
        scope: str,
        scope_id: str,
        key: str,
        default: _VT = None,
    ) -> _VT:
        """读一个偏好值（真实现 `shared_preferences.py:267-286`）。"""
        await self.initialize()
        if scope_id is None or key is None:
            return default
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM preferences WHERE scope=? AND scope_id=? AND key=?",
                    (scope, scope_id, key),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return default
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            # ⚠️ 损坏的记录**不静默当默认值**：那会让"设了不生效"永远查不出来
            logger.warning(
                "共享偏好记录损坏（scope=%s scope_id=%s key=%s）→ 按默认值返回",
                scope, scope_id, key,
            )
            return default
        if not isinstance(payload, dict) or "val" not in payload:
            logger.warning(
                "共享偏好记录形状异常（缺 val）：scope=%s key=%s", scope, key
            )
            return default
        return deepcopy(payload["val"])

    async def range_get_async(
        self,
        scope: str,
        scope_id: str | None = None,
        key: str | None = None,
    ) -> list[Preference]:
        """按范围读多条（真实现 `shared_preferences.py:287-296`）。

        ⚠️ 返回 `Preference` 对象，`value` 是 `{"val": ...}` 形状 ——
        陪伴 `native.py:240` 就是 `raw.get("val")` 这样读的。
        """
        await self.initialize()
        sql = "SELECT scope, scope_id, key, value FROM preferences WHERE scope=?"
        args: list[Any] = [scope]
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(scope_id)
        if key is not None:
            sql += " AND key=?"
            args.append(key)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        out: list[Preference] = []
        for s, sid, k, raw in rows:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("共享偏好记录损坏（scope=%s key=%s）→ 跳过", s, k)
                continue
            if not isinstance(value, dict):
                value = {"val": value}
            out.append(Preference(scope=s, scope_id=sid, key=k, value=value))
        return out

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def put_async(self, scope: str, scope_id: str, key: str, value: Any) -> None:
        """写一个偏好（真实现 `shared_preferences.py:366`）。"""
        await self.initialize()
        payload = json.dumps({"val": value}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO preferences (scope, scope_id, key, value) VALUES (?,?,?,?)"
                    " ON CONFLICT(scope, scope_id, key) DO UPDATE SET value=excluded.value",
                    (scope, scope_id, key, payload),
                )
                conn.commit()
            finally:
                conn.close()

    async def remove_async(self, scope: str, scope_id: str, key: str) -> None:
        await self.initialize()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM preferences WHERE scope=? AND scope_id=? AND key=?",
                    (scope, scope_id, key),
                )
                conn.commit()
            finally:
                conn.close()

    async def clear_async(self, scope: str, scope_id: str) -> None:
        await self.initialize()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM preferences WHERE scope=? AND scope_id=?",
                    (scope, scope_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ---- 会话/全局便捷方法（真实现有，插件可能用到）----

    async def session_get(self, umo: str | None, key: str | None = None, default: Any = None):
        if umo is None or key is None:
            return await self.range_get_async("umo", umo, key)
        return await self.get_async("umo", umo, key, default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        await self.put_async("umo", umo, key, value)

    async def session_remove(self, umo: str, key: str) -> None:
        await self.remove_async("umo", umo, key)

    async def global_get(self, key: str | None, default: Any = None):
        if key is None:
            return await self.range_get_async("global", "global", None)
        return await self.get_async("global", "global", key, default)

    async def global_put(self, key: str, value: Any) -> None:
        await self.put_async("global", "global", key, value)

    async def global_remove(self, key: str) -> None:
        await self.remove_async("global", "global", key)

    # ---- 同步 API（真实现标了 deprecated，但插件可能还在用）----

    def _sync_get(self, scope: str, scope_id: str, key: str, default: Any) -> Any:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM preferences WHERE scope=? AND scope_id=? AND key=?",
                (scope, scope_id, key),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return default
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return default
        return deepcopy(payload.get("val", default)) if isinstance(payload, dict) else default

    def get(self, scope: str, scope_id: str, key: str, default: _VT = None) -> _VT:
        return self._sync_get(scope, scope_id, key, default)

    def range_get(self, scope: str, scope_id: str | None = None, key: str | None = None):
        sql = "SELECT scope, scope_id, key, value FROM preferences WHERE scope=?"
        args: list[Any] = [scope]
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(scope_id)
        if key is not None:
            sql += " AND key=?"
            args.append(key)
        conn = self._connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        out = []
        for s, sid, k, raw in rows:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            out.append(Preference(s, sid, k, value if isinstance(value, dict) else {"val": value}))
        return out

    def put(self, scope: str, scope_id: str, key: str, value: Any) -> None:
        payload = json.dumps({"val": value}, ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO preferences (scope, scope_id, key, value) VALUES (?,?,?,?)"
                " ON CONFLICT(scope, scope_id, key) DO UPDATE SET value=excluded.value",
                (scope, scope_id, key, payload),
            )
            conn.commit()
        finally:
            conn.close()


__all__ = ["Preference", "SharedPreferences"]
