# -*- coding: utf-8 -*-
"""判据：**回退模型**（主模型超时/连不上/拒答时改用它）。

## 背景（情绪 2026-11-04）

> 「是我给潮汐的对话模型换成 4a 了。你得在配置文件界面设置"回退模型"了，
>  陪伴不管对话模型的用的是平台的对话模型配置。然后超时改成 120s」

现场：对话模型换成 `cpa/gemini-4-argon-high`（推理模型），图文消息 60 秒 ReadTimeout
⇒ **整轮失败**，用户只看到"她不回我"。

## 这个文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | 默认超时是 120 秒 | 情绪明确要求的数字 |
| ② | **超时**会触发回退 | 本 bug 的修复本身 |
| ③ | **连不上**会触发回退 | 同类传输层失败 |
| ④ | **HTTP 400/401 不**触发回退 | 换模型只会再错一次，还盖掉真因 |
| ⑤ | 回退模型来自会话字段 | 用户能在设置页配 |
| ⑥ | 空回退 = 不回退、如实抛错 | **不许偷偷换模型**（铁律） |
| ⑦ | 错误信息**不能是空白** | 实测 `str(ReadTimeout)` 是 `""` |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# ① 超时默认值
# ---------------------------------------------------------------------------


def test_default_upstream_timeout_is_120_seconds():
    """情绪 2026-11-04：「超时改成 120s」。"""
    from emotion_app.infra.config import UpstreamConfig

    assert UpstreamConfig().timeout_seconds == 120.0


# ---------------------------------------------------------------------------
# ②③④ 哪些异常值得重试
# ---------------------------------------------------------------------------


def test_timeout_is_retryable():
    import httpx

    from emotion_app.domain.chat import _retryable_upstream_error

    assert _retryable_upstream_error(httpx.ReadTimeout("")) is True
    assert _retryable_upstream_error(httpx.ConnectTimeout("")) is True


def test_connect_error_is_retryable():
    import httpx

    from emotion_app.domain.chat import _retryable_upstream_error

    assert _retryable_upstream_error(httpx.ConnectError("refused")) is True


def test_auth_and_bad_request_are_not_retryable():
    """HTTP 400/401 换模型只会再错一次，还会把真因盖掉。"""
    from emotion_app.domain.chat import _retryable_upstream_error
    from emotion_app.domain.upstream import UpstreamError

    assert _retryable_upstream_error(UpstreamError("bad", status=400)) is False
    assert _retryable_upstream_error(UpstreamError("auth", status=401)) is False
    assert _retryable_upstream_error(UpstreamError("forbidden", status=403)) is False


def test_server_error_is_retryable():
    """5xx 是上游临时故障，换模型有意义。"""
    from emotion_app.domain.chat import _retryable_upstream_error
    from emotion_app.domain.upstream import UpstreamError

    assert _retryable_upstream_error(UpstreamError("boom", status=502)) is True


# ---------------------------------------------------------------------------
# ⑦ 错误信息不能是空白
# ---------------------------------------------------------------------------


def test_timeout_error_text_is_not_blank_and_mentions_seconds():
    """🔴 实测 `str(httpx.ReadTimeout())` 是空串 ——
    于是日志里只有 `上游连接失败：`，谁也看不出是超时。
    """
    import httpx

    from emotion_app.domain.backends import _transport_error_text

    text = _transport_error_text(httpx.ReadTimeout(""), 120)
    assert text.strip(), "错误信息不能是空白"
    assert "超时" in text, f"要让人知道是超时：{text}"
    assert "120" in text, f"要带上实际的秒数：{text}"


def test_connect_error_text_tells_you_to_check_the_address():
    import httpx

    from emotion_app.domain.backends import _transport_error_text

    text = _transport_error_text(httpx.ConnectError("refused"), 120)
    assert "连不上" in text
    assert "地址" in text, f"要给出可行动方向：{text}"


# ---------------------------------------------------------------------------
# ⑤⑥ 回退模型来自会话 / 空值不回退
# ---------------------------------------------------------------------------


def test_session_has_a_fallback_model_field():
    from emotion_app.domain.session_repo import Session

    assert Session().fallback_model == ""


def test_fallback_model_is_writable_via_update():
    """白名单漏了它 ⇒ 前端传了也被静默忽略、用户看到"保存成功"。

    ⚠️ 这条守的是 `session_repo.update()` 里那个 `allowed` 集合 ——
       它的注释明确写了「加字段时必须同时加到这里」。
    """
    import inspect

    from emotion_app.domain import session_repo

    source = inspect.getsource(session_repo)
    # 找 allowed = {...} 那一行，确认含 fallback_model
    marker = "allowed = {"
    idx = source.find(marker)
    assert idx > 0, "没找到 allowed 白名单"
    snippet = source[idx:idx + 400]
    assert "fallback_model" in snippet, f"白名单漏了 fallback_model：{snippet[:200]}"


# ---------------------------------------------------------------------------
# ⑧ 🔴 **装配判据**：重试必须真的接在 `_chat_with_fallback` 里
#
# 这条是被**注入反证当场逼出来的**：我把
# `if idx + 1 < len(models) and _retryable_upstream_error(exc):` 改成 `if False:`，
# 上面 9 条判据**全绿** —— 因为它们只测 `_retryable_upstream_error` 这个纯函数。
# 这正是本项目反复记的「模块能工作 ≠ 被接上了」。
# ---------------------------------------------------------------------------


class _FlakyBackend:
    """第一次超时，第二次成功。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, messages, *, model, **kwargs):  # noqa: ANN001
        import httpx

        from emotion_app.domain.upstream import ChatResult

        self.calls.append(model)
        if len(self.calls) == 1:
            raise httpx.ReadTimeout("")
        return ChatResult(content="回退模型救回来了", model=model, raw={})


