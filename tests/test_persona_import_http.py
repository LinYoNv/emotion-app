# -*- coding: utf-8 -*-
"""人设导入/导出的 HTTP 层测试。

重点覆盖**批量语义**与**三种冲突策略**（爱语 `ty.c()` 的 overwrite/rename/skip），
以及"失败要说清楚"（不静默导入 0 个却说成功）。
"""
from __future__ import annotations

import io
import json

import pytest
from werkzeug.datastructures import FileStorage

from emotion_app.domain.persona_repo import PersonaRepository
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import create_app
from emotion_app.web.auth import AuthManager

TOKEN = "t" * 20


def _backup(*names: str) -> dict:
    """造一个爱语备份（N 个人设）。"""
    return {
        "data": {
            "prompts": {
                f"id-{n}": {
                    "data": {
                        "name": n,
                        "description": f"{n}的描述",
                        "personality": f"{n}的性格",
                        "scenario": "场景",
                        "creator_notes": "",
                        "avatar_path": None,
                    }
                }
                for n in names
            }
        }
    }


@pytest.fixture
async def ctx(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "import.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()

    personas = PersonaRepository(store)
    app = create_app(
        cfg, store, AuthManager(access_token=TOKEN), None, personas=personas
    )
    client = app.test_client()
    await client.post("/api/v1/auth/login", json={"token": TOKEN})

    yield client, personas, store
    await store.close()


# ===========================================================================
# 鉴权
# ===========================================================================


async def test_import_requires_auth(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "noauth.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=TOKEN))
    c = app.test_client()

    r = await c.post("/api/v1/personas/import", json={"text": "{}"})
    assert r.status_code == 401
    await store.close()


# ===========================================================================
# 粘贴导入（批量）
# ===========================================================================


async def test_import_pasted_backup_creates_all(ctx):
    """⭐ 一个文件含多人设 → 全部导入，**回显 N**。"""
    client, personas, _ = ctx

    r = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("甲", "乙", "丙"), ensure_ascii=False)},
    )
    assert r.status_code == 201
    d = await r.get_json()

    assert d["count"] == 3
    assert d["source_format"] == "aiyu_backup"
    assert {p["name"] for p in d["personas"]} == {"甲", "乙", "丙"}
    assert await personas.count() == 3


async def test_import_persists_description(ctx):
    client, personas, _ = ctx
    await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("甲"), ensure_ascii=False)},
    )
    got = (await personas.list())[0]
    assert got.description == "甲的描述"
    assert got.personality == "甲的性格"


async def test_import_invalid_json_is_400_with_reason(ctx):
    """🔴 非法输入 → 400 + 原因，**绝不**建空人设。"""
    client, personas, _ = ctx

    r = await client.post("/api/v1/personas/import", json={"text": "{bad json"})
    assert r.status_code == 400
    d = await r.get_json()
    assert "JSON" in d["error"]["message"]
    assert await personas.count() == 0


async def test_import_empty_text_is_400(ctx):
    client, _, _ = ctx
    r = await client.post("/api/v1/personas/import", json={"text": "   "})
    assert r.status_code == 400


async def test_import_bad_conflict_mode_is_400(ctx):
    """未知冲突策略要**明确报错**，不静默退回默认。"""
    client, _, _ = ctx
    r = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("甲"), ensure_ascii=False), "conflict": "nope"},
    )
    assert r.status_code == 400
    assert "conflict" in (await r.get_json())["error"]["message"]


# ===========================================================================
# 冲突策略（照爱语 ty.c()）
# ===========================================================================


async def test_conflict_rename_keeps_both(ctx):
    """默认 rename：同名导入 → 两份共存，新的叫 `_1`。"""
    client, personas, _ = ctx

    await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("妹妹"), ensure_ascii=False)},
    )
    r = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("妹妹"), ensure_ascii=False), "conflict": "rename"},
    )
    d = await r.get_json()
    assert d["count"] == 1
    assert d["personas"][0]["name"] == "妹妹_1"
    assert await personas.count() == 2


