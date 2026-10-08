# -*- coding: utf-8 -*-
"""人设仓库：CRUD + 持久化。

存法：`store` 的 `personas` section 是一个 `{id: persona_dict}` 映射。
⚠️ 刻意**先不分表**（见 `sqlite_store.py` 的说明）—— 等模型稳定再规范化。
"""
from __future__ import annotations

import time
from typing import Any

from ..infra.logging_setup import get_logger
from ..storage.base import StoreBackend, StorageError
from .persona import Persona

log = get_logger(__name__)

SECTION = "personas"

#: 单人设上限 —— 防"角色卡可以有多张"变成无上限膨胀。
#: 情绪说"一个人用但角色卡可以有多张"，几百张足够；这个数只是防御性上限。
MAX_PERSONAS = 500


class PersonaNotFound(KeyError):
    """人设不存在。"""


class PersonaRepository:
    """人设的读写。"""

    def __init__(self, store: StoreBackend) -> None:
        self._store = store
        self._cache: dict[str, Persona] | None = None

    # ---- 载入 ----

    async def _ensure(self) -> dict[str, Persona]:
        if self._cache is None:
            raw = await self._store.load_store()
            section = raw.get(SECTION) or {}
            if not isinstance(section, dict):
                raise StorageError(f"{SECTION} section 格式错误：期望对象")
            self._cache = {
                pid: Persona.from_dict(d)
                for pid, d in section.items()
                if isinstance(d, dict)
            }
            log.info("人设已载入", extra={"data": {"count": len(self._cache)}})
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    async def _persist(self) -> None:
        data = await self._ensure()
        await self._store.save_sections(
            {SECTION: {pid: p.to_dict(include_secrets=True) for pid, p in data.items()}}
        )

    # ---- 读 ----

    async def list(self) -> list[Persona]:
        """全部人设，按更新时间倒序（最近的在前）。"""
        data = await self._ensure()
        return sorted(data.values(), key=lambda p: p.updated_at, reverse=True)

    async def get(self, persona_id: str) -> Persona:
        data = await self._ensure()
        if persona_id not in data:
            raise PersonaNotFound(persona_id)
        return data[persona_id]

    async def exists(self, persona_id: str) -> bool:
        return persona_id in (await self._ensure())

    async def count(self) -> int:
        return len(await self._ensure())

    # ---- 写 ----

    async def create(self, persona: Persona) -> Persona:
        data = await self._ensure()
        if len(data) >= MAX_PERSONAS:
            raise StorageError(f"人设数量已达上限 {MAX_PERSONAS}")

        persona.validate()

        # id 冲突时重新分配（不静默覆盖已有）
        if persona.id in data:
            from uuid import uuid4

            persona.id = uuid4().hex
            log.warning("人设 id 冲突，已重新分配")

        now = time.time()
        persona.created_at = now
        persona.updated_at = now
        data[persona.id] = persona
        await self._persist()
        log.info("人设已创建", extra={"data": {"id": persona.id, "name": persona.name}})
        return persona

    async def update(self, persona_id: str, patch: dict[str, Any]) -> Persona:
        """局部更新。

        ⚠️ `id` / `created_at` **不可改**（改了会破坏引用）。
        """
        data = await self._ensure()
        if persona_id not in data:
            raise PersonaNotFound(persona_id)

        current = data[persona_id]
        current_dict = current.to_dict(include_secrets=True)

        allowed = {
            "name", "description", "personality", "scenario",
            "creator_notes", "avatar_path",
            "api_url", "api_key", "model", "prompt_sections",
            # 🆕 人设参考图（面板上传后写这个字段，2026-09-25）
            "reference_image_path",
        }
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key == "prompt_sections":
                from .persona import PromptSection

                if not isinstance(value, list):
                    raise ValueError("prompt_sections 必须是数组")
                # 统一成 dict —— 调用方可能传 dict 也可能传 PromptSection
                current_dict[key] = [
                    x.to_dict() if isinstance(x, PromptSection)
                    else PromptSection.from_dict(x).to_dict()
                    for x in value
                ]
            else:
                current_dict[key] = value

        updated = Persona.from_dict(current_dict)
        updated.id = persona_id          # 强制保持
        updated.created_at = current.created_at
        updated.updated_at = time.time()
        updated.validate()

        data[persona_id] = updated
        await self._persist()
        log.info("人设已更新", extra={"data": {"id": persona_id}})
        return updated

    async def delete(self, persona_id: str) -> None:
        data = await self._ensure()
        if persona_id not in data:
            raise PersonaNotFound(persona_id)
        data.pop(persona_id)
        await self._persist()
        log.info("人设已删除", extra={"data": {"id": persona_id}})

    async def duplicate(self, persona_id: str, *, new_name: str = "") -> Persona:
        """复制一份（爱语的角色卡是"多张"，复制是常用操作）。"""
        src = await self.get(persona_id)
        clone = Persona.from_dict(src.to_dict(include_secrets=True))
        from uuid import uuid4

        clone.id = uuid4().hex
        clone.name = new_name or f"{src.name} (副本)"
        clone.created_at = 0.0
        clone.updated_at = 0.0
        return await self.create(clone)
