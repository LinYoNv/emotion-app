# -*- coding: utf-8 -*-
"""提示词组装器。

⭐ 这是 S2 人设体系的**核心**（见 `docs/PERSONA_PROMPT_SYSTEM.md`）：
   实测爱语把人物设定 + 各类能力说明**组装成分节的 system prompt**（51 个 `【】` 小节），
   **人设不是字段集合，而是组装器**。

三条硬原则：
  P1 **片段化** —— 不是一坨大字符串（可按条件开关）
  P2 **来源可追溯** —— 出问题知道是谁加的
  P3 **稳定前缀** —— 易变内容放后面，提高上游 prompt 缓存命中
                        （爱语"记忆好"的真实机制，不是提示词玄学）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .message import ROLE_SYSTEM, Message
from .persona import Persona

#: 提示词片段的来源 —— 决定"这段是谁加的"，便于调试与治理。
SECTION_SOURCES = (
    "builtin",   # 系统内置（如时间、硬格式约束）
    "persona",   # 人设自带（来自 `Persona.system_prompt`）
    "user",      # 用户手写
    "memory",    # 记忆注入（S4）
    "module",    # 模块注入（S5 预留）
)


@dataclass(slots=True)
class PromptSection:
    """一段提示词。片段化是刻意设计 —— 不是一坨大字符串（P1）。

    🔴 **2026-10-29 从 `persona.py` 搬到这里**。

    为什么搬：`PromptSection` 是**组装层的内部结构**，不是人设的存储结构。
    `builtin_sections()` / `time_section()` / 记忆注入都在造它 ——
    它们跟"人设长什么样"完全无关。

    之前它放在 `persona.py` 里，导致重构人设模型（改成 AstrBot 的
    `system_prompt` 单段结构）时差点把它一起删掉 —— 那会让整个组装器
    起不来（`ImportError: cannot import name 'PromptSection'`）。
    **两个概念分居两个模块，才不会一次改动牵连另一个。**
    """

    title: str                       # 小节名，如"当前时间"
    content: str = ""
    enabled: bool = True
    order: int = 100                 # 越小越靠前；易变内容应放后面（P3）
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


#: 分节分隔符 —— 与爱语一致（实测用 `【】`）
SECTION_OPEN = "【"
SECTION_CLOSE = "】"

#: 内置片段的 order 区间（给外部片段留出插入空间）
ORDER_BUILTIN_HEAD = 10     # 最前：硬约束
ORDER_PERSONA_CORE = 20     # 人设核心（稳定，尽量少变）
ORDER_CONTEXT = 900         # 上下文/记忆（易变，放后面）


@dataclass(slots=True)
class AssembledPrompt:
    """组装结果。

    刻意返回**结构**而非一个裸字符串 —— 便于调试"这句话是谁加的"。
    """

    sections: list[PromptSection] = field(default_factory=list)

    @property
    def text(self) -> str:
        """拼成最终 system 文本。"""
        parts: list[str] = []
        for s in self.sections:
            body = (s.content or "").strip()
            if not body:
                continue
            parts.append(f"{SECTION_OPEN}{s.title}{SECTION_CLOSE}\n{body}")
        return "\n\n".join(parts)

    def titles(self) -> list[str]:
        return [s.title for s in self.sections]

    def by_source(self, source: str) -> list[PromptSection]:
        return [s for s in self.sections if s.source == source]


# ---------------------------------------------------------------------------
# 内置片段
# ---------------------------------------------------------------------------


#: 时间片段的 order —— **刻意排在最后**（见下方说明）
ORDER_TIME = 990


def builtin_sections() -> list[PromptSection]:
    """系统内置片段。

    ⚠️ 刻意**极少**（当前返回空列表）。理由（照 `AIYU_MEMORY_REVIEW.md` §5 的评审）：
      爱语的 51 个小节里有大量硬编码（"严重健忘症模式"、"每条消息必须写记忆"、
      "禁止说不知道"、固定轮数…），那些**会造成误记和协议泄漏，不该抄**。
    """
    return []


def time_section(now: float | None = None) -> PromptSection:
    """当前时间片段。

    🔴 **重要修正（实测踩到）**：
      时间片段最初被放在 `ORDER_BUILTIN_HEAD`（最前），这**直接违反了 P3 稳定前缀原则**
      —— 它每秒都在变，会把整个 system 前缀打乱，**上游 prompt 缓存永远命中不了**，
      正好毁掉爱语"记忆好"的那个机制。

    → 正确做法：**时间放最后**（order=990），让稳定的人设部分待在前面。
       这与 `AIYU_MEMORY_REVIEW.md` 的结论一致：
       「稳定前缀改善连续性……缓存本身不产生记忆，但它减少了上下文结构抖动」。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now) if now else time.localtime())
    return PromptSection(
        title="当前时间",
        content=f"现在的时间是 {stamp}。",
        order=ORDER_TIME,
        source="builtin",
    )


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def persona_core_sections(persona: Persona) -> list[PromptSection]:
    """从人设生成核心片段 —— **现在是 AstrBot 的单段 `system_prompt`**。

    🔴 2026-10-29 重构（情绪拍板「改成 astrbot 那样」）：

    原来这里从爱语 6 字段（description/personality/scenario/creator_notes）
    各生成一段。问题有两个，都很实在：

      ① **面板上看不全** —— 界面只画那 4 个框，人设正文若放在
         `prompt_sections`（旧结构），四个框就是**空白**。
         实测：莉莉丝 8621 字符全文躺在 `prompt_sections`，面板全空。
      ② **陪伴插件不认** —— 它读 AstrBot 的 `Personality`（`prompt` 字段），
         根本不看 `description` 那一族。

    AstrBot 的人格就是 **`persona_id` + 一整段 `system_prompt`**
    （`astrbot/core/db/po.py:145`），没有拆分。所以这里也收敛成一段。

    ⭐ 仍然保持 P3（稳定前缀）：有正文就**恰好一段**，`order` 固定，
       不会因为字段多少而抖动。
    """
    content = (persona.system_prompt or "").strip()
    if not content:
        # 空正文是合法的（= 纯默认人格），此时不产生片段，
        # 让 `DEFAULT_PERSONA_PROMPT` 那条兜底生效（见 persona_manager）。
        return []
    return [
        PromptSection(
            title=PERSONA_CORE_TITLE,
            content=content,
            order=ORDER_PERSONA_CORE,
            source="persona",
        )
    ]


