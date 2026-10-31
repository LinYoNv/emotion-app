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

🔴 **2026-10-29 人设结构重构（情绪拍板「改成 astrbot 那样」）**：
   `Persona` 不再是「爱语 6 字段 + `prompt_sections` 片段」，而是
   **`persona_id` + 一整段 `system_prompt`**（对齐 AstrBot `core/db/po.py:145`）。
   本模块的改动只有一类：**来源字段不再塞进片段，而是拼成 `system_prompt`**。

   * 导入：`description → personality → scenario → creator_notes` 用 `\n\n`
     顺序拼接（**顺序固定**，理由见 `_join_prompt_parts`）。
   * 导出：`system_prompt` 写回角色卡的标准主字段（SillyTavern 用 `description`）。
   * `avatar_path` / `reference_image_path` 的逻辑**一个字没动**（它们与本次重构无关）。

   ⚠️ 下面出现的 `description` / `personality` / `scenario` / `creator_notes`
   一律是**外部格式的 dict key**（爱语备份、SillyTavern 卡的字段名），
   不是我们 `Persona` 的属性 —— 我们的模型里已经没有这些字段了。
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

from .persona import Persona

#: PNG 魔数
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: 单文件上限。爱语备份可含多个人设，比单张卡大，给到 32 MB。
MAX_CARD_BYTES = 32 * 1024 * 1024

#: 单个备份文件最多接受的人设数 —— 防一个畸形文件撑爆内存
MAX_PERSONAS_PER_FILE = 500

#: 卡数据在 PNG 里的块关键字（`chara` 通用；`ccv3` 是 V3）
CARD_KEYWORDS = ("chara", "ccv3")

#: 爱语人设的 6 个字段（照 `PersonaInfo` 的 `@SerializedName`）
#:
#: ⚠️ 这是**外部格式的 dict key 名**，不是我们 `Persona` 的属性 ——
#:    2026-10-29 重构后模型里已经没有 `description` 那一族字段了。
#:    它现在的唯一用途：算 `extra_fields`（如实告诉用户"备份里这个键我们没接"）。
AIYU_FIELDS = (
    "name", "description", "personality", "scenario", "creator_notes", "avatar_path",
)

#: 拼 `system_prompt` 时来源字段的**固定顺序**（2026-10-29 人设结构重构）。
#:
#: 🔴 顺序固定有两个实在理由：
#:   ① 稳定前缀（P3）—— 同一张卡每次导入必须拼出**逐字相同**的正文，
#:      否则上游 prompt 缓存的稳定前缀一直在变，缓存等于没有；
#:   ② 可预测 —— 用户看拼接结果能一眼对上"这段来自哪个字段"。
#:
#: 这 4 个键在爱语备份与 SillyTavern 卡里**同名同义**，所以一条规则两边通用。
PROMPT_FIELD_ORDER: tuple[str, ...] = (
    "description", "personality", "scenario", "creator_notes",
)

#: 字段分隔符：空一行。空字段**整段跳过**，不会拼出连续三个换行。
PROMPT_JOINER = "\n\n"


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


def _join_prompt_fields(source: dict[str, Any]) -> str:
    """把角色卡的文字字段按**固定顺序**拼成人设正文（AstrBot 的 `system_prompt`）。

    🔴 **2026-10-29**：人设结构改成 AstrBot 的「`persona_id` + 一整段
    `system_prompt`」后，角色卡那些字段（description/personality/scenario/
    creator_notes）不再各占一个人设字段，而是**拼成一段正文**。

    ⚠️ 只在**判断空**时 strip，拼进去的是**原文** —— 实测踩过：
       用 `content.strip()` 存会丢掉尾部换行，对长提示词是信息损失。

    Args:
        source: 角色卡 dict（`creator_notes` 的别名 `creatorcomment` 需调用方先归一）。

    Returns:
        拼好的正文（没有可用字段时返回 `""`）。
    """
    parts: list[str] = []
    for key in PROMPT_FIELD_ORDER:
        content = source.get(key)
        # ⚠️ 只在**判断空**时 strip，拼进去的是**原文**。
        if isinstance(content, str) and content.strip():
            parts.append(content)
    return PROMPT_JOINER.join(parts)


