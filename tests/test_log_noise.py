# -*- coding: utf-8 -*-
"""判据：**日志不许被第三方库的周期性噪音刷屏**。

## 现场（情绪 2026-11-05）

> 「这个日志一直刷屏不管吗，是什么东西」

面板「服务日志」整屏都是同一句：

    INFO aiocqhttp: received event: meta_event.heartbeat

每 10 秒一条 × 2 个 OneBot 适配器 ⇒ **每分钟 12 行**，
把真正的 `WARNING` / `ERROR` 全冲走了。

## 根因（第三方库，不能改它）

`aiocqhttp/__init__.py:594`：

    event_name = ev.name
    self.logger.info(f'received event: {event_name}')

它对**每一个**事件都打 INFO，包括心跳这种纯维持连接的噪音。
它的 `logger` 是 `server_app.logger`（名字 `aiocqhttp`），
自己的 handler 是 WARNING，但 **`propagate=True`** ⇒ 经根 logger
（我们设的 INFO）再打一遍。

## 本文件守什么

| # | 判据 |
|---|---|
| ① | 已知噪音 logger 必须被压到 WARNING 以上 |
| ② | 必须 `propagate = False`（只设 level 挡不住，见上） |
| ③ | **根 logger 级别不许被顺带降掉** —— 否则我们自己的 INFO 全没了 |
| ④ | 我们自己的 logger 仍然是 INFO（不能一刀切） |
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotion_app.infra.logging_setup import _NOISY_LOGGERS, setup_logging  # noqa: E402


def test_known_noisy_loggers_are_suppressed():
    """① 噪音 logger 必须被压到 WARNING 以上。"""
    setup_logging("INFO")
    for name in _NOISY_LOGGERS:
        lvl = logging.getLogger(name).getEffectiveLevel()
        assert lvl >= logging.WARNING, (
            f"{name} 的有效级别是 {logging.getLevelName(lvl)} —— "
            "INFO 会漏出来（心跳那类每 10 秒一条，会把真日志冲走）"
        )


def test_noisy_loggers_do_not_propagate():
    """② 必须 `propagate = False`。

    ⚠️ 只设 `setLevel` 挡不住：记录会照常**冒泡**到根 logger，
       而根的级别是 INFO ⇒ 还是打出来。两处都要设。
    """
    setup_logging("INFO")
    for name in _NOISY_LOGGERS:
        assert logging.getLogger(name).propagate is False, (
            f"{name} 还在 propagate —— 只设 level 挡不住，INFO 仍会经根 logger 打出来"
        )


def test_root_level_is_not_lowered():
    """③ **根 logger 不许被顺带降级**。

    把根降到 WARNING 确实能止住刷屏，但那会把**我们自己的** INFO 日志
    全砍掉（插件加载、会话解析、上游调用……）—— 那是拿诊断能力换安静。
    """
    setup_logging("INFO")
    assert logging.getLogger().level <= logging.INFO, (
        "根 logger 被降到 WARNING 以上了 —— 我们自己的 INFO 日志会全部消失"
    )


def test_our_own_loggers_still_info():
    """④ 我们自己的 logger 必须还能打 INFO（反证：证明②不是"全关"）。"""
    setup_logging("INFO")
    for name in ("emotion_app.web.app", "emotion_app.plugins", "astrbot"):
        assert logging.getLogger(name).getEffectiveLevel() <= logging.INFO, (
            f"{name} 被误伤了 —— 它属于我们自己的诊断日志，不该降级"
        )


def test_noisy_table_is_not_empty():
    """⑤ 表不能是空的（空表 = 判据① 恒真 = 假判据）。"""
    assert _NOISY_LOGGERS, "_NOISY_LOGGERS 是空的，上面的判据等于没写"
    assert "aiocqhttp" in _NOISY_LOGGERS, (
        "aiocqhttp 不在噪音表里 —— 心跳刷屏会回来"
    )
