# -*- coding: utf-8 -*-
"""`astrbot.core.provider.sources.request_retry` 兼容层 —— Provider 请求重试。

⭐ 依据：真 AstrBot `core/provider/sources/request_retry.py`（实测 163 行）。

⚠️ **为什么这个文件不能只搬常量**：`retry_provider_request()` /
   `retry_provider_request_context()` 是 provider 实际调用的**行为入口**，
   只留 `REQUEST_RETRY_ATTEMPTS` 而让函数缺失，插件会在**运行时调用点**
   才炸（比加载期失败更难查）。所以这里把重试语义整体等价实现。

⚠️ **相对真实现省略 / 替代了什么（逐条）**：

   ① **`tenacity` → 标准库自实现**（真实现依赖 `AsyncRetrying` /
      `retry_if_exception` / `stop_after_attempt` / `wait_exponential`）。
      这里实现 `_AsyncRetrying` + `_Attempt`，**刻意保留 tenacity 的
      `async for attempt in retrying: with attempt: ...` 协议**，
      这样两个公开函数的函数体可以**逐字照抄**（含
      `raise RuntimeError("Provider request retry loop exited unexpectedly.")`），
      避免"用自己的循环重写一遍"引入语义漂移。等价性要点：
        * `with attempt:` 里抛异常 → `__exit__` **吞掉**并记入 outcome，
          由循环决定「再试」还是「原样抛出」（对应 `reraise=True`）；
        * `stop_after_attempt(n)` → 第 n 次尝试失败后**不再重试**；
        * `wait_exponential(multiplier=1, min=0.2, max=30)` →
          `max(min, min(1 * 2**(n-1), max))`（照 tenacity 公式，
          第 1 次失败后等 1s，依次 1/2/4/8s）。
      ⚠️ 私有函数 `_build_retrying()` 的**返回类型**因此不再是真的
      `tenacity.AsyncRetrying`。它是下划线私有名，插件不该用；
      真有插件硬用，请让它显式报错而不是静默降级。
   ② **`astrbot.logger` → `logging.getLogger("astrbot")`**：
      真实现 `from astrbot import logger`，该 logger 就是
      `logging.getLogger("astrbot")`（实测 `astrbot/__init__.py`）。
      用标准库直接取同名 logger，宿主既有的日志配置**照常能接管**。
   ③ **`is_connection_error` 内联**（真实现在 `core/utils/network_utils.py`，
      该模块不在本次移植范围内）。判据逐字保留，只是把 httpx 那一段做成
      **可选 import**：装了 httpx（本项目已有）时按类型精确命中
      `ConnectError/ConnectTimeout/ReadTimeout/WriteTimeout/PoolTimeout/
      NetworkError/ProxyError/RequestError`；没装时降级到标准库的
      `TimeoutError/OSError/ConnectionError`（仍覆盖 `socket`/`ssl` 一族）。
      ⚠️ **为什么要可选**：兼容层必须能被"只想 import 它"的场景导入成功
      （例如纯标准库环境下做静态检查），import 期挂掉会连累整条影子包注册链。
   ④ **`coerce_int_config` 内联为 `_coerce_int_config`**（真实现在
      `core/utils/config_number.py`，同样不在本次移植范围内）。
      分支、告警文案、`min_value` 兜底**逐字照搬** ——
      它决定"配了脏值（`"abc"`/`True`/`-3`）时回退到几次重试"，
      是**可观测行为**，不能简化成 `int(value)`。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TypeVar

T = TypeVar("T")

REQUEST_RETRY_ATTEMPTS = 5  # default value
REQUEST_RETRY_WAIT_MIN_S = 0.2
REQUEST_RETRY_WAIT_MAX_S = 30
REQUEST_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}

#: 与真实现 `astrbot/__init__.py` 里的 `logger` 同名，宿主日志配置可直接接管
logger = logging.getLogger("astrbot")

try:  # pragma: no cover - 取决于运行环境是否装了 httpx
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None  # type: ignore[assignment]


def _get_status_code(error: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(error, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

    return None


def _is_connection_error(exc: BaseException) -> bool:
    """照真实现 `core/utils/network_utils.is_connection_error`（内联版）。

    ⚠️ 真实现**显式按异常类型判断**而不是匹配错误字符串 ——
       "Connection error." 这类文案会随 httpx 版本变，字符串匹配是脆的。
       这里保持类型判断，httpx 部分可缺省（见文件头 ③）。
    """
    httpx_error_types: tuple[type[BaseException], ...] = ()
    if _httpx is not None:
        httpx_error_types = (
            _httpx.ConnectError,
            _httpx.ConnectTimeout,
            _httpx.ReadTimeout,
            _httpx.WriteTimeout,
            _httpx.PoolTimeout,
            _httpx.NetworkError,
            _httpx.ProxyError,
            _httpx.RequestError,
        )
    if httpx_error_types and isinstance(exc, httpx_error_types):
        return True

    # Check for common Python network errors
    if isinstance(exc, (TimeoutError, OSError, ConnectionError)):
        return True

    # Check the __cause__ chain for wrapped connection errors
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_connection_error(cause)

    return False


def _is_retryable_provider_request_error(
    error: BaseException,
    *,
    retry_rate_limits: bool,
) -> bool:
    if _is_connection_error(error):
        return True

    error_type_name = type(error).__name__
    if error_type_name in {"APIConnectionError", "APITimeoutError"}:
        return True

    status_code = _get_status_code(error)
    if status_code is None:
        return False

    if status_code == 429 and not retry_rate_limits:
        return False

    return status_code in REQUEST_RETRY_STATUS_CODES or 500 <= status_code <= 599


def _log_retry(
    provider_label: str,
    retry_state: _RetryCallState,
    max_attempts: int,
) -> None:
    error = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        f"[{provider_label}] Request failed with retryable error; "
        f"retrying ({retry_state.attempt_number + 1}/{max_attempts}): "
        f"{error}"
    )


def _coerce_int_config(
    value: object,
    *,
    default: int,
    min_value: int | None = None,
    field_name: str | None = None,
    source: str = "config",
    warn: bool = True,
) -> int:
    """照真实现 `core/utils/config_number.coerce_int_config`（内联版）。"""
    label = f"'{field_name}'" if field_name else "value"

    if isinstance(value, bool):
        if warn:
            logger.warning(
                "%s %s should be numeric, got boolean. Fallback to %s.",
                source,
                label,
                default,
            )
        parsed = default
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            if warn:
                logger.warning(
                    "%s %s value '%s' is not numeric. Fallback to %s.",
                    source,
                    label,
                    value,
                    default,
                )
            parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            if warn:
                logger.warning(
                    "%s %s has unsupported type %s. Fallback to %s.",
                    source,
                    label,
                    type(value).__name__,
                    default,
                )
            parsed = default

    if min_value is not None and parsed < min_value:
        if warn:
            logger.warning(
                "%s %s=%s is below minimum %s. Fallback to %s.",
                source,
                label,
                parsed,
                min_value,
                min_value,
            )
        parsed = min_value
    return parsed


def _wait_exponential_seconds(attempt_number: int) -> float:
    """照 tenacity `wait_exponential(multiplier=1, min=0.2, max=30)`。"""
    try:
        result = 1 * (2 ** (attempt_number - 1))
    except OverflowError:
        return REQUEST_RETRY_WAIT_MAX_S
    return max(
        max(0, REQUEST_RETRY_WAIT_MIN_S),
        min(result, REQUEST_RETRY_WAIT_MAX_S),
    )


class _Outcome:
    """tenacity `Future` 的最小等价物（只用得到 `exception()`）。"""

    __slots__ = ("_exception",)

    def __init__(self, exception: BaseException) -> None:
        self._exception = exception

    def exception(self) -> BaseException:
        return self._exception


class _RetryCallState:
    """tenacity `RetryCallState` 的最小等价物。

    ⚠️ `attempt_number` **从 1 开始**（照 tenacity）——
       `_log_retry` 里的 `attempt_number + 1` 依赖这个基准，
       改成从 0 开始会让日志里的「第几次/共几次」整体错一位。
    """

    __slots__ = ("attempt_number", "outcome")

    def __init__(self, attempt_number: int) -> None:
        self.attempt_number = attempt_number
        self.outcome: _Outcome | None = None


class _Attempt:
    """tenacity `AttemptManager` 的最小等价物（`with attempt:` 用）。"""

    __slots__ = ("_retry_state",)

    def __init__(self, retry_state: _RetryCallState) -> None:
        self._retry_state = retry_state

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            # 照 tenacity：先把异常记进 outcome，再**吞掉** ——
            # 要不要重试由外层循环判定（对应 reraise=True 时最终原样抛出）。
            self._retry_state.outcome = _Outcome(exc)
            return True
        self._retry_state.outcome = None
        return False


class _AsyncRetrying:
    """tenacity `AsyncRetrying` 的最小等价物（只实现被用到的协议）。"""

    def __init__(
        self,
        *,
        retry_rate_limits: bool,
        max_attempts: int,
        before_sleep: Callable[[_RetryCallState], None],
    ) -> None:
        self._retry_rate_limits = retry_rate_limits
        self._max_attempts = max_attempts
        self._before_sleep = before_sleep

    async def _iterate(self) -> AsyncIterator[_Attempt]:
        attempt_number = 0
        while True:
            attempt_number += 1
            retry_state = _RetryCallState(attempt_number)
            yield _Attempt(retry_state)

            # 走到这里 = `with attempt:` 块没能 return（要么抛了异常，
            # 要么正常结束）。正常结束时 outcome 为 None，直接收尾。
            if retry_state.outcome is None:
                return

            error = retry_state.outcome.exception()

            # retry_if_exception(...)：判定为不可重试 → 立刻原样抛出
            if not _is_retryable_provider_request_error(
                error,
                retry_rate_limits=self._retry_rate_limits,
            ):
                raise error
            # stop_after_attempt(max_attempts)
            if attempt_number >= self._max_attempts:
                raise error

            # before_sleep=... + wait=...（tenacity 在睡眠前回调）
            self._before_sleep(retry_state)
            await asyncio.sleep(_wait_exponential_seconds(attempt_number))

    def __aiter__(self) -> AsyncIterator[_Attempt]:
        return self._iterate()


def _build_retrying(
    provider_label: str,
    *,
    retry_rate_limits: bool,
    max_attempts: int | None = None,
) -> _AsyncRetrying:
    max_attempts = _coerce_int_config(
        max_attempts if max_attempts is not None else REQUEST_RETRY_ATTEMPTS,
        default=REQUEST_RETRY_ATTEMPTS,
        min_value=1,
        field_name="request_max_retries",
        source=provider_label,
    )

    return _AsyncRetrying(
        retry_rate_limits=retry_rate_limits,
        max_attempts=max_attempts,
        before_sleep=lambda retry_state: _log_retry(
            provider_label,
            retry_state,
            max_attempts,
        ),
    )


async def retry_provider_request(
    provider_label: str,
    request_factory: Callable[[], Awaitable[T]],
    *,
    retry_rate_limits: bool = True,
    max_attempts: int | None = None,
) -> T:
    retrying = _build_retrying(
        provider_label,
        retry_rate_limits=retry_rate_limits,
        max_attempts=max_attempts,
    )

    async for attempt in retrying:
        with attempt:
            return await request_factory()

    raise RuntimeError("Provider request retry loop exited unexpectedly.")


@asynccontextmanager
async def retry_provider_request_context(
    provider_label: str,
    context_manager_factory: Callable[[], AbstractAsyncContextManager[T]],
    *,
    retry_rate_limits: bool = True,
    max_attempts: int | None = None,
) -> AsyncIterator[T]:
    manager: AbstractAsyncContextManager[T] | None = None

    async def _enter_context() -> T:
        nonlocal manager
        manager = context_manager_factory()
        return await manager.__aenter__()

    value = await retry_provider_request(
        provider_label,
        _enter_context,
        retry_rate_limits=retry_rate_limits,
        max_attempts=max_attempts,
    )

    if manager is None:
        raise RuntimeError("Provider request context was not created.")

    try:
        yield value
    except BaseException as error:
        if await manager.__aexit__(type(error), error, error.__traceback__):
            return
        raise
    else:
        await manager.__aexit__(None, None, None)


__all__ = [
    "REQUEST_RETRY_ATTEMPTS",
    "REQUEST_RETRY_STATUS_CODES",
    "REQUEST_RETRY_WAIT_MAX_S",
    "REQUEST_RETRY_WAIT_MIN_S",
    "retry_provider_request",
    "retry_provider_request_context",
]
