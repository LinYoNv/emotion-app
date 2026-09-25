# -*- coding: utf-8 -*-
"""存储层测试（SQLite）。"""
from __future__ import annotations

import pytest

from emotion_app.infra.config import StorageConfig
from emotion_app.storage.base import StorageError, validate_import_mode
from emotion_app.storage.factory import build_store
from emotion_app.storage.sqlite_store import SCHEMA_VERSION, SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(tmp_path / "t.db")
    await s.open()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------


async def test_empty_store(store):
    assert await store.load_store() == {}
    assert await store.exists() is False


async def test_save_and_load(store):
    await store.save_store({"persona": {"name": "测试"}, "chat": []})
    data = await store.load_store()
    assert data["persona"]["name"] == "测试"
    assert await store.exists() is True


async def test_load_sections_is_selective(store):
    """⭐ 按需读取：只要指定 section，不全量载入。"""
    await store.save_store({"a": 1, "b": 2, "c": 3})
    assert await store.load_sections(["a", "c"]) == {"a": 1, "c": 3}
    assert await store.load_sections(["missing"]) == {}
    assert await store.load_sections([]) == {}


async def test_save_sections_is_partial(store):
    """局部更新只覆盖给定 section，其余保留。"""
    await store.save_store({"a": 1, "b": 2})
    await store.save_sections({"b": 20})
    data = await store.load_store()
    assert data == {"a": 1, "b": 20}


async def test_unicode_roundtrip(store):
    payload = {"名称": "爱语", "内容": "你好，世界 🌏"}
    await store.save_store(payload)
    assert await store.load_store() == payload


# ---------------------------------------------------------------------------
# revision
# ---------------------------------------------------------------------------


async def test_revision_increments_on_write(store):
    assert await store.current_revision() == 0
    await store.save_store({"a": 1})
    assert await store.current_revision() == 1
    await store.save_sections({"a": 2})
    assert await store.current_revision() == 2


async def test_next_revision_explicit(store):
    assert await store.next_revision() == 1
    assert await store.next_revision() == 2
    assert await store.current_revision() == 2


# ---------------------------------------------------------------------------
# 可移植性（从第一天就有）
# ---------------------------------------------------------------------------


async def test_export_contains_metadata(store):
    await store.save_store({"persona": {"name": "x"}})
    exported = await store.export_store()
    assert exported["schema_version"] == SCHEMA_VERSION
    assert exported["backend"] == "sqlite"
    assert exported["revision"] == 1
    assert exported["data"]["persona"]["name"] == "x"


async def test_import_replace(store):
    await store.save_store({"old": 1})
    await store.import_store(
        {"schema_version": SCHEMA_VERSION, "data": {"new": 2}}, mode="replace"
    )
    assert await store.load_store() == {"new": 2}


async def test_import_merge_keeps_others(store):
    await store.save_store({"keep": 1, "overwrite": 2})
    await store.import_store({"data": {"overwrite": 99}}, mode="merge")
    assert await store.load_store() == {"keep": 1, "overwrite": 99}


async def test_import_rejects_bad_mode(store):
    with pytest.raises(StorageError):
        await store.import_store({"data": {}}, mode="destroy")


async def test_import_rejects_missing_data_key(store):
    with pytest.raises(StorageError):
        await store.import_store({"nope": 1})


async def test_import_rejects_non_dict_data(store):
    with pytest.raises(StorageError):
        await store.import_store({"data": [1, 2, 3]})


def test_validate_import_mode():
    assert validate_import_mode("replace") == "replace"
    assert validate_import_mode("merge") == "merge"
    with pytest.raises(StorageError):
        validate_import_mode("wipe")


# ---------------------------------------------------------------------------
# 运维与工厂
# ---------------------------------------------------------------------------


async def test_health_check_ok(store):
    h = await store.health_check()
    assert h["ok"] is True
    assert h["backend"] == "sqlite"
    assert h["schema_version"] == SCHEMA_VERSION


async def test_health_check_reports_failure_when_closed(tmp_path):
    """关掉连接后健康检查应返回 ok=False，而不是抛异常。"""
    s = SqliteStore(tmp_path / "closed.db")
    await s.open()
    await s.close()
    h = await s.health_check()
    assert h["ok"] is False
    assert "error" in h


async def test_operations_without_open_raise(tmp_path):
    """未 open 就用 → 明确报错，不静默失败。"""
    s = SqliteStore(tmp_path / "x.db")
    with pytest.raises(StorageError):
        await s.load_store()


def test_factory_builds_sqlite(tmp_path):
    cfg = StorageConfig(backend="sqlite", sqlite_path=str(tmp_path / "f.db"))
    assert build_store(cfg).backend_name() == "sqlite"


def test_factory_rejects_json_for_now(tmp_path):
    """json 后端已预留但未实现 —— 必须明确报错，不假装可用。"""
    cfg = StorageConfig(backend="json", json_path=str(tmp_path / "f.json"))
    with pytest.raises(StorageError) as ei:
        build_store(cfg)
    assert "尚未实现" in str(ei.value)


async def test_persistence_across_reopen(tmp_path):
    """重启后数据仍在（真实场景：服务重启）。"""
    path = tmp_path / "persist.db"

    s1 = SqliteStore(path)
    await s1.open()
    await s1.save_store({"persona": {"name": "保留我"}})
    await s1.close()

    s2 = SqliteStore(path)
    await s2.open()
    assert (await s2.load_store())["persona"]["name"] == "保留我"
    await s2.close()
