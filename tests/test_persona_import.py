# -*- coding: utf-8 -*-
"""人设导入/导出测试。

⭐ 格式依据是**爱语真 dex 实测**（`爱语/docs/PERSONA_IMPORT_FORMAT.md`）：
   爱语用自己的备份格式 `{"data":{"prompts":{"<id>":{"data":{6 字段}}}}}`，
   **不用 SillyTavern 卡**，且**一个文件可含多个人设**。

判据（照文档 §6）：
  1. 导入爱语备份 → 逐字段一致
  2. 一个文件含 N 个人设 → 导入 N 个，**回显 N**
  3. 非法 JSON → **明确失败**，不静默导入 0 个
  4. 导出→导入往返 → 字段全等
"""
from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

from emotion_app.domain.persona import Persona, PromptSection
from emotion_app.domain.persona_import import (
    CONFLICT_OVERWRITE,
    CONFLICT_RENAME,
    CONFLICT_SKIP,
    CardImportError,
    export_filename,
    parse_card,
    parse_pasted_text,
    resolve_name_conflict,
    to_aiyu_backup,
    to_export_dict,
)

#: 爱语备份的真实结构（三层包装，照 ty.java:117-142）
AIYU_BACKUP = {
    "data": {
        "prompts": {
            "sister-high": {
                "data": {
                    "name": "妹妹",
                    "description": "你是我的妹妹，高中生",
                    "personality": "活泼、爱撒娇",
                    "scenario": "在家里",
                    "creator_notes": "注意安全",
                    "avatar_path": "/data/user/0/com.shuoshuo.aiyu/files/avatar.png",
                }
            },
            "teacher": {
                "data": {
                    "name": "老师",
                    "description": "语文老师",
                    "personality": "温柔",
                    "scenario": "教室",
                    "creator_notes": "",
                    "avatar_path": None,
                }
            },
        }
    }
}


