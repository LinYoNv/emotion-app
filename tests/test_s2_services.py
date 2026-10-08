# -*- coding: utf-8 -*-
"""S2 测试：运行时设置 · 人设仓库。"""
from __future__ import annotations

import pytest

from emotion_app.domain.persona import Persona, PromptSection
from emotion_app.domain.persona_repo import MAX_PERSONAS, PersonaNotFound, PersonaRepository
from emotion_app.domain.settings import WRITABLE_FIELDS, SettingsService
from emotion_app.infra.config import Config
from emotion_app.storage.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(tmp_path / "s2.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def config():
    cfg = Config()
    cfg.upstream.base_url = "https://env.test/v1"
    cfg.upstream.model = "env-model"
    cfg.upstream.api_key = "sk-env-key"
    return cfg


# ===========================================================================
# SettingsService
# ===========================================================================


async def test_settings_defaults_come_from_env(store, config):
    svc = SettingsService(store, config)
    await svc.load()
    up = svc.effective_upstream()
    assert up["base_url"] == "https://env.test/v1"
    assert up["model"] == "env-model"


async def test_settings_runtime_override_wins(store, config):
    """⭐ 情绪 A1 ②：业务配置要能在运行时改。"""
    svc = SettingsService(store, config)
    await svc.load()
    await svc.update({"upstream_model": "runtime-model"})
    assert svc.effective_upstream()["model"] == "runtime-model"


async def test_settings_rejects_non_writable_fields(store, config):
    """🔴 部署配置（server/auth/storage）不可运行时改 —— 改错了服务起不来。"""
    svc = SettingsService(store, config)
    await svc.load()
    await svc.update({"server_port": 9999, "auth_access_token": "hacked"})
    # 不该抛异常（宽容），但**绝不生效**
    # 🔴 锁的是"**默认端口**不被运行时设置改掉"这个契约本身，
    #    620/6191 具体是多少不重要 —— 但改端口时**这条断言必须跟着改**，
    #    否则它会以"运行时设置竟然能改端口"的假象失败（其实是我的默认值改了）。
    assert config.server.port == 6191
    assert not hasattr(svc, "server_port")


async def test_settings_public_view_hides_key(store, config):
    """🔴 api_key 只报"有没有"和"哪来的"，不回显。"""
    svc = SettingsService(store, config)
    await svc.load()
    await svc.update({"upstream_api_key": "sk-runtime-secret"})

    view = svc.public_view()
    assert "sk-runtime-secret" not in str(view)
    assert view["upstream"]["api_key_configured"] is True
    assert view["upstream"]["api_key_source"] == "runtime"


async def test_settings_key_source_env(store, config):
    svc = SettingsService(store, config)
    await svc.load()
    assert svc.public_view()["upstream"]["api_key_source"] == "env"


async def test_settings_key_source_none(store):
    cfg = Config()
    cfg.upstream.api_key = ""
    svc = SettingsService(store, cfg)
    await svc.load()
    assert svc.public_view()["upstream"]["api_key_source"] == "none"


async def test_settings_persist_across_reload(store, config):
    svc = SettingsService(store, config)
    await svc.load()
    await svc.update({"upstream_model": "persisted"})

    svc2 = SettingsService(store, config)
    await svc2.load()
    assert svc2.effective_upstream()["model"] == "persisted"


async def test_settings_clear_restores_env(store, config):
    svc = SettingsService(store, config)
    await svc.load()
    await svc.update({"upstream_model": "runtime"})
    await svc.clear("upstream_model")
    assert svc.effective_upstream()["model"] == "env-model"


async def test_settings_active_persona(store, config):
    svc = SettingsService(store, config)
    await svc.load()
    await svc.set_active_persona("abc123")
    assert svc.active_persona_id == "abc123"


def test_writable_fields_excludes_deploy_config():
    """白名单必须**不含**部署配置。"""
    for bad in ("server_port", "server_host", "auth_access_token", "storage_backend"):
        assert bad not in WRITABLE_FIELDS


# ===========================================================================
# PersonaRepository
# ===========================================================================


async def test_repo_empty(store):
    repo = PersonaRepository(store)
    assert await repo.list() == []
    assert await repo.count() == 0


async def test_repo_create_and_get(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="小爱", description="描述"))
    got = await repo.get(p.id)
    assert got.name == "小爱"
    assert got.description == "描述"
    assert got.created_at > 0


async def test_repo_requires_name(store):
    repo = PersonaRepository(store)
    with pytest.raises(ValueError, match="name"):
        await repo.create(Persona(name="  "))


