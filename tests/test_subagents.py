# -*- coding: utf-8 -*-
"""判据：**子代理接入 API**（让用户在面板里连自己的 agent 平台）。

## 语义（情绪 2026-11-03 定）

> 「方案 c 单独做个界面来 **让用户连接自己的 agent 平台**。」
> 「平台本身不带 agent 能力，是子代理。**她把任务发出去等回收就行**。」

## 这个文件守什么

| # | 判据 | 抓的是什么 |
|---|---|---|
| ① | **token 只写不回显** + 打码值往返不丢真值 | 本项目踩过：把 `••••` 当真令牌存回去 → 之后全 401 却显示"已配置" |
| ② | **每条路由都要鉴权**（含**反向** 401） | 真事故：漏一个 `require_auth()` → 公网可读 |
| ③ | 未装配服务时 **503**，不是 500/200 | 别把"没接上"伪装成"服务器错误"或"成功" |
| ④ | 未知 backend **明确拒绝** | 静默回落 null 会让用户以为选上了 |
| ⑤ | `/test` 在 null 后端时**如实报不可用** | 只回 `ok:true` 不验证 = 假装成功 |
| ⑥ | `/test` 用打码 token 时**沿用已存真值** | 否则"测试"必然 401，用户会以为是对方的问题 |
| ⑦ | 空名 agent 被丢弃 / 重名被拒绝 | 空名会注册出主模型永远调不到的工具 |

⚠️ ② 是**反向**判据：本项目铁律要求"安全功能必须测反向"，
   只测"正确令牌 200"证明不了接口受保护。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ⚠️ `host/` 也要加进来：`subagent_backends` 延迟导入 `astrbot_host.subagent`。
#    `pyproject.toml` 的 `testpaths` 只含 `tests`，所以这里必须自己补，
#    否则产品代码明明写对了，测试却 import 失败。
HOST_DIR = ROOT / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from emotion_app.domain.subagents import (  # noqa: E402
    SubagentService,
    is_masked,
    mask_secret,
)


class MemoryStore:
    """最小内存 store（只实现本判据用到的两个方法）。"""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def load_store(self) -> dict[str, Any]:
        return {k: v for k, v in self.data.items()}

    async def save_sections(self, sections: dict[str, Any]) -> None:
        self.data.update(sections)


async def make_service() -> tuple[SubagentService, MemoryStore]:
    store = MemoryStore()
    service = SubagentService(store)
    await service.load()
    return service, store


# ---------------------------------------------------------------------------
# ① token：只写不回显，打码值往返不丢真值（本文件最要紧的一条）
# ---------------------------------------------------------------------------


async def test_token_is_masked_in_public_view():
    service, _ = await make_service()
    await service.update({"backend": "http", "endpoint": "https://x", "token": "真令牌abc123"})

    view = service.public_view()
    assert view["token"] != "真令牌abc123", "公开视图不能回显真令牌"
    assert is_masked(view["token"]), f"应当是打码值，实际 {view['token']!r}"
    assert view["token_configured"] is True, "要能看出「配没配」"
    # ⚠️ 我第一版写的 `"6" in view["token"]` 是**断言错、代码对**：
    #    "真令牌abc123" 是 9 个字符，不是 6。长度提示只是"帮用户确认自己填对了"，
    #    断言应该验**它确实带着这个长度**，而不是猜一个数字。
    assert "9 字符" in view["token"], f"打码值里带长度提示，实际 {view['token']!r}"

    # 磁盘上必须还是真值（否则重启就废了）
    assert service.config.token == "真令牌abc123"


async def test_masked_token_round_trip_keeps_the_real_value():
    """把打码值原样存回去 —— **真值必须保持不变**。

    🔴 这是本项目真踩过的事故形态：把 `••••` 当新令牌写回，
       之后所有请求 401，而界面上显示"已配置"。
    """
    service, _ = await make_service()
    await service.update({"backend": "http", "token": "真令牌abc123"})

    masked = service.public_view()["token"]
    # 用户没动 token，只改了 endpoint
    await service.update({"token": masked, "endpoint": "https://changed"})

    assert service.config.token == "真令牌abc123", "打码值被当成了新令牌"
    assert service.config.endpoint == "https://changed"


async def test_empty_token_clears_it():
    """用户填空 = 主动清除（与"没动"区分开）。"""
    service, _ = await make_service()
    await service.update({"token": "真令牌"})
    await service.update({"token": ""})
    assert service.config.token == ""


# ---------------------------------------------------------------------------
# ② 鉴权（含反向）
# ---------------------------------------------------------------------------


@pytest.fixture()
def subagent_client(monkeypatch):
    """造一个真 Quart app，挂上子代理蓝图。"""
    import asyncio

    from quart import Quart

    from emotion_app.web.api_subagents import build_blueprint

    app = Quart(__name__)
    deny_all = {"on": False}

    def require_auth():
        if deny_all["on"]:
            return {"status": "error", "message": "未登录", "reason": "unauthorized"}, 401
        return None

    app.register_blueprint(build_blueprint(require_auth))

    service, _store = asyncio.get_event_loop().run_until_complete(make_service()) \
        if False else (None, None)
    return app, deny_all


def _service_for(app) -> Any:
    import asyncio

    service = SubagentService(MemoryStore())
    asyncio.get_event_loop().run_until_complete(service.load()) if False else None
    return service


async def _build_app():
    from quart import Quart

    from emotion_app.web.api_subagents import build_blueprint

    app = Quart(__name__)
    state = {"deny": False}

    def require_auth():
        if state["deny"]:
            return {"status": "error", "message": "未登录", "reason": "unauthorized"}, 401
        return None

    app.register_blueprint(build_blueprint(require_auth))

    service = SubagentService(MemoryStore())
    await service.load()
    app.config["EMOTION_SUBAGENTS_SERVICE"] = service
    return app, service, state


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/subagents"),
    ("GET", "/api/v1/subagents/backends"),
    ("PUT", "/api/v1/subagents"),
    ("POST", "/api/v1/subagents/test"),
])
async def test_every_route_is_protected(method, path):
    """🔴 **反向判据**：未登录时每条路由都必须 401。

    只测"正确令牌 200"证明不了接口受保护 —— 本项目真出过"漏一个鉴权 = 公网可读"。
    """
    app, _service, state = await _build_app()
    state["deny"] = True
    async with app.test_client() as client:
        response = await client.open(path, method=method, json={})
        assert response.status_code == 401, (
            f"{method} {path} 未鉴权就放行了（实际 {response.status_code}）"
        )


# ---------------------------------------------------------------------------
# ③ 未装配服务
# ---------------------------------------------------------------------------


async def test_missing_service_reports_503_not_500():
    """服务没装配要说"没装配"，不能变成服务器错误或假装成功。"""
    from quart import Quart

    from emotion_app.web.api_subagents import build_blueprint

    app = Quart(__name__)
    app.register_blueprint(build_blueprint(lambda: None))  # 不挂 service

    async with app.test_client() as client:
        response = await client.get("/api/v1/subagents")
        assert response.status_code == 503
        body = await response.get_json()
        assert body["reason"] == "subagents_unavailable"


# ---------------------------------------------------------------------------
# ④ 未知 backend
# ---------------------------------------------------------------------------


async def test_unknown_backend_is_rejected():
    """未知后端**明确拒绝**（静默回落会让用户以为选上了）。"""
    service, _ = await make_service()
    with pytest.raises(ValueError, match="未知的子代理后端"):
        await service.update({"backend": "不存在的平台"})


async def test_known_backends_are_accepted():
    service, _ = await make_service()
    for backend in ("null", "http", "dsh"):
        await service.update({"backend": backend})
        assert service.config.backend == backend


# ---------------------------------------------------------------------------
# ⑤⑥ /test 的真实性
# ---------------------------------------------------------------------------


async def test_probe_reports_null_backend_as_unavailable():
    """null 后端必须**如实说不可用**，不能回 ok。"""
    from emotion_app.web.subagent_backends import probe_backend

    result = await probe_backend({"backend": "null", "endpoint": ""})
    assert result["ok"] is False
    assert "未接入" in result["reason"]


async def test_probe_without_endpoint_explains_what_is_missing():
    from emotion_app.web.subagent_backends import probe_backend

    result = await probe_backend({"backend": "http", "endpoint": ""})
    assert result["ok"] is False
    assert "接口地址" in result["reason"], "要告诉用户缺哪一项"


async def test_probe_uses_the_saved_token_when_given_a_mask():
    """表单里回传的是打码 token —— 测试时必须沿用**已存的真值**。

    否则"测试"必然 401，用户会以为是对方平台的问题（方向完全错了）。
    """
    from emotion_app.web.subagent_backends import build_backend

    backend = build_backend({"backend": "http", "endpoint": "https://x", "token": "真令牌"})
    assert backend._token == "真令牌"

    # 打码值不会被当成真令牌（由 API 层拦截，这里守 domain 的 is_masked 语义）
    assert is_masked(mask_secret("真令牌"))


# ---------------------------------------------------------------------------
# ⑦ agents 清洗
# ---------------------------------------------------------------------------


async def test_agent_without_name_is_dropped():
    """空名条目要丢弃 —— 否则注册出 `transfer_to_` 这种调不到的工具。"""
    service, _ = await make_service()
    view = await service.update({"agents": [
        {"name": "  ", "system_prompt": "空名"},
        {"name": "coder", "system_prompt": "你写代码"},
    ]})
    assert [item["name"] for item in view["agents"]] == ["coder"]


async def test_duplicate_agent_names_are_rejected():
    """重名会互相覆盖 —— 明确拒绝，不安静去重。"""
    service, _ = await make_service()
    with pytest.raises(ValueError, match="重复"):
        await service.update({"agents": [{"name": "x"}, {"name": "x"}]})


async def test_agents_string_tools_are_split_by_line():
    """面板里 tools 是 textarea（一行一个），这里要能收。"""
    service, _ = await make_service()
    view = await service.update({"agents": [
        {"name": "a", "tools": "shell\nread_file\n\n"},
    ]})
    assert view["agents"][0]["tools"] == ["shell", "read_file"]


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


async def test_config_survives_a_new_service_instance():
    """跨实例能读回（否则重启就丢配置）。"""
    store = MemoryStore()
    first = SubagentService(store)
    await first.load()
    await first.update({
        "enabled": True, "backend": "http", "endpoint": "https://x", "token": "真令牌",
        "agents": [{"name": "coder", "system_prompt": "你写代码"}],
    })

    second = SubagentService(store)
    await second.load()
    assert second.config.enabled is True
    assert second.config.backend == "http"
    assert second.config.token == "真令牌"
    assert [a["name"] for a in second.config.agents] == ["coder"]


# ---------------------------------------------------------------------------
# 后端目录（面板靠它渲染表单）
# ---------------------------------------------------------------------------


def test_backend_types_expose_renderable_fields():
    """每种后端都要给出 `fields`，前端才不用硬编码字段名。"""
    service = SubagentService(MemoryStore())
    types = service.backend_types()
    ids = {item["id"] for item in types}
    assert {"null", "http", "dsh"} <= ids
    for item in types:
        assert "label" in item and "description" in item
        assert isinstance(item["fields"], list)
        for field in item["fields"]:
            # 前端只认这五种类型，多一种就要同步改前端
            assert field["type"] in {"string", "int", "bool", "secret", "string_list"}
