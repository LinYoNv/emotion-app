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


# ---------------------------------------------------------------------------
# ⑧ 装配判据：默认人格必须从**注册表**读，不能读旧的单文件
#
# 这条是被真机验证抓出来的：改造后 `_default_persona_id` 还读
# `ConfigProfileStore()`（旧的单文件路径），于是"面板改了、插件不认"= 假成功。
# ---------------------------------------------------------------------------


def test_default_persona_reads_from_the_registry_not_the_legacy_file(tmp_path):
    """`_default_persona_id()` 必须走注册表的默认那份。"""
    from emotion_app.domain.config_profile import SECTION_SESSION

    registry = ConfigProfileRegistry(tmp_path / "profiles")
    registry.store(registry.default_id()).patch(
        {SECTION_SESSION: {"default_persona_id": "liloth-from-registry"}}
    )

    # 造一个最小的 bridge（只有这个方法需要的属性）
    from emotion_app.web.plugin_host import PluginHostService

    class _Stub(PluginHostService):
        def __init__(self, reg):  # noqa: D107
            self.config_profiles = reg
            self.config = None
            self.host = None

    got = _Stub(registry)._default_persona_id()
    assert got == "liloth-from-registry", (
        f"没有从注册表读默认人格（读到了 {got!r}）"
        "—— 说明还在用旧的单文件路径，面板改了插件不认"
    )


def test_default_persona_falls_back_gracefully_without_registry():
    """注册表未注入时不能崩（回落到旧路径或空串，都行）。"""
    from emotion_app.web.plugin_host import PluginHostService

    class _Stub(PluginHostService):
        def __init__(self):  # noqa: D107
            self.config_profiles = None
            self.config = None
            self.host = None

    assert isinstance(_Stub()._default_persona_id(), str)


# ---------------------------------------------------------------------------
# ⑨ 前端「回退对话模型」必须是有序多选，不是文本框
#
# 情绪 2026-11-04：「回退模型你咋不加下拉框嘞，ui也还用着默认的丑死了你看看 astrbot。
#                   单选添加一个，多选就添加两个还支持上下切换顺序。」
# ---------------------------------------------------------------------------


def test_provider_multi_is_not_a_textarea():
    """`provider_multi` 必须走 `makeProviderMulti`，不能退化成 textarea。

    🔴 为什么这个必须守：
       1. **顺序有语义**（回退是按顺序尝试），textarea 只能靠手挪行；
       2. 模型 id 是 `源名/模型名`，手打必拼错，拼错后**不报错**，
          只在真回退时静默失败。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "emotion_app" / "web" / "static" / "js" / "views" / "settings.js")
    text = src.read_text(encoding="utf-8")

    # 找到 provider_multi 分支
    idx = text.find('if (t === "provider_multi") {')
    assert idx > 0, "找不到 provider_multi 的渲染分支"
    branch = text[idx:idx + 260]
    assert "makeProviderMulti" in branch, (
        "provider_multi 没有走 makeProviderMulti —— 说明又退化成文本框了"
    )


def test_ordered_multi_control_has_reorder_and_remove():
    """控件必须支持 ↑↓ 调序与 ✕ 移除（照 AstrBot）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "emotion_app" / "web" / "static" / "js" / "views" / "settings.js")
    text = src.read_text(encoding="utf-8")

    idx = text.find("export function makeProviderMulti")
    assert idx > 0, "找不到 makeProviderMulti"
    body = text[idx:idx + 4200]
    for needle, why in (
        ("↑", "没有上移按钮"),
        ("↓", "没有下移按钮"),
        ("✕", "没有移除按钮"),
        ("dataset.value", "没有把值挂在容器上（collectConfigChanges 会读到空）"),
    ):
        assert needle in body, f"有序多选控件缺「{why}」"


def test_provider_multi_value_is_read_from_dataset():
    """`collectConfigChanges` 必须从容器 `dataset.value` 读。

    ⚠️ 以前是 textarea 按行 split。换成有序列表后如果没跟着改，
       会读到 `""`（`<div>` 没有 `.value`）⇒ 保存下去**把回退模型清空**。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "emotion_app" / "web" / "static" / "js" / "views" / "settings.js")
    text = src.read_text(encoding="utf-8")

    idx = text.find('else if (f.type === "provider_multi") {')
    assert idx > 0
    branch = text[idx:idx + 500]
    assert "dataset.value" in branch, "取值没走 dataset.value（会读到空串）"