def _b(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 爱语备份格式（主路径）
# ---------------------------------------------------------------------------


def test_parse_aiyu_backup_single():
    """一个条目的爱语备份 → 逐字段一致。"""
    one = {"data": {"prompts": {"sister-high": AIYU_BACKUP["data"]["prompts"]["sister-high"]}}}
    parsed = parse_card(_b(one))

    assert parsed.source_format == "aiyu_backup"
    assert len(parsed.personas) == 1

    p = parsed.persona
    assert p.name == "妹妹"
    assert p.description == "你是我的妹妹，高中生"
    assert p.personality == "活泼、爱撒娇"
    assert p.scenario == "在家里"
    assert p.creator_notes == "注意安全"


def test_parse_aiyu_backup_is_batch():
    """⭐ 一个文件含多人设 → 全部导入（回显 N）。"""
    parsed = parse_card(_b(AIYU_BACKUP))
    assert len(parsed.personas) == 2
    assert {p.name for p in parsed.personas} == {"妹妹", "老师"}


def test_aiyu_avatar_path_is_dropped():
    """爱语的 avatar_path 是**手机上的路径**，搬到后端后无意义 → 不保留。"""
    parsed = parse_card(_b(AIYU_BACKUP))
    sister = next(p for p in parsed.personas if p.name == "妹妹")
    assert sister.avatar_path == ""


def test_aiyu_entry_without_data_is_skipped_not_fatal():
    """条目缺 `data` → **跳过**（照 ty.h() 的行为），其余仍导入。"""
    card = {
        "data": {
            "prompts": {
                "ok": {"data": {"name": "有效"}},
                "broken": {"notdata": {}},
            }
        }
    }
    parsed = parse_card(_b(card))
    assert [p.name for p in parsed.personas] == ["有效"]
    assert any("跳过" in n for n in parsed.notes)


def test_aiyu_all_entries_broken_is_error():
    """全坏 → 必须报错，不能"成功导入 0 个"。"""
    card = {"data": {"prompts": {"a": {"x": 1}, "b": None}}}
    with pytest.raises(CardImportError, match="没有可用的人设"):
        parse_card(_b(card))


def test_aiyu_missing_prompts_is_error():
    with pytest.raises(CardImportError, match="data.prompts"):
        parse_card(_b({"data": {}}))


def test_aiyu_missing_data_is_error():
    with pytest.raises(CardImportError, match="data"):
        parse_card(_b({"prompts": {}}))


def test_aiyu_entry_without_name_is_error_for_that_entry():
    """有 name 才建人设 —— 缺 name 的条目跳过并说明。"""
    card = {"data": {"prompts": {"x": {"data": {"description": "没有名字"}}}}}
    with pytest.raises(CardImportError, match="没有可用的人设"):
        parse_card(_b(card))


def test_aiyu_roundtrip_is_lossless():
    """导出（爱语格式）→ 再导入 → 6 个字段全等。"""
    original = parse_card(_b(AIYU_BACKUP)).personas
    exported = to_aiyu_backup(original)
    again = parse_card(_b(exported)).personas

    assert len(again) == len(original)
    for a, b in zip(original, again):
        assert a.name == b.name
        assert a.description == b.description
        assert a.personality == b.personality
        assert a.scenario == b.scenario
        assert a.creator_notes == b.creator_notes


def test_aiyu_export_never_includes_api_key():
    """🔴 本地凭据不能跟着角色卡到处跑。"""
    p = Persona(name="带密钥", api_key="sk-secret-should-not-leak")
    exported = json.dumps(to_aiyu_backup([p]), ensure_ascii=False)
    assert "sk-secret-should-not-leak" not in exported


def test_export_dict_never_includes_api_key():
    p = Persona(name="带密钥", api_key="sk-secret-should-not-leak")
    exported = json.dumps(to_export_dict(p), ensure_ascii=False)
    assert "sk-secret-should-not-leak" not in exported


# ---------------------------------------------------------------------------
# 冲突策略（照爱语 ty.c() 的三种）
# ---------------------------------------------------------------------------


def test_conflict_rename_appends_index():
    """重命名是 `<name>_1`、`_2`…（照 ty.c()，**不是**加时间戳）。"""
    assert resolve_name_conflict("妹妹", {"妹妹"}, CONFLICT_RENAME) == "妹妹_1"
    assert resolve_name_conflict("妹妹", {"妹妹", "妹妹_1"}, CONFLICT_RENAME) == "妹妹_2"


def test_conflict_overwrite_keeps_name():
    assert resolve_name_conflict("妹妹", {"妹妹"}, CONFLICT_OVERWRITE) == "妹妹"


def test_conflict_skip_returns_none():
    assert resolve_name_conflict("妹妹", {"妹妹"}, CONFLICT_SKIP) is None


def test_conflict_none_when_no_clash():
    for mode in (CONFLICT_RENAME, CONFLICT_OVERWRITE, CONFLICT_SKIP):
        assert resolve_name_conflict("新名字", {"妹妹"}, mode) == "新名字"


# ---------------------------------------------------------------------------
# SillyTavern 兼容路径
# ---------------------------------------------------------------------------


ST_V2 = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Seraphina",
        "description": "a knight",
        "personality": "brave",
        "scenario": "castle",
        "creator_notes": "note",
        "system_prompt": "You are a knight.",
        "mes_example": "<START>\n{{user}}: hi",
        "first_mes": "Greetings!",
        "tags": ["fantasy"],
    },
}


def test_parse_sillytavern_v2():
    parsed = parse_card(_b(ST_V2))
    assert parsed.source_format == "sillytavern_v2"
    p = parsed.persona
    assert p.name == "Seraphina"
    assert p.scenario == "castle"

    titles = {s.title: s for s in p.prompt_sections}
    assert titles["角色卡系统提示词"].content == "You are a knight."
    # ⭐ 开场白导入但**默认关闭**（它不是 system prompt）
    assert titles["开场白（默认关闭）"].enabled is False
    assert any("默认关闭" in n for n in parsed.notes)


