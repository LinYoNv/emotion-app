# -*- coding: utf-8 -*-
"""鉴权与 HTTP 接口测试。"""
from __future__ import annotations

import pytest

from emotion_app.infra.config import Config, generate_token
from emotion_app.storage.sqlite_store import SqliteStore
from emotion_app.web.app import SESSION_COOKIE, create_app
from emotion_app.web.auth import AuthManager, parse_bearer

TOKEN = "t" * 20


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


async def test_login_with_correct_token():
    auth = AuthManager(access_token=TOKEN)
    session = await auth.login(TOKEN)
    assert session is not None
    assert auth.verify_session(session.token) is True


async def test_login_with_wrong_token():
    auth = AuthManager(access_token=TOKEN)
    assert await auth.login("wrong-token-here") is None


async def test_login_with_empty_token():
    auth = AuthManager(access_token=TOKEN)
    assert await auth.login("") is None


async def test_plaintext_token_not_retained():
    """🔴 明文令牌不能在实例上留下引用。"""
    auth = AuthManager(access_token=TOKEN)
    assert auth.access_token == ""
    assert TOKEN not in repr(auth.__dict__)


async def test_verify_session_rejects_unknown():
    auth = AuthManager(access_token=TOKEN)
    assert auth.verify_session("never-issued") is False
    assert auth.verify_session(None) is False


async def test_session_expires():
    auth = AuthManager(access_token=TOKEN, ttl_hours=1)
    session = await auth.login(TOKEN)
    session.expires_at = 0  # 手动过期
    assert auth.verify_session(session.token) is False


async def test_logout_invalidates():
    auth = AuthManager(access_token=TOKEN)
    session = await auth.login(TOKEN)
    assert auth.logout(session.token) is True
    assert auth.verify_session(session.token) is False


async def test_stats_is_redacted():
    auth = AuthManager(access_token=TOKEN)
    await auth.login(TOKEN)
    stats = auth.stats()
    assert stats["active_sessions"] == 1
    assert TOKEN not in str(stats)


def test_parse_bearer():
    assert parse_bearer("Bearer abc123") == "abc123"
    assert parse_bearer("bearer abc123") == "abc123"
    assert parse_bearer("Basic abc123") is None
    assert parse_bearer("Bearer") is None
    assert parse_bearer(None) is None
    assert parse_bearer("Bearer   ") is None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    cfg = Config()
    cfg.auth.access_token = TOKEN
    cfg.storage.sqlite_path = str(tmp_path / "http.db")

    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    auth = AuthManager(access_token=TOKEN, ttl_hours=cfg.auth.session_ttl_hours)
    app = create_app(cfg, store, auth)

    test_client = app.test_client()
    yield test_client
    await store.close()


async def test_health_is_open_and_redacted(client):
    """健康检查免鉴权，但**必须脱敏**。"""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = await resp.get_json()

    assert body["status"] == "ok"
    assert body["service"] == "emotion-app"
    assert body["storage"]["ok"] is True
    assert TOKEN not in (await resp.get_data(as_text=True))


async def test_status_requires_auth(client):
    """🔴 未鉴权 → 401（不是 200 空数据）。"""
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 401
    body = await resp.get_json()
    assert body["error"]["type"] == "unauthorized"


async def test_login_then_access_status(client):
    resp = await client.post("/api/v1/auth/login", json={"token": TOKEN})
    assert resp.status_code == 200
    assert SESSION_COOKIE in resp.headers.get("Set-Cookie", "")

    resp2 = await client.get("/api/v1/status")
    assert resp2.status_code == 200


async def test_login_with_bad_token_is_401(client):
    resp = await client.post("/api/v1/auth/login", json={"token": "nope"})
    assert resp.status_code == 401
    body = await resp.get_json()
    assert body["error"]["type"] == "invalid_token"


async def test_login_via_bearer_header(client):
    resp = await client.post(
        "/api/v1/auth/login", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 200


async def test_logout_then_status_401(client):
    await client.post("/api/v1/auth/login", json={"token": TOKEN})
    await client.post("/api/v1/auth/logout")

    resp = await client.get("/api/v1/status")
    assert resp.status_code == 401


async def test_404_uses_openai_shaped_error(client):
    """错误体形状与爱语/上游一致（便于通用客户端对接）。"""
    resp = await client.get("/api/v1/nonexistent")
    assert resp.status_code == 404
    body = await resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "Not Found" in body["error"]["message"]


async def test_cookie_is_httponly(client):
    """会话 cookie 必须 HttpOnly（JS 读不到）。"""
    resp = await client.post("/api/v1/auth/login", json={"token": TOKEN})
    cookie_header = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header


async def test_generated_token_works_end_to_end(tmp_path):
    """端到端：生成令牌 → 配好 → 能登录。"""
    token = generate_token()
    cfg = Config()
    cfg.auth.access_token = token
    cfg.storage.sqlite_path = str(tmp_path / "e2e.db")

    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=token))
    c = app.test_client()

    assert (await c.post("/api/v1/auth/login", json={"token": token})).status_code == 200
    await store.close()


