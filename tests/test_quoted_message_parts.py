# -*- coding: utf-8 -*-
"""判据：默认回复管线**不能静默丢弃用户输入的任何一种段**。

## 背景（情绪 2026-11-03 报障）

> 「无法识别引用消息」/ 她的回话：「看不到具体引用的那句，后台这边只显示了你打的这行字。」

现场：用户引用了机器人上一条回复再发消息，模型完全不知道引用了什么。

根因两层，**都在我们自己这边**：

1. `host/.../components.py` 的 `Reply` 兼容类只有 `id` / `text`，
   适配器辛苦解析出来的 `chain` / `sender_nickname` / `message_str`
   **被 `**_` 静默吞掉**，而且不报错（诊断拿到的真因：
   `'Reply' object has no attribute 'chain'`）。
2. `external_message_parts()` **压根不认 `reply` 类型** —— 它只处理
   `plain` / `image` / `record` / `audio` / `video`，`reply` 落不进任何分支，
   **连日志都没有**。

这正是本项目铁律点名的形态：**「凡过滤/丢弃用户输入的分支必须留日志」**。
所以本文件除了功能判据，还要守「拿不到引用原文时必须留痕」。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from emotion_app.web.default_llm_reply import external_message_parts  # noqa: E402


def make_event(components: list[Any], message_str: str = "") -> Any:
    return SimpleNamespace(
        get_messages=lambda: components,
        message_str=message_str,
        unified_msg_origin="onebot-napcat1:FriendMessage:1",
    )


def reply_component(**overrides: Any) -> Any:
    from astrbot_host.compat.core.message.components import Reply

    values = {"id": "111", "chain": [], "sender_id": "3412642082",
              "sender_nickname": "情緒", "time": 1793600000,
              "message_str": "想我就去翻冰箱，光想能顶饱吗。"}
    values.update(overrides)
    return Reply(**values)


def text_of(parts: list[dict[str, Any]]) -> str:
    return "\n".join(str(p.get("text", "")) for p in parts if p.get("type") == "text")


# ---------------------------------------------------------------------------
# ① 引用的原文必须真的进到模型输入里
# ---------------------------------------------------------------------------


def test_quoted_message_text_reaches_the_model():
    """引用消息的**原文**必须出现在 parts 里，并带上发送者。"""
    event = make_event([reply_component(message_str="光想能顶饱吗。")], "你笨啊")
    parts = external_message_parts(event)
    blob = text_of(parts)

    assert "光想能顶饱吗。" in blob, f"引用原文被丢了，实际 parts={parts}"
    assert "情緒" in blob, "引用里的发送者昵称要带上，否则模型分不清谁说的"
    assert "你笨啊" in blob, "用户本轮正文不能因为引用而丢失"


def test_quote_without_nickname_still_works():
    """昵称拿不到也要能用（只给原文），不能整段丢掉。"""
    event = make_event([reply_component(sender_nickname="", message_str="原文")], "嗯")
    blob = text_of(external_message_parts(event))
    assert "原文" in blob


# ---------------------------------------------------------------------------
# ② 🔴 拿不到引用原文时**必须留痕**（铁律：丢弃必须留日志）
# ---------------------------------------------------------------------------


def test_unreadable_quote_is_logged_not_silently_dropped(caplog):
    """引用已被撤回 / 取消息失败时，正文为空 —— 此时**必须打日志**。

    ⚠️ 这是本文件最重要的一条：以前这条路径既不报错也不打日志，
       用户只能凭感觉说「她好像看不到我引用的那句」。
    """
    event = make_event([reply_component(message_str="", text="")], "你看看这个")
    with caplog.at_level(logging.WARNING):
        external_message_parts(event)

    assert "引用" in caplog.text, f"丢弃引用却没留日志：{caplog.text!r}"


# ---------------------------------------------------------------------------
# ③ 回归：别为了修引用把别的段弄坏
# ---------------------------------------------------------------------------


def test_plain_text_is_unchanged_by_the_reply_branch():
    """纯文本消息行为不变（引用分支不能顺手改动它）。"""
    from astrbot_host.compat.core.message.components import Plain

    parts = external_message_parts(make_event([Plain(text="你好")], "你好"))
    assert "你好" in text_of(parts)


def test_image_and_quote_can_coexist():
    """引用 + 图片混排时，两者都要保留（别让一个分支吃掉另一个）。"""
    from astrbot_host.compat.core.message.components import Image

    image = Image(file="https://example.invalid/x.png", url="https://example.invalid/x.png")
    event = make_event([reply_component(message_str="看看这张"), image], "你觉得呢")
    parts = external_message_parts(event)

    kinds = [p.get("type") for p in parts]
    assert "image_url" in kinds, f"图片被引用分支吃掉了：{parts}"
    assert "看看这张" in text_of(parts), "引用原文被图片分支吃掉了"
