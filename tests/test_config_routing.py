# -*- coding: utf-8 -*-
"""判据：**umo 路由表**（照 AstrBot `umop_config_router.py`）。

## 背景（情绪 2026-11-04）

> 「要么你就学 astrbot，在配置器页面按 umo/会话选配置文件」

AstrBot 的做法（实读 `core/umop_config_router.py`，全文 90 行）：
  * 路由表 `{umop: conf_id}`
  * `umop` = `[platform_id]:[message_type]:[session_id]`
  * 每段支持 `fnmatch` 通配；**空串 = 该段任意**
  * `"::"` = 所有
  * **按顺序遍历，命中第一个就用它**；全不中 ⇒ 默认配置

## 本文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | `split_umo` 只切两刀 | 我们的会话 id 自己含冒号，切碎就匹配不上 |
| ② | 空段 = 该段任意 | 这是"批量规则"的全部表达力 |
| ③ | `"::"` 匹配所有 | AstrBot 的约定 |
| ④ | **按顺序，先配的优先** | 顺序反了，宽规则会盖掉精确规则 |
| ⑤ | 显式指定 > 路由表 > 默认 | 顺序反了，用户选了却不生效 |
| ⑥ | 一条不合法**整批拒绝** | 半套生效的路由表比没有更危险 |
| ⑦ | 路由指向不存在的配置要拒绝 | 否则命中后读不到、静默回落 |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotion_app.domain.config_profile import (  # noqa: E402
    ConfigProfileError,
    ConfigProfileRegistry,
    UmopConfigRouter,
    split_umo,
    umo_matches,
)


@pytest.fixture()
def registry(tmp_path):
    return ConfigProfileRegistry(tmp_path / "profiles")


@pytest.fixture()
def router(registry):
    return UmopConfigRouter(registry)


# ---------------------------------------------------------------------------
# ① split_umo 只切两刀
# ---------------------------------------------------------------------------


def test_split_umo_keeps_colons_inside_the_session_id():
    """第三段自己含冒号 —— 必须只切两刀，否则匹配全废。

    ⚠️ 实测我们的原生会话 id 是
       `onebot-napcat1:FriendMessage:3412642082:conversation:7eced1a3-...`（五段）。
    """
    got = split_umo("onebot-napcat1:FriendMessage:3412642082:conversation:abc")
    assert got == ("onebot-napcat1", "FriendMessage", "3412642082:conversation:abc")


def test_split_umo_rejects_wrong_shape():
    assert split_umo("只有两段:abc") is None
    assert split_umo("") is None
    assert split_umo(None) is None


# ---------------------------------------------------------------------------
# ②③ 通配语义
# ---------------------------------------------------------------------------


def test_empty_segments_match_anything():
    assert umo_matches("::", "11:FriendMessage:123") is True
    assert umo_matches("11::", "11:GroupMessage:999") is True
    assert umo_matches(":FriendMessage:", "11:FriendMessage:123") is True


def test_wildcard_in_a_segment():
    assert umo_matches("onebot-*::", "onebot-napcat1:FriendMessage:123") is True
    assert umo_matches("onebot-*::", "other:FriendMessage:123") is False


def test_non_matching_platform_is_refused():
    assert umo_matches("12::", "11:FriendMessage:123") is False


# ---------------------------------------------------------------------------
# ④ 顺序即优先级
# ---------------------------------------------------------------------------


def test_first_matching_rule_wins(registry, router):
    """**按表顺序，命中第一个就用它**（照 AstrBot）。

    ⚠️ 顺序反了，`"::"` 这种宽规则会盖掉后面所有精确规则 —— 静默配错。
    """
    a = registry.create("精确")
    b = registry.create("兜底")
    router.replace([
        {"umop": "11:FriendMessage:123", "profile_id": a.id},
        {"umop": "::", "profile_id": b.id},
    ])

    assert router.resolve_umo("11:FriendMessage:123") == a.id
    assert router.resolve_umo("11:FriendMessage:456") == b.id


# ---------------------------------------------------------------------------
# ⑤ 三级优先：显式 > 路由 > 默认
# ---------------------------------------------------------------------------


def test_explicit_beats_routing(registry, router):
    explicit = registry.create("会话上直接选的")
    routed = registry.create("路由表指定")
    router.replace([{"umop": "::", "profile_id": routed.id}])

    got = registry.resolve_for_umo("11:FriendMessage:123", explicit.id)
    assert got == explicit.id, "用户在会话上选了，不该被路由表覆盖"


def test_routing_beats_default(registry, router):
    routed = registry.create("路由指定")
    router.replace([{"umop": "11::", "profile_id": routed.id}])
    assert registry.resolve_for_umo("11:FriendMessage:123") == routed.id


def test_falls_back_to_default(registry, router):
    assert registry.resolve_for_umo("11:FriendMessage:999") == registry.default_id()


def test_unknown_explicit_falls_through_to_routing(registry, router):
    """显式指定了一个**不存在**的 id ⇒ 按路由/默认处理（不能返回坏 id）。"""
    routed = registry.create("路由指定")
    router.replace([{"umop": "::", "profile_id": routed.id}])
    assert registry.resolve_for_umo("11:FriendMessage:1", "已经删掉的") == routed.id


# ---------------------------------------------------------------------------
# ⑥⑦ 校验
# ---------------------------------------------------------------------------


def test_bad_umop_rejects_the_whole_batch(registry, router):
    """一条不合法 ⇒ **整批拒绝**。

    🔴 半套生效比没有更危险：用户以为配好了 5 条，实际只进去 3 条。
    """
    ok = registry.create("好的")
    with pytest.raises(ConfigProfileError, match="格式不对"):
        router.replace([
            {"umop": "11::", "profile_id": ok.id},
            {"umop": "格式错误", "profile_id": ok.id},
        ])
    assert router.table() == {}, "整批该被拒绝，一条都不该落盘"


def test_routing_to_missing_profile_is_refused(registry, router):
    with pytest.raises(ConfigProfileError, match="不存在"):
        router.replace([{"umop": "::", "profile_id": "不存在"}])


# ---------------------------------------------------------------------------
# 增删
# ---------------------------------------------------------------------------


def test_upsert_and_remove(registry, router):
    a = registry.create("A")
    b = registry.create("B")
    router.upsert("11:FriendMessage:1", a.id)
    assert router.resolve_umo("11:FriendMessage:1") == a.id
    router.upsert("11:FriendMessage:1", b.id)
    assert router.resolve_umo("11:FriendMessage:1") == b.id
    router.remove("11:FriendMessage:1")
    assert router.resolve_umo("11:FriendMessage:1") is None


def test_routing_survives_a_new_router_instance(registry, router):
    a = registry.create("A")
    router.replace([{"umop": "::", "profile_id": a.id}])
    fresh = UmopConfigRouter(ConfigProfileRegistry(registry.root))
    assert fresh.resolve_umo("11:FriendMessage:1") == a.id