def _persona_from_aiyu(d: dict[str, Any]) -> Persona:
    """爱语 `PersonaInfo` → 我们的 `Persona`。

    🔴 **2026-10-29 改法**（人设结构对齐 AstrBot）：原来这里把 6 字段各物化成
       一段 `PromptSection` 塞进 `prompt_sections`，理由是"面板上要有东西可编辑"。
       新结构下面板编辑的是 `system_prompt` **一个框**，片段那个概念已经删了 ——
       继续往 `prompt_sections` 写等于**写进一个没人读的地方**（静默失效）。

    现在：4 个正文来源字段由 `_join_prompt_fields` 按**固定顺序**拼成一段
    `system_prompt`（`description → personality → scenario → creator_notes`）。

    ⚠️ `creator_notes` 在爱语备份里可能叫 `creatorcomment`，先归一。
    ⚠️ 爱语的 `avatar_path` 是**手机上的路径**，搬到后端后无意义
       → 只记名字不记路径（指过去也是不存在的文件）。
    """
    name = _as_text(d.get("name")).strip()
    if not name:
        raise CardImportError("备份里有条目没有 `name`（名字是必填的）")

    persona = Persona(name=name, avatar_path="")
    source = dict(d)
    if not source.get("creator_notes"):
        source["creator_notes"] = d.get("creatorcomment")
    persona.system_prompt = _join_prompt_fields(source)
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


