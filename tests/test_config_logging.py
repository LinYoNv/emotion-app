# -*- coding: utf-8 -*-
"""配置与日志测试。重点测**失败路径**（项目铁律：不静默降级）。"""
from __future__ import annotations

import json
import logging

import pytest

from emotion_app.infra.config import (
    ENV_PREFIX,
    MIN_TOKEN_LENGTH,
    ConfigError,
    generate_token,
    load_config,
)
from emotion_app.infra.logging_setup import (
    REDACTED,
    JsonFormatter,
    is_sensitive_key,
    scrub,
    scrub_text,
)

GOOD_TOKEN = "a" * MIN_TOKEN_LENGTH  # 16 字符，刚好合规


# ---------------------------------------------------------------------------
# 配置：成功路径
# ---------------------------------------------------------------------------


def test_defaults_are_safe():
    """默认值必须是安全的：绑本地、端口合法。"""
    cfg = load_config({}, validate=False)
    assert cfg.server.host == "127.0.0.1"
    assert 1 <= cfg.server.port <= 65535
    assert cfg.storage.backend == "sqlite"  # 情绪 2026-09-18 拍板


def test_load_from_env():
    cfg = load_config(
        {
            f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": GOOD_TOKEN,
            f"{ENV_PREFIX}SERVER_PORT": "7777",
            f"{ENV_PREFIX}UPSTREAM_MODEL": "test-model",
            f"{ENV_PREFIX}LOGGING_JSON": "true",
        }
    )
    assert cfg.server.port == 7777
    assert cfg.upstream.model == "test-model"
    assert cfg.logging.json_output is True


def test_generate_token_is_long_enough():
    assert len(generate_token()) >= MIN_TOKEN_LENGTH


# ---------------------------------------------------------------------------
# 配置：失败路径（关键）
# ---------------------------------------------------------------------------


def test_missing_token_refuses_to_start():
    """🔴 没有令牌 → 必须拒绝启动（不是警告）。"""
    with pytest.raises(ConfigError) as ei:
        load_config({})
    assert "令牌" in str(ei.value)


def test_short_token_refuses_to_start():
    """🔴 令牌过短 → 拒绝启动（照 AstrBot 陪伴插件的硬校验）。"""
    with pytest.raises(ConfigError) as ei:
        load_config({f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": "short"})
    assert "过短" in str(ei.value)


def test_bad_port_rejected():
    with pytest.raises(ConfigError):
        load_config(
            {f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": GOOD_TOKEN, f"{ENV_PREFIX}SERVER_PORT": "70000"}
        )


def test_non_numeric_port_rejected():
    """类型错误也要明确报错，不能静默用默认值。"""
    with pytest.raises(ConfigError) as ei:
        load_config(
            {f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": GOOD_TOKEN, f"{ENV_PREFIX}SERVER_PORT": "abc"}
        )
    assert "无法转换" in str(ei.value)


def test_bad_upstream_url_rejected():
    with pytest.raises(ConfigError):
        load_config(
            {
                f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": GOOD_TOKEN,
                f"{ENV_PREFIX}UPSTREAM_BASE_URL": "ftp://nope",
            }
        )


def test_unknown_storage_backend_rejected():
    with pytest.raises(ConfigError):
        load_config(
            {
                f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": GOOD_TOKEN,
                f"{ENV_PREFIX}STORAGE_BACKEND": "mysql",
            }
        )


# ---------------------------------------------------------------------------
# 脱敏（关键：令牌不能出现在任何输出里）
# ---------------------------------------------------------------------------


def test_public_view_hides_secrets():
    """🔴 脱敏视图绝不含密钥明文。"""
    secret_token = "s" * 20
    secret_key = "sk-abcdefghijklmnopqrstuvwxyz"
    cfg = load_config(
        {
            f"{ENV_PREFIX}AUTH_ACCESS_TOKEN": secret_token,
            f"{ENV_PREFIX}UPSTREAM_API_KEY": secret_key,
        }
    )
    view = json.dumps(cfg.public_view(), ensure_ascii=False)

    assert secret_token not in view
    assert secret_key not in view
    assert cfg.public_view()["auth"]["token_configured"] is True
    assert cfg.public_view()["upstream"]["api_key_configured"] is True


def test_is_sensitive_key():
    for k in ("access_token", "api_key", "APIKEY", "password", "Authorization", "my_secret"):
        assert is_sensitive_key(k), k
    for k in ("username", "host", "port", "model"):
        assert not is_sensitive_key(k), k


def test_diagnostic_fields_are_not_redacted():
    """🔴 回归：过度脱敏也是 bug。

    实测踩到：`token_configured`（布尔）和 `token_length`（数字）被误打成 ***REDACTED***，
    导致日志失去诊断价值。**脱敏要精确，不能"见词就杀"。**
    """
    for k in ("token_configured", "token_length", "api_key_configured", "token_set"):
        assert not is_sensitive_key(k), f"{k} 不该被脱敏"

    out = scrub({"token_configured": True, "token_length": 32})
    assert out == {"token_configured": True, "token_length": 32}


def test_redaction_covers_real_secret_keys():
    """回归的反面：真正的密钥字段必须仍然被打掉。"""
    for k in ("access_token", "upstream_api_key", "Authorization", "db_password"):
        assert is_sensitive_key(k), f"{k} 必须被脱敏"


def test_entity_ids_are_not_redacted():
    """🔴 回归：32 位 hex 的**实体 id 不是密钥**，不能被打掉。

    实测踩到：最初有一条裸的 `[A-Fa-f0-9]{32,}` 启发式，把 uuid4().hex 形状的人设 id
    也打成了 ***REDACTED*** → 日志彻底失去诊断价值。
    **这是第三次过度脱敏 —— 脱敏要精确，宁可少杀不可错杀。**
    """
    pid = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert scrub({"id": pid})["id"] == pid
    assert scrub({"persona_id": pid})["persona_id"] == pid
    assert scrub(f"pipeline: {pid}") == f"pipeline: {pid}"


def test_hex_secret_with_context_is_still_redacted():
    """反面：带上下文的 hex **仍然必须**被打掉。"""
    secret = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    for text in (f"api_key={secret}", f"token: {secret}", f"secret={secret}"):
        assert secret not in scrub_text(text), text
        assert REDACTED in scrub_text(text), text


def test_scrub_redacts_dict_values():
    out = scrub({"api_key": "sk-realsecretvalue123", "model": "gpt", "nested": {"token": "t"}})
    assert out["api_key"] == REDACTED
    assert out["nested"]["token"] == REDACTED
    assert out["model"] == "gpt"


def test_scrub_text_kills_common_secret_shapes():
    """自由文本里的密钥形态也要打掉。"""
    assert "sk-" not in scrub_text("key is sk-abcdefghijklmnop1234 here")
    assert "ghp_" not in scrub_text("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert REDACTED in scrub_text("Authorization: Bearer abcdefghijklmnop")


def test_json_formatter_redacts():
    """🔴 端到端：日志格式化后不能含密钥。"""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="calling upstream", args=(), exc_info=None,
    )
    record.data = {"api_key": "sk-supersecretvalue", "url": "https://x/y"}

    payload = json.loads(formatter.format(record))
    assert payload["data"]["api_key"] == REDACTED
    assert "supersecret" not in formatter.format(record)