# ---------------------------------------------------------------------------
# 登录失败限流（绑 0.0.0.0 后的纵深防御）
# ---------------------------------------------------------------------------


async def test_rate_limit_blocks_after_max_failures():
    """超过失败上限后必须**拒绝**（即使是正确令牌）—— 否则限流形同虚设。"""
    auth = AuthManager(access_token=TOKEN, max_failures=3)
    for _ in range(3):
        assert await auth.login("wrong", "1.2.3.4") is None

    assert auth.is_rate_limited("1.2.3.4") is True
    # 🔴 关键：被限流后**正确令牌也不能进**，否则爆破者只要猜中就直接绕过限流
    assert await auth.login(TOKEN, "1.2.3.4") is None


async def test_rate_limit_is_per_client():
    """一个来源被限流不得影响另一个来源（否则一个人能锁死所有人）。"""
    auth = AuthManager(access_token=TOKEN, max_failures=2)
    await auth.login("wrong", "1.1.1.1")
    await auth.login("wrong", "1.1.1.1")

    assert auth.is_rate_limited("1.1.1.1") is True
    assert auth.is_rate_limited("2.2.2.2") is False
    assert await auth.login(TOKEN, "2.2.2.2") is not None


async def test_successful_login_clears_failures():
    """登录成功后清空该来源的失败记录 —— 否则偶尔打错几次会累积到被锁。"""
    auth = AuthManager(access_token=TOKEN, max_failures=3)
    await auth.login("wrong", "3.3.3.3")
    await auth.login("wrong", "3.3.3.3")
    assert await auth.login(TOKEN, "3.3.3.3") is not None

    # 记录已清空 → 再错两次也不会触发限流（阈值是 3）
    await auth.login("wrong", "3.3.3.3")
    await auth.login("wrong", "3.3.3.3")
    assert auth.is_rate_limited("3.3.3.3") is False


async def test_failure_window_expires(monkeypatch):
    """窗口过期后应自动恢复（避免"永久封禁自己"）。"""
    import emotion_app.web.auth as auth_mod

    auth = AuthManager(access_token=TOKEN, max_failures=2, failure_window_seconds=60)
    await auth.login("wrong", "4.4.4.4")
    await auth.login("wrong", "4.4.4.4")
    assert auth.is_rate_limited("4.4.4.4") is True

    # 把时间推到窗口之外
    real_time = auth_mod.time.time
    monkeypatch.setattr(auth_mod.time, "time", lambda: real_time() + 61)
    assert auth.is_rate_limited("4.4.4.4") is False


async def test_login_endpoint_returns_429_when_limited(tmp_path):
    """HTTP 层：被限流要返回 **429**，且错误类型可判别（不能混成 401）。"""
    token = generate_token()
    cfg = Config()
    cfg.storage.sqlite_path = str(tmp_path / "rl.db")
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=token, max_failures=2))
    c = app.test_client()

    for _ in range(2):
        r = await c.post("/api/v1/auth/login", json={"token": "nope-nope-nope"})
        assert r.status_code == 401

    r = await c.post("/api/v1/auth/login", json={"token": "nope-nope-nope"})
    assert r.status_code == 429
    assert (await r.get_json())["error"]["type"] == "rate_limited"

    # 正确令牌此时也应被挡（限流优先于校验）
    r = await c.post("/api/v1/auth/login", json={"token": token})
    assert r.status_code == 429
    await store.close()


async def test_cookie_secure_flag_follows_config(tmp_path):
    """`https_only` 必须真的作用到 cookie 的 Secure 标志。

    ⚠️ 踩过的取舍：本机 http 下若带 Secure，浏览器**直接不收 cookie**，
       表现为"点了登录没反应" —— 所以默认必须是 False。
    """
    token = generate_token()
    cfg = Config()
    cfg.storage.sqlite_path = str(tmp_path / "sec.db")
    cfg.server.https_only = True
    store = SqliteStore(cfg.storage.sqlite_path)
    await store.open()
    app = create_app(cfg, store, AuthManager(access_token=token))
    c = app.test_client()

    resp = await c.post("/api/v1/auth/login", json={"token": token})
    assert "Secure" in resp.headers.get("Set-Cookie", "")
    await store.close()


def test_is_public_detects_non_loopback():
    """绑定地址判定：回环不算开放，其它都算。"""
    from emotion_app.infra.config import Config as C

    for host, expected in [
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("192.168.1.10", True),
    ]:
        cfg = C()
        cfg.server.host = host
        assert cfg.server.is_public() is expected, host