async def test_repo_get_missing_raises(store):
    repo = PersonaRepository(store)
    with pytest.raises(PersonaNotFound):
        await repo.get("nope")


async def test_repo_update(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="旧"))
    updated = await repo.update(p.id, {"name": "新", "personality": "温柔"})
    assert updated.name == "新"
    assert updated.personality == "温柔"
    assert updated.id == p.id                    # id 不变
    assert updated.created_at == p.created_at    # 创建时间不变
    assert updated.updated_at >= p.updated_at


async def test_repo_update_cannot_change_id_or_created_at(store):
    """🔴 id/created_at 不可改（改了会破坏引用）。"""
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x"))
    updated = await repo.update(p.id, {"id": "hacked", "created_at": 0})
    assert updated.id == p.id
    assert updated.created_at == p.created_at


async def test_repo_update_missing_raises(store):
    repo = PersonaRepository(store)
    with pytest.raises(PersonaNotFound):
        await repo.update("nope", {"name": "x"})


async def test_repo_delete(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x"))
    await repo.delete(p.id)
    assert await repo.count() == 0
    with pytest.raises(PersonaNotFound):
        await repo.delete(p.id)


async def test_repo_duplicate(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="原版", description="d", model="m"))
    clone = await repo.duplicate(p.id)
    assert clone.id != p.id
    assert "副本" in clone.name
    assert clone.description == "d"
    assert clone.model == "m"
    assert await repo.count() == 2


async def test_repo_duplicate_custom_name(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="原版"))
    clone = await repo.duplicate(p.id, new_name="自定义名")
    assert clone.name == "自定义名"


async def test_repo_persists_across_reload(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="持久", description="d"))

    repo2 = PersonaRepository(store)   # 新实例 = 重新读
    got = await repo2.get(p.id)
    assert got.name == "持久"


async def test_repo_list_sorted_by_updated(store):
    repo = PersonaRepository(store)
    a = await repo.create(Persona(name="A"))
    b = await repo.create(Persona(name="B"))
    await repo.update(a.id, {"description": "改过了"})   # A 更新
    items = await repo.list()
    assert items[0].id == a.id
    assert items[1].id == b.id


async def test_repo_sections_roundtrip(store):
    """人设的提示词片段要能完整存取。"""
    repo = PersonaRepository(store)
    p = await repo.create(Persona(
        name="x",
        prompt_sections=[
            PromptSection(title="说话风格", content="简洁", order=30, source="user"),
            PromptSection(title="禁忌", content="别提某事", order=40, source="persona", enabled=False),
        ],
    ))
    got = await repo.get(p.id)
    assert len(got.prompt_sections) == 2
    assert got.prompt_sections[0].title == "说话风格"
    assert got.prompt_sections[1].enabled is False
    assert got.prompt_sections[1].source == "persona"


async def test_repo_sections_via_update(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x"))
    updated = await repo.update(p.id, {
        "prompt_sections": [{"title": "新片段", "content": "内容", "order": 10, "source": "user"}]
    })
    assert len(updated.prompt_sections) == 1
    assert updated.prompt_sections[0].title == "新片段"


async def test_repo_update_rejects_bad_sections(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x"))
    with pytest.raises(ValueError):
        await repo.update(p.id, {"prompt_sections": "not a list"})


async def test_repo_id_collision_reassigned(store):
    """id 冲突要重新分配，**不能静默覆盖**已有的人设。"""
    repo = PersonaRepository(store)
    p1 = await repo.create(Persona(name="第一个"))
    p2 = Persona(name="第二个", id=p1.id)
    created = await repo.create(p2)
    assert created.id != p1.id
    assert await repo.count() == 2


async def test_repo_api_key_hidden_in_list(store):
    """🔴 列表出口不含 key。"""
    repo = PersonaRepository(store)
    await repo.create(Persona(name="x", api_key="sk-persona-secret"))
    items = await repo.list()
    assert "sk-persona-secret" not in str(items[0].to_dict())

    # 但存储里要有（否则重启就丢了）
    raw = await store.load_store()
    assert "sk-persona-secret" in str(raw["personas"])


async def test_repo_max_personas(store):
    repo = PersonaRepository(store)
    await store.save_sections({
        "personas": {f"id{i}": {"id": f"id{i}", "name": f"p{i}"} for i in range(MAX_PERSONAS)}
    })
    with pytest.raises(Exception, match="上限"):
        await repo.create(Persona(name="超了"))
