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

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .base import StorageError, StoreBackend, validate_import_mode

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
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("SqliteStore 未 open()——请先 await store.open()")
        return self._conn

    # ---- 身份 ----

    def backend_name(self) -> str:
        return "sqlite"

    # ---- 读 ----

    async def load_store(self) -> dict[str, Any]:
        conn = self._require_conn()
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
        conn = self._require_conn()
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
        conn = self._require_conn()
        async with conn.execute("SELECT 1 FROM store LIMIT 1") as cur:
            return await cur.fetchone() is not None

    # ---- 写 ----

    async def save_store(self, data: dict[str, Any]) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM store")
        await conn.executemany(
            "INSERT INTO store(section, payload) VALUES(?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()],
        )
        await self._bump_revision(conn)
        await conn.commit()

    async def save_sections(self, sections: dict[str, Any]) -> None:
        conn = self._require_conn()
        await conn.executemany(
            "INSERT INTO store(section, payload, updated_at) VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(section) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in sections.items()],
        )
        await self._bump_revision(conn)
        await conn.commit()

    # ---- 版本 ----

    async def next_revision(self) -> int:
        conn = self._require_conn()
        rev = await self._read_revision(conn) + 1
        await conn.execute(
            "INSERT INTO meta(key, value) VALUES('revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(rev),),
        )
        await conn.commit()
        return rev

    async def current_revision(self) -> int:
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
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": await self.current_revision(),
            "backend": self.backend_name(),
            "data": await self.load_store(),
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
            conn = self._require_conn()
            async with conn.execute("SELECT COUNT(*) FROM store") as cur:
                (count,) = await cur.fetchone()
            return {
                "ok": True,
                "backend": self.backend_name(),
                "path": str(self._path),
                "sections": count,
                "revision": await self.current_revision(),
                "schema_version": SCHEMA_VERSION,
            }
        except Exception as exc:  # noqa: BLE001 — 健康检查要把任何异常变成结构化结果
            return {"ok": False, "backend": self.backend_name(), "error": str(exc)}