async def test_timeout_actually_falls_back_through_the_real_call_path():
    """真走 `_chat_with_fallback`：第一次超时 ⇒ 自动换模型再试 ⇒ 拿到结果。

    ⚠️ 必须从 `_chat_with_fallback` 进（不是直接调 `_retryable_upstream_error`），
       否则"重试没接上"这种错永远测不出来。
    """
    from emotion_app.domain.chat import ChatService

    backend = _FlakyBackend()
    service = ChatService.__new__(ChatService)  # 只测这一个方法，跳过整机装配
    service._client = backend
    service._refusal_keywords = ()

    from emotion_app.domain.chat import UpstreamTarget

    target = UpstreamTarget(base_url="http://x", api_key="k", model="慢模型", timeout=120)
    result = await service._chat_with_fallback(
        target, [], ("快模型",), None, None,
    )

    assert backend.calls == ["慢模型", "快模型"], (
        f"超时后必须换到回退模型，实际调用序列 {backend.calls}"
    )
    assert result.content == "回退模型救回来了"


async def test_no_fallback_configured_raises_instead_of_switching():
    """没配回退时**如实抛错**，绝不偷偷换成别的模型（铁律）。"""
    import httpx

    from emotion_app.domain.chat import ChatService, UpstreamTarget

    class _AlwaysTimeout:
        async def chat(self, messages, *, model, **kwargs):  # noqa: ANN001
            raise httpx.ReadTimeout("")

    service = ChatService.__new__(ChatService)
    service._client = _AlwaysTimeout()
    service._refusal_keywords = ()
    target = UpstreamTarget(base_url="http://x", api_key="k", model="唯一模型", timeout=120)

    with pytest.raises(httpx.ReadTimeout):
        await service._chat_with_fallback(target, [], (), None, None)


# ---------------------------------------------------------------------------
# ⑨ 序列化往返（否则"存了但读不回来"）
# ---------------------------------------------------------------------------


def test_fallback_model_survives_a_dict_round_trip():
    """`to_dict` / `from_dict` 都必须带它。

    🔴 实测踩过：只加了字段和更新白名单，**漏了 `to_dict`** ⇒
       数据库里存着，但 API 返回的 JSON 里没有 ⇒ 前端控件永远显示"未选"，
       用户以为没保存上。**这类"存了但读不回来"比不存更让人困惑。**
    """
    from emotion_app.domain.session_repo import Session

    original = Session()
    original.fallback_model = "cpa/gemini-3.8-flash-high"

    dumped = original.to_dict()
    assert dumped.get("fallback_model") == "cpa/gemini-3.8-flash-high"
    assert Session.from_dict(dumped).fallback_model == "cpa/gemini-3.8-flash-high"


def test_old_sessions_without_fallback_model_still_load():
    """旧数据没有这个键 ⇒ 空串（不回退），不能 KeyError。"""
    from emotion_app.domain.session_repo import Session

    loaded = Session.from_dict({"id": "x", "title": "老会话"})
    assert loaded.fallback_model == ""
