# -*- coding: utf-8 -*-
"""运行时设置：可持久化、可在面板/App 里改的配置。

🔴 为什么需要它（情绪 A1 答复）：
   「App 端修改配置」= **① 后端连接 + ② 业务配置（人设/模型/上游 key）**。
   环境变量只在启动时读一次，**运行时改不了** → 需要一层"可写配置"。

设计：
  * 存 `store` 的 `settings` section；
  * **只覆盖允许覆盖的字段**（上游相关 + 当前人设），不是万能开关；
  * `api_key` **只进不出**（写进去可以，读出来永远脱敏）；
  * 空字符串 = "用环境变量的默认值"（不是"设为空"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..infra.config import Config
from ..infra.logging_setup import get_logger
from ..storage.base import StoreBackend

log = get_logger(__name__)

SECTION = "settings"

#: 允许被运行时覆盖的字段白名单。
#: ⚠️ 刻意不含 `server.*` / `auth.*` / `storage.*` —— 那些是**部署时**定的，
#:    改错了服务起不来（这正是情绪说的"③ 不在 App 改"）。
WRITABLE_FIELDS = (
    "upstream_base_url",
    "upstream_model",
    "upstream_api_key",
    "active_persona_id",
)


@dataclass
class RuntimeSettings:
    """可写的运行时配置。空值表示"沿用环境变量默认"。"""

    upstream_base_url: str = ""
    upstream_model: str = ""
    upstream_api_key: str = ""
    active_persona_id: str = ""
    _loaded: bool = field(default=False, repr=False)

    def to_storage(self) -> dict[str, Any]:
        """落盘形态（**含** api_key）。"""
        return {
            "upstream_base_url": self.upstream_base_url,
            "upstream_model": self.upstream_model,
            "upstream_api_key": self.upstream_api_key,
            "active_persona_id": self.active_persona_id,
        }

    @classmethod
    def from_storage(cls, d: dict[str, Any]) -> RuntimeSettings:
        return cls(
            upstream_base_url=str(d.get("upstream_base_url") or ""),
            upstream_model=str(d.get("upstream_model") or ""),
            upstream_api_key=str(d.get("upstream_api_key") or ""),
            active_persona_id=str(d.get("active_persona_id") or ""),
            _loaded=True,
        )


class SettingsService:
    """读写运行时设置，并与环境变量默认值合并。"""

    def __init__(self, store: StoreBackend, config: Config) -> None:
        self._store = store
        self._config = config
        self._data = RuntimeSettings()

    # ---- 载入 / 保存 ----

    async def load(self) -> None:
        raw = await self._store.load_store()
        section = raw.get(SECTION) or {}
        if isinstance(section, dict):
            self._data = RuntimeSettings.from_storage(section)
        log.info(
            "运行时设置已载入",
            extra={"data": {"has_overrides": bool(self._data.upstream_api_key or self._data.upstream_model)}},
        )

    async def _persist(self) -> None:
        await self._store.save_sections({SECTION: self._data.to_storage()})

    # ---- 读 ----

    def effective_upstream(self) -> dict[str, Any]:
        """合并后的上游配置（运行时覆盖 > 环境变量）。"""
        up = self._config.upstream
        return {
            "base_url": self._data.upstream_base_url or up.base_url,
            "model": self._data.upstream_model or up.model,
            "api_key": self._data.upstream_api_key or up.api_key,
            "timeout": up.timeout_seconds,
        }

    @property
    def active_persona_id(self) -> str:
        return self._data.active_persona_id

    def public_view(self) -> dict[str, Any]:
        """**脱敏**视图 —— api_key 只报"是否已设置"与来源。"""
        up = self.effective_upstream()
        env = self._config.upstream
        return {
            "upstream": {
                "base_url": up["base_url"],
                "model": up["model"],
                "api_key_configured": bool(up["api_key"]),
                # 让用户知道"这个 key 是我在面板设的，还是环境变量给的"
                "api_key_source": (
                    "runtime" if self._data.upstream_api_key
                    else ("env" if env.api_key else "none")
                ),
            },
            "active_persona_id": self._data.active_persona_id,
            "overrides": {
                k: bool(getattr(self._data, k)) for k in WRITABLE_FIELDS
                if k != "upstream_api_key"
            },
        }

    # ---- 写 ----

    async def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """局部更新。**只接受白名单字段**，其余忽略（不报错，但不生效）。

        Returns:
            脱敏后的新视图。
        """
        applied: list[str] = []
        for key in WRITABLE_FIELDS:
            if key not in patch:
                continue
            value = patch[key]
            if value is None:
                continue
            setattr(self._data, key, str(value))
            applied.append(key)

        if applied:
            await self._persist()
            # ⚠️ 只记字段名，**不记值**（值里可能有 key）
            log.info("运行时设置已更新", extra={"data": {"fields": applied}})

        return self.public_view()

    async def set_active_persona(self, persona_id: str) -> None:
        self._data.active_persona_id = persona_id or ""
        await self._persist()

    async def clear(self, *fields: str) -> None:
        """把字段恢复成"用环境变量默认"。"""
        changed = []
        for f in fields:
            if f in WRITABLE_FIELDS and getattr(self._data, f):
                setattr(self._data, f, "")
                changed.append(f)
        if changed:
            await self._persist()
