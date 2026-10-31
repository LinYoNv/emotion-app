# -*- coding: utf-8 -*-
"""人设模型 —— **对齐 AstrBot 的 `Persona`**（情绪 2026-10-29 拍板）。

## 为什么改成 AstrBot 的结构

原来这里是「爱语 `PersonaInfo` 的 6 字段 + `prompt_sections` 片段化」。
那套结构在**面板上看不全**，而且**陪伴插件根本不认**：

* 面板界面只画 `description` / `personality` / `scenario` / `creator_notes`
  四个输入框 —— 人设正文如果放进 `prompt_sections`，界面上就是**空白**
  （实测：莉莉丝 8621 字符全文躺在 `prompt_sections`，面板四个框全空）。
* 陪伴插件通过 `astrbot_host/persona_manager.py` 读的是 AstrBot 的
  `Personality`（`provider.meta()` 那一族），字段名是 `prompt` / `name` /
  `begin_dialogs` / `tools` / `skills` / `custom_error_message`。
  它**不认识** `description` 那 6 个字段。

情绪原话：「把'人格'改成astrbot的那样……json字段也要按照astrbot那样做，
感觉这样就不容易出问题」。

## 权威来源（实测读出来的，不是猜的）

`astrbot/core/db/po.py:145` 的 `Persona`（v4 推荐，SQLModel 表）：

```python
persona_id: str = Field(max_length=255, nullable=False)
system_prompt: str = Field(sa_type=Text, nullable=False)
begin_dialogs: list | None = Field(default=None, sa_type=JSON)
tools: list | None = Field(default=None, sa_type=JSON)
skills: list | None = Field(default=None, sa_type=JSON)
custom_error_message: str | None = Field(default=None, sa_type=Text)
folder_id: str | None = Field(default=None, max_length=36)
sort_order: int = Field(default=0)
```

`astrbot/core/db/po.py:581` 的 `Personality`（v3 TypedDict，兼容用）：

```python
prompt: str; name: str
begin_dialogs: list[str]; mood_imitation_dialogs: list[str]
tools: list[str] | None; skills: list[str] | None
custom_error_message: str | None
_begin_dialogs_processed: list[dict]; _mood_imitation_dialogs_processed: str
```

⚠️ 我们**不实现** `_begin_dialogs_processed` / `_mood_imitation_dialogs_processed`
—— 那是 AstrBot 自己的**缓存字段**（名字带下划线前缀），由它的
`get_v3_persona_data()` 现算。我们照抄它们只会让"谁是真值"变模糊。

## 关键取舍

1. **`persona_id` == `name`**（AstrBot 就是这么用的：`personas_v3` 里
   `persona["name"] == persona_id`，见 `persona_mgr.py:56`）。
   内部仍保留 `id`（uuid）当存储主键 —— 因为它**允许改名**而不丢引用
   （AstrBot 那边改 `persona_id` 等于换了一个人格）。
2. **`system_prompt` 是唯一的人设正文**，不再有 6 字段拆分。
3. **兼容读取**：`from_dict()` 遇到旧数据（只有 6 字段 / `prompt_sections`）
   会自动**拼成 `system_prompt`** —— 旧数据一行都不丢，只是结构变了。
4. `api_url` / `api_key` / `model` / `reference_image_path` / `avatar_path`
   保留（AstrBot 没有，但这是我们自己的扩展，与结构不冲突）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: AstrBot `Personality`（v3）的字段名 —— 供兼容层按需转换。
#: ⚠️ 只列**真值字段**，不含 `_` 开头的缓存字段（见模块 docstring）。
ASTRBOT_PERSONALITY_FIELDS = (
    "prompt",
    "name",
    "begin_dialogs",
    "mood_imitation_dialogs",
    "tools",
    "skills",
    "custom_error_message",
)


def _as_str_list(value: Any) -> list[str]:
    """把 AstrBot 的 `list | None` 归一成 `list[str]`（None → []）。

    ⚠️ `None` 在 AstrBot 里有**特殊含义**（`tools=None` = 用全部工具，
    `tools=[]` = 不用任何工具）。所以这个归一**只用于展示/落盘**，
    真正传给陪伴插件时要保留原始 `None`（见 `to_personality()`）。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    return []


