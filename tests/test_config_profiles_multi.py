# -*- coding: utf-8 -*-
"""判据：**多配置文件**（照 AstrBot 的 `abconf` 分级）。

## 背景（情绪 2026-11-04）

> 「是这样的 astrbot 那边的设置是以"配置文件"进行分级，给会话选择用什么配置文件。合理了」

原来我们只有**一份全局配置**，所有会话共用 —— 情绪指出这不合理。

## AstrBot 的对应机制（实读 `core/astrbot_config_mgr.py`）

* `abconf_data: {conf_id: {path, name}}` —— 所有配置文件的元数据
* `ucr.get_conf_id_for_umop(umo)` —— **umo → 配置文件 id** 的绑定
* 找不到绑定 ⇒ 回落默认那份

## 本文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | 默认那份**不许删** | 删了就没有回落目标，未绑定会话全废 |
| ② | 空/不存在的 id **回落默认**且有日志 | 不能悄悄给个空配置 |
| ③ | 新建可复制已有配置的内容 | 这才是"分级"的实用价值 |
| ④ | 重名被拒绝 | 列表里两行同名，用户没法选 |
| ⑤ | 索引损坏**不静默重建** | 重建 = 用户建的配置文件全"消失" |
| ⑥ | 会话绑定按 umo 解析 | 插件的 `get_config(umo)` 靠它 |
| ⑦ | 回退模型来自**配置文件** | 挪进去之后要真的生效 |
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotion_app.domain.config_profile import (  # noqa: E402
    DEFAULT_PROFILE_ID,
    SECTION_SESSION,
    ConfigProfileError,
    ConfigProfileRegistry,
    ProfileNotFound,
)


@pytest.fixture()
def registry(tmp_path):
    return ConfigProfileRegistry(tmp_path / "profiles")


# ---------------------------------------------------------------------------
# ① 默认那份不许删
# ---------------------------------------------------------------------------


def test_default_profile_cannot_be_deleted(registry):
    """删了默认那份 ⇒ 所有未绑定会话没有回落目标。必须拒绝。"""
    with pytest.raises(ConfigProfileError, match="默认配置文件不能删除"):
        registry.delete(DEFAULT_PROFILE_ID)


def test_default_profile_exists_on_first_run(registry):
    """首次运行自动建默认那份（不能是空列表，否则什么都没得选）。"""
    profiles = registry.list()
    assert len(profiles) == 1
    assert profiles[0].id == DEFAULT_PROFILE_ID
    assert profiles[0].is_default is True


# ---------------------------------------------------------------------------
# ② 回落链
# ---------------------------------------------------------------------------


def test_empty_profile_id_resolves_to_default(registry):
    assert registry.resolve("") == DEFAULT_PROFILE_ID
    assert registry.resolve(None) == DEFAULT_PROFILE_ID


def test_unknown_profile_id_resolves_to_default_and_logs(registry, caplog):
    """不存在的 id 回落默认 —— 但要**留痕**，不能静默换。"""
    import logging

    with caplog.at_level(logging.WARNING):
        assert registry.resolve("nope") == DEFAULT_PROFILE_ID
    assert "nope" in caplog.text, f"回落了却没留日志：{caplog.text!r}"


def test_store_of_unknown_id_is_refused_not_defaulted(registry):
    """`store()` 是**精确取**，不做回落 —— 回落只在 `resolve()` 发生。

    ⚠️ 这两个语义混起来会很难查：用户以为在编辑 A，实际写进了默认那份。
    """
    with pytest.raises(ProfileNotFound):
        registry.store("nope")


# ---------------------------------------------------------------------------
# ③ 新建 / 复制
# ---------------------------------------------------------------------------


def test_create_and_list(registry):
    info = registry.create("陪伴专用")
    assert info.name == "陪伴专用"
    assert [p.name for p in registry.list()] == ["default" if False else "默认配置", "陪伴专用"]


def test_create_can_clone_another_profile(registry):
    """「分级」的实用价值就在这里：从一份改出另一份，不用从头填。"""
    base = registry.store(DEFAULT_PROFILE_ID)
    base.patch({SECTION_SESSION: {"request_max_retries": 7}})

    info = registry.create("工作", clone_from=DEFAULT_PROFILE_ID)
    cloned = registry.store(info.id).load()
    assert cloned[SECTION_SESSION]["request_max_retries"] == 7

    # 改副本**不影响**原件（这才是"分级"而不是"同一个"）
    registry.store(info.id).patch({SECTION_SESSION: {"request_max_retries": 1}})
    assert registry.store(DEFAULT_PROFILE_ID).load()[SECTION_SESSION]["request_max_retries"] == 7


def test_blank_name_is_rejected(registry):
    with pytest.raises(ConfigProfileError, match="不能为空"):
        registry.create("   ")


# ---------------------------------------------------------------------------
# ④ 重名
# ---------------------------------------------------------------------------


def test_duplicate_name_is_rejected(registry):
    registry.create("工作")
    with pytest.raises(ConfigProfileError, match="已经有叫"):
        registry.create("工作")


def test_rename_to_existing_name_is_rejected(registry):
    a = registry.create("工作")
    registry.create("学习")
    with pytest.raises(ConfigProfileError, match="已经有叫"):
        registry.rename(a.id, "学习")


# ---------------------------------------------------------------------------
# ⑤ 索引损坏不静默重建
# ---------------------------------------------------------------------------


def test_corrupt_index_is_backed_up_not_silently_rebuilt(registry, caplog):
    """索引坏了要**先备份并报错**。

    🔴 静默重建的后果：用户建的配置文件文件还在磁盘上，
       但索引里没了 ⇒ 列表里看不到、绑定的会话全回落默认。
       **这比直接报错更伤人。**
    """
    import logging

    registry.create("工作")
    registry._index_path.write_text("{ 坏掉的 json", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        profiles = registry.list()

    # 重建后只剩默认（但必须留了备份 + ERROR）
    assert [p.id for p in profiles] == [DEFAULT_PROFILE_ID]
    assert registry._index_path.with_suffix(".corrupt").is_file(), "没有留备份"
    assert "损坏" in caplog.text, f"没报错：{caplog.text!r}"


# ---------------------------------------------------------------------------
# ⑥ 会话绑定按 umo 解析
# ---------------------------------------------------------------------------


def test_session_has_config_profile_id_field():
    from emotion_app.domain.session_repo import Session

    assert Session().config_profile_id == ""


def test_config_profile_id_survives_round_trip():
    from emotion_app.domain.session_repo import Session

    s = Session()
    s.config_profile_id = "p123"
    dumped = s.to_dict()
    assert dumped.get("config_profile_id") == "p123"
    assert Session.from_dict(dumped).config_profile_id == "p123"


def test_config_profile_id_is_writable():
    """白名单漏了它 ⇒ 前端传了也被静默忽略。"""
    import inspect

    from emotion_app.domain import session_repo

    source = inspect.getsource(session_repo)
    idx = source.find("allowed = {")
    assert idx > 0
    assert "config_profile_id" in source[idx:idx + 400]


# ---------------------------------------------------------------------------
# ⑦ 回退模型在配置文件里
# ---------------------------------------------------------------------------


def test_fallback_models_live_in_the_profile():
    """回退模型是**配置文件**的字段（不是会话字段）。"""
    from emotion_app.domain.config_profile import SPECS

    keys = {f.key for f in SPECS[SECTION_SESSION]}
    assert "fallback_chat_provider_ids" in keys
    assert "fallback_model" not in keys, "不该有单数版（那是旧的会话字段名）"


def test_fallback_models_default_to_empty_list():
    from emotion_app.domain.config_profile import default_profile

    assert default_profile()[SECTION_SESSION]["fallback_chat_provider_ids"] == []


def test_two_profiles_can_have_different_fallbacks(registry):
    """分级的意义：不同配置文件可以有不同的回退策略。"""
    a = registry.store(DEFAULT_PROFILE_ID)
    a.patch({SECTION_SESSION: {"fallback_chat_provider_ids": ["x/fast"]}})
    b_info = registry.create("工作", clone_from=DEFAULT_PROFILE_ID)
    b = registry.store(b_info.id)
    b.patch({SECTION_SESSION: {"fallback_chat_provider_ids": ["y/fast", "z/fast"]}})

    assert a.load()[SECTION_SESSION]["fallback_chat_provider_ids"] == ["x/fast"]
    assert b.load()[SECTION_SESSION]["fallback_chat_provider_ids"] == ["y/fast", "z/fast"]