def assemble(
    persona: Persona | None,
    *,
    history: list[Message] | None = None,
    include_time: bool = True,
    extra_sections: list[PromptSection] | None = None,
) -> AssembledPrompt:
    """组装 system prompt。

    Args:
        persona: 人设；None 表示无（用极简默认）。
        history: 历史消息（S3 起用于上下文，本阶段只取长度做占位）。
        include_time: 是否包含当前时间片段。
        extra_sections: 额外片段（记忆/模块注入用；S4/S5 接入）。

    Returns:
        AssembledPrompt（结构化，可查"哪段是谁加的"）。

    🔴🔴 **为什么不能简单地把"6 字段片段"和"自带片段"都 extend 进去**
    （2026-09-21 实测抓到的真 bug，**代价是提示词整整翻倍**）：

        这两者是**同一个东西的两个视图**——
          * `persona_core_sections()`：从 **6 字段**（description/personality/
            scenario/creator_notes）现算 4 段，标题固定为
            角色设定 / 性格 / 场景 / 创作者备注；
          * `persona.prompt_sections`：`persona_import.py:235-252` 在**导入时**
            把**同样这 4 个字段**物化成 4 段（`source="persona"`），
            好让面板的"提示词片段"表能编辑它们。

        两个都 extend → **每段注入两次**。实测（糯米茨，真 `/preview`）：

            组装后总字数 27608（**正确值是 ~13804**）
            【角色设定】出现 2 次 · 【性格】2 次 · 【场景】2 次 · 【创作者备注】2 次
            同一个 7670 字的正文在偏移 0 与偏移 7679 各出现一次

        → 后果不只是"浪费 token"：**重复的长 system 提示词会稀释指令
          （同一个约束说两遍，模型更容易只遵守其中一份的措辞）**，
          而且上游 prompt 缓存的稳定前缀也被撑大一倍。

    ✅ **去重规则**（既消重、又不吃掉用户的编辑意图）：

        ① 标题相同的 `source="persona"` 片段与 6 字段片段**视为同一个**；
        ② **用户在面板上停用**了某段（`enabled=false`）→ **6 字段那份也要跟着去掉**
           （否则"停用"形同虚设 —— 内容从字段那条路又进来了）；
        ③ 优先用**存储的那份原文**（`persona_import` 刻意存**未 strip** 的原文，
           照抄字段会丢掉尾部空白 —— 对长提示词是信息损失）；
        ④ 标题**不在** 6 字段集合里的片段（用户手写的、来源 memory/module 的）
           → 照旧全部加进去（那些是真正的"额外片段"）。
    """
    sections: list[PromptSection] = []

    # 1) 内置（当前为空 —— 见 builtin_sections 的说明）
    sections.extend(builtin_sections())

    # 2) 人设核心（6 字段）+ 自带片段 —— **去重后**合并（见 docstring）
    if persona is not None:
        sections.extend(merge_persona_sections(persona))

    # 3.5) 时间 —— ⚠️ 易变，**放最后**（P3：稳定前缀）
    if include_time:
        sections.append(time_section())

    # 4) 外部注入（记忆/模块）—— 放后面（易变内容，P3）
    if extra_sections:
        sections.extend(extra_sections)

    # 稳定排序：order → title（确定性，避免顺序抖动破坏缓存）
    sections.sort(key=lambda s: (s.order, s.title))

    return AssembledPrompt(sections=[s for s in sections if s.enabled])


