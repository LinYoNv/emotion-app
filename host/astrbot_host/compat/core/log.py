# -*- coding: utf-8 -*-
"""`astrbot.core.log` 兼容层 —— 日志管理器（`LogManager`）。

⭐ 依据：真 AstrBot `core/log.py`（v4.27.5，只读参考）。

## 为什么必须补（2026-10-19，陪伴 7.0.0 实测）

陪伴 7.0.0 的 `astrbot_host/logging_bridge.py:6` 在**模块顶层**执行
`from astrbot.core.log import LogManager`，而 `logging_bridge` 又被
`import_lifecycle` 导入、`import_lifecycle` 是 `main.py:15` 的**第一行插件导入**
⇒ 缺这个模块 **整个插件加载失败**（实测 `ModuleNotFoundError: No module named
'astrbot.core.log'`，插件直接进 `failed`，面板一片空白）。

## 与真实现的差异（如实记录）

真实现基于 **loguru** 做双通道（loguru 文件/控制台 + logging 桥 + WebUI 队列）。
我们**不引入 loguru**：宿主的日志体系是标准 `logging`
（`astrbot_host/*` 全部用 `logging.getLogger(...)`），再引一套 loguru 只会
让同一行日志出现两次、且落点不同。

所以本模块**只保留插件真正会用的接口**，语义与真实现对齐：

| 真接口 | 这里 | 语义 |
|---|---|---|
| `LogManager.GetLogger(name)` | ✅ | 取一个挂了宿主管道的 logger，`propagate=False` |
| `LogManager.get_plugin_logger(name)` | ✅ | `astrbot.plugin.<name>`，**每插件独立等级** |
| `LogManager.configure_logger(logger, cfg, override)` | ✅ | 按 config 的 `log_level` 设级 |
| `set_plugin_log_level(name, level)` | ✅ | 面板改插件日志等级（真实现有持久化，这里只管进程内） |

⚠️ **`GetLogger` 的 `propagate` 必须为 False**：真实现就是 False
（`core/log.py:286`）。设成 True 会让同一行日志沿 root logger 再打一次。

⚠️ **`get_plugin_logger` 不能重置调用方已设的等级**：真实现 `GetLogger()` 会把
level 重置成 DEBUG，然后 `:400` 再 `setLevel(_effective_plugin_log_level(...))` 恢复。
我们照抄这个顺序 —— 漏掉后面那步会让**面板设的等级被静默清掉**。
"""
from __future__ import annotations

import logging
import os

#: 每个插件的 logger 名前缀（照真实现 `PLUGIN_LOGGER_PREFIX`，`core/log.py:22`）
PLUGIN_LOGGER_PREFIX = "astrbot.plugin."

#: 允许的插件日志等级（照真实现 `PLUGIN_LOG_LEVELS`，`core/log.py:25`）
PLUGIN_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: 进程内的"每插件等级"覆盖（真实现落盘到 `plugin_log_levels.json`，见下）
_plugin_level_overrides: dict[str, str] = {}

#: 已经挂过宿主管道的 logger，避免重复添加 handler
_managed: set[str] = set()