async def test_conflict_overwrite_replaces_not_duplicates(ctx):
    """覆盖：更新已有那条（**保留 id**），而不是新建重复项。"""
    client, personas, _ = ctx

    first = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("妹妹"), ensure_ascii=False)},
    )
    original_id = (await first.get_json())["personas"][0]["id"]

    r = await client.post(
        "/api/v1/personas/import",
        json={
            "text": json.dumps(_backup("妹妹"), ensure_ascii=False),
            "conflict": "overwrite",
        },
    )
    d = await r.get_json()
    assert d["overwritten"] == ["妹妹"]
    assert d["personas"][0]["id"] == original_id, "覆盖必须保留原 id"
    assert await personas.count() == 1, "覆盖不该产生重复项"


async def test_conflict_skip_imports_nothing(ctx):
    client, personas, _ = ctx
    await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("妹妹"), ensure_ascii=False)},
    )
    r = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("妹妹"), ensure_ascii=False), "conflict": "skip"},
    )
    assert r.status_code == 400
    assert "没有导入任何人设" in (await r.get_json())["error"]["message"]
    assert await personas.count() == 1


async def test_conflict_skip_partial_still_imports_new_ones(ctx):
    """跳过的只是同名的，**新的仍要导入**。"""
    client, personas, _ = ctx
    await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("旧"), ensure_ascii=False)},
    )
    r = await client.post(
        "/api/v1/personas/import",
        json={
            "text": json.dumps(_backup("旧", "新"), ensure_ascii=False),
            "conflict": "skip",
        },
    )
    d = await r.get_json()
    assert d["count"] == 1
    assert d["skipped"] == ["旧"]
    assert await personas.count() == 2


# ===========================================================================
# 文件上传
# ===========================================================================


async def test_import_via_file_upload(ctx):
    """表单上传 JSON 文件（手机端主要用这条路径）。"""
    client, _, _ = ctx
    raw = json.dumps(_backup("上传的"), ensure_ascii=False).encode()

    r = await client.post(
        "/api/v1/personas/import",
        files={"file": FileStorage(io.BytesIO(raw), filename="backup.json",
                                   content_type="application/json")},
    )
    assert r.status_code == 201
    d = await r.get_json()
    assert d["count"] == 1
    assert d["personas"][0]["name"] == "上传的"


async def test_import_file_with_bad_format_reports_filename(ctx):
    """报错要带**文件名**（用户才知道是哪个文件出问题）。"""
    client, _, _ = ctx
    r = await client.post(
        "/api/v1/personas/import",
        files={"file": FileStorage(io.BytesIO(b"{broken"), filename="mycard.json",
                                   content_type="application/json")},
    )
    assert r.status_code == 400
    assert "mycard.json" in (await r.get_json())["error"]["message"]


# ===========================================================================
# 导出
# ===========================================================================


async def test_export_single_aiyu_format(ctx):
    client, _, _ = ctx
    made = await client.post("/api/v1/personas", json={"name": "导出我"})
    pid = (await made.get_json())["id"]

    r = await client.get(f"/api/v1/personas/{pid}/export?format=aiyu")
    assert r.status_code == 200
    d = await r.get_json()

    # 必须是爱语的三层结构
    assert "prompts" in d["data"]
    entry = next(iter(d["data"]["prompts"].values()))
    assert entry["data"]["name"] == "导出我"
    assert "filename=" in r.headers.get("Content-Disposition", "")


async def test_export_sillytavern_format(ctx):
    client, _, _ = ctx
    made = await client.post("/api/v1/personas", json={"name": "ST卡"})
    pid = (await made.get_json())["id"]

    r = await client.get(f"/api/v1/personas/{pid}/export?format=sillytavern")
    d = await r.get_json()
    assert d["spec"] == "chara_card_v2"
    assert d["data"]["name"] == "ST卡"


async def test_export_bad_format_is_400(ctx):
    client, _, _ = ctx
    made = await client.post("/api/v1/personas", json={"name": "x"})
    pid = (await made.get_json())["id"]
    r = await client.get(f"/api/v1/personas/{pid}/export?format=pdf")
    assert r.status_code == 400


