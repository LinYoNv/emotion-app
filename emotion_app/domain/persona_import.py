# -*- coding: utf-8 -*-
"""人设导入：解析角色卡 / 备份文件。

⭐⭐ **格式依据是爱语真 dex 实测**（不是推测）→ 见
   `爱语/docs/PERSONA_IMPORT_FORMAT.md`（依据：`ty.java` / `g.java` / `j1.java`）。

🔴 **重要修正**：爱语**不用 SillyTavern 角色卡格式**。
   真 dex 里 `chara_card`/`spec_version`/`first_mes`/`mes_example` **全是 0 次**，
   它用的是**自己的备份格式**：

       {"data": {"prompts": {"<id>": {"data": {6 个字段}}}}}

   ⭐ 且**一个文件可含多个人设**（`ty.h()` 遍历全部加载，
      Toast 文案是「共加载 N 个人设」）。

支持四种来源（`parse_any` 自动识别）：

| 格式 | 识别方式 | 优先级 |
|---|---|---|
| **爱语备份** | 顶层 `data.prompts` | ⭐ **主路径** |
| 本程序导出 | 顶层 `spec=chara_card_v2` + `extensions.emotion_app` | 往返无损 |
| SillyTavern V2/V1 | 顶层 `spec=chara_card_v2` 或扁平 6 字段 | 兼容（6 字段重合，顺手支持） |
| PNG 内嵌卡 | PNG 魔数 + `tEXt` 块（`chara`/`ccv3`） | 兼容（爱语**没有**这功能） |

⚠️ 纯标准库实现（`struct`/`zlib`/`base64`/`json`）——
   本项目依赖刻意只有 quart/hypercorn/aiosqlite/httpx，
   **不为读个 PNG 块引入 Pillow**。
"""
from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
import time
from dataclasses import dataclass, field
from typing import Any

from .persona import Persona, PromptSection

#: PNG 魔数
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: 单文件上限。爱语备份可含多个人设，比单张卡大，给到 32 MB。
MAX_CARD_BYTES = 32 * 1024 * 1024

#: 单个备份文件最多接受的人设数 —— 防一个畸形文件撑爆内存
MAX_PERSONAS_PER_FILE = 500

#: 卡数据在 PNG 里的块关键字（`chara` 通用；`ccv3` 是 V3）
CARD_KEYWORDS = ("chara", "ccv3")

#: 爱语人设的 6 个字段（照 `PersonaInfo` 的 `@SerializedName`）
AIYU_FIELDS = (
    "name", "description", "personality", "scenario", "creator_notes", "avatar_path",
)


class CardImportError(ValueError):
    """无法解析 —— 调用方转 400 并把原因说清楚（**绝不静默导入 0 个**）。"""


@dataclass(slots=True)
class ParsedCard:
    """解析结果（**可能是多个人设** —— 爱语备份就是批量的）。"""

    personas: list[Persona] = field(default_factory=list)
    #: 从 PNG 里提取到的头像（PNG 字节）。只对 PNG 来源有效，且只对应第一个人设。
    avatar_bytes: bytes | None = None
    #: `aiyu_backup` / `emotion_app` / `sillytavern_v2` / `sillytavern_v1` / `png`
    source_format: str = ""
    #: 解析过程中的提示（给用户看）
    notes: list[str] = field(default_factory=list)
    #: 备份里出现、但我们模型没有的字段名（**如实告知，不静默丢弃**）
    extra_fields: list[str] = field(default_factory=list)

    @property
    def persona(self) -> Persona:
        """便捷取第一个（单卡场景）。"""
        if not self.personas:
            raise CardImportError("没有解析出任何人设")
        return self.personas[0]


# ---------------------------------------------------------------------------
# PNG 文本块（兼容路径）
# ---------------------------------------------------------------------------


