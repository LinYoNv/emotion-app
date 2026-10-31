# -*- coding: utf-8 -*-
"""SQLite 存储后端（默认实现）。

选它的理由（情绪 2026-09-18 拍板）：单用户自用、单文件、事务安全、查询方便。

**表结构刻意极简**（见 docs/BACKEND_FRAMEWORK.md §7.2 的硬约束）：
  * `store`      —— 分 section 存 JSON 文档（起步不建业务表，先跑通骨架）
  * `meta`       —— key/value，存 revision 与 schema 版本

为什么先不分业务表：第一版只要"人设+聊天"，用 section 存 JSON 足够；
过早规范化会在需求变化时反复迁移。**等模型稳定了再拆表**。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import aiosqlite

from .base import T, StorageError, StoreBackend, validate_import_mode

#: 当前 schema 版本 —— 将来迁移用
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store (
    section    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteStore(StoreBackend):
    """基于 aiosqlite 的存储后端。"""

    def __init__(
        self,
        path: str | Path,
        *,
        default_factory: Any = None,
    ) -> None:
        self._path = Path(path)
        self._default_factory = default_factory or (lambda: {})
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def open(self) -> None:
        """建立连接并建表。幂等。"""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        # WAL：读写并发更稳；foreign_keys 起步用不上但先开着
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('revision', '0')"
        )
        await self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @asynccontextmanager
    async def _transaction(self, *, write: bool = True):
        async with self._lock:
            conn = self._require_conn()
            try:
                await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("SqliteStore 未 open()——请先 await store.open()")
        return self._conn

    # ---- 身份 ----

    def backend_name(self) -> str:
        return "sqlite"

    # ---- 读 ----

    async def load_store(self) -> dict[str, Any]:
        async with self._lock:
            return await self._load_store(self._require_conn())

    async def _load_store(self, conn: aiosqlite.Connection) -> dict[str, Any]:
        async with conn.execute("SELECT section, payload FROM store") as cur:
            rows = await cur.fetchall()
        if not rows:
            return dict(self._default_factory())
        out: dict[str, Any] = {}
        for section, payload in rows:
            try:
                out[section] = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise StorageError(f"section {section!r} 的 JSON 损坏：{exc}") from exc
        return out

    async def load_sections(self, keys: list[str]) -> dict[str, Any]:
        if not keys:
            return {}
        async with self._lock:
            return await self._load_sections(self._require_conn(), keys)

    @staticmethod
    async def _load_sections(conn: aiosqlite.Connection, keys: list[str]) -> dict[str, Any]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        out: dict[str, Any] = {}
        async with conn.execute(
            f"SELECT section, payload FROM store WHERE section IN ({placeholders})", keys
        ) as cur:
            async for section, payload in cur:
                try:
                    out[section] = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise StorageError(f"section {section!r} 的 JSON 损坏：{exc}") from exc
        return out

    async def exists(self) -> bool:
        async with self._lock:
            conn = self._require_conn()
            async with conn.execute("SELECT 1 FROM store LIMIT 1") as cur:
                return await cur.fetchone() is not None

    # ---- 写 ----

    async def save_store(self, data: dict[str, Any]) -> None:
        async with self._transaction() as conn:
            payloads = [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()]
            await conn.execute("DELETE FROM store")
            await conn.executemany(
                "INSERT INTO store(section, payload) VALUES(?, ?)", payloads,
            )
            await self._bump_revision(conn)

    async def save_sections(self, sections: dict[str, Any]) -> None:
        async with self._transaction() as conn:
            await self._save_sections(conn, sections)
            await self._bump_revision(conn)

    @staticmethod
    async def _save_sections(conn: aiosqlite.Connection, sections: dict[str, Any]) -> None:
        await conn.executemany(
            "INSERT INTO store(section, payload, updated_at) VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(section) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in sections.items()],
        )

    async def update_sections(
        self, keys: list[str], mutate: Callable[[dict[str, Any]], T]
    ) -> T:
        async with self._transaction() as conn:
            sections = await self._load_sections(conn, keys)
            before = deepcopy(sections)
            result = mutate(sections)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise StorageError("section 更新回调必须同步执行，不能返回 awaitable")
            if set(sections) - set(keys) or set(before) - set(sections):
                raise StorageError("section 更新回调只能更新指定键，不能删除 section")
            result = deepcopy(result)
            changed = {k: v for k, v in sections.items() if k not in before or v != before[k]}
            if changed:
                await self._save_sections(conn, changed)
                await self._bump_revision(conn)
            return result

    # ---- 版本 ----

    async def next_revision(self) -> int:
        async with self._transaction() as conn:
            await self._bump_revision(conn)
            return await self._read_revision(conn)

    async def current_revision(self) -> int:
        async with self._lock:
            return await self._read_revision(self._require_conn())

    @staticmethod
    async def _read_revision(conn: aiosqlite.Connection) -> int:
        async with conn.execute("SELECT value FROM meta WHERE key='revision'") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _bump_revision(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='revision'"
        )

    # ---- 可移植性 ----

    async def export_store(self) -> dict[str, Any]:
        async with self._transaction(write=False) as conn:
            return {
                "schema_version": SCHEMA_VERSION,
                "revision": await self._read_revision(conn),
                "backend": self.backend_name(),
                "data": await self._load_store(conn),
            }

    async def import_store(self, data: dict[str, Any], *, mode: str = "replace") -> None:
        validate_import_mode(mode)
        if not isinstance(data, dict) or "data" not in data:
            raise StorageError("导入数据缺少 'data' 字段")

        incoming = data["data"]
        if not isinstance(incoming, dict):
            raise StorageError("导入数据 'data' 必须是对象")

        if mode == "replace":
            await self.save_store(incoming)
            return

        # merge：只覆盖传入的 section，其余保留
        await self.save_sections(incoming)

    # ---- 运维 ----

    async def health_check(self) -> dict[str, Any]:
        try:
            async with self._lock:
                conn = self._require_conn()
                async with conn.execute("SELECT COUNT(*) FROM store") as cur:
                    (count,) = await cur.fetchone()
                return {
                    "ok": True,
                    "backend": self.backend_name(),
                    "path": str(self._path),
                    "sections": count,
                    "revision": await self._read_revision(conn),
                    "schema_version": SCHEMA_VERSION,
                }
        except Exception as exc:  # noqa: BLE001 — 健康检查要把任何异常变成结构化结果
            return {"ok": False, "backend": self.backend_name(), "error": str(exc)}
