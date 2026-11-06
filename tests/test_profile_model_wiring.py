# -*- coding: utf-8 -*-
"""判据：**模型只看配置文件，不做会话级**（情绪 2026-11-06 定）。

> 原话：「对啊说了不要会话级，按配置文件来」

## 现场

情绪问：「那怎么会没选模型呢，配置文件不是有模型了嘛」

真相：会话行那个「未选模型」读的是 `session.model`（会话自身字段），
而那个字段的**界面入口早就被删掉了**（改成「配置文件」那节时
`#cfg-session-model-fields` 一起没了）——
用户没有任何办法改它，它却还在生效、还在显示。

真正决定模型的是**会话绑的那份配置文件**的 `default_chat_provider_id`。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 配置文件里的模型**必须真的参与解析** |
| ② | 位置：**低于**显式指定（override/persona/会话字段）、**高于**环境变量 |
| ③ | `complete()` 与 `stream()` **两处都要传**（只改一处会让流式/非流式走不同模型） |
| ④ | 会话响应要给出 `effective_model`，前端直接显示，不再自己拼 |
| ⑤ | 读不到时**不许编造**模型名 |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHAT = ROOT / "emotion_app" / "domain" / "chat.py"
ADMIN = ROOT / "emotion_app" / "web" / "api_admin.py"
SESSION_JS = ROOT / "emotion_app" / "web" / "static" / "js" / "views" / "session.js"


def test_profile_model_is_actually_used():
    """① 配置文件里的模型必须真的读出来、真的参与解析。

    ⚠️ 只加字段不接线 = 死配置。本项目已有 8 个这样的字段，
       它们看起来能用、实际不生效，**比没有更糟**。
    """
    src = CHAT.read_text(encoding="utf-8")
    assert "_profile_default_model" in src, "没有读配置文件模型的方法"
    assert "default_chat_provider_id" in src, (
        "没读 `default_chat_provider_id` —— 配置里填了模型也不生效"
    )
    # 必须**真的传进** resolve_target（数调用点，不是查关键字）
    # ⚠️ 我第一版只断言 `"profile_model" in src` —— 把参数留在签名里、
    #    把**调用处**的实参删掉后判据仍然通过（假判据）。
    assert "profile_model: str = \"\"," in src, "resolve_target 没有 profile_model 形参"
    call_sites = src.count("session_model, profile_model")
    assert call_sites == 2, (
        f"只有 {call_sites} 处调用真的传了 profile_model（应为 2：complete + stream）。"
        " 算出来不传 = 死代码"
    )


def test_profile_model_is_lower_than_explicit():
    """② 位置必须低于显式指定、高于环境变量。

    写错位置的后果：
      * 放太高 ⇒ 用户明确选的模型被配置覆盖（"我选了却不生效"）
      * 放太低 ⇒ 配置里的模型被全局默认盖掉（"我配了却不生效"）—— 就是这次的 bug
    """
    src = CHAT.read_text(encoding="utf-8")
    i = src.find("if profile_model and not model_override and sess_target is None:")
    assert i > 0, (
        "没找到「配置文件模型」的条件分支 —— "
        "它必须显式让位给 override / persona / 会话字段"
    )
    # 且必须在自己被采用后覆盖 model
    seg = src[i:i + 700]
    assert "model = prof_target.model" in seg, "取了 target 却没把模型名换过去"


@pytest.mark.parametrize("func", ["complete", "stream"])
def test_both_paths_pass_profile_model(func):
    """③ `complete()` 与 `stream()` **两处都要传**。

    ⚠️ 只改一处的分叉极难发现：非流式正常、流式用另一个模型，
       用户只会觉得"有时好有时不好"。
    """
    src = CHAT.read_text(encoding="utf-8")
    assert src.count("profile_model = self._profile_default_model(session)") == 2, (
        "不是两处都传了 —— 流式与非流式会走不同模型"
    )


def test_payload_exposes_effective_model():
    """④ 会话响应必须给 `effective_model`（后端算好的结论）。"""
    src = ADMIN.read_text(encoding="utf-8")
    i = src.find('d["umo"] = umo')
    assert i > 0
    seg = src[i:i + 4000]
    assert "effective_model" in seg, (
        "_session_payload 没给 effective_model —— 前端只能显示会话残留字段，"
        "会出现「配置里有模型却显示未选模型」"
    )
    assert "default_chat_provider_id" in seg, "没读配置文件里的模型"


def test_frontend_shows_effective_model():
    """⑤ 前端优先显示 `effective_model`，且**读不到时不编造**。"""
    src = SESSION_JS.read_text(encoding="utf-8")
    # ⚠️ 必须断言**优先读 effective_model 的那个表达式**，
    #    只查关键字的话，把表达式改回 `s.model` 判据仍会通过（假判据）。
    assert 'String(s.effective_model || s.model || "")' in src, (
        "会话行没有优先读 effective_model —— 会显示会话残留字段（未选模型）"
    )
    assert '"未选模型"' in src or "'未选模型'" in src, (
        "没有「未选模型」的如实说法 —— 可能在编造模型名"
    )