class LogManager:
    """日志管理器（照真实现的类方法形态，调用点代码一字不改）。"""

    _plugin_logger_names: set[str] = set()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @classmethod
    def GetLogger(cls, log_name: str = "default") -> logging.Logger:
        """取（并初始化）一个走宿主管道的 logger。

        ⚠️ 与真实现的两点一致：`propagate=False`、`level=DEBUG`
        （等级由 `configure_logger` / 调用方再决定）。
        """
        log = logging.getLogger(log_name)
        cls._ensure_handler(log)
        log.setLevel(logging.DEBUG)
        log.propagate = False
        return log

    @classmethod
    def _ensure_handler(cls, log: logging.Logger) -> None:
        """把记录转到**宿主自己的 root 管道**，而不是另建一份。

        ⚠️ 这里用 StreamHandler 指向 root 的已有 handler 链是做不到的
        （logging 没有"转发到父管道但又不重复"的语义）。所以策略是：
        把 `propagate` 打开交给 root 处理，**除非调用方显式关掉**。
        但 `propagate=False` 是真实现的行为、插件可能依赖（避免重复记录）……

        ⇒ 结论：**维持 `propagate=False`，但确保 logger 至少有一个 handler**，
        指向与宿主 root 相同的 stderr 目标。这样 `logger.info(...)` 不会变成
        "有 logger 无输出"的静默黑洞（那种情况比重复更贵）。
        """
        if log.name in _managed:
            return
        if not log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            log.addHandler(handler)
        _managed.add(log.name)

    @classmethod
    def configure_logger(
        cls,
        logger: logging.Logger,
        config: dict | None,
        override_level: str | None = None,
    ) -> None:
        """按配置设置等级（照真实现 `core/log.py:486-509`）。"""
        if not config:
            return
        level = override_level or config.get("log_level")
        if not level:
            return
        try:
            logger.setLevel(level)
        except (ValueError, TypeError):
            # 真实现同样兜底成 INFO（`core/log.py:499-500`）——
            # ⚠️ 不静默：非法等级要能把原因说出来，否则面板永远显示"设了但没用"
            logging.getLogger("astrbot").warning(
                "configure_logger: 非法日志等级 %r，已回退 INFO", level
            )
            logger.setLevel(logging.INFO)

        # 没显式覆盖的插件 logger 跟随全局等级（照真实现 :502-509）
        effective = logger.level
        for name in list(cls._plugin_logger_names):
            if name in _plugin_level_overrides:
                continue
            logging.getLogger(f"{PLUGIN_LOGGER_PREFIX}{name}").setLevel(effective)

    # ------------------------------------------------------------------
    # 每插件 logger（陪伴 7.0.0 的 logging_bridge 用这个）
    # ------------------------------------------------------------------

    @classmethod
    def get_plugin_logger(cls, plugin_name: str) -> logging.Logger:
        """取插件专属 logger（照真实现 `core/log.py:381-401`）。"""
        plugin_logger = cls.GetLogger(f"{PLUGIN_LOGGER_PREFIX}{plugin_name}")
        cls._plugin_logger_names.add(plugin_name)
        # ⚠️ 顺序不能反：GetLogger 刚把 level 重置成 DEBUG，
        #    这里必须恢复成"覆盖值 或 全局等级"（真实现 :399-400 同款）。
        plugin_logger.setLevel(cls._effective_plugin_log_level(plugin_name))
        return plugin_logger

    @classmethod
    def _effective_plugin_log_level(cls, plugin_name: str) -> int:
        override = _plugin_level_overrides.get(plugin_name)
        if override in PLUGIN_LOG_LEVELS:
            return getattr(logging, override)
        # 跟随全局 `astrbot` logger 的当前等级
        return logging.getLogger("astrbot").level or logging.INFO

    # ------------------------------------------------------------------
    # 面板改等级（真实现有持久化；这里只做进程内 + 从文件读一次）
    # ------------------------------------------------------------------

    @classmethod
    def set_plugin_log_level(cls, plugin_name: str, level: str) -> None:
        """设置插件日志等级。非法等级**抛 ValueError**（不静默忽略）。"""
        if level not in PLUGIN_LOG_LEVELS:
            raise ValueError(
                f"非法插件日志等级 {level!r}；合法值：{', '.join(PLUGIN_LOG_LEVELS)}"
            )
        _plugin_level_overrides[plugin_name] = level
        logging.getLogger(f"{PLUGIN_LOGGER_PREFIX}{plugin_name}").setLevel(
            getattr(logging, level)
        )

    @classmethod
    def get_plugin_log_levels(cls) -> dict[str, str]:
        return dict(_plugin_level_overrides)

    # ------------------------------------------------------------------
    # 兼容垫片：真实现里还有这些，插件可能间接碰到
    # ------------------------------------------------------------------

    @classmethod
    def configure_trace_logger(cls, config: dict | None = None) -> None:
        """真实现用来配 trace 通道；我们没有独立 trace 通道 → **显式空操作**。"""
        return None

    @classmethod
    def set_queue_handler(cls, logger: logging.Logger, broker: object) -> None:
        """真实现给 WebUI 控制台挂队列 handler；宿主自己有日志页 → 空操作。

        ⚠️ 不抛异常：真实现里这是装配步骤，抛了会让插件初始化失败，
        而我们**不需要**这个通道（宿主用 `logs` 页读自己的日志）。
        """
        return None


__all__ = ["LogManager", "PLUGIN_LOGGER_PREFIX", "PLUGIN_LOG_LEVELS"]