def test_parse_sillytavern_v1_flat():
    parsed = parse_card(_b({"name": "扁平", "description": "v1 格式"}))
    assert parsed.source_format == "sillytavern_v1"
    assert parsed.persona.name == "扁平"


def test_own_export_roundtrip_preserves_section_structure():
    """⭐ 我们自己的导出带 `extensions.emotion_app` → 往返**无损**（order/enabled 都保住）。"""
    p = Persona(
        name="片段测试",
        prompt_sections=[
            PromptSection(title="甲", content="AAA", order=10, source="user"),
            PromptSection(title="乙", content="BBB", order=200, source="persona", enabled=False),
        ],
    )
    exported = to_export_dict(p)
    again = parse_card(_b(exported))

    assert again.source_format == "emotion_app"
    got = {s.title: s for s in again.persona.prompt_sections}
    assert got["甲"].order == 10
    assert got["乙"].order == 200
    assert got["乙"].enabled is False
    assert got["乙"].source == "persona"


def test_unknown_format_json_is_rejected():
    """认不出的 JSON → 报错并**列出支持的格式**（给用户下一步）。"""
    with pytest.raises(CardImportError, match="认不出"):
        parse_card(_b({"foo": "bar"}))


def test_extra_fields_are_reported():
    """卡里有我们没有的字段 → **如实列出**，不静默丢弃。"""
    card = dict(ST_V2)
    card["data"] = dict(card["data"], weird_field="x", another=1)
    parsed = parse_card(_b(card))
    assert "weird_field" in parsed.extra_fields
    assert "another" in parsed.extra_fields


# ---------------------------------------------------------------------------
# 输入健壮性
# ---------------------------------------------------------------------------


def test_empty_file_rejected():
    with pytest.raises(CardImportError, match="空"):
        parse_card(b"")


def test_invalid_json_reports_line():
    with pytest.raises(CardImportError, match="第 1 行"):
        parse_card(b"{not json")


def test_json_array_top_level_rejected():
    with pytest.raises(CardImportError, match="顶层不是对象"):
        parse_card(b"[1,2,3]")


def test_utf8_bom_is_tolerated():
    """带 BOM 的备份文件很常见，不该因此失败。"""
    raw = b"\xef\xbb\xbf" + _b({"data": {"prompts": {"a": {"data": {"name": "带BOM"}}}}})
    assert parse_card(raw).persona.name == "带BOM"


def test_oversize_rejected():
    from emotion_app.domain.persona_import import MAX_CARD_BYTES
    with pytest.raises(CardImportError, match="过大"):
        parse_card(b"x" * (MAX_CARD_BYTES + 1))


@pytest.mark.parametrize("value,expected", [
    (["a", "b"], "1. a\n2. b"),      # 数组 → 编号文本
    (True, "是"),
    (42, "42"),
    (None, ""),
])
def test_text_coercion(value, expected):
    from emotion_app.domain.persona_import import _as_text
    assert _as_text(value) == expected


# ---------------------------------------------------------------------------
# 粘贴 / PNG
# ---------------------------------------------------------------------------


def test_paste_json_text():
    parsed = parse_pasted_text(json.dumps(AIYU_BACKUP, ensure_ascii=False))
    assert len(parsed.personas) == 2


def test_paste_data_url():
    """`data:image/png;base64,…` 直接粘贴也能用。"""
    png = _make_png_with_card({"data": {"prompts": {"a": {"data": {"name": "PNG人设"}}}}})
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    parsed = parse_pasted_text(url)
    assert parsed.persona.name == "PNG人设"
    assert parsed.source_format == "png"
    assert parsed.avatar_bytes == png


def test_paste_empty_rejected():
    with pytest.raises(CardImportError, match="空"):
        parse_pasted_text("   ")


def test_png_without_card_chunk_is_explicit_error():
    """普通 PNG（没有 chara 块）→ 明确告知"改用 JSON 导入"。"""
    png = _make_png_with_card(None)
    with pytest.raises(CardImportError, match="没有角色卡数据块"):
        parse_card(png)