#: 人设正文片段的固定标题（2026-10-29 起只有一段，见 `persona_core_sections`）。
PERSONA_CORE_TITLE: str = "人设"

#: 兼容期保留：旧数据里可能有这 4 个标题的片段（爱语 6 字段派生）。
#: ⚠️ 它们仍算"人设核心"，这样 `from_dict` 迁移时不会把它们当成
#:    "用户手写的额外片段"而重复注入。
PERSONA_CORE_TITLES: frozenset[str] = frozenset(
    {"人设", "角色设定", "性格", "场景", "创作者备注"}
)


def merge_persona_sections(persona: Persona) -> list[PromptSection]:
    """人设正文 → 片段列表。

    🔴 **2026-10-29 大幅简化**（人设结构改成 AstrBot 的 `system_prompt` 单段后）：

    原来这里要做四件事：① 6 字段各生成一段；② 尊重"用户在面板停用某段"；
    ③ 优先用存储的原文（避免 strip 丢尾部空白）；④ 把非核心片段当"额外片段"加进来。

    新结构下 **②③④ 全部消失**，因为：
      * 人设只有**一段正文**（`system_prompt`），没有"多段可停用"的概念；
      * 没有第二份副本，也就不存在"用哪份原文"的选择；
      * 没有 `prompt_sections` 字段，也就没有"额外片段"。

    ⚠️ 函数**保留**（不内联）是为了让判据能直接测"到底注入了哪些片段"，
       而不用去解析 `assemble()` 排序后的整体输出。
    """
    return persona_core_sections(persona)


def to_system_message(prompt: AssembledPrompt, *, inject_markers: bool = True) -> Message:
    """把组装结果包成一条 system 消息。"""
    msg = Message(role=ROLE_SYSTEM, content=prompt.text)
    msg.validate()
    return msg


def describe(prompt: AssembledPrompt) -> dict[str, Any]:
    """给日志/API 看的**结构描述**（不含正文，避免日志过长与泄漏）。"""
    return {
        "section_count": len(prompt.sections),
        "titles": prompt.titles(),
        "by_source": {
            src: len(prompt.by_source(src))
            for src in ("builtin", "persona", "user", "memory", "module")
        },
        "total_chars": len(prompt.text),
    }
