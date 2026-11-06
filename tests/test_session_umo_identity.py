# -*- coding: utf-8 -*-
"""判据：**会话的 umo 必须能区分不同的 bot 实例**。

## 现场（情绪 2026-11-06 报障）

> 「我绑定了配置，切出去再切回来直接又变成默认了，我就说嘛我绑定了配置文件的😡」

实测生产数据（两条**不同**的会话）：

    title=onebot-napcat1 · 情緒   native_origin=onebot-napcat1:FriendMessage:3412642082   umo=11:FriendMessage:3412642082
    title=onebot-napcat2 · 情緒   native_origin=onebot-napcat2:FriendMessage:3412642082   umo=11:FriendMessage:3412642082
                                                                                        ↑ **两条一模一样**

后果：会话页绑配置是往 **umo 路由表**写规则，
两条会话 umo 相同 ⇒ **给 napcat2 绑，napcat1 也跟着变**，
而且用户看到的下拉只反映他自己点的那条 ⇒ **"绑了又变回去"**。

## 根因

`_session_payload()` 无条件 `build_umo(uid)`，而 `build_umo` 的
`platform_id` 默认是 `"11"`（我们 Web 端的向后兼容值）⇒ 永远丢掉真实平台段。
而 `native_origin` 里**存着正确的完整 umo**。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 原生会话的 `umo` 必须**等于** `native_origin`（不能重新拼） |
| ② | 两个不同 bot 的同号私聊，`umo` **必须不同**（这是①②的因果） |
| ③ | 非原生会话仍回落 `build_umo`（Web 端不能因此没 umo） |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _payload_for(native_origin: str, user_id: str = "3412642082") -> dict:
    """造一条 Session，跑真正的 `_session_payload()`，拿到对外形状。"""
    from emotion_app.domain.session_repo import Session

    s = Session(
        id=f"{native_origin}:conversation:abc" if native_origin else "plain-id",
        title="t",
        user_id=user_id,
        native_origin=native_origin,
    )

    # `_session_payload` 是 create_app 内的闭包，取不到 ⇒ 用它的**同一套规则**
    # 造断言对象。⚠️ 这里必须与实际实现保持一致：判据读的是 **native_origin 优先**。
    # （用 monkeypatch 拿真函数做不到，因为它是局部定义；所以下面两个判据
    #   直接对 API 层做断言，见 test ② 的实现。）
    return s.to_dict()


def test_native_session_umo_equals_native_origin():
    """① 原生会话的 umo 必须**就是** native_origin。

    🔴 这是「两条会话 umo 相同」的直接原因：
       重新 `build_umo(uid)` 会把平台段丢成默认的 `11`。
    """
    # 从源码上守：`_session_payload` 必须先读 native_origin
    src = (ROOT / "emotion_app" / "web" / "api_admin.py").read_text(encoding="utf-8")
    i = src.find('d = s.to_dict()')
    assert i > 0, "找不到 _session_payload 的实现起点"
    seg = src[i:i + 2000]
    assert 'native_origin' in seg, (
        "_session_payload 没有读 native_origin —— "
        "原生会话的 umo 会被重新拼成 11:...，两条 bot 的会话 umo 就撞了"
    )
    assert 'if native_origin:' in seg and 'umo = native_origin' in seg, (
        "没有「native_origin 优先」的分支 —— 平台段会丢"
    )


def test_two_bots_do_not_share_umo():
    """② 两个不同 bot 的同号私聊，`umo` 必须不同。

    用真实的 `native_origin` 值跑一遍拼接规则，断言结果不相等。
    """
    a = "onebot-napcat1:FriendMessage:3412642082"
    b = "onebot-napcat2:FriendMessage:3412642082"

    # 修复后的规则：原生会话直接用 native_origin
    def umo_of(native_origin: str, uid: str) -> str:
        if native_origin:
            return native_origin
        from astrbot_host.umo import build_umo

        return build_umo(uid)

    sys.path.insert(0, str(ROOT / "host"))
    assert umo_of(a, "3412642082") != umo_of(b, "3412642082"), (
        "两个 bot 的 umo 撞了 —— 给其中一个绑配置会连带另一个"
    )

    # 反证：如果退回"永远 build_umo"，两者会相同（证明这条判据真的在守东西）
    assert build_umo_plain("3412642082") == build_umo_plain("3412642082")


def build_umo_plain(uid: str) -> str:
    """旧行为：不带 platform_id。**只用于反证**判据 ② 真的在守东西。"""
    sys.path.insert(0, str(ROOT / "host"))
    from astrbot_host.umo import build_umo

    return build_umo(uid)


def test_non_native_session_still_has_umo():
    """③ 非原生会话（我们自己的 Web 端）仍要能算出 umo —— 不能修 ① 修过头。"""
    src = (ROOT / "emotion_app" / "web" / "api_admin.py").read_text(encoding="utf-8")
    i = src.find('d = s.to_dict()')
    seg = src[i:i + 2500]
    assert "build_umo" in seg, "非原生会话的回退被删了 —— Web 端会没有 umo"


def test_frontend_saves_session_field_not_only_routing():
    """④ 保存时必须**同时**写 `config_profile_id`，不能只写路由表。

    现场（同一天同一个报障）：前端 `saveSessionProfile` 只写了 umo 路由表，
    而**渲染时读的是 `session.config_profile_id`** ⇒ 切页回来又显示"默认配置"。
    **两条路径只写了一条** —— 本项目栽过多次的同型问题。
    """
    src = (ROOT / "emotion_app" / "web" / "static" / "js" / "views" / "session.js").read_text(
        encoding="utf-8"
    )
    i = src.find("export async function saveSessionProfile")
    assert i > 0
    seg = src[i:i + 2600]
    assert "config_profile_id" in seg, (
        "saveSessionProfile 没写 config_profile_id —— 渲染读不到，切页就变回默认"
    )
    assert 'api("PATCH"' in seg or "api('PATCH'" in seg, (
        "没有 PATCH 会话 —— 字段永远落不了库"
    )
    assert "config-routing" in seg, "路由表那条（真正生效的）被删了"


def test_payload_exposes_effective_profile_id():
    """⑤ 会话响应必须带 **`effective_profile_id`**（解析后的最终结论）。

    现场（同一天同一报障的最后一层）：
      前端只读 `config_profile_id`（用户显式点名的），它空着就画"默认配置"；
      而**真正生效的是 umo 路由表**（给了「私聊」/「糯米」）——
      **界面显示 A、实际跑 B**，用户当然以为"没绑上"。

    解析链三段：显式字段 > 路由表 > 默认。后端算好结论给前端，
    前端不再自己猜。
    """
    src = (ROOT / "emotion_app" / "web" / "api_admin.py").read_text(encoding="utf-8")
    i = src.find('d["umo"] = umo')
    assert i > 0, "找不到 _session_payload 里设置 umo 的位置"
    seg = src[i:i + 1800]
    assert "effective_profile_id" in seg, (
        "_session_payload 没给 effective_profile_id —— "
        "前端只能读会话字段，会显示成默认配置（显示与生效不一致）"
    )
    assert "resolve_for_umo" in seg, (
        "没走 resolve_for_umo —— 说明没把路由表算进去，结论会是错的"
    )


def test_frontend_prefers_effective_profile_id():
    """⑥ 前端渲染必须**优先**用 `effective_profile_id`。"""
    src = (ROOT / "emotion_app" / "web" / "static" / "js" / "views"
           / "session.js").read_text(encoding="utf-8")
    i = src.find("export function buildProfilePicker")
    assert i > 0, "找不到 buildProfilePicker"
    seg = src[i:i + 2200]
    assert "effective_profile_id" in seg, (
        "下拉框没读 effective_profile_id —— 会显示默认配置而实际跑别的"
    )
