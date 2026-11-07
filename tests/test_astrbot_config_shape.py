# -*- coding: utf-8 -*-
"""判据：`get_config(umo)` 必须**同时**给我们自己的形状和 AstrBot 的形状。

## 现场（情绪 2026-11-06）

> 「问题为什么会是 liloth，我哪里没弄好？」

他哪里都没弄错。真相是**我们没把配置喂成插件读的形状**：

我们存的是：`{"session": {"default_persona_id": "<UUID>"}}`

陪伴插件读的是（`companion_v2/adapters/astrbot/native.py`）：
```python
configuration["agent_runner"]["config"]["persona"]["persona_id"]
# 或
configuration["provider_settings"]["default_personality"]
```

**两个键都没有** ⇒ 插件拿到空 ⇒ 一路回落"全局默认人设" ⇒
用户明明在配置里指定了「糯米茨」，界面上永远显示默认那个。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 两个 AstrBot 键都要补上 |
| ② | 值必须是**人设名字**，不是 UUID（插件主键是 name） |
| ③ | 原始 `session`/`plugins` 段**原样保留**（我们面板还读它们） |
| ④ | **不修改入参**（那份 dict 可能来自缓存，改了会污染别的调用方） |
| ⑤ | 查不到名字时**不写**脏值（让插件走它自己的兜底） |
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT, ROOT / "host"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from emotion_app.web.plugin_host import PluginHostService  # noqa: E402


class _Stub(PluginHostService):
    """只实现本判据要的那一个方法。"""

    NAMES = {
        "uuid-nuomi": "糯米茨",
        "uuid-liloth": "liloth",
    }

    def __init__(self, names=None):  # noqa: D107
        self._names = names if names is not None else dict(self.NAMES)

    def _persona_name_of(self, pid: str) -> str:
        return self._names.get(pid, "")


def _aug(profile, names=None):
    return _Stub(names)._augment_astrbot_config_shape(profile, umo="x:FriendMessage:1")


def test_both_astrbot_keys_are_added():
    """① 两个键都要有 —— 插件两条路径取其一，给全才不会因形状差异回落。"""
    out = _aug({"session": {"default_persona_id": "uuid-nuomi"}})
    assert out["provider_settings"]["default_personality"] == "糯米茨"
    assert out["agent_runner"]["config"]["persona"]["persona_id"] == "糯米茨"


def test_value_is_name_not_uuid():
    """② 必须是**名字**。

    ⚠️ 写 UUID 进去 = 看起来配了、实际用不上：
       AstrBot 侧会校验 `native["name"] == binding.persona_id`。
    """
    out = _aug({"session": {"default_persona_id": "uuid-nuomi"}})
    v = out["provider_settings"]["default_personality"]
    assert v == "糯米茨"
    assert not v.startswith("uuid-"), "把 UUID 当名字写进去了 —— 插件会匹配不上"


def test_original_sections_are_preserved():
    """③ 我们自己的段必须原样保留（面板还靠它们）。"""
    src = {"session": {"default_persona_id": "uuid-nuomi", "request_max_retries": 3},
           "plugins": {"inactivated_plugins": ["x"]}}
    out = _aug(src)
    assert out["session"]["request_max_retries"] == 3
    assert out["plugins"]["inactivated_plugins"] == ["x"]


def test_input_is_not_mutated():
    """④ **不许改入参**。

    那份 dict 是 `registry.store(pid).load()` 读出来的 ——
    如果它走了缓存，就地修改会污染**下一次**读同一份配置的调用方。
    """
    src = {"session": {"default_persona_id": "uuid-nuomi"}}
    before = repr(src)
    _aug(src)
    assert repr(src) == before, "入参被就地改了 —— 缓存里的那份会被污染"
    assert "provider_settings" not in src


def test_missing_persona_name_writes_nothing():
    """⑤ 查不到名字时**不写脏值**（空串会让插件误判成"人格叫空"）。"""
    out = _aug({"session": {"default_persona_id": "uuid-unknown"}})
    assert "provider_settings" not in out or not out["provider_settings"].get("default_personality")
    assert out.get("agent_runner", {}).get("config", {}).get("persona", {}).get("persona_id") in (None, "")


def test_empty_persona_id_is_noop():
    """没有指定人设时什么都不加（别凭空造一个）。"""
    src = {"session": {}}
    out = _aug(src)
    assert "provider_settings" not in out
    assert out["session"] == {}


# ---------------------------------------------------------------------------
# ⑨ 三个官方 `provider_settings.*` 键必须传过去
#    （2026-11-07，情绪：「流式这块…得你完全弄清楚陪伴有没有流式开关」）
# ---------------------------------------------------------------------------


def test_three_official_provider_settings_are_forwarded():
    """我们的字段名**就是** AstrBot 官方的键名，必须原样传过去。

    实读 `astrbot/core/config/default.py`（官方）：

        provider_settings.streaming_response       默认 False
        provider_settings.display_reasoning_text   默认 False
        provider_settings.datetime_system_prompt   默认 True

    🔴 我们存了这三个值却**从来没传给插件** ⇒ 插件读到的永远是官方默认值，
       用户在配置页改了不生效。**和「人格永远是默认那个」是同一个病。**

    ⚠️ 特别注意 `streaming_response`：陪伴插件作者建议在 AstrBot 侧**开流式**
       （插件自己的 `model.streaming` 只管它自己生成的路径，不是主链）。
    """
    out = _aug({
        "session": {
            "default_persona_id": "uuid-nuomi",
            "streaming_response": True,
            "display_reasoning_text": True,
            "datetime_system_prompt": False,
        }
    })
    ps = out.get("provider_settings") or {}
    assert ps.get("streaming_response") is True, (
        "`streaming_response` 没传 —— 作者建议开的主链流式开不了"
    )
    assert ps.get("display_reasoning_text") is True, "`display_reasoning_text` 没传"
    assert ps.get("datetime_system_prompt") is False, (
        "`datetime_system_prompt` 没传（注意传 False 也算「设过」）"
    )


def test_unset_provider_settings_keys_are_not_forced():
    """**没设过的键不许写** —— 否则会用空值盖掉官方默认。

    ⚠️ `datetime_system_prompt` 官方默认是 `True`。我们若因为"字段没值"
       就写 `False`，等于把用户的默认行为关掉了。
    """
    out = _aug({"session": {"default_persona_id": "uuid-nuomi"}})
    ps = out.get("provider_settings") or {}
    for k in ("streaming_response", "display_reasoning_text", "datetime_system_prompt"):
        assert k not in ps, f"{k} 没设过却被写进去了 —— 会盖掉官方默认值"


def test_tts_stt_fields_are_gone():
    """⑩ TTS / STT 字段必须**已删除**（改用陪伴插件的 `voice_reply`）。

    情绪 2026-11-07：「删掉 tts 和 stt 用陪伴的功能」。

    依据：
      * 陪伴插件 `voice_reply.*` 有 **30 项完整实现**（多语言 provider、
        私聊/群聊概率、冷却、触发词…），我们这 4 个字段**0 个消费方**；
      * 我们自己的 hint 都写着「本轮只保存配置，暂不出声」——填了不出声；
      * STT 陪伴也没有，我们也没实现。
    """
    import dataclasses

    from emotion_app.domain.config_profile import SECTION_SESSION, SPECS

    keys = {dataclasses.asdict(f).get("key") for f in SPECS[SECTION_SESSION]}
    for gone in ("tts_enable", "default_tts_provider_id", "tts_trigger_probability",
                 "stt_enable", "default_stt_provider_id"):
        assert gone not in keys, (
            f"{gone} 还在 —— 它是「填了不生效」的死字段，比没有更糟"
        )