#: V2 `data` 里"同样是喂给模型的指令、但我们没有独立字段装"的键。
#:
#: 🔴 2026-10-29：原来这里第三列是**片段 order**（那套已删）。现在只留键名顺序，
#:    它们会被**按此顺序追加**到 `system_prompt` 末尾 —— 顺序固定，
#:    保证同一张卡每次导入得到逐字相同的正文（P3 稳定前缀）。
#:
#: ⚠️ 为什么不是所有键都进正文：`description` 那 4 个是"人设正文本体"
#:    （由 `_join_prompt_fields` 处理，排在最前）；这三个是"附在正文后面的补充
#:    指令"，语义上本来就是后半段。开场白则**完全不是**正文（见 `begin_dialogs`）。
_V2_EXTRA_PROMPT_KEYS: tuple[str, ...] = (
    "system_prompt",
    "mes_example",
    "post_history_instructions",
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

    # 🔴 **2026-10-29：正文改成一段 `system_prompt`**（人设结构对齐 AstrBot）。
    #
    # ① 先用我们自己的 4 字段规则拼正文（`_join_prompt_fields`，顺序固定）。
    #    ⚠️ `creator_notes` 在 SillyTavern 里还有个别名 `creatorcomment`，
    #    所以先做一个**只在本函数内生效**的视图，不动调用方的 dict。
    # ② 再补两类"卡里有、但我们没人设字段装"的东西：
    #    * 卡自己的 `system_prompt` / `mes_example` / `post_history_instructions`
    #      —— 同样是喂给模型的指令，追加到正文后面；
    #    * 开场白（`first_mes` 等）—— **不是**系统提示词，所以放
    #      `begin_dialogs`（AstrBot 那个字段就是为"开场对话"准备的）。
    #      硬塞进正文会让模型以为"这些话已经说过了"。
    # ③ 我们自己的导出块（`extensions.emotion_app`）里带**原文**，优先级最高 ——
    #    那是往返无损的唯一保证，不依赖对方前端的字段习惯。
    source = dict(data)
    if not source.get("creator_notes"):
        source["creator_notes"] = data.get("creatorcomment")

    persona = Persona(name=name)
    persona.system_prompt = _join_prompt_fields(source)

    ext = data.get("extensions")
    own = ext.get("emotion_app") if isinstance(ext, dict) else None

    if isinstance(own, dict):
        notes.append("识别到本程序导出的扩展块（emotion_app）")
        fmt = "emotion_app"
        # 我们自己的完整正文优先（导出→导入无损往返）
        own_prompt = own.get("system_prompt")
        if isinstance(own_prompt, str) and own_prompt.strip():
            persona.system_prompt = own_prompt
            notes.append("人设正文取自本程序导出的完整 system_prompt（无损）")

        # 🔴 2026-10-29：**导出写了的键，导入必须读回来**。
        #    原来这里只恢复 model/api_url，而 `to_export_dict` 把
        #    begin_dialogs/tools/skills/custom_error_message/sort_order 也写进了
        #    扩展块 → 导出再导入那些人设的**工具白名单与开场对话全丢**，
        #    而且完全不报错（往返测试才发现）。导出与导入必须成对改。
        own_dialogs = own.get("begin_dialogs")
        if isinstance(own_dialogs, list):
            persona.begin_dialogs = [x for x in own_dialogs if isinstance(x, str)]

        # ⚠️ `tools`/`skills` 的 **None 语义**必须原样保住：
        #    `None` = 用全部工具，`[]` = 一个都不给。写成 `or []` 会把
        #    "全开"变成"全禁" —— 那是功能性故障，不只是显示问题。
        for key in ("tools", "skills"):
            if key in own:
                value = own[key]
                if value is None:
                    setattr(persona, key, None)
                elif isinstance(value, list):
                    setattr(persona, key, [str(x) for x in value])

        own_err = own.get("custom_error_message")
        if isinstance(own_err, str) and own_err.strip():
            persona.custom_error_message = own_err
        try:
            persona.sort_order = int(own.get("sort_order") or 0)
        except (TypeError, ValueError):
            # 扩展块被手改坏时不该让整个导入失败 —— 但也不静默：
            notes.append("扩展块里的 `sort_order` 不是整数，已按 0 处理")

        if isinstance(own.get("model"), str) and not persona.model:
            persona.model = own["model"]
        if isinstance(own.get("api_url"), str) and not persona.api_url:
            persona.api_url = own["api_url"]

    # 角色卡自带的额外指令 —— 追加到正文后面（顺序固定，保证可复现）。
    #
    # ⚠️ 判重是刻意的：我们自己的导出把正文放在 `description`，同时
    #    `system_prompt` 留空 —— 那种情况不该追加；但第三方卡可能
    #    `description` 与 `system_prompt` 都有内容，那就都要。
    # ⚠️ 用 `in` 做子串判断只在"同一个值"这一种情况下可靠，
    #    所以这里比的是**原始字段**是否已经出现在正文里，而不是去重合并。
    extra_parts: list[str] = []
    for key in _V2_EXTRA_PROMPT_KEYS:
        content = _as_text(data.get(key))
        if content.strip() and content not in persona.system_prompt:
            extra_parts.append(content)
    if extra_parts:
        persona.system_prompt = "\n\n".join(
            ([persona.system_prompt] if persona.system_prompt.strip() else []) + extra_parts
        )

    # 开场白 → `begin_dialogs`（AstrBot 的"开场对话"字段）。
    #
    # 🔴 格式必须照 AstrBot 的**真实实现**（`astrbot/core/persona_mgr.py:374-405`）：
    #    `begin_dialogs` 是**交替的纯字符串数组** —— 偶数条，第 0 条是 user、
    #    第 1 条是 assistant，依次交替，由 AstrBot 自己配对成 `{role, content}`。
    #    ⚠️ 它不是 `[{role, content}]` 那种 dict 数组（那是 **processed 之后的**
    #       `_begin_dialogs_processed`，是 AstrBot 的缓存字段，别再传回去）。
    #    ⚠️ 条数为奇数时 AstrBot 会**整组丢弃**并打 error 日志（`persona_mgr.py:381`）
    #       —— 所以这里绝不能塞进去单数条。
    greetings: list[str] = []
    first = _as_text(data.get("first_mes"))
    if first.strip():
        greetings.append(first)
    alt = data.get("alternate_greetings")
    if isinstance(alt, list):
        greetings.extend(
            str(x) for x in alt if isinstance(x, str) and x.strip()
        )
    elif isinstance(alt, str) and alt.strip():
        greetings.append(alt)

    # AstrBot 要偶数条（user/assistant 交替）。开场白天然只有"assistant 说一句"，
    # 没有对应的 user 发言 ⇒ **成对补齐**会伪造用户台词（比丢掉更糟），
    # 所以这里如实保留，并把奇数这件事**告诉用户**，不静默变形。
    if greetings:
        persona.begin_dialogs = greetings
        notes.append(
            f"开场白已导入 {len(greetings)} 条，放在 `begin_dialogs`（不混进人设正文）"
        )
        if len(greetings) % 2:
            notes.append(
                "⚠️ `begin_dialogs` 目前是奇数条。AstrBot 要求 user/assistant "
                "交替成对（偶数条），奇数会被它整组丢弃 —— "
                "请到面板补一条用户台词，或删掉多余的一条。"
            )

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
    # (2026-10-29) 原来是 `not persona.description and ... and not sections`
    # —— 那三个属性都已经不存在了。现在的判据是"正文与开场白都空"。
    if not persona.system_prompt.strip() and not persona.begin_dialogs:
        notes.append("⚠️ 这张卡几乎是空的（没有正文、也没有开场白）")

    return ParsedCard(personas=[persona], source_format=fmt,
                      notes=notes, extra_fields=extra)


def _parse_json_card(card: dict[str, Any]) -> ParsedCard:
    """自动识别 JSON 的格式（AstrBot → 爱语备份 → 本程序导出 → SillyTavern）。

    🔴 **2026-10-29 新增 AstrBot 原生格式**（情绪提供的真实样本
       `/root/personas/persona_莉莉丝.json`）：

       ```json
       {"persona_id": "莉莉丝", "system_prompt": "<全文>", "begin_dialogs": []}
       ```

       ⚠️ 它**只有这 3 个字段**，既没有 `name` 也没有 `spec` —— 原来的识别
       逻辑三条分支全落空，直接报"认不出这个 JSON 的格式"。
       而人设结构刚刚按 AstrBot 对齐，**认不了自家格式**就说不过去了。

       🔴 必须放在**最前面**判断：`persona_id` 是 AstrBot 独有的键，
          同时出现它和 `name` 时也应优先当 AstrBot 格式（`name` 在
          SillyTavern 里语义不同，别混用）。
    """
    # ⓪ AstrBot 原生导出（`persona_id` + `system_prompt`）
    if "persona_id" in card and isinstance(card.get("system_prompt"), str):
        return _parse_astrbot_persona(card)

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
        "① AstrBot 人设（`persona_id` + `system_prompt`）"
        "② 爱语备份（`data.prompts`）"
        "③ 本程序导出的角色卡（`spec: chara_card_v2`）"
        "④ SillyTavern 角色卡（含 `name` 字段）"
    )


