# -*- coding: utf-8 -*-
"""鉴权：令牌 → 短期会话。

设计照 AstrBot 陪伴插件 `standalone_webui.py`（见
docs/REF_ASTRBOT_COMPANION_STACK.md §3.1），要点全部保留：
  * 令牌**存摘要**（hmac+sha256），不存明文；
  * 登录颁发**短期会话**（TTL 可配，默认 24h）；
  * 比对用 `hmac.compare_digest`（防时序侧信道）；
  * 并发登录加锁（防竞态）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from ..infra.logging_setup import get_logger

log = get_logger(__name__)

#: 会话令牌字节长度（token_urlsafe 的输入）
SESSION_TOKEN_BYTES = 32


@dataclass(slots=True)
class Session:
    token: str
    created_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass
class AuthManager:
    """令牌校验 + 会话管理。

    用法::

        auth = AuthManager(access_token=cfg.access_token, ttl_hours=cfg.session_ttl_hours)
        session = await auth.login(provided_token)   # 失败返回 None
        ok = auth.verify_session(session_token)

    ⚠️ **登录失败限流**（2026-09-19 加）：
        绑定地址改成 `0.0.0.0` 后，登录接口就对整个网络开放了。
        令牌本身是 128 bit 熵（32 位 hex），**穷举不现实** ——
        限流是**纵深防御**，不是主要屏障。
        取舍：窗口刻意宽松（10 次 / 5 分钟），因为**误锁自己比被爆破更现实**：
        单人自用场景下把主人挡在门外是更糟的故障。
    """

    access_token: str
    ttl_hours: int = 24

    #: 窗口内允许的失败次数，超过即 429
    max_failures: int = 10
    #: 失败计数滑动窗口（秒）
    failure_window_seconds: float = 300.0

    #: 会话落盘后端（可选）。给了就持久化，没给就纯内存。
    #:
    #: 🔴 **为什么需要它**（情绪 2026-11-02 报障「cookie 说 24 小时，切出去
    #:    半小时就要重填令牌」）：会话原本只存在这个字典里，**服务一重启就
    #:    全部作废**，而浏览器里的 cookie 还在 ⇒ 带着一个服务端不认识的
    #:    cookie 过来 ⇒ 401 ⇒ 弹回登录框。cookie 的 `Max-Age` 是好的
    #:    （生产实测 `Max-Age=604800`，7 天），**问题从来不在 cookie**。
    #:    开发期频繁重启时，表现就是"随手改个东西就得重新登录"。
    store: Any = field(default=None, repr=False)

    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: client_key → 失败时间戳列表
    _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # 只存摘要，明文用完即弃（不留在实例属性里）
        self._token_digest = self._digest(self.access_token)
        self.access_token = ""  # ⚠️ 立刻清掉明文引用

    # ---- 摘要 ----

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _token_matches(self, provided: str) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(self._digest(provided), self._token_digest)

    def is_access_token(self, provided: str | None) -> bool:
        """判断给定字符串**是不是访问令牌本身**。

        用途：让 `/api/v1/*` 与 `/v1/*` 都能直接用令牌当 Bearer，
        省掉"先登录取 cookie"这一步（对 App 与 API 调用都更自然）。
        ⚠️ 与 `login()` 共用同一套摘要比对，**不引入第二条凭据路径**。
        """
        return self._token_matches(provided or "")

    # ---- 登录 ----

    def _recent_failures(self, client_key: str, now: float) -> list[float]:
        """取窗口内的失败时间戳（顺带清掉过期的，防止字典无限增长）。"""
        cutoff = now - self.failure_window_seconds
        kept = [t for t in self._failures.get(client_key, []) if t > cutoff]
        if kept:
            self._failures[client_key] = kept
        else:
            self._failures.pop(client_key, None)
        return kept

    def is_rate_limited(self, client_key: str, now: float | None = None) -> bool:
        """该来源当前是否已被限流。"""
        now = now if now is not None else time.time()
        return len(self._recent_failures(client_key, now)) >= self.max_failures

    def _record_failure(self, client_key: str, now: float) -> None:
        self._failures.setdefault(client_key, []).append(now)

    async def login(
        self, provided_token: str, client_key: str = "-"
    ) -> Session | None:
        """用访问令牌换一个短期会话。失败返回 None（**不抛异常**，由调用方转 401）。

        Args:
            client_key: 来源标识（通常是客户端 IP），仅用于失败限流。
                ⚠️ 只用于**计数**，不写日志明文（避免把 IP 当身份信息留存）。
        """
        now = time.time()
        async with self._lock:
            if self.is_rate_limited(client_key, now):
                log.warning(
                    "登录被限流：失败次数过多",
                    extra={"data": {"max_failures": self.max_failures}},
                )
                return None

            if not self._token_matches(provided_token):
                self._record_failure(client_key, now)
                log.warning(
                    "登录失败：令牌不匹配",
                    extra={"data": {
                        "failures_in_window": len(self._failures.get(client_key, [])),
                        "limit": self.max_failures,
                    }},
                )
                return None

            # 成功即清空该来源的失败记录
            self._failures.pop(client_key, None)
            session = Session(
                token=secrets.token_urlsafe(SESSION_TOKEN_BYTES),
                created_at=now,
                expires_at=now + self.ttl_hours * 3600,
            )
            self._sessions[session.token] = session
            self._purge_expired(now)
            await self._persist()
            log.info("登录成功", extra={"data": {"ttl_hours": self.ttl_hours}})
            return session

    # ---- 落盘（可选）----

    #: 会话落在 store 的这个 section 里。
    #: 与 `sessions`（用户的聊天会话）**完全不是一个概念**，别合并 ——
    #: 那个是业务数据，这个是登录凭据。
    SECTION = "auth_sessions"

    @staticmethod
    def _encode(sessions: dict[str, Session]) -> dict[str, dict[str, float]]:
        """只存令牌本身与两个时间戳。

        ⚠️ 这里存的是**会话令牌**（随机 32 字节，本就不含用户信息），
        不是访问令牌；访问令牌仍然只以摘要形式留在内存里。
        """
        return {token: {"created_at": s.created_at, "expires_at": s.expires_at}
                for token, s in sessions.items()}

    async def load(self) -> int:
        """从 store 载入未过期会话。返回载入条数。

        ⚠️ **失败必须不让服务起不来**：登录态丢了顶多重新登录一次，
        而 store 读失败如果抛出去，整个服务就是起不来 —— 代价不对等。
        所以这里只记日志、按空会话继续。
        """
        if self.store is None:
            return 0
        try:
            raw = await self.store.load_sections([self.SECTION])
        except Exception as exc:  # noqa: BLE001
            log.warning("会话载入失败，按空会话继续", extra={"data": {"error": type(exc).__name__}})
            return 0
        items = (raw or {}).get(self.SECTION) or {}
        if not isinstance(items, dict):
            log.warning("会话 section 形状不对，已忽略")
            return 0
        now = time.time()
        loaded = 0
        for token, value in items.items():
            if not isinstance(token, str) or not isinstance(value, dict):
                continue
            try:
                created = float(value["created_at"])
                expires = float(value["expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if expires <= now:      # 落盘后已经过期的，直接不要
                continue
            self._sessions[token] = Session(token=token, created_at=created, expires_at=expires)
            loaded += 1
        if loaded:
            log.info("已载入登录会话", extra={"data": {"loaded": loaded}})
        return loaded

    async def _persist(self) -> None:
        """把当前会话整体写回 store。没配 store 时是空操作。"""
        if self.store is None:
            return
        try:
            self._purge_expired()
            await self.store.save_sections({self.SECTION: self._encode(self._sessions)})
        except Exception as exc:  # noqa: BLE001
            # 写盘失败不该让登录本身失败：本次会话在内存里照样有效，
            # 只是重启后需要重新登录。降级而非报错。
            log.warning("会话落盘失败", extra={"data": {"error": type(exc).__name__}})

    # ---- 校验 ----

    def verify_session(self, session_token: str | None) -> bool:
        """校验会话令牌是否有效且未过期。"""
        if not session_token:
            return False
        session = self._sessions.get(session_token)
        if session is None:
            return False
        if session.is_expired():
            self._sessions.pop(session_token, None)
            return False
        return True

    async def logout(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        removed = self._sessions.pop(session_token, None) is not None
        if removed:
            await self._persist()
        return removed

    # ---- 运维 ----

    def _purge_expired(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        dead = [t for t, s in self._sessions.items() if s.is_expired(now)]
        for t in dead:
            self._sessions.pop(t, None)
        return len(dead)

    def stats(self) -> dict[str, Any]:
        """**脱敏**统计 —— 不含任何令牌。"""
        return {
            "active_sessions": len(self._sessions),
            "ttl_hours": self.ttl_hours,
        }


def parse_bearer(header_value: str | None) -> str | None:
    """从 `Authorization: Bearer xxx` 里取出令牌。"""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
