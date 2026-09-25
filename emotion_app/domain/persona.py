# -*- coding: utf-8 -*-
"""人设模型。

字段来源：爱语真 dex 的 `PersonaInfo.java`（**实测 6 个字段**）。
⭐ 判据：这 6 个字段恰好是 **SillyTavern 角色卡标准结构** → 可导入现成角色卡生态。

⚠️ 但人设**不只是字段**（见 `docs/PERSONA_PROMPT_SYSTEM.md`）：
实测爱语把人物设定**组装成分节的 system prompt**（51 个 `【】` 小节）提交。
所以这里除 6 字段外，还带 `prompt_sections`（提示词片段）与 per-persona 上游覆盖。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: 提示词片段来源 —— 决定"这段是谁加的"，便于调试与治理
SECTION_SOURCES = (
    "builtin",   # 系统内置（如"强制格式约束"）
    "persona",   # 人设自带（角色卡里的设定）
    "user",      # 用户手写
    "memory",    # 记忆注入（S4）
    "module",    # 模块注入（S5 预留）
)


@dataclass(slots=True)
class PromptSection:
    """一段提示词。片段化是刻意设计——不是一坨大字符串。

    理由（见 `PERSONA_PROMPT_SYSTEM.md` §3.2）：
      ① 片段化 → 可按条件开关；
      ② 来源可追溯 → 出问题知道是谁加的；
      ③ 顺序稳定 → 稳定前缀提高缓存命中（爱语"记忆好"的真实机制）。
    """

    title: str                       # 小节名，如"文生图系统"
    content: str = ""
    enabled: bool = True
    order: int = 100                 # 越小越靠前；易变内容应放后面
    source: str = "user"
    condition: str = ""              # 条件表达式（S3+ 用，暂不解析）

    def validate(self) -> None:
        if not self.title or not self.title.strip():
            raise ValueError("PromptSection.title 不能为空")
        if self.source not in SECTION_SOURCES:
            raise ValueError(f"未知 source：{self.source!r}，允许 {SECTION_SOURCES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content,
            "enabled": self.enabled,
            "order": self.order,
            "source": self.source,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PromptSection:
        return cls(
            title=d["title"],
            content=d.get("content", ""),
            enabled=bool(d.get("enabled", True)),
            order=int(d.get("order", 100)),
            source=d.get("source", "user"),
            condition=d.get("condition", ""),
        )


@dataclass(slots=True)
class Persona:
    """一个人设（角色卡）。

    ⚠️ 情绪 2026-09-18：「就是一个人用，但是**角色卡可以有多张**就像爱语一样」
    → **不做多用户**，但人设是一对多。
    """

    # ---- 爱语 PersonaInfo 的 6 个字段（照抄，可导入 SillyTavern 卡）----
    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    creator_notes: str = ""
    avatar_path: str = ""

    # ---- 元数据 ----
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- 提示词组装（⭐ 核心，见 PERSONA_PROMPT_SYSTEM.md）----
    prompt_sections: list[PromptSection] = field(default_factory=list)

    # ---- per-persona 上游覆盖（爱语实测支持）----
    api_url: str = ""
    api_key: str = ""
    model: str = ""

    def validate(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Persona.name 不能为空")
        if len(self.name) > 200:
            raise ValueError("Persona.name 过长（>200）")
        for s in self.prompt_sections:
            s.validate()

    # ---- 序列化 ----

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """转 dict。

        Args:
            include_secrets: 是否包含 `api_key`。**默认 False**
                —— 出口（API/日志）一律脱敏，只有存储时才用 True。
        """
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "personality": self.personality,
            "scenario": self.scenario,
            "creator_notes": self.creator_notes,
            "avatar_path": self.avatar_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "prompt_sections": [s.to_dict() for s in self.prompt_sections],
            "api_url": self.api_url,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
        }
        if include_secrets:
            d["api_key"] = self.api_key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Persona:
        return cls(
            id=d.get("id") or uuid.uuid4().hex,
            name=d.get("name", ""),
            description=d.get("description", ""),
            personality=d.get("personality", ""),
            scenario=d.get("scenario", ""),
            creator_notes=d.get("creator_notes", ""),
            avatar_path=d.get("avatar_path", ""),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
            prompt_sections=[
                PromptSection.from_dict(x) for x in d.get("prompt_sections", [])
            ],
            api_url=d.get("api_url", ""),
            api_key=d.get("api_key", ""),
            model=d.get("model", ""),
        )

    # ---- 提示词组装 ----

    def enabled_sections(self) -> list[PromptSection]:
        """启用中的片段，**按 order 稳定排序**（同 order 按 title 保证确定性）。

        ⭐ 稳定性是刻意的：顺序抖动会破坏上游的 prompt 缓存命中。
        """
        return sorted(
            (s for s in self.prompt_sections if s.enabled),
            key=lambda s: (s.order, s.title),
        )