def _iter_png_text_chunks(data: bytes) -> list[tuple[str, str]]:
    """遍历 PNG 文本块，产出 `(keyword, text)`（只读 `tEXt`/`iTXt`）。

    Raises:
        CardImportError: 文件**结构损坏**（连一个合法块都没有）。
            ⚠️ 区分两种情况很重要（实测踩到）：
              * "是合法 PNG，但没有卡数据块" → 提示改用 JSON 导入
              * "根本不是合法 PNG（结构损坏）" → 提示文件坏了
            两者混为一谈会让用户拿着坏文件去改格式，白折腾。
    """
    if not data.startswith(PNG_SIGNATURE):
        raise CardImportError("不是合法的 PNG（魔数不匹配）")

    out: list[tuple[str, str]] = []
    pos = len(PNG_SIGNATURE)
    total = len(data)
    valid_chunks = 0

    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body_start = pos + 8
        body_end = body_start + length

        if body_end + 4 > total:
            # 截断的 PNG：前面的块仍可用（有些工具产出的尾部不完整）
            break

        # 块类型必须是 4 个 ASCII 字母，否则结构已经错位
        if not ctype.isalpha():
            break

        valid_chunks += 1
        body = data[body_start:body_end]

        if ctype == b"tEXt":
            if b"\x00" in body:
                kw, _, txt = body.partition(b"\x00")
                out.append((kw.decode("latin-1"), txt.decode("latin-1")))
        elif ctype == b"iTXt":
            try:
                kw, rest = body.split(b"\x00", 1)
                comp_flag = rest[0]
                rest = rest[2:]                      # 跳过 comp_flag + comp_method
                _lang, rest = rest.split(b"\x00", 1)
                _trans, txt = rest.split(b"\x00", 1)
                if comp_flag == 1:
                    txt = zlib.decompress(txt)
                out.append((kw.decode("latin-1"), txt.decode("utf-8", "replace")))
            except (IndexError, ValueError, zlib.error):
                continue                             # 单块坏了不影响其它块

        if ctype == b"IEND":
            break
        pos = body_end + 4        # 跳过 CRC

    if valid_chunks == 0:
        raise CardImportError(
            "PNG 结构损坏（没有任何合法数据块）。文件可能没下载完整。"
        )

    return out