async def test_export_all_is_aiyu_backup(ctx):
    """导出全部 → 一份爱语备份（可整体搬走）。"""
    client, _, _ = ctx
    for n in ("甲", "乙"):
        await client.post("/api/v1/personas", json={"name": n})

    r = await client.get("/api/v1/personas/export")
    assert r.status_code == 200
    d = await r.get_json()
    assert len(d["data"]["prompts"]) == 2


async def test_export_all_empty_is_400(ctx):
    """没东西可导 → 明确报错，不返回空文件让用户以为成功了。"""
    client, _, _ = ctx
    r = await client.get("/api/v1/personas/export")
    assert r.status_code == 400


async def test_export_route_not_shadowed_by_id_route(ctx):
    """🔴 路由顺序陷阱：`/personas/export` 不能被 `/personas/<id>` 吃掉。

    否则会去查一个 id 叫 "export" 的人设 → 404。
    """
    client, _, _ = ctx
    await client.post("/api/v1/personas", json={"name": "占位"})
    r = await client.get("/api/v1/personas/export")
    assert r.status_code == 200, "`export` 被当成人设 id 了（路由顺序错）"


async def test_export_never_leaks_api_key(ctx):
    """🔴 导出**绝不能**带 api_key。"""
    client, _, _ = ctx
    made = await client.post(
        "/api/v1/personas",
        json={"name": "带密钥", "api_key": "sk-leak-me-if-you-can"},
    )
    pid = (await made.get_json())["id"]

    for fmt in ("aiyu", "sillytavern"):
        r = await client.get(f"/api/v1/personas/{pid}/export?format={fmt}")
        assert "sk-leak-me-if-you-can" not in (await r.get_data(as_text=True)), fmt


# ===========================================================================
# 往返（HTTP 层）
# ===========================================================================


async def test_export_then_import_roundtrip(ctx):
    """⭐ 完整往返：导入 → 导出 → 再导入，字段一致。"""
    client, personas, _ = ctx

    await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(_backup("往返"), ensure_ascii=False)},
    )

    exp = await client.get("/api/v1/personas/export")
    backup = await exp.get_json()

    # 清空后重新导入
    for p in await personas.list():
        await personas.delete(p.id)
    assert await personas.count() == 0

    r = await client.post(
        "/api/v1/personas/import",
        json={"text": json.dumps(backup, ensure_ascii=False)},
    )
    assert r.status_code == 201
    d = await r.get_json()
    assert d["count"] == 1
    p = d["personas"][0]
    assert p["name"] == "往返"
    assert p["description"] == "往返的描述"
    assert p["personality"] == "往返的性格"


# ===========================================================================
# 头像
# ===========================================================================


async def test_avatar_404_when_none(ctx):
    client, _, _ = ctx
    made = await client.post("/api/v1/personas", json={"name": "无头像"})
    pid = (await made.get_json())["id"]
    r = await client.get(f"/api/v1/personas/{pid}/avatar")
    assert r.status_code == 404


async def test_avatar_requires_auth(tmp_path):
    """头像可能含隐私 → **未登录不能取**。

    ⚠️ 先登录建好人设，再用**另一个不带 cookie 的客户端**验证 401 ——
       否则测的是"创建也失败"，根本没走到头像路由（这个测试第一版就写错了）。
    """
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "av.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    personas = PersonaRepository(store)
    app = create_app(cfg, store, AuthManager(access_token=TOKEN), None, personas=personas)
    c = app.test_client()

    await c.post("/api/v1/auth/login", json={"token": TOKEN})
    made = await c.post("/api/v1/personas", json={"name": "x"})
    assert made.status_code == 201, "前置条件：登录后应能创建人设"
    pid = (await made.get_json())["id"]

    anon = app.test_client()
    r = await anon.get(f"/api/v1/personas/{pid}/avatar")
    assert r.status_code == 401, "头像可能含隐私，必须鉴权"
    await store.close()


async def test_avatar_missing_returns_404(ctx):
    """未设置头像 → 404（不是 500，也不是返回别的东西）。"""
    client, _, _ = ctx
    made = await client.post("/api/v1/personas", json={"name": "无头像2"})
    pid = (await made.get_json())["id"]
    r = await client.get(f"/api/v1/personas/{pid}/avatar")
    assert r.status_code == 404