@dataclass(slots=True)
class Persona:
    """一个人设 —— 字段与 AstrBot `Persona` 对齐。

    ⚠️ 情绪 2026-09-18：「就是一个人用，但是**角色卡可以有多张**」
    → **不做多用户**，但人设是一对多。
    """

    # ---- AstrBot `Persona` 的核心（照抄字段名与语义）----
    #: 人格名 = AstrBot 的 `persona_id`（两者同值，见模块 docstring）
    name: str = ""
    #: **完整系统提示词**（`.txt` 全文就放这里）。AstrBot 的 `system_prompt`。
    system_prompt: str = ""
    #: 开场对话（AstrBot `begin_dialogs`）
    begin_dialogs: list[str] = field(default_factory=list)
    #: 工具白名单：`None` = 用全部，`[]` = 不用任何（**语义不同，别归一**）
    tools: list[str] | None = None
    #: Skills 白名单：同上
    skills: list[str] | None = None
    #: 出错时的自定义回复
    custom_error_message: str | None = None
    #: 所属文件夹（AstrBot `folder_id`）
    folder_id: str | None = None
    #: 排序（AstrBot `sort_order`）
    sort_order: int = 0

    # ---- 存储主键与元数据（AstrBot 用自增 id，我们用 uuid 以便改名）----
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- 我们的扩展（AstrBot 没有，但与本结构不冲突）----
    avatar_path: str = ""
    #: per-persona 上游覆盖
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    #: 生图固定参考图（纯文件名，落在 `data/persona_refs/`）
    reference_image_path: str = ""

    # ------------------------------------------------------------------

    def validate(self) -> None:
        """校验。**名称必填，正文可以为空**（空正文 = 纯默认人格，合法）。"""
        if not self.name or not self.name.strip():
            raise ValueError("Persona.name 不能为空")
        if len(self.name) > 200:
            raise ValueError("Persona.name 过长（>200）")
        if self.sort_order < 0:
            raise ValueError("Persona.sort_order 不能为负")
        for i, dialog in enumerate(self.begin_dialogs):
            if not isinstance(dialog, str):
                raise ValueError(f"begin_dialogs[{i}] 必须是字符串")

    # ---- 序列化 ----

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """转 dict（**字段名与 AstrBot 一致**，另加我们自己的几个）。

        Args:
            include_secrets: 是否包含 `api_key`。**默认 False**
                —— 出口（API/日志）一律脱敏，只有存储时才用 True。
        """
        d: dict[str, Any] = {
            # AstrBot Persona 字段
            "persona_id": self.name,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "begin_dialogs": list(self.begin_dialogs),
            "tools": None if self.tools is None else list(self.tools),
            "skills": None if self.skills is None else list(self.skills),
            "custom_error_message": self.custom_error_message,
            "folder_id": self.folder_id,
            "sort_order": self.sort_order,
            # 我们的存储/元数据
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "avatar_path": self.avatar_path,
            "api_url": self.api_url,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "reference_image_path": self.reference_image_path,
            "has_reference_image": bool(self.reference_image_path),
        }
        if include_secrets:
            d["api_key"] = self.api_key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Persona:
        """从 dict 还原。**兼容旧结构**（爱语 6 字段 / `prompt_sections`）。

        🔴 为什么必须兼容：旧数据已经在 `emotion.db` 里了（莉莉丝、糯米茨）。
           直接不认旧字段 = **用户的人设凭空消失**，那是不可接受的数据事故。

        旧结构 → 新结构的映射（按信息量从大到小，拼接顺序固定）：
          `prompt_sections[*].content`（按 order/title 稳定排序）
          + `description` + `personality` + `scenario` + `creator_notes`
        """
        prompt = d.get("system_prompt")

        if not isinstance(prompt, str) or not prompt.strip():
            prompt = cls._migrate_legacy_prompt(d)

        name = str(d.get("name") or d.get("persona_id") or "")
        if not name.strip():
            raise ValueError("Persona.name 不能为空（旧数据也没有 name）")

        return cls(
            name=name,
            system_prompt=prompt,
            begin_dialogs=_as_str_list(d.get("begin_dialogs")),
            # ⚠️ tools/skills **保留原始 None**（None≠[]，见模块 docstring）
            tools=None if d.get("tools") is None else _as_str_list(d.get("tools")),
            skills=None if d.get("skills") is None else _as_str_list(d.get("skills")),
            custom_error_message=d.get("custom_error_message"),
            folder_id=d.get("folder_id"),
            sort_order=int(d.get("sort_order") or 0),
            id=d.get("id") or uuid.uuid4().hex,
            created_at=float(d.get("created_at") or time.time()),
            updated_at=float(d.get("updated_at") or time.time()),
            avatar_path=d.get("avatar_path", ""),
            api_url=d.get("api_url", ""),
            api_key=d.get("api_key", ""),
            model=d.get("model", ""),
            reference_image_path=d.get("reference_image_path", ""),
        )

    @staticmethod
    def _migrate_legacy_prompt(d: dict[str, Any]) -> str:
        """把旧结构（6 字段 / prompt_sections）拼成 `system_prompt`。

        ⚠️ **顺序稳定**是刻意的：同 order 按 title 排，保证每次跑出来一字不差
           （否则上游 prompt 缓存会一直失效）。
        """
        parts: list[str] = []

        sections = d.get("prompt_sections")
        if isinstance(sections, list):
            usable = []
            for s in sections:
                if not isinstance(s, dict):
                    continue
                if s.get("enabled") is False:
                    continue
                content = s.get("content")
                if isinstance(content, str) and content.strip():
                    usable.append(
                        (int(s.get("order", 100) or 0), str(s.get("title") or ""), content)
                    )
            for _order, _title, content in sorted(usable, key=lambda x: (x[0], x[1])):
                parts.append(content)

        # 6 字段里剩下的部分（prompt_sections 里通常已有副本，这里去重）
        joined = "\n".join(parts)
        for key in ("description", "personality", "scenario", "creator_notes"):
            value = d.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in joined:
                parts.append(value.strip())

        return "\n\n".join(parts)

    # ---- 与 AstrBot `Personality` 互转 ----

    def to_personality(self) -> dict[str, Any]:
        """转成 AstrBot v3 `Personality` 的形状（供兼容层喂给插件）。

        🔴 `prompt` 用 `system_prompt`；`tools`/`skills` 的 **None 语义原样保留**
           （`None`=全部，`[]`=一个都不给）。把它们归一成 `[]` 会让插件
           以为"这个角色不许用任何工具" —— 那是**功能性故障**，不只是显示问题。
        """
        return {
            "prompt": self.system_prompt,
            "name": self.name,
            "begin_dialogs": list(self.begin_dialogs),
            "mood_imitation_dialogs": [],
            "tools": None if self.tools is None else list(self.tools),
            "skills": None if self.skills is None else list(self.skills),
            "custom_error_message": self.custom_error_message,
        }

    @property
    def persona_id(self) -> str:
        """AstrBot 的 `persona_id`（= `name`）。"""
        return self.name