def _extract_json_from_png(data: bytes) -> dict[str, Any]:
    """从 PNG 里取出卡 JSON（图片本体直接用整个文件）。"""
    for keyword, text in _iter_png_text_chunks(data):
        if keyword not in CARD_KEYWORDS:
            continue
        try:
            raw = base64.b64decode(text, validate=False)
            found = json.loads(raw.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise CardImportError(
                f"PNG 里有 `{keyword}` 数据块，但 base64/JSON 解析失败：{exc}"
            ) from exc
        if not isinstance(found, dict):
            raise CardImportError("PNG 里的角色卡数据不是 JSON 对象")
        return found

    raise CardImportError(
        "这个 PNG 里没有角色卡数据块"
        f"（需要 `{'` 或 `'.join(CARD_KEYWORDS)}` 的 tEXt 块）。"
        "如果它只是普通图片，请改用 JSON 导入。"
    )


# ---------------------------------------------------------------------------
# 字段工具
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """把值转成文本。

    ⚠️ `alternate_greetings` 是数组、有些字段是数字/布尔 —— 统一成**可读文本**，
    而不是丢掉，或让 `str()` 产出 `['a', 'b']` 这种带引号的怪样子。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            f"{i}. {item}" if isinstance(item, str)
            else f"{i}. {json.dumps(item, ensure_ascii=False)}"
            for i, item in enumerate(value, 1)
        )
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _persona_from_aiyu(d: dict[str, Any]) -> Persona:
    """爱语 `PersonaInfo` → 我们的 `Persona`（**6 字段逐一对应**）。

    ⭐ **把 6 字段物化成可编辑的提示词片段**（2026-09-19 修）。

    为什么必须这么做（实测踩到）：爱语备份**只有 6 个字段，不含片段结构**。
    如果只填字段、`prompt_sections` 留空，面板的人设页会显示**空片段表** ——
    用户在界面上看不到、也改不了这些人设内容，而这恰恰是本项目
    「人设 = 提示词组装器」的核心（见 `PERSONA_PROMPT_SYSTEM.md`）。
    （`/preview` 仍能显示内容，因为组装器会从字段现算 —— 所以这个 bug
      **只有真的打开面板点进人设才会发现**，接口测试全绿也照样漏。）

    `order` 取值照 `PERSONA_PROMPT_SYSTEM.md` 的稳定前缀原则：
    描述 → 性格 → 场景 → 备注（稳定内容在前，时间/记忆等易变内容由内置片段排到后面）。
    """
    name = _as_text(d.get("name")).strip()
    if not name:
        raise CardImportError("备份里有条目没有 `name`（名字是必填的）")

    persona = Persona(
        name=name,
        description=_as_text(d.get("description")),
        personality=_as_text(d.get("personality")),
        scenario=_as_text(d.get("scenario")),
        creator_notes=_as_text(d.get("creator_notes")),
        # ⚠️ 爱语的 avatar_path 是**手机上的路径**，搬到后端后无意义
        #    → 只记名字，不记路径（路径会指向不存在的文件）
        avatar_path="",
    )

    #: (字段, 片段标题, order) —— 顺序即注入顺序
    #:
    #: 🔴 **标题必须与 `prompt.persona_core_sections()` 逐字一致**：那边也按
    #:    这 4 个标题从 6 字段现算片段，`prompt.merge_persona_sections()` 靠
    #:    **标题相等**判断"这两份是同一个东西"→ 去重。
    #:    ⚠️ 标题一旦漂移，去重会**静默失效**（每段又注入两次，界面看不出异常），
    #:    所以这里用 `PERSONA_CORE_TITLES` 做**一致性断言**（见下方 assert）。
    field_sections = (
        ("description", "角色设定", 20),
        ("personality", "性格", 21),
        ("scenario", "场景", 22),
        ("creator_notes", "创作者备注", 23),
    )
    from .prompt import PERSONA_CORE_TITLES as _CORE_TITLES

    assert {t for _, t, _ in field_sections} == set(_CORE_TITLES), (
        "persona_import 的片段标题与 prompt.PERSONA_CORE_TITLES 不一致 —— "
        "去重会失效，人设提示词会被注入两次"
    )
    sections: list[PromptSection] = []
    for attr, title, order in field_sections:
        content = getattr(persona, attr)
        # ⚠️ 只在**判断空**时 strip，存进去的是**原文**。
        #    实测踩到：用 `content.strip()` 存会丢掉尾部换行，
        #    导致"片段内容"与原字段不再逐字相等 —— 用户编辑片段保存后
        #    会**静默改掉原文**（对他的长提示词来说这是信息损失）。
        if content.strip():
            sections.append(
                PromptSection(title=title, content=content, order=order, source="persona")
            )
    persona.prompt_sections = sections

    return persona


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------


def _parse_aiyu_backup(card: dict[str, Any]) -> ParsedCard:
    """解析爱语备份格式：`{"data": {"prompts": {"<id>": {"data": {...}}}}}`。

    照 `ty.h()`（`ty.java:117-148`）的逐层校验：
      * `data == null` → 失败
      * `prompts == null` → 失败
      * 条目 `data == null` → **跳过**（不是失败）—— 与原实现一致
    """
    data = card.get("data")
    if not isinstance(data, dict):
        raise CardImportError("备份缺少 `data` 对象（不是爱语格式？）")
    prompts = data.get("prompts")
    if not isinstance(prompts, dict):
        raise CardImportError("备份缺少 `data.prompts` 对象（不是爱语格式？）")

    personas: list[Persona] = []
    notes: list[str] = []
    skipped = 0
    extra: set[str] = set()

    for pid, wrapper in prompts.items():
        inner = wrapper.get("data") if isinstance(wrapper, dict) else None
        if not isinstance(inner, dict):
            skipped += 1                      # 与 ty.h() 一致：跳过而非失败
            continue
        try:
            p = _persona_from_aiyu(inner)
        except CardImportError as exc:
            notes.append(f"条目 `{pid}` 已跳过：{exc}")
            skipped += 1
            continue
        extra.update(k for k in inner if k not in AIYU_FIELDS)
        personas.append(p)

    if not personas:
        detail = f"（{skipped} 个条目无法解析）" if skipped else ""
        raise CardImportError(f"备份里没有可用的人设{detail}")
    if len(personas) > MAX_PERSONAS_PER_FILE:
        raise CardImportError(
            f"备份含 {len(personas)} 个人设，超过上限 {MAX_PERSONAS_PER_FILE}"
        )
    if skipped:
        notes.append(f"{skipped} 个条目缺少 `data`，已跳过（与爱语行为一致）")

    return ParsedCard(
        personas=personas, source_format="aiyu_backup",
        notes=notes, extra_fields=sorted(extra),
    )


#: V2 `data` 里我们没有对应字段、但值得保留的键 → 转成提示词片段
_V2_SECTION_MAP: tuple[tuple[str, str, int], ...] = (
    ("system_prompt", "角色卡系统提示词", 60),
    ("mes_example", "对话示例", 80),
    ("post_history_instructions", "历史后指令", 950),
)

#: 问候语不是 system prompt（是"第一条消息"）→ **默认禁用**存进片段
_GREETING_KEYS: tuple[tuple[str, str], ...] = (
    ("first_mes", "开场白（默认关闭）"),
    ("alternate_greetings", "备选开场白（默认关闭）"),
)


def _parse_sillytavern(card: dict[str, Any], fmt: str) -> ParsedCard:
    """解析 SillyTavern V2/V1（**兼容路径** —— 6 字段与爱语重合，顺手支持）。"""
    data = card
    spec = card.get("spec")
    if isinstance(spec, str) and spec.startswith("chara_card"):
        inner = card.get("data")
        if not isinstance(inner, dict):
            raise CardImportError("V2 角色卡缺少 `data` 对象")
        data = inner
        fmt = "sillytavern_v2"

    name = _as_text(data.get("name")).strip()
    if not name:
        raise CardImportError("角色卡没有 `name`（名字是必填的）")

    notes: list[str] = []
    persona = Persona(
        name=name,
        description=_as_text(data.get("description")),
        personality=_as_text(data.get("personality")),
        scenario=_as_text(data.get("scenario")),
        creator_notes=_as_text(data.get("creator_notes") or data.get("creatorcomment")),
    )

    sections: list[PromptSection] = []

    # ⭐ 优先用**我们自己导出**的完整结构（保留 order/enabled → 无损往返）
    ext = data.get("extensions")
    own = ext.get("emotion_app") if isinstance(ext, dict) else None
    own_sections = own.get("prompt_sections") if isinstance(own, dict) else None

    if isinstance(own_sections, list) and own_sections:
        for raw in own_sections:
            if not isinstance(raw, dict):
                continue
            try:
                sections.append(PromptSection.from_dict(raw))
            except (KeyError, ValueError) as exc:
                notes.append(f"片段结构异常已跳过：{exc}")
        notes.append(f"识别到本程序导出的完整片段结构（{len(sections)} 段，无损导入）")
        fmt = "emotion_app"
        if isinstance(own.get("model"), str) and not persona.model:
            persona.model = own["model"]
        if isinstance(own.get("api_url"), str) and not persona.api_url:
            persona.api_url = own["api_url"]
    else:
        for key, title, order in _V2_SECTION_MAP:
            content = _as_text(data.get(key)).strip()
            if content:
                sections.append(
                    PromptSection(title=title, content=content, order=order, source="persona")
                )
        for key, title in _GREETING_KEYS:
            content = _as_text(data.get(key)).strip()
            if content:
                sections.append(
                    PromptSection(title=title, content=content, order=900,
                                  source="persona", enabled=False)
                )
                notes.append(f"「{title}」已导入但**默认关闭**（它不是系统提示词）")

    persona.prompt_sections = sections

    known = {
        "name", "description", "personality", "scenario", "creator_notes",
        "creatorcomment", "system_prompt", "mes_example",
        "post_history_instructions", "first_mes", "alternate_greetings",
        "avatar", "spec", "spec_version", "data",
        # ⚠️ `extensions` 留在 known 里：我们自己的导出带它，
        #    否则"导出→导入"会误报"有 1 个字段未导入"
        "tags", "creator", "character_version", "extensions",
    }
    extra = sorted(k for k in data if k not in known)
    if extra:
        notes.append(f"卡里另有 {len(extra)} 个字段未导入：" + "、".join(extra))
    if not persona.description and not persona.personality and not sections:
        notes.append("⚠️ 这张卡几乎是空的（没有描述/性格/提示词）")

    return ParsedCard(personas=[persona], source_format=fmt,
                      notes=notes, extra_fields=extra)


def _parse_json_card(card: dict[str, Any]) -> ParsedCard:
    """自动识别 JSON 的格式（爱语备份 → 本程序导出 → SillyTavern）。"""
    # ① 爱语备份（主路径）：有 data.prompts
    data = card.get("data")
    if isinstance(data, dict) and isinstance(data.get("prompts"), dict):
        return _parse_aiyu_backup(card)

    # ② spec 标识（我们的导出 / SillyTavern V2）
    spec = card.get("spec")
    if isinstance(spec, str) and spec.startswith("chara_card"):
        return _parse_sillytavern(card, "sillytavern_v2")

    # ③ 扁平 6 字段（SillyTavern V1，或手写的简单人设）
    if "name" in card:
        return _parse_sillytavern(card, "sillytavern_v1")

    raise CardImportError(
        "认不出这个 JSON 的格式。支持："
        "① 爱语备份（`data.prompts`）"
        "② 本程序导出的角色卡（`spec: chara_card_v2`）"
        "③ SillyTavern 角色卡（含 `name` 字段）"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_card(data: bytes, *, filename: str = "") -> ParsedCard:
    """解析文件字节流。**自动识别** PNG / 爱语备份 / 角色卡 JSON。

    Raises:
        CardImportError: 报错信息要说清"原因 + 该怎么办"。
    """
    if not data:
        raise CardImportError("文件是空的")
    if len(data) > MAX_CARD_BYTES:
        raise CardImportError(
            f"文件过大（{len(data) // 1024 // 1024} MB > "
            f"{MAX_CARD_BYTES // 1024 // 1024} MB 上限）"
        )

    label = f"「{filename}」" if filename else "该文件"

    # PNG（兼容路径）
    if data.startswith(PNG_SIGNATURE):
        card = _extract_json_from_png(data)
        parsed = _parse_json_card(card)
        parsed.avatar_bytes = data
        parsed.source_format = "png"
        return parsed

    text = data.decode("utf-8", "replace").strip()
    if not text:
        raise CardImportError(f"{label} 解码后是空的（可能不是 UTF-8 文本）")

    # 有些备份文件带 UTF-8 BOM
    if text.startswith("\ufeff"):
        text = text[1:]

    try:
        card = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardImportError(
            f"{label} 既不是 PNG 也不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"
        ) from exc

    if not isinstance(card, dict):
        raise CardImportError(f"{label} 的 JSON 顶层不是对象")

    return _parse_json_card(card)


def parse_pasted_text(text: str) -> ParsedCard:
    """解析面板里**粘贴**的内容（额外支持 `data:image/png;base64,...`）。"""
    if not text or not text.strip():
        raise CardImportError("粘贴的内容是空的")

    stripped = text.strip()

    if stripped.startswith("data:"):
        header, _, payload = stripped.partition(",")
        if not payload:
            raise CardImportError("数据 URL 格式不对（逗号后没有内容）")
        if "base64" not in header:
            raise CardImportError("只支持 base64 编码的数据 URL")
        try:
            raw = base64.b64decode(payload, validate=False)
        except binascii.Error as exc:
            raise CardImportError(f"数据 URL 的 base64 解码失败：{exc}") from exc
        return parse_card(raw, filename="粘贴的数据 URL")

    return parse_card(stripped.encode("utf-8"), filename="粘贴的内容")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def to_aiyu_backup(personas: list[Persona]) -> dict[str, Any]:
    """导出成**爱语备份格式**（`ty.k()` 的结构）。

    ⭐ 为什么用爱语格式而不是自己的：情绪的目标是「复刻爱语并自用」，
       用**它能读的格式**导出，双向都通。

    ⚠️ 不含 `api_key`（本地凭据不该跟着备份到处跑）。
    """
    prompts: dict[str, Any] = {}
    for p in personas:
        key = p.name or p.id
        # ID 冲突时加后缀 —— 照 ty.c() 的"重命名"策略
        if key in prompts:
            i = 1
            while f"{key}_{i}" in prompts:
                i += 1
            key = f"{key}_{i}"
        prompts[key] = {
            "data": {
                "name": p.name,
                "description": p.description,
                "personality": p.personality,
                "scenario": p.scenario,
                "creator_notes": p.creator_notes,
                # 头像在备份里存路径；我们的头像存后端，导出时留空
                # （导出的 JSON 不会带图，避免文件巨大）
                "avatar_path": None,
            }
        }
    return {"data": {"prompts": prompts}}


def to_export_dict(persona: Persona) -> dict[str, Any]:
    """导出成 **SillyTavern V2**（能再被本程序与其它前端导入）。

    ⚠️ 与 `to_aiyu_backup` 的分工：
       * 发给爱语/备份 → 用 `to_aiyu_backup`
       * 与角色卡生态互通 → 用这个（带 `extensions.emotion_app` 保证无损往返）
    """
    sections = {s.title: s.content for s in persona.prompt_sections}
    data: dict[str, Any] = {
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "creator_notes": persona.creator_notes,
        "system_prompt": sections.get("角色卡系统提示词", ""),
        "post_history_instructions": sections.get("历史后指令", ""),
        "mes_example": sections.get("对话示例", ""),
        "first_mes": sections.get("开场白（默认关闭）", ""),
        "alternate_greetings": [],
        "tags": ["情绪app"],
        "creator": "情绪app",
        "character_version": "1.0",
        "extensions": {
            # 保留片段完整结构（order/enabled/source/condition）→ 往返无损
            "emotion_app": {
                "prompt_sections": [s.to_dict() for s in persona.prompt_sections],
                "model": persona.model,
                "api_url": persona.api_url,
            }
        },
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def merge_import_sections(
    parsed: ParsedCard, override: dict[str, Any] | None = None
) -> list[Persona]:
    """把导入结果与面板上的覆盖项合并（**批量**）。

    ⚠️ 只允许覆盖少数字段 —— 不开放 `api_key` 之类敏感项。
    """
    if not override:
        return parsed.personas

    allowed = ("name", "description", "personality", "scenario", "creator_notes", "model")
    for p in parsed.personas:
        for key in allowed:
            value = override.get(key)
            if isinstance(value, str) and value.strip():
                setattr(p, key, value)
    return parsed.personas


#: 冲突策略（照爱语 `ty.c()` 的 int 取值）
CONFLICT_OVERWRITE = "overwrite"   # 0 → 覆盖
CONFLICT_RENAME = "rename"         # 1 → 重命名（<id>_1、<id>_2…）
CONFLICT_SKIP = "skip"             # 2 的简化：跳过同名

CONFLICT_MODES = (CONFLICT_OVERWRITE, CONFLICT_RENAME, CONFLICT_SKIP)


def resolve_name_conflict(name: str, existing: set[str], mode: str) -> str | None:
    """按策略决定导入后的最终名字。

    Returns:
        最终名字；`None` 表示**跳过该条**（skip 策略且重名）。

    ⚠️ 照 `ty.c()`：重命名是 `<name>_1`、`<name>_2`…（不是加时间戳）。
       （`ty.h()` 加载磁盘备份时才用 `<id>_import_<毫秒>`，两处策略不同。）
    """
    if name not in existing:
        return name
    if mode == CONFLICT_OVERWRITE:
        return name
    if mode == CONFLICT_SKIP:
        return None
    i = 1
    while f"{name}_{i}" in existing:
        i += 1
    return f"{name}_{i}"


def export_filename(persona: Persona, *, ext: str = "json") -> str:
    """导出的文件名（照爱语 `persona_<名字>_<时间戳>.json`）。"""
    safe = "".join(c for c in persona.name if c.isalnum() or c in "-_") or "persona"
    return f"persona_{safe}_{int(time.time() * 1000)}.{ext}"