def test_png_with_valid_magic_but_broken_structure():
    """魔数对但块结构损坏 → 要说"文件坏了"，**而不是**"改用 JSON"。

    ⚠️ 这两种提示的**下一步动作不同**：前者要重新下载，后者要换格式。
       混为一谈会让用户拿着坏文件去改格式，白折腾。
    """
    with pytest.raises(CardImportError, match="结构损坏"):
        parse_card(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)


def test_not_png_at_all_falls_through_to_json_error():
    """非 PNG 非 JSON → 报 JSON 解析错误（不是 PNG 相关的话术）。"""
    with pytest.raises(CardImportError, match="不是 PNG 也不是合法 JSON"):
        parse_card(b"hello world this is not json")


def test_export_filename_matches_aiyu_pattern():
    """照爱语 `persona_<名字>_<时间戳>.json`。"""
    name = export_filename(Persona(name="妹妹"))
    assert name.startswith("persona_妹妹_")
    assert name.endswith(".json")


def test_export_filename_sanitizes_unsafe_chars():
    """名字里的路径分隔符不能进文件名（防穿越 + 防下载失败）。"""
    name = export_filename(Persona(name="../../etc/passwd"))
    assert "/" not in name and ".." not in name


# ---------------------------------------------------------------------------
# PNG 构造工具（测兼容路径）
# ---------------------------------------------------------------------------


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)


def _make_png_with_card(card: dict | None) -> bytes:
    """造一个最小合法 PNG，可选地内嵌 `chara` 块。"""
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    # IHDR：1x1 RGB
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    out += _png_chunk(b"IHDR", ihdr)
    if card is not None:
        payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode())
        out += _png_chunk(b"tEXt", b"chara\x00" + payload)
    # 1x1 图像数据
    raw = zlib.compress(b"\x00\x00\x00\x00")
    out += _png_chunk(b"IDAT", raw)
    out += _png_chunk(b"IEND", b"")
    return bytes(out)


# ---------------------------------------------------------------------------
# ⭐ 真机导出文件（最有价值的判据）
# ---------------------------------------------------------------------------
#
# 这份 fixture 是**情绪 2026-09-19 从爱语真机导出**的实际文件
# （`persona_糯米茨_1789819973418.json`），不是我们构造的。
# → 它证明**我们对格式的理解与真机一致**，而不是自证。
#
# 判据：5 个文本字段**逐字相同**（不是"能解析"就算过）。

import pathlib  # noqa: E402

REAL_EXPORT = pathlib.Path(__file__).parent / "fixtures" / "aiyu_real_export.json"


@pytest.fixture
def real_raw() -> dict:
    return json.loads(REAL_EXPORT.read_text(encoding="utf-8"))


def test_real_export_parses_as_aiyu_backup():
    """真机文件必须被识别为爱语备份格式（不是 ST 卡）。"""
    parsed = parse_card(REAL_EXPORT.read_bytes(), filename=REAL_EXPORT.name)
    assert parsed.source_format == "aiyu_backup"
    assert len(parsed.personas) == 1


def test_real_export_fields_are_byte_identical(real_raw):
    """⭐ 5 个字段**逐字相同** —— 这是"格式理解正确"的硬判据。"""
    raw = next(iter(real_raw["data"]["prompts"].values()))["data"]
    p = parse_card(REAL_EXPORT.read_bytes()).persona

    for field in ("name", "description", "personality", "scenario", "creator_notes"):
        assert getattr(p, field) == (raw.get(field) or ""), f"{field} 不一致"


def test_real_export_reports_no_lost_fields():
    """真机文件里没有我们丢失的字段（`extra_fields` 应为空）。"""
    parsed = parse_card(REAL_EXPORT.read_bytes())
    assert parsed.extra_fields == [], f"有字段没被导入：{parsed.extra_fields}"


