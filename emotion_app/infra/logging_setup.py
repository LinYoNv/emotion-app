# -*- coding: utf-8 -*-
"""日志：结构化、带密钥脱敏。

铁律（见 docs/BACKEND_FRAMEWORK.md §6.2）：
  **绝不打印 key / token。** 本项目在 aitag-site 上有过"凭据进公开仓库"的真实事故，
  所以这里做的是**主动脱敏**：不是"记得别打"，而是**打不出来**。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

#: 需要脱敏的字段名（不区分大小写，子串匹配）
SENSITIVE_KEYS = (
    "token",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "authorization",
    "bearer",
    "credential",
    "private_key",
    "access_token",
)

#: 常见密钥字面量形态（即使出现在自由文本里也要打掉）。
#:
#: ⚠️ **实测教训（第三次过度脱敏）**：最初这里有一条裸的 `\b[A-Fa-f0-9]{32,}\b`，
#:    本意是抓 hex 密钥，结果把 **uuid4().hex 形状的实体 id** 也打成了 ***REDACTED***
#:    （人设 id 就是 32 位 hex）→ 日志里 `"id": "***REDACTED***"`，完全失去诊断价值。
#:
#: **修正**：hex 形态**必须带上下文前缀**才算密钥（`key=` / `token=` / `secret=` 等），
#:    单独的 hex 串**不是**密钥。脱敏要精确，宁可少杀不可错杀。
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    # hex 串仅在紧跟敏感词时才算密钥
    re.compile(
        r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|credential)\b\s*[=:]\s*"
        r"[A-Fa-f0-9]{32,}"
    ),
)

REDACTED = "***REDACTED***"


#: 安全后缀 —— 这些虽然含敏感词，但值是布尔/计数，**必须保留**
#: 起因：实测发现 `token_configured` / `token_length` 被过度脱敏成 ***REDACTED***，
#: 把有用的诊断信息也打掉了。**过度脱敏和泄漏一样是 bug。**
SAFE_SUFFIXES = (
    "_configured",
    "_length",
    "_set",
    "_present",
    "_count",
    "_enabled",
    "_required",
)


def is_sensitive_key(key: str) -> bool:
    """字段名是否敏感。

    规则（按序）：
      1. 命中安全后缀（`_configured` 等）→ **不敏感**（值是布尔/计数）；
      2. 与敏感词**相等**或**以其结尾** → 敏感；
      3. 其余 → 不敏感。

    ⚠️ 不用"子串包含"判断：那会把 `token_configured` 这类诊断字段一起打掉。
    """
    low = str(key).lower()
    if low.endswith(SAFE_SUFFIXES):
        return False
    return any(low == s or low.endswith("_" + s) or low.endswith(s) for s in SENSITIVE_KEYS)


def scrub_text(text: str) -> str:
    """把自由文本里看起来像密钥的片段打掉。"""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def scrub(value: Any) -> Any:
    """递归脱敏任意结构（dict / list / str）。

    判据：本函数是**唯一**的出口——所有进入日志的数据都经过它。
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_sensitive_key(k) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v) for v in value)
    if isinstance(value, str):
        return scrub_text(value)
    return value


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """单行 JSON —— 便于机器读，且**强制**走 scrub。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # 结构化附加字段：logger.info("...", extra={"data": {...}})
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data

        return json.dumps(scrub(payload), ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """人类可读；同样强制走 scrub。

    🔴 **必须带日期**（2026-10-08 修）：
      起因是我上轮报了个错——说 `log_triage.py` 打出 `00:00:00`。那是我看错了；
      真问题是**这里只打了 `%H:%M:%S`、没有日期**，而 `logs/serve.out` 是
      `nohup >>` 追加的，**横跨多天、含 26 次重启**（实测首行 09-25 19:06 → 末行 10-08 15:15）。
      后果：任何"这条是历史残留还是当前仍在发生"都判不了 ——
      上轮我复盘"当前进程 0 个 5xx"就吃了这个亏。
      ⚠️ Hypercorn 自己的行本来就带 `[YYYY-MM-DD HH:MM:SS +0800]`，
         只有我们自己的日志缺日期 ⇒ 文件里两种格式并存，更容易误判。
    """

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        data = getattr(record, "data", None)
        if data is not None:
            base += "  " + json.dumps(scrub(data), ensure_ascii=False)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return scrub_text(base)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

_CONFIGURED = False


# ---------------------------------------------------------------------------
# 内存环形缓冲（面板「日志」板块的数据源）
# ---------------------------------------------------------------------------

#: 最多留多少条。**有意取小值**：这是给人看的诊断窗口，不是归档。
#: 面板按时间倒序展示最近若干条；要完整历史去看 `logs/server.log`。
LOG_BUFFER_SIZE = 500


class RingBufferHandler(logging.Handler):
    """把日志收进内存环形缓冲，供面板「日志」板块读取。

    🔴 **为什么用内存缓冲而不是读日志文件**：
       * 服务是 `nohup ... > logs/server.log` 起的，文件里**混着 Hypercorn 自己的输出**，
         且轮转/权限都不受我们控制；
       * 更重要的是**脱敏**：文件里是已经写出去的字节，而内存里我们能在**出口**统一走
         `scrub()` —— 与其它日志同一条路径，**不引入第二条会泄漏的通道**。

    ⚠️ 线程安全：Hypercorn 是多线程/多协程的，`deque` 的 `append`/`maxlen` 本身原子，
       但"读一批"要用 `list()` 快照，避免边读边被改（RuntimeError: deque mutated）。
    """

    def __init__(self, capacity: int = LOG_BUFFER_SIZE) -> None:
        super().__init__()
        from collections import deque

        # ⚠️ `formatTime` 来自 `logging.Formatter`,**Handler 自己没有** ——
        #    我第一版直接 `self.formatTime(...)` → AttributeError（实测踩到）。
        #    显式配一个 formatter 才拿得到 `formatTime`/`formatException`。
        self.setFormatter(logging.Formatter())
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        # ⚠️ `formatTime` / `formatException` 在 **Formatter** 上，`Handler` 没有 ——
        #    我第一版写成 `self.formatTime(...)` → AttributeError（实测踩到）。
        #    `self.formatter` 由 `__init__` 的 `setFormatter()` 保证非 None。
        fmt = self.formatter or logging.Formatter()
        ts = fmt.formatTime(record, "%Y-%m-%d %H:%M:%S")

        # 🔴 缓冲是**尽力而为**：这里抛异常会污染整个日志系统（logging 会打
        #    "--- Logging error ---"）。所以自己吞掉，但**不静默** —— 记一个标记。
        try:
            entry: dict[str, Any] = {
                "ts": ts,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            data = getattr(record, "data", None)
            if data is not None:
                entry["data"] = data
            if record.exc_info:
                entry["exc"] = fmt.formatException(record.exc_info)
            # 🔴 与其它出口**同一条脱敏路径**（绝不另开一条可能泄漏的通道）
            self._buf.append(scrub(entry))
        except Exception:  # noqa: BLE001
            self._buf.append({
                "ts": ts,
                "level": "ERROR",
                "logger": "emotion_app.infra.logging_setup",
                "msg": "日志入缓冲失败（该条已丢弃）",
            })

    def snapshot(self, *, limit: int = 200, level: str = "") -> list[dict[str, Any]]:
        """取最近的日志（**倒序**：最新在前）。`level` 非空时只看该级别及以上。"""
        items = list(self._buf)  # 快照，避免边读边改
        if level:
            want = getattr(logging, level.upper(), None)
            if want is not None:
                items = [i for i in items if getattr(logging, i.get("level", ""), 0) >= want]
        return list(reversed(items))[: max(1, min(int(limit), self._buf.maxlen or 500))]


#: 全局唯一的缓冲 handler（供 `api_logs` 取用）。未 setup 前为 None。
_buffer_handler: RingBufferHandler | None = None


def get_log_buffer() -> RingBufferHandler | None:
    """取内存日志缓冲（未初始化时 None —— 调用方要**如实**说"日志不可用"）。"""
    return _buffer_handler


def setup_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """初始化根 logger。幂等。"""
    global _CONFIGURED, _buffer_handler

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)

    # 🔴 内存缓冲**挂在根 logger 上**，这样所有 `get_logger(__name__)` 的记录都进得来。
    #    注意 `clear()` 之后要重建 —— 否则重复 setup 会留下两个缓冲，
    #    面板读到的是**半份日志**（另一半在旧缓冲里），这种"看起来有日志但少了"最难查。
    _buffer_handler = RingBufferHandler()
    root.addHandler(_buffer_handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """取 logger；若未初始化则给一个安全的默认值（仍然脱敏）。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
