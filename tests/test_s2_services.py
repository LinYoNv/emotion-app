# -*- coding: utf-8 -*-
"""S2 测试：运行时设置 · 人设仓库。"""
from __future__ import annotations

import pytest

from emotion_app.domain.persona import Persona
# 2026-10-29：此处原本 `from emotion_app.domain.persona import PromptSection`，
# 现已删除 —— 人设不再有 `prompt_sections`，本文件也没有任何片段相关断言了。
# （`PromptSection` 本身搬到了 `emotion_app.domain.prompt`，需要时从那里导入。）
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
    p = await repo.create(Persona(name="小爱", system_prompt="描述"))
    got = await repo.get(p.id)
    assert got.name == "小爱"
    # 2026-10-29：守的意图是"建进去的正文取出来还在"，只是字段名换了
    assert got.system_prompt == "描述"
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
    updated = await repo.update(p.id, {"name": "新", "system_prompt": "温柔"})
    assert updated.name == "新"
    # 2026-10-29：`personality` 已从模型删除，改正文走 `system_prompt`
    assert updated.system_prompt == "温柔"
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
    p = await repo.create(Persona(name="原版", system_prompt="d", model="m"))
    clone = await repo.duplicate(p.id)
    assert clone.id != p.id
    assert "副本" in clone.name
    # 复制必须**连正文一起**复制（否则副本是个空壳）
    assert clone.system_prompt == "d"
    assert clone.model == "m"
    assert await repo.count() == 2


async def test_repo_duplicate_custom_name(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="原版"))
    clone = await repo.duplicate(p.id, new_name="自定义名")
    assert clone.name == "自定义名"


async def test_repo_persists_across_reload(store):
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="持久", system_prompt="d"))

    repo2 = PersonaRepository(store)   # 新实例 = 重新读
    got = await repo2.get(p.id)
    assert got.name == "持久"


async def test_repo_list_sorted_by_updated(store):
    repo = PersonaRepository(store)
    a = await repo.create(Persona(name="A"))
    b = await repo.create(Persona(name="B"))
    await repo.update(a.id, {"system_prompt": "改过了"})   # A 更新
    items = await repo.list()
    assert items[0].id == a.id
    assert items[1].id == b.id


# ---------------------------------------------------------------------------
# 人设字段完整存取
#
# 🔴 2026-10-29 这一组**概念被替换**，不是断言放宽：
#
#   旧的三条守的是「`prompt_sections` 能完整存取」（含 order/enabled/source）。
#   人设结构改成 AstrBot 的「一整段 `system_prompt`」后，
#   `Persona` 上**没有** `prompt_sections` 了，仓库层的
#   `PUT /personas/<id>/sections` 也已删除（见 `api_admin.py:1321`）。
#   所以"片段存取"这个能力本身不存在了 —— 不是我们不想测，是没东西可测。
#
#   但那一组守的**真实风险**还在，而且现在更值得守：
#   ① 人设的所有 AstrBot 字段必须能**完整落盘再读回**（含 `None` 语义）
#   ② 经过一次 `update()` 后字段不许被悄悄丢掉
#      （`update()` 走的是 `to_dict` → `from_dict` 往返，最容易在这里丢）
#   ③ 非法值必须**明确报错**，不能静默忽略
# ---------------------------------------------------------------------------


async def test_repo_persists_all_astrbot_fields(store):
    """⭐ AstrBot 的字段必须**落盘再读回一个不少**（含 `tools=None` 语义）。"""
    repo = PersonaRepository(store)
    p = await repo.create(Persona(
        name="字段齐全",
        system_prompt="正文",
        begin_dialogs=["你好", "你好呀"],
        tools=None,                      # None = 用全部工具
        skills=[],
        custom_error_message="出错啦",
        folder_id="f1",
        sort_order=5,
    ))
    got = await repo.get(p.id)
    assert got.system_prompt == "正文"
    assert got.begin_dialogs == ["你好", "你好呀"]
    # 🔴 `None` 与 `[]` **语义不同**，往返必须各自保持原样
    assert got.tools is None
    assert got.skills == []
    assert got.custom_error_message == "出错啦"
    assert got.folder_id == "f1"
    assert got.sort_order == 5


async def test_repo_update_system_prompt(store):
    """改正文走 `system_prompt`（原来的 `/sections` 已删）。"""
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x", system_prompt="旧正文"))
    updated = await repo.update(p.id, {"system_prompt": "新正文"})
    assert updated.system_prompt == "新正文"

    # 再读一次，确认真的落盘了（不是只在返回值里改了）
    assert (await repo.get(p.id)).system_prompt == "新正文"


async def test_repo_update_preserves_tools_none_semantics(store):
    """🔴 `update()` 不许把 `tools=None` 归一成 `[]`。

    这是本项目"功能性故障"级风险：`None` 与 `[]` 在 AstrBot 里分别是
    "用全部工具" 与 "一个都不用"，归一就等于**悄悄禁掉这个角色的所有工具**。
    """
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x", tools=None))
    assert (await repo.update(p.id, {"name": "改名"})).tools is None
    assert (await repo.update(p.id, {"tools": []})).tools == []
    assert (await repo.update(p.id, {"tools": None})).tools is None
    # 显式数组也要能设
    assert (await repo.update(p.id, {"tools": ["a"]})).tools == ["a"]


async def test_repo_update_rejects_bad_tools(store):
    """非法值必须**明确报错**，不能静默忽略（否则用户以为改成了）。"""
    repo = PersonaRepository(store)
    p = await repo.create(Persona(name="x"))
    with pytest.raises(ValueError):
        await repo.update(p.id, {"tools": "not a list"})
    with pytest.raises(ValueError):
        await repo.update(p.id, {"begin_dialogs": "not a list"})


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
