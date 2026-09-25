# -*- coding: utf-8 -*-
"""厂商预设目录（`infra/provider_presets.py` + `GET /api/v1/provider-presets`）判据。

## 为什么单独一个文件

`tests/test_providers_api.py` 已经**超了本项目「单文件 ≤1500 行」的硬约束**
（约 2100 行）—— 再往里塞只会让它更难维护。新能力放新文件，
旧文件只做**必要**的修订（范围变更那几条）。

## 这个文件守什么

预设目录是**照真 AstrBot v4.27.5 抄来的**（50 条），所以判据的**第一要务**是
"抄得对不对 + 有没有悄悄丢数据"，而不是"函数能不能跑"。
→ 因此下面有一条**对着源 JSON 逐字段比对**的判据：
  它才是防"抄漏一条/抄错一个地址"的那道闸门。

## 🔴 能力诚实（本项目铁律）

50 条里我们**只真能执行 2 种适配器**（`openai_chat_completion` / `openai_responses`）。
其余是 AstrBot 自己的适配器。判据必须守住：
**能存 ≠ 能跑**，且"能跑"只能由 `adapter_is_executable()` 回答。
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emotion_app.infra.provider_presets import (  # noqa: E402
    ALL_PRESETS,
    LOCAL_EXTENSION_PRESETS,
    PRESETS,
    find_preset,
    list_presets,
    preset_public_view,
)
from emotion_app.infra.providers_store import (  # noqa: E402
    EXECUTABLE_ADAPTER_TYPES,
    SUPPORTED_PROVIDER_TYPES,
    adapter_is_executable,
)

#: 抓取时的源数据（**仓库内**保存，所以判据可复现；不是 /tmp 里的临时产物）。
_SOURCE = (
    Path(__file__).resolve().parent.parent / "conductor" / "presets_source.json"
)


def _source_rows() -> dict[str, dict]:
    """读源 JSON（照真 AstrBot 抓的原始数据）。缺失就是**判据失败**，不是跳过。"""
    assert _SOURCE.is_file(), (
        f"预设源数据不在：{_SOURCE} —— 判据无法验证目录是否正确（不许跳过）"
    )
    return {row["id"]: row for row in json.loads(_SOURCE.read_text(encoding="utf-8"))}


# ===========================================================================
# ① 数据正确性：对着源逐字段比（防抄漏/抄错）
# ===========================================================================


def test_preset_ids_match_source_exactly():
    """🔴 预设 id 集合必须与源**完全一致**（既不缺、也不多）。"""
    src = _source_rows()
    got = {p.id for p in PRESETS}
    assert got == set(src), (
        f"少了：{sorted(set(src) - got)}；多了：{sorted(got - set(src))}"
    )


def test_every_preset_field_matches_source():
    """🔴 **逐字段**比对 provider / type / provider_type / api_base。

    ⚠️ 为什么这条最重要：预设的价值**全在数据本身**。抄错一个 `api_base`
       或把 `openai_responses` 抄成 `openai_chat_completion`，
       函数照样"能跑"、其它判据照样全绿，但用户会打到错的端点。
       → 只有对着源逐字段比，才抓得住这类错。
    """
    src = _source_rows()
    bad: list[str] = []
    for p in PRESETS:
        want = src[p.id]
        for fieldname in ("provider", "type", "provider_type", "api_base"):
            got_v = getattr(p, fieldname)
            if got_v != want[fieldname]:
                bad.append(f"{p.id}.{fieldname}: 模块={got_v!r} 源={want[fieldname]!r}")
    assert not bad, "预设与源数据不符：\n  " + "\n  ".join(bad)


def test_provider_type_counts_match_source():
    """按分类的数量也要对得上（防"整体条数对、分组错"）。"""
    src = Counter(r["provider_type"] for r in _source_rows().values())
    got = Counter(p.provider_type for p in PRESETS)
    assert got == src, f"分组数量不符：模块={dict(got)} 源={dict(src)}"


def test_every_preset_type_is_in_supported_or_known_unsupported():
    """每条预设的 `provider_type` 必须是我们**认识**的（支持或明确已知不支持）。

    ⚠️ 这条防的是"AstrBot 新增了一类，我们抄进来了却从没想过它是什么"。
       `speech_to_text` 就属于"认识但本轮不支持" —— 它在已知清单里，所以放行。
    """
    from emotion_app.infra.providers_store import KNOWN_UNSUPPORTED_PROVIDER_TYPES

    known = set(SUPPORTED_PROVIDER_TYPES) | set(KNOWN_UNSUPPORTED_PROVIDER_TYPES)
    unknown = sorted({p.provider_type for p in PRESETS} - known)
    assert not unknown, f"预设里出现了我们不认识的 provider_type：{unknown}"


def test_agent_runner_is_never_offered():
    """🔴 情绪明确：情绪app **不需要 agent 能力** → 预设里不许出现 `agent_runner`。"""
    assert not [p for p in PRESETS if p.provider_type == "agent_runner"], (
        "预设里混进了 agent_runner —— 情绪明确说不需要 agent 能力"
    )


# ===========================================================================
# ② 能力诚实：能存 ≠ 能跑
# ===========================================================================


def test_executable_flag_is_exactly_the_two_openai_adapters():
    """🔴 `executable_by_us` 只对两种 OpenAI 格式为真，且数量**精确**。

    ⚠️ 断言"精确数量"而不是"至少有一个"：后者会在
       "把一堆跑不了的适配器标成能跑"时**照样通过**（本项目踩过这种假通过）。
    """
    ok = [p for p in PRESETS if preset_public_view(p)["executable_by_us"]]
    assert all(p.type in EXECUTABLE_ADAPTER_TYPES for p in ok), (
        f"有跑不了的适配器被标成可执行：{[(p.id, p.type) for p in ok]}"
    )
    n_chat = sum(1 for p in ok if p.type == "openai_chat_completion")
    n_resp = sum(1 for p in ok if p.type == "openai_responses")
    # 实测（照源数据数出来的）：16 条 chat + 3 条 responses = 19。
    # ⚠️ 写**精确**数字，这样"AstrBot 目录变了"或"我们误标了一批"都会当场报警，
    #    而不是被"至少有一条"这种宽判据放过去（本项目踩过这种假通过）。
    assert (n_chat, n_resp) == (16, 3), (
        f"可执行的两种格式数量变了：chat={n_chat} responses={n_resp}"
        "（若 AstrBot 目录确实变了，请同步更新本判据并说明）"
    )
    # `openai_responses` 的三条要**点名**（它们是本轮新增能力的关键证据）
    resp_ids = sorted(p.id for p in ok if p.type == "openai_responses")
    assert resp_ids == ["deepseek-responses", "openai-responses", "xai"], resp_ids


def test_adapter_is_executable_is_strict():
    """`adapter_is_executable` 对空/空白/None/未知一律 **False**（不能"看着像就放行"）。

    ⚠️ **大小写敏感是有意的**：适配器类型是**协议标识**，不是用户输入，
       真 AstrBot 给的就是全小写。`OPENAI_CHAT_COMPLETION` 不是同一个东西 →
       判 False 才是对的（放行它等于悄悄接受一个我们没验证过的标识）。
    ⚠️ **首尾空白会被 strip**：这与 `ProviderSource.from_dict` 的既有归一化一致
       （它会把 `"openai_chat_completion "` 存成 `"openai_chat_completion"` 并打 WARNING）。
       所以这里判 True 是**对的** —— 若判 False，就会出现
       "存进去的类型能跑、但预检说不认识"的自相矛盾。
    """
    assert adapter_is_executable("openai_chat_completion") is True
    assert adapter_is_executable("openai_responses") is True
    # 首尾空白 → strip 后能对上（与 store 的归一化保持一致）
    assert adapter_is_executable("openai_chat_completion ") is True
    assert adapter_is_executable("  openai_responses  ") is True
    # 空 / None / 未知识别符 / 大小写不同 → 一律 False
    for bad in ("", "   ", None, "  openai  ", "edge_tts", "openai_embedding",
                "OPENAI_CHAT_COMPLETION", "Openai_Chat_Completion", "openai-chat-completion"):
        assert adapter_is_executable(bad) is False, f"{bad!r} 不该被判为可执行"


def test_public_view_shape_is_frozen():
    """响应形状冻结（前端直接读这些键，少一个就是 `undefined`）。"""
    view = preset_public_view(find_preset("openai-responses"))
    assert set(view) == {
        "id", "label", "provider", "type", "provider_type", "api_base", "note",
        "executable_by_us",
    }, sorted(view)
    assert view["executable_by_us"] is True
    assert isinstance(view["note"], str) and view["note"]


def test_labels_are_human_readable_not_raw_ids():
    """展示名必须是**人话**，不能直接把 id 当 label（面板上会很难看）。

    ⚠️ 抽查几条关键的：OpenAI 两种格式必须能一眼区分。
    """
    assert find_preset("openai").label == "OpenAI"
    assert "Responses" in find_preset("openai-responses").label
    assert find_preset("deepseek").label == "DeepSeek"
    assert find_preset("siliconflow").label == "硅基流动"
    # 不允许 label == id（那就是没起名）
    same = [p.id for p in PRESETS if p.label == p.id]
    assert not same, f"这些预设的 label 直接等于 id（没起展示名）：{same}"


# ===========================================================================
# ③ 查询行为
# ===========================================================================


def test_list_presets_filters_by_type():
    """按分类过滤：只回该类，且**不是空集**（空集说明过滤写错了）。"""
    for t in SUPPORTED_PROVIDER_TYPES:
        got = list_presets(t)
        assert got, f"{t} 一条都没有 —— 过滤写错或数据丢了"
        assert {p.provider_type for p in got} == {t}
    # ⚠️ 无参 = **只要存得下的**（见下一条判据）。2026-09-21 支持 STT 后，
    #    AstrBot 那 50 条**全部存得进** → 两者相等。
    #    🔴 断言按 `SUPPORTED_PROVIDER_TYPES` 算，**不写死 50** ——
    #       将来加/减支持的类型时这条判据自动跟着走。
    #    🆕 2026-09-25：对照物从 `PRESETS` 换成 `ALL_PRESETS`（忠实副本 + 本地扩展）。
    #       **机制没变**（下发集 == 可存集），只是目录长大了 —— 不是为迁就数据改判据。
    storable = [p for p in ALL_PRESETS if p.provider_type in SUPPORTED_PROVIDER_TYPES]
    assert len(list_presets(None)) == len(storable)
    unstorable = len(ALL_PRESETS) - len(storable)
    assert unstorable == 0, (
        f"有 {unstorable} 条预设存不进本平台；若是有意的（新类型未支持），"
        "请同步更新本断言与 SUPPORTED_PROVIDER_TYPES 的说明"
    )


def test_list_presets_storable_only_drops_what_we_cannot_save():
    """🔴 **选得中就必须存得进** —— 默认只下发我们**存得下**的类型。

    实测背景：AstrBot 目录里有 3 条 `speech_to_text` 预设，而我们**当时**不支持 STT。
    照全量下发 → 面板列出一个用户**选得中、一保存就 400** 的预设，
    属于"看着能用其实不能用"（本项目最忌讳的一类）。

    ⚠️ 2026-09-21 支持 STT 后，这 3 条**变成存得进**了 → 本判据的**行为不变**
       （它照 `SUPPORTED_PROVIDER_TYPES` 动态算），只是不再是"3 条被挡"。
       🔴 本判据守的是**机制**（下发集 == 可存集），不是"有 3 条被挡"这个现象 ——
          机制不变就不该为了迁就数据去改判据。
    """
    default_ids = {p.id for p in list_presets(None)}
    storable = {p.id for p in ALL_PRESETS if p.provider_type in SUPPORTED_PROVIDER_TYPES}
    assert default_ids == storable, (
        f"默认下发里混进了存不进的：{sorted(default_ids - storable)}"
    )
    # 显式要全量时仍给全量（保留"看看 AstrBot 有什么"的能力）
    assert {p.id for p in list_presets(None, storable_only=False)} == {
        p.id for p in ALL_PRESETS
    }
    # 而 storable_only 的结果里，绝不该有存不进的
    assert all(
        p.provider_type in SUPPORTED_PROVIDER_TYPES
        for p in list_presets(None, storable_only=True)
    )


def test_list_presets_rejects_bad_input_loudly(caplog):
    """非法入参 → **空列表 + WARNING**（不静默返回全部，那会让面板显示错的清单）。"""
    caplog.set_level(logging.WARNING, logger="emotion_app.infra.provider_presets")
    assert list_presets("") == []
    assert list_presets("不存在的类型") == []
    assert list_presets(123) == []  # type: ignore[arg-type]
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) >= 3, f"非法入参没有留下告警（静默了）：{warns}"


def test_find_preset_hit_and_miss():
    assert find_preset("deepseek") is not None
    assert find_preset("nope") is None
    assert find_preset("") is None


def test_presets_tuple_has_no_duplicate_ids():
    ids = [p.id for p in PRESETS]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    assert not dupes, f"预设 id 重复：{dupes}"


# ===========================================================================
# ④ 路由：GET /api/v1/provider-presets
# ===========================================================================


async def test_route_returns_all_presets(mk):
    c = await mk()
    r = await c.get("/api/v1/provider-presets")
    assert r.status_code == 200, await r.get_data(as_text=True)
    from tests.test_providers_api import _data

    d = _data(await r.get_json())
    assert d["total"] == len(list_presets(None)), d["total"]
    assert len(d["presets"]) == d["total"]
    assert {"id", "label", "type", "provider_type", "executable_by_us"} <= set(d["presets"][0])


async def test_route_filters_by_type(mk):
    c = await mk()
    from tests.test_providers_api import _data

    r = await c.get("/api/v1/provider-presets?provider_type=text_to_speech")
    assert r.status_code == 200
    d = _data(await r.get_json())
    assert d["total"] > 0
    assert {p["provider_type"] for p in d["presets"]} == {"text_to_speech"}


async def test_route_rejects_unknown_provider_type(mk):
    """🔴 拼错的分类必须 **400**（静默回空列表会被误读成"没有预设"）。"""
    c = await mk()
    r = await c.get("/api/v1/provider-presets?provider_type=text_to_speach")
    assert r.status_code == 400, r.status_code
    body = await r.get_json()
    assert body["status"] == "error"
    assert body["reason"] == "invalid_provider_type", body
    assert "text_to_speech" in body["message"], body  # 消息里要列出合法值


async def test_route_requires_auth(mk):
    """未登录 → 401/403（预设目录虽非机密，也要与其它供应商路由一致地鉴权）。"""
    c = await mk(login=False)
    r = await c.get("/api/v1/provider-presets")
    assert r.status_code in (401, 403), r.status_code


async def test_route_actually_serves_local_extensions(mk):
    """🔴 **接线判据**：本地扩展必须真的出现在面板拿到的响应里。

    ⚠️ 只测 `list_presets()` 是**不够的**（本项目踩过「函数对 ≠ 接线对」）：
       路由若哪天改回 `PRESETS`，函数判据照样全绿，但面板上那 7 条消失了。
       所以这里**从 HTTP 响应出发**点名要它们。
    """
    c = await mk()
    from tests.test_providers_api import _data

    r = await c.get("/api/v1/provider-presets?provider_type=chat_completion")
    assert r.status_code == 200, await r.get_data(as_text=True)
    d = _data(await r.get_json())
    got = {p["id"]: p for p in d["presets"]}
    for p in LOCAL_EXTENSION_PRESETS:
        assert p.id in got, f"面板拿不到本地扩展 {p.id}（路由没走 ALL_PRESETS？）"
        assert got[p.id]["executable_by_us"] is True, p.id
    # 全量响应里也要有（不带 query 的那条路）
    r2 = await c.get("/api/v1/provider-presets")
    allids = {p["id"] for p in _data(await r2.get_json())["presets"]}
    assert {p.id for p in LOCAL_EXTENSION_PRESETS} <= allids
    # 而忠实副本一条都不能少
    assert {p.id for p in PRESETS} <= allids


# ===========================================================================
# ⑤ 范围变更：TTS/embedding 现在存得下（但跑不了）
# ===========================================================================


@pytest.mark.parametrize("ptype", ["text_to_speech", "embedding"])
async def test_preset_provider_types_are_storable(mk, ptype):
    """预设里出现的分类，必须**真的能存进 store**（否则又是"选得中存不进"）。"""
    from tests.test_providers_api import _add_source, _data

    c = await mk()
    sample = list_presets(ptype)[0]
    r = await _add_source(
        c, id=f"p-{ptype}", provider=sample.provider, type=sample.type,
        provider_type=sample.provider_type, api_base=sample.api_base or "https://x/v1",
    )
    assert r.status_code == 201, (sample.id, await r.get_json())
    d = _data(await (await c.get("/api/v1/provider-sources")).get_json())
    assert [s["provider_type"] for s in d["provider_sources"]] == [ptype]


def test_stt_presets_are_listed_and_storable_but_not_executable():
    """🔴 N1 的核心判据：STT **存得进、列得出**，但**执行不了**（如实标注）。

    这是"能力诚实"（作者验收判据③）在 STT 上的具体形状 ——
    与 TTS 同类处理：**配置层完整可用，执行层如实标注不可用**。

    三重断言，每一条都能失败：
      ① 3 条 STT 预设**能被单独查出**（`list_presets("speech_to_text")` 非空）；
      ② 它们**在下发集里**（默认 `list_presets(None)` 含它们 —— 否则用户根本选不到）；
      ③ 它们的 `executable_by_us` **全部为 False**，且有 `note` 说明原因
         （若哪天我们真实现了 whisper，这条会**失败并提醒改标注** —— 不会静默说谎）。

    注入验证：把 `PROVIDER_TYPE_STT` 从 `SUPPORTED_PROVIDER_TYPES` 移除 →
    断言 ①② 必须挂；把 `openai_whisper_api` 加进 `EXECUTABLE_ADAPTER_TYPES` →
    断言 ③ 必须挂。
    """
    from emotion_app.infra.providers_store import SUPPORTED_PROVIDER_TYPES as _SUPPORTED

    # ① 能单独查出
    stt = list_presets("speech_to_text")
    assert len(stt) == 3, f"STT 预设应有 3 条（whisper/xinference_stt/mimo_stt），实得 {len(stt)}"
    assert {p.id for p in stt} == {"whisper", "xinference_stt", "mimo_stt"}

    # ② 在默认下发集里（用户选得到）
    assert "speech_to_text" in _SUPPORTED, "STT 不在支持集里 → 存不进，N1 没生效"
    default_ids = {p.id for p in list_presets(None)}
    for pid in ("whisper", "xinference_stt", "mimo_stt"):
        assert pid in default_ids, f"{pid} 不在下发集里 —— 用户选不到它"

    # ③ 诚实标注:全不可执行 + 有说明
    for p in stt:
        view = preset_public_view(p)
        assert view["executable_by_us"] is False, (
            f"{p.id}({p.type}) 被标成可执行了 —— 若真实现了它，请同步更新这里与文档"
        )
        assert view["note"], f"{p.id} 没有 note，用户看不出为什么不能用"


# ===========================================================================
# ④ 🆕 本地扩展预设（2026-09-25）
# ===========================================================================
#
# 背景：AstrBot 给若干厂商写的适配器其实是 `ProviderOpenAIOfficial` 的**薄包装**
# （10~59 行，只加 header / 归一化 URL / 改默认模型）—— 即**那些厂商本来就是
# OpenAI 格式**。AstrBot 目录里它们被标成专有适配器名，于是我们判成"不能执行"。
# 我们按实测补了 7 条（`LOCAL_EXTENSION_PRESETS`），**不改** AstrBot 那份忠实副本。
#
# 取证方式（**不需要真 key**）：带假 key 打 `GET {api_base}/models` ——
# 401/403 = 端点存在且要鉴权（URL 对了）；404 = 路径不对（不许加）。


def test_local_extensions_do_not_pollute_the_faithful_copy():
    """🔴 本地扩展**绝不能**混进 `PRESETS`（那是拿源数据逐字段比的对照物）。

    这是本组最重要的判据：一旦有人图省事把扩展直接塞进 `PRESETS`，
    `test_preset_ids_match_source_exactly` 会红，但若那条被判据作者顺手"放宽"了，
    忠实副本就悄悄腐化了 —— 这条独立盯着它。
    """
    faithful_ids = {p.id for p in PRESETS}
    ext_ids = {p.id for p in LOCAL_EXTENSION_PRESETS}
    assert not (faithful_ids & ext_ids), (
        f"本地扩展的 id 撞进了忠实副本：{sorted(faithful_ids & ext_ids)}"
    )
    assert len(ALL_PRESETS) == len(PRESETS) + len(LOCAL_EXTENSION_PRESETS)
    assert set(p.id for p in ALL_PRESETS) == faithful_ids | ext_ids


def test_local_extensions_are_all_executable_openai_chat():
    """扩展**必须**是我们真能执行的格式 —— 否则加它只是把"看着能用"搬进面板。"""
    for p in LOCAL_EXTENSION_PRESETS:
        assert p.type in EXECUTABLE_ADAPTER_TYPES, (
            f"{p.id} 的适配器 {p.type!r} 我们跑不了，不该作为本地扩展加进来"
        )
        assert preset_public_view(p)["executable_by_us"] is True, p.id
        assert p.provider_type in SUPPORTED_PROVIDER_TYPES, p.id


def test_local_extensions_have_urls_and_notes():
    """扩展必须**带地址、带说明** —— 空地址等于让用户自己猜，空说明等于隐瞒差异。"""
    for p in LOCAL_EXTENSION_PRESETS:
        assert p.api_base.startswith("https://"), f"{p.id} 的 api_base 不像地址：{p.api_base!r}"
        assert p.note and len(p.note) > 10, f"{p.id} 没有说明文案"
        assert p.label and p.label != p.id, f"{p.id} 没起展示名"
        # 面板上要和厂商原条目**并排可选**（N4-3 的验收点）
        assert p.provider, p.id


def test_longcat_extension_url_carries_v1():
    """🔴 `longcat` 的坑：AstrBot 源数据里是 `/openai`（**不带 /v1**），
    而它的适配器会在初始化时归一化成 `/openai/v1`。

    → 我们直接用 OpenAI 适配器打源数据那个地址会**打到错路径**。
       这条判据点名守住：扩展里的 longcat 地址**必须**带 `/v1`。
    """
    ext = find_preset("longcat-openai")
    assert ext is not None, "本地扩展里的 longcat 预设不见了"
    assert ext.api_base.endswith("/v1"), (
        f"longcat 扩展地址必须以 /v1 结尾（AstrBot 会归一化，我们不归一化）：{ext.api_base}"
    )
    # 而忠实副本里那条**保持原样**（别去改它，它就是 AstrBot 的原文）
    faithful = find_preset("longcat")
    assert faithful.api_base == "https://api.longcat.chat/openai", faithful.api_base


def test_local_extension_notes_disclose_unported_adapter_behaviour():
    """🔴 哪些适配器行为我们**没搬**，必须在 note 里说清（不说 = 用户以为一模一样）。

    点名三条已知差异：
      * groq     —— 适配器会剔除 assistant 历史里的 reasoning 字段，我们不做
      * aihubmix —— 适配器带 APP-Code 折扣 header，我们不带
      * openrouter / ssycloud —— 适配器带归属/标题 header，我们不带
    """
    for pid, keyword in [
        ("groq-openai", "reasoning"),
        ("aihubmix-openai", "APP-Code"),
        ("openrouter-openai", "header"),
        ("ssycloud-openai", "X-Title"),
    ]:
        p = find_preset(pid)
        assert p is not None, f"{pid} 不见了"
        assert keyword in p.note, f"{pid} 的 note 没写清未搬过来的行为（缺 {keyword!r}）：{p.note}"


def test_known_non_openai_vendors_are_not_extended():
    """🔴 **不许**给不是 OpenAI 格式的厂商加扩展（实测 404）。

    `kimi-code` / `minimax-token-plan` / `xiaomi-token-plan` 继承的是
    `ProviderAnthropic`，`anthropic` / `gemini` 是真专有 —— 给它们挂
    `openai_chat_completion` 就是"看着能用其实不能用"。
    """
    forbidden = {
        "anthropic", "google_gemini", "kimi-code", "minimax-token-plan",
        "xiaomi-token-plan",
    }
    ext_providers = {p.provider for p in LOCAL_EXTENSION_PRESETS}
    bad = sorted(forbidden & ext_providers)
    assert not bad, f"给这些厂商加了 OpenAI 扩展，但它们实测不是 OpenAI 格式：{bad}"


def test_find_preset_reaches_local_extensions():
    """面板按 id 取预设 —— 取不到就等于没加。"""
    for p in LOCAL_EXTENSION_PRESETS:
        got = find_preset(p.id)
        assert got is not None and got.id == p.id, p.id
        # 同时**不能**把忠实副本挤掉
        assert find_preset("openai") is not None