#: AstrBot 人设 JSON 的已知字段（用于报"哪些没接"）。
_ASTRBOT_PERSONA_KEYS = frozenset({
    "persona_id", "system_prompt", "begin_dialogs",
    # v3 `Personality` 的其它字段（我们模型里都有对应项）
    "tools", "skills", "custom_error_message",
    # 缓存字段：AstrBot 自己现算的，不该信也不该存
    "_begin_dialogs_processed", "_mood_imitation_dialogs_processed",
    "mood_imitation_dialogs",
})


def _parse_astrbot_persona(card: dict[str, Any]) -> ParsedCard:
    """解析 AstrBot 原生人设 JSON → 我们的 `Persona`（结构已对齐，几乎 1:1）。

    🔴 `begin_dialogs` 的格式（照 `astrbot/core/persona_mgr.py:374-405` 的**实测**语义）：

       它是**交替的纯字符串数组**，第 0 条当 user、第 1 条当 assistant，
       由 AstrBot 自己配对。`[{role, content}]` 那是它内部的**缓存字段**
       `_begin_dialogs_processed`，不是输入格式。

       ⚠️ **奇数条会被 AstrBot 整组丢弃并打 error** —— 所以这里如实导入
          并在 notes 里明确告知，**不伪造配对**（编造一句用户台词是篡改数据）。

    ⚠️ `_begin_dialogs_processed` / `_mood_imitation_dialogs_processed`
       是我们的**不读也不存**的字段（下划线前缀 = AstrBot 的缓存），
       列进 `extra` 让用户知道"这两个我们没接，但那是缓存不是内容"。
    """
    persona_id = _as_text(card.get("persona_id")).strip()
    if not persona_id:
        raise CardImportError("AstrBot 人设缺少 `persona_id`")

    system_prompt = _as_text(card.get("system_prompt"))
    notes: list[str] = []

    dialogs_raw = card.get("begin_dialogs")
    begin_dialogs: list[str] = []
    if isinstance(dialogs_raw, list):
        begin_dialogs = [str(x) for x in dialogs_raw if str(x).strip()]
        if len(begin_dialogs) % 2 != 0:
            notes.append(
                f"⚠️ `begin_dialogs` 是 {len(begin_dialogs)} 条（奇数）。"
                "AstrBot 会**整组丢弃**奇数条的开场对话，建议补一条凑成偶数后再用。"
            )
    elif dialogs_raw is not None:
        notes.append("`begin_dialogs` 不是数组，已忽略")

    persona = Persona(
        name=persona_id,
        system_prompt=system_prompt,
        begin_dialogs=begin_dialogs,
        # ⚠️ `tools` / `skills` 的 **None 语义**原样保留：
        #    None=全部可用，[]=一个都不用。用 `or []` 归一会让"全禁"和
        #    "全开"混成一个意思 —— 那是功能故障，不只是显示问题。
        tools=(
            None if card.get("tools") is None
            else [str(x) for x in card["tools"] if str(x).strip()]
        ),
        skills=(
            None if card.get("skills") is None
            else [str(x) for x in card["skills"] if str(x).strip()]
        ),
        custom_error_message=card.get("custom_error_message"),
    )

    extra = sorted(k for k in card if k not in _ASTRBOT_PERSONA_KEYS)
    if extra:
        notes.append(f"文件里另有 {len(extra)} 个字段未导入：" + "、".join(extra))
    if not system_prompt.strip():
        notes.append("⚠️ 这个人设的 `system_prompt` 是空的")

    return ParsedCard(
        personas=[persona],
        source_format="astrbot",
        notes=notes,
        extra_fields=extra,
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
        # 🔴 2026-10-29：新版人设只有一整段 `system_prompt`，
        #    爱语备份格式没有对应字段 ⇒ 写进它的 `description`
        #    （那是爱语那边的主正文位，语义最接近）。
        #    ⚠️ 不拆回 personality/scenario —— 拆了就必然丢信息（原文是整段）。
        prompts[key] = {
            "data": {
                "name": p.name,
                "description": p.system_prompt,
                "personality": "",
                "scenario": "",
                "creator_notes": "",
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
    # 🔴 2026-10-29：正文是**一整段** `system_prompt`。
    #    SillyTavern 的 `description` 是它的主正文位 —— 整段放那里，
    #    保证"导出→别的角色卡前端"能看到完整人设。
    #    同时在我们自己的 `extensions.emotion_app` 里存**原文**，
    #    往返时优先用它（无损），不依赖对方前端的字段习惯。
    greetings = list(persona.begin_dialogs)
    data: dict[str, Any] = {
        "name": persona.name,
        "description": persona.system_prompt,
        "personality": "",
        "scenario": "",
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "mes_example": "",
        "first_mes": greetings[0] if greetings else "",
        "alternate_greetings": greetings[1:] if len(greetings) > 1 else [],
        "tags": ["情绪app"],
        "creator": "情绪app",
        "character_version": "1.0",
        "extensions": {
            "emotion_app": {
                # 原文无损往返（导入时优先读它）
                "system_prompt": persona.system_prompt,
                "begin_dialogs": greetings,
                "model": persona.model,
                "api_url": persona.api_url,
                "tools": persona.tools,
                "skills": persona.skills,
                "custom_error_message": persona.custom_error_message,
                "sort_order": persona.sort_order,
            }
        },
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def merge_import_sections(
    parsed: ParsedCard, override: dict[str, Any] | None = None
) -> list[Persona]:
    """把导入结果与面板上的覆盖项合并（**批量**）。

    ⚠️ 只允许覆盖少数字段 —— 不开放 `api_key` 之类敏感项。

    🔴 **2026-10-29 白名单跟着人设结构改**：新 `Persona` 没有
       `description` / `personality` / `scenario` / `creator_notes` 了。
       原来那份白名单里的 4 个键若继续 `setattr`，会直接
       **AttributeError 把整个导入接口打成 500**（`slots=True` 的 dataclass
       —— 连"悄悄加个属性"都不行）。

       现在只认**模型里真实存在**的键。其中：
         * `system_prompt` —— 覆盖人设正文（最常用）；
         * `tools` / `skills` —— **保留 None 语义**（None=全部，[]=一个都不给）；
         * `begin_dialogs` —— AstrBot 的"开场对话"，必须是字符串数组。
    """
    if not override:
        return parsed.personas

    text_keys = ("name", "system_prompt", "model", "api_url")
    for p in parsed.personas:
        for key in text_keys:
            value = override.get(key)
            if isinstance(value, str) and value.strip():
                setattr(p, key, value)

        # `begin_dialogs`：**只接受字符串数组**（照 AstrBot `persona_mgr.py:374`）。
        # ⚠️ 不打平、不转 str —— 混进 dict 会让 AstrBot 那边整组丢弃。
        dialogs = override.get("begin_dialogs")
        if isinstance(dialogs, list) and all(isinstance(x, str) for x in dialogs):
            p.begin_dialogs = list(dialogs)

        # `tools` / `skills`：**None 与 [] 语义不同**（见 `persona.py` docstring），
        # 所以这里不能用 `if value:` —— 那会把 `None` 和 `[]` 一起跳过。
        for key in ("tools", "skills"):
            if key in override:
                value = override[key]
                if value is None:
                    setattr(p, key, None)
                elif isinstance(value, list):
                    setattr(p, key, [str(x) for x in value])

        # 旧面板可能还在送这 4 个键（前端没跟着改的时候）。**不静默吞掉**：
        # 拼进正文之前不动，直接把它们的值当正文覆盖项处理会丢信息，
        # 所以只在正文**为空**时用它们兜底拼一段，并留一条 note 让用户看得见。
        if not p.system_prompt.strip():
            legacy = _join_prompt_fields(override)
            if legacy:
                p.system_prompt = legacy
                parsed.notes.append(
                    "覆盖项里送的是旧字段"
                    "（description/personality/scenario/creator_notes），"
                    "已按固定顺序拼成 `system_prompt`"
                )
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
