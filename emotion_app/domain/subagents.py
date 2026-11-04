# -*- coding: utf-8 -*-
"""子代理配置：让用户**连接自己的 agent 平台**。

## 语义（情绪 2026-11-03 定，原话）

> 「我想要的是**平台本身不带 agent 能力**，是子代理。**她把任务发出去等回收就行**。
>  就像是我们是主人，把任务发给佣人，明白不。」
>
> 「方案 c 单独做个界面来 **让用户连接自己的 agent 平台**。」

⇒ 这份配置描述的是「**佣人住哪儿、怎么找到他**」，不是「平台自己怎么当 agent」。

## 存储

独立 section `subagents`（与 `settings` 同级）。刻意**不塞进 settings**：
两件事的生命周期不同 —— 换上游模型和换子代理平台互不相干，
混在一张表里会让"保存子代理配置"顺带重写上游配置。

## 🔴 token 只写不回显

`public_view()` 给的是**打码值**（`•` 开头 + 长度提示）。
保存时若收到的是打码值，就**保持原值**、绝不把它当新令牌写回去。

⚠️ 这条不是洁癖：本项目踩过一次同类事故 —— 把打码值当真值存回去，
令牌就变成了 `••••••••`，之后所有请求 401，而界面上显示"已配置"。
（`api_adapters.py` 有同一套处理，照它做的。）

## 为什么后端类型要可扩展

「用户自己的 agent 平台」没有统一协议。所以这里只定**目录 + 字段描述**，
后端实现由 `host/astrbot_host/subagent.py` 的 `SubagentBackend` 协议承担。
加一种平台 = 加一条目录项 + 一个实现了 `run()` 的类，**不改本文件**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..infra.logging_setup import get_logger
from ..storage.base import StoreBackend

log = get_logger(__name__)

SECTION = "subagents"

#: 打码前缀。保存时看到这个前缀就认为"用户没改"。
MASK = "•"

#: 后端类型目录。
#:
#: `fields` 驱动面板渲染（`type: string|int|bool|secret|string_list`）——
#: **前端不许硬编码字段名**，否则加一种后端就要改两处，早晚漂移。
BACKEND_TYPES: tuple[dict[str, Any], ...] = (
    {
        "id": "null",
        "label": "未接入（默认）",
        "description": (
            "不接任何外部 agent。角色的 transfer_to_* 工具仍会注册，"
            "但调用时会**如实返回「没有可用的子代理后端」**，不会假装成功。"
        ),
        "fields": [],
    },
    {
        "id": "http",
        "label": "通用 HTTP agent",
        "description": (
            "把任务 POST 给你的 agent 服务，期望返回 JSON {\"text\": \"结果\"}。"
            "适合自己写的服务、n8n、Dify 等任何能收 HTTP 的平台。"
        ),
        "fields": [
            {"key": "endpoint", "label": "接口地址", "type": "string",
             "required": True, "placeholder": "https://your-agent.example.com/run"},
            {"key": "token", "label": "访问令牌（可选）", "type": "secret",
             "required": False, "help": "会以 Authorization: Bearer <token> 发送。"},
            {"key": "timeout_seconds", "label": "超时（秒）", "type": "int",
             "required": False, "default": 120,
             "help": "子代理跑得久就调大。超时后如实报失败，不静默。"},
        ],
    },
    {
        "id": "dsh",
        "label": "DSH（预留）",
        "description": (
            "DeepSeek Harness 会话桥接。**这一版尚未实现具体适配**，"
            "选择后等同通用 HTTP；等接口定稿再补。"
        ),
        "fields": [
            {"key": "endpoint", "label": "DSH 地址", "type": "string",
             "required": True, "placeholder": "http://127.0.0.1:3082"},
            {"key": "token", "label": "访问令牌", "type": "secret", "required": False},
            {"key": "timeout_seconds", "label": "超时（秒）", "type": "int",
             "required": False, "default": 300},
        ],
    },
)

_BACKEND_IDS = frozenset(item["id"] for item in BACKEND_TYPES)


def mask_secret(value: str) -> str:
    """把令牌打成 `••••（43 字符）` 这种"能看出有没有、看不出是什么"的形态。"""
    if not value:
        return ""
    return f"{MASK * 8}（{len(value)} 字符）"


def is_masked(value: Any) -> bool:
    """是不是打码值（用户没动过）。"""
    return isinstance(value, str) and value.startswith(MASK)


@dataclass
class SubagentConfig:
    """用户的子代理接入配置。"""

    enabled: bool = False
    backend: str = "null"
    endpoint: str = ""
    token: str = ""
    timeout_seconds: int = 120
    agents: list[dict[str, Any]] = field(default_factory=list)
    _loaded: bool = field(default=False, repr=False)

    def to_storage(self) -> dict[str, Any]:
        """落盘形态（**含 token 真值**）。"""
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "endpoint": self.endpoint,
            "token": self.token,
            "timeout_seconds": self.timeout_seconds,
            "agents": [dict(item) for item in self.agents],
        }

    @classmethod
    def from_storage(cls, d: dict[str, Any]) -> SubagentConfig:
        agents = d.get("agents")
        return cls(
            enabled=bool(d.get("enabled")),
            backend=str(d.get("backend") or "null"),
            endpoint=str(d.get("endpoint") or ""),
            token=str(d.get("token") or ""),
            timeout_seconds=_positive_int(d.get("timeout_seconds"), 120),
            # ⚠️ 脏数据不能让它变成非 list —— 下游到处 `.get("name")` 会炸
            agents=[dict(item) for item in agents if isinstance(item, dict)]
            if isinstance(agents, list) else [],
            _loaded=True,
        )


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class SubagentService:
    """读写子代理配置，并把它转成宿主能用的形状。"""

    def __init__(self, store: StoreBackend) -> None:
        self._store = store
        self._data = SubagentConfig()

    # ---- 载入 / 保存 ----

    async def load(self) -> None:
        raw = await self._store.load_store()
        section = raw.get(SECTION) or {}
        if isinstance(section, dict):
            self._data = SubagentConfig.from_storage(section)
        log.info(
            "子代理配置已载入",
            extra={"data": {
                "enabled": self._data.enabled,
                "backend": self._data.backend,
                "agents": len(self._data.agents),
                # 只报"配没配"，绝不打印 token
                "endpoint_configured": bool(self._data.endpoint),
                "token_configured": bool(self._data.token),
            }},
        )

    async def _persist(self) -> None:
        await self._store.save_sections({SECTION: self._data.to_storage()})

    # ---- 读 ----

    @property
    def config(self) -> SubagentConfig:
        return self._data

    def public_view(self) -> dict[str, Any]:
        """**脱敏**视图 —— token 只给打码值。"""
        return {
            "enabled": self._data.enabled,
            "backend": self._data.backend,
            "endpoint": self._data.endpoint,
            "token": mask_secret(self._data.token),
            "token_configured": bool(self._data.token),
            "timeout_seconds": self._data.timeout_seconds,
            "agents": [dict(item) for item in self._data.agents],
        }

    def backend_types(self) -> list[dict[str, Any]]:
        """后端目录（面板据此渲染表单）。"""
        return [dict(item) for item in BACKEND_TYPES]

    def orchestrator_config(self) -> dict[str, Any]:
        """转成 `SubagentOrchestrator.reload_from_config()` 吃的形状。"""
        return {
            "enabled": self._data.enabled,
            "agents": [dict(item) for item in self._data.agents],
        }

    def connection(self) -> dict[str, Any]:
        """当前连接参数（**含真 token**，只交给后端实现用，绝不外发）。"""
        return {
            "backend": self._data.backend,
            "endpoint": self._data.endpoint,
            "token": self._data.token,
            "timeout_seconds": self._data.timeout_seconds,
        }

    # ---- 写 ----

    async def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """局部更新。**打码的 token 表示"用户没动"，保持原值。**

        返回脱敏视图。
        """
        if not isinstance(patch, dict):
            raise ValueError("子代理配置必须是对象")

        if "enabled" in patch:
            self._data.enabled = bool(patch["enabled"])

        if "backend" in patch:
            backend = str(patch["backend"] or "").strip()
            if backend not in _BACKEND_IDS:
                # 明确拒绝，不静默回落到 null（否则用户以为选上了）
                raise ValueError(f"未知的子代理后端：{backend!r}")
            self._data.backend = backend

        if "endpoint" in patch:
            self._data.endpoint = str(patch["endpoint"] or "").strip()

        if "token" in patch:
            value = patch["token"]
            # 🔴 打码值 = 用户没改 ⇒ **保持原值**。这是本文件最要紧的一条。
            if not is_masked(value):
                self._data.token = str(value or "")

        if "timeout_seconds" in patch:
            self._data.timeout_seconds = _positive_int(patch["timeout_seconds"], 120)

        if "agents" in patch:
            self._data.agents = _normalize_agents(patch["agents"])

        await self._persist()
        log.info(
            "子代理配置已保存",
            extra={"data": {
                "enabled": self._data.enabled,
                "backend": self._data.backend,
                "agents": len(self._data.agents),
            }},
        )
        return self.public_view()


def _normalize_agents(value: Any) -> list[dict[str, Any]]:
    """清洗 agents 列表。

    🔴 **空名直接丢弃并记日志**（不静默）：空名会注册出一个
       `transfer_to_` 这种主模型永远调不到的工具。
    """
    if not isinstance(value, list):
        raise ValueError("agents 必须是数组")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            log.warning("丢弃了一个没有名字的子代理条目（它无法注册成工具）")
            continue
        tools = item.get("tools")
        if isinstance(tools, str):
            tools = [line.strip() for line in tools.splitlines() if line.strip()]
        elif isinstance(tools, list):
            tools = [str(t).strip() for t in tools if str(t).strip()]
        else:
            tools = []
        result.append({
            "name": name,
            "system_prompt": str(item.get("system_prompt", "") or "").strip(),
            "public_description": str(item.get("public_description", "") or "").strip(),
            "tools": tools,
            "provider_id": str(item.get("provider_id", "") or "").strip(),
            "enabled": item.get("enabled", True) is not False,
        })
    # 重名会互相覆盖（后者顶掉前者），这里**如实拒绝**而不是安静去重
    seen: set[str] = set()
    for item in result:
        if item["name"] in seen:
            raise ValueError(f"子代理名字重复：{item['name']!r}")
        seen.add(item["name"])
    return result
