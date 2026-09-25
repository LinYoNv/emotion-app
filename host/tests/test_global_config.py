# -*- coding: utf-8 -*-
"""判据：**插件读得到全局配置**（P-N5）—— `context.get_config(umo)` 不许恒为空。

## 本文件守的那个真缺口（2026-09-22，实测）

宿主给插件的是 `HostPorts(config={})`（`emotion_app/web/plugin_host.py`），
于是插件的 `context.get_config(umo)` **恒返回空字典**。实测后果
（**20 处调用 / 12 个文件，全部静默降级**）：

| 配置键 | 读不到时 |
|---|---|
| `provider_settings.tool_call_timeout` | 静默回落 `120`（`private_image.py:6413`） |
| `provider_tts_settings.*`（11 键） | TTS 判为"未配置" |
| `timezone` | 静默回落 `Asia/Shanghai` |
| `provider_settings.web_search*` | 联网搜索判为不可用 |

⚠️ 它还是那句**假根因**的来源：本轮实测里「上游 base_url 为空」正是从这条链路
报出来的（读不到配置 → 拿到默认值 → 走空 base_url）——**离根因好几层**。

## 🔴 判据为什么必须"真读真文件"

`load_global_config` 的核心难点不是"读 JSON"，而是三个**会静默出错**的点：

① **BOM**：真 `cmd_config.json` **带 UTF-8 BOM**（`\ufeff{...}`）。
   用 `utf-8` 读会 `JSONDecodeError`；如果被 `except` 成 `{}`
   → **又回到"配置恒为空"那个 bug**（同类错误换马甲）。
   → 判据必须**真造一个带 BOM 的文件**去读，不能只读仓库里那个（
     那样测不出"换个没 BOM 的写法会不会坏"）。
② **失败要响亮**：文件缺失 / JSON 坏 / 顶层不是对象 —— 三种都要
   **记 ERROR**，不许静默给 `{}`（静默降级是本项目最贵的 bug 类型）。
③ **返回形状**：必须是 plain `dict`（插件多处 `config.get(...)`；
   且有 12 处按位置传 umo 调 `get_config`）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

APP_ROOT = HOST_DIR.parent
REAL_CMD_CONFIG = APP_ROOT / "data" / "cmd_config.json"


# ---------------------------------------------------------------------------
# ① 基本形状
# ---------------------------------------------------------------------------


def test_returns_plain_dict(tmp_path):
    """必须返回 **plain `dict`**（不是 AstrBotConfig / 不是 None）。"""
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    p = tmp_path / "cmd_config.json"
    p.write_text(json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8")
    out = load_global_config(p)

    assert type(out) is dict, f"必须是 plain dict，实际是 {type(out).__name__}"
    assert out["timezone"] == "Asia/Shanghai"


def test_reads_file_with_utf8_bom(tmp_path, caplog):
    """⭐ **带 BOM 的文件必须能读**（真 `cmd_config.json` 就是带 BOM 的）。

    用 `utf-8` 读会 `JSONDecodeError: Unexpected UTF-8 BOM`；
    若把它吞成 `{}`，就等于**又造了一遍"配置恒为空"**。
    """
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    p = tmp_path / "cmd_config.json"
    # 显式写 BOM（模拟真 AstrBot 落盘方式）
    p.write_text(json.dumps({"timezone": "UTC"}), encoding="utf-8-sig")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf"), "前置：文件确实带 BOM"

    with caplog.at_level("ERROR"):
        out = load_global_config(p)

    assert out.get("timezone") == "UTC", f"带 BOM 的文件没读出来：{out}"
    assert not [r for r in caplog.records if r.levelname == "ERROR"], (
        "读带 BOM 的文件不该报错"
    )


def test_parses_the_real_cmd_config(tmp_path):
    """真 `data/cmd_config.json`（带 BOM）必须能读，且关键键都在。"""
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    if not REAL_CMD_CONFIG.is_file():
        pytest.fail(f"真全局配置不存在：{REAL_CMD_CONFIG}（判据不许跳过）")
    assert REAL_CMD_CONFIG.read_bytes().startswith(b"\xef\xbb\xbf"), (
        "前置：真配置应当带 BOM（若已改，本判据的 BOM 覆盖由上面那条守住）"
    )

    out = load_global_config(REAL_CMD_CONFIG)
    assert out, "真配置读出来是空的"
    assert out.get("timezone") == "Asia/Shanghai", f"timezone={out.get('timezone')!r}"
    for key in ("provider_settings", "provider_tts_settings"):
        assert isinstance(out.get(key), dict), f"{key} 不是 dict"


# ---------------------------------------------------------------------------
# ② 静默降级防线：三种失败都要**响亮**
# ---------------------------------------------------------------------------


def test_missing_file_logs_error_and_returns_empty(tmp_path, caplog):
    """文件不存在 → 记 **ERROR** 并说明后果（不许静默给 `{}`）。"""
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    missing = tmp_path / "nope.json"
    with caplog.at_level("ERROR"):
        out = load_global_config(missing)

    assert out == {}
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errs, "文件不存在必须记 ERROR（静默降级是最贵的 bug 类型）"
    text = " ".join(r.getMessage() for r in errs)
    assert "静默降级" in text or "空字典" in text, (
        f"ERROR 要说清后果（现在是：{text[:120]}）"
    )


def test_broken_json_logs_error(tmp_path, caplog):
    """JSON 坏 → 记 ERROR（不是静默 `{}`）。"""
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level("ERROR"):
        out = load_global_config(p)

    assert out == {}
    assert [r for r in caplog.records if r.levelname == "ERROR"], "坏 JSON 必须记 ERROR"


def test_non_object_json_logs_error(tmp_path, caplog):
    """顶层不是对象（如 `[]`）→ 记 ERROR 并按空配置处理。"""
    from astrbot_host.compat.core.config.astrbot_config import load_global_config

    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with caplog.at_level("ERROR"):
        out = load_global_config(p)

    assert out == {}
    assert [r for r in caplog.records if r.levelname == "ERROR"], (
        "顶层不是对象必须记 ERROR"
    )


# ---------------------------------------------------------------------------
# ③ 契约：get_config 的签名与形状（插件按位置传 umo，12 处）
# ---------------------------------------------------------------------------


def test_get_config_keeps_umo_positional():
    """🔴 `get_config(umo)` 的 `umo` 必须**仍是位置参数**。

    插件有 **12 处**按位置传（`get_config(target)` / `get_config(umo)` /
    `get_config(str(event.unified_msg_origin))`）—— 改成 keyword-only 会
    **静默打断**它们（多数被宽 `except` 吞成 `{}`）。
    真实现签名就是 `def get_config(self, umo: str | None = None)`。
    """
    import inspect

    from astrbot_host.compat.core.star.context import Context

    params = list(inspect.signature(Context.get_config).parameters.values())
    names = [p.name for p in params]
    assert "umo" in names, f"get_config 的签名变了：{names}"
    umo = next(p for p in params if p.name == "umo")
    assert umo.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
        f"`umo` 变成了 {umo.kind} —— 按位置传的 12 处调用会被静默打断"
    )


def test_get_config_returns_the_injected_config():
    """`Context.get_config()` 返回**注入的那份**配置（且是 plain dict）。"""
    from astrbot_host.compat.core.star.context import Context, HostPorts

    cfg = {"timezone": "Asia/Shanghai", "provider_settings": {"tool_call_timeout": 120}}
    ctx = Context(ports=HostPorts(config=cfg))
    assert ctx.get_config() is cfg
    assert ctx.get_config("11:FriendMessage:default") is cfg, "传 umo 也要能取到"
    assert type(ctx.get_config()) is dict


def test_get_config_empty_when_host_injects_nothing():
    """宿主没注入 → 空 dict（如实，不是 None）——插件 `.get()` 不会崩。"""
    from astrbot_host.compat.core.star.context import Context, HostPorts

    ctx = Context(ports=HostPorts())
    assert ctx.get_config() == {}
    assert ctx.get_config() is not None
