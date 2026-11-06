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