def test_real_export_drops_phone_avatar_path(real_raw):
    """爱语的 `avatar_path` 是手机路径 → 必须丢弃（搬后端后无意义）。"""
    raw = next(iter(real_raw["data"]["prompts"].values()))["data"]
    assert raw.get("avatar_path"), "fixture 里本该有 avatar_path（否则这条测试没意义）"

    p = parse_card(REAL_EXPORT.read_bytes()).persona
    assert p.avatar_path == ""


def test_real_export_roundtrip_is_lossless():
    """真机文件 → 导出 → 再导入，5 字段仍逐字相同（**无损往返**）。"""
    first = parse_card(REAL_EXPORT.read_bytes()).persona
    again = parse_card(_b(to_aiyu_backup([first]))).persona

    for field in ("name", "description", "personality", "scenario", "creator_notes"):
        assert getattr(first, field) == getattr(again, field), field


def test_real_export_long_text_not_truncated():
    """长文本（description 7000+ 字）不能被截断。"""
    raw = next(iter(
        json.loads(REAL_EXPORT.read_text(encoding="utf-8"))["data"]["prompts"].values()
    ))["data"]
    p = parse_card(REAL_EXPORT.read_bytes()).persona

    # 用 fixture 自己的长度做判据，避免硬编码会随 fixture 更新而失效
    assert len(p.description) == len(raw["description"])
    assert len(p.creator_notes) == len(raw["creator_notes"])
    assert len(p.description) > 1000, "fixture 应含长文本（否则这条测试没意义）"


def test_import_materializes_editable_sections():
    """⭐ 导入必须**物化出可编辑的片段**（2026-09-19 修的真 bug）。

    起因：爱语备份只有 6 个字段、不含片段结构。若只填字段而让
    `prompt_sections` 留空，**面板人设页会显示空片段表** ——
    用户在界面上看不到也改不了人设内容，与"人设=提示词组装器"的设计冲突。

    ⚠️ 这个 bug **接口测试全绿也照样漏**：`/preview` 会从字段现算，
       所以只有真的打开面板点进人设才看得见。
    """
    p = parse_card(REAL_EXPORT.read_bytes()).persona
    titles = [s.title for s in p.prompt_sections]

    assert titles == ["角色设定", "性格", "场景", "创作者备注"], titles

    by_title = {s.title: s for s in p.prompt_sections}
    assert by_title["角色设定"].content == p.description
    assert by_title["性格"].content == p.personality
    assert by_title["场景"].content == p.scenario
    assert by_title["创作者备注"].content == p.creator_notes


def test_materialized_sections_are_ordered_stable_first():
    """片段 order 必须**稳定内容在前**（时间等易变内容由内置片段排后面）。"""
    p = parse_card(REAL_EXPORT.read_bytes()).persona
    orders = [s.order for s in p.prompt_sections]
    assert orders == sorted(orders), f"order 不是递增：{orders}"
    assert all(o < 900 for o in orders), "人设片段应排在易变内容（时间=990）之前"


def test_materialized_sections_are_enabled_and_attributed():
    """片段默认启用，且 `source=persona`（来源可追溯）。"""
    p = parse_card(REAL_EXPORT.read_bytes()).persona
    for s in p.prompt_sections:
        assert s.enabled is True
        assert s.source == "persona"


def test_empty_fields_produce_no_empty_sections():
    """空字段不该造出空片段（否则面板上一堆空行）。"""
    card = {"data": {"prompts": {"a": {"data": {
        "name": "只有名字", "description": "", "personality": "  ",
        "scenario": "", "creator_notes": "",
    }}}}}
    p = parse_card(_b(card)).persona
    assert p.prompt_sections == []


def test_aiyu_roundtrip_preserves_fields_even_with_sections():
    """物化片段后，爱语格式往返仍以 **6 字段**为准（片段不干扰）。"""
    first = parse_card(REAL_EXPORT.read_bytes()).persona
    again = parse_card(_b(to_aiyu_backup([first]))).persona

    for field in ("name", "description", "personality", "scenario", "creator_notes"):
        assert getattr(first, field) == getattr(again, field), field
