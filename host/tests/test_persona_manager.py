# -*- coding: utf-8 -*-
"""判据：`context.persona_manager` 桥（宿主把人设库交给插件的那一层）。

🔴 本文件守的**两个**风险，一个是功能、一个是**数据安全**：

## ① 功能：插件读不到人格 → 面板那句**误诊**报错

情绪 2026-09-21 报障（实测复现，`POST .../page/roleplay/draft_from_persona`）：

    {"error":"还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置"}

根因**不是**"没触发过对话"，而是 compat `Context` 完全没有 `persona_manager`
属性。完整链路（每步都有 file:line）：

    page_api._roleplay_persona_prompt_for_id        (page_api.py:14799)
      └─ plugin._refresh_default_persona_prompt("") (daily_state.py:11073)
           └─ manager = getattr(context, "persona_manager", None)   → None
                └─ getter = get_default_persona_v3 if manager else None → None
                     └─ return cached or _get_default_persona_prompt(umo)
                          └─ DEFAULT_PERSONA_PROMPT_FALLBACK            (daily_state.py:163)
      └─ prompt.startswith("未读取到 AstrBot 默认人格") → 报错      (page_api.py:14849)

⚠️ **那句文案会把排查带偏**：它让用户"让 Bot 触发一次对话"，但
   `_refresh_default_persona_prompt` 的两个缓存写入口（`daily_state.py:11113`
   /`:11153`）都在 `manager` 分支**之后** → `manager is None` 时**永远不写缓存**
   → 触发了也修不好，而且**一行日志都没有**。

## ② 🔴🔴 数据安全：本桥会**新解锁**插件一条删文件的路径

插件有一处对账（`main.py:3430`，启动 `main.py:8751` 与 `page_api.py:14426` 触发）
把我们的全量人格 ID 当**权威集合**，把"插件档案里有、我们这里没有"的人格
判为已删除 → `_retire_deleted_persona_store_sync()` **删 `.db`/`.json`（+WAL/SHM）**。

实测本机推演（`primary=""`，因为 `plugin_specific_persona_id` 为空）：

    candidates   = {"default"}     ← data/plugins/…/persona_profiles/default.db
    official_ids = {"糯米茨"}       ← 桥报出去的人设名
    → stale = ["default"] → **会删掉 default.db**

而 `default.db` 里是**用户真实数据**（实测 150 段 / 163 KB：`persona_settings`、
衣柜 `WARDROBE_*`、`unified_person`、`_req041_private_memory`、
`primary_store_ownership` …）。

⚠️ **加桥之前这条路走不到**（`persona_manager is None` → 对账直接跳过），
   所以 `test_publishing_persona_list_keeps_builtin_default` 这条判据是
   **给"我的修复"上闸**，不是给老代码上闸。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


class _FakePersona:
    """领域层 `Persona` 的最小替身（只要有那几个字段就能被组装）。

    ⚠️ **必须带上 `enabled_sections()`**：领域层组装器
       （`domain/prompt.py:171`）会调它 —— 少了它组装抛 AttributeError，
       而 `_persona_prompt_text` 会**兜底回落**到手搓拼接
       → 判据"prompt 是否走组装器"就会**假绿**。
       **通则：替身要长齐被测代码真正会调的方法**（本项目已踩过多次）。
    """

    def __init__(self, pid: str, name: str, description: str = "", personality: str = ""):
        self.id = pid
        self.name = name
        self.description = description
        self.personality = personality
        self.scenario = ""
        self.creator_notes = ""
        self.avatar_path = ""
        self.prompt_sections: list = []

    def enabled_sections(self) -> list:
        return []


class _FakeRepo:
    """领域层 `PersonaRepository` 的最小替身（只实现 `list()`）。"""

    def __init__(self, items=None, raises: Exception | None = None):
        self._items = items or []
        self._raises = raises

    async def list(self):
        if self._raises is not None:
            raise self._raises
        return list(self._items)


def _mgr(items=None, *, default_persona_id: str = "", raises=None):
    from astrbot_host.persona_manager import HostPersonaManager

    m = HostPersonaManager(
        repo=_FakeRepo(items, raises=raises),
        default_persona_id=default_persona_id,
    )
    return m


# ---------------------------------------------------------------------------
# ③ 装配闸门：Context 必须**真的有** persona_manager 属性
# ---------------------------------------------------------------------------


def test_context_exposes_persona_manager():
    """`Context` 必须暴露 `persona_manager`（含未注入时为 None）。

    🔴 这条守的是**根因本身**：我原来连属性都没有 → 插件每一处
       `getattr(context, "persona_manager", None)` 都拿 None，
       而且**全部静默**（`getattr` 带默认值），只有那句误诊文案露出来。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts

    ctx = Context(ports=HostPorts())
    assert hasattr(ctx, "persona_manager"), (
        "Context 没有 `persona_manager` 属性 —— 插件读不到人格的根因"
    )
    assert ctx.persona_manager is None

    sentinel = object()
    ctx2 = Context(ports=HostPorts(persona_manager=sentinel))
    assert ctx2.persona_manager is sentinel


def test_capabilities_report_persona_availability():
    """能力诊断里要**分开**报"有管理器"与"真有人格可用"。

    ⚠️ 照本项目已有的 `provider_manager`/`provider_usable` 做法：
       只报"有个对象"会让诊断看起来可用、实际插件拿到兜底文案。

    ⚠️ `capabilities()` 在 **`HostPorts`** 上（不是 `Context`）——
       与 `provider_usable` 等同一张表，别记错挂在哪一边。
    """
    from astrbot_host.compat.core.star.context import HostPorts

    empty = HostPorts().capabilities()
    assert empty["persona_manager"] is False
    assert empty["persona_usable"] is False

    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    got = HostPorts(persona_manager=m).capabilities()
    assert got["persona_manager"] is True
    assert got["persona_usable"] is True, "有 1 个人格时应报可用"

    # 「有管理器但一个人格都没有」必须与「可用」区分开
    barren = HostPorts(persona_manager=_mgr([])).capabilities()
    assert barren["persona_manager"] is True
    assert barren["persona_usable"] is False, (
        "空管理器被报成 usable 会让诊断说谎（插件实际拿兜底文案）"
    )


# ---------------------------------------------------------------------------
# ④ 数据形状：插件**两种读法**都要能过
# ---------------------------------------------------------------------------


def test_personas_v3_elements_are_dicts_with_personality_keys():
    """`personas_v3` 的元素必须是 **dict**，且含 `Personality` 的必需键。

    实测插件的两种读法（缺一种就有路径挂）：
      * `main.py:3354-3362` 先 `isinstance(item, dict)` → `.get("persona_id"/"name"/"id")`；
      * 真实现 `persona_mgr.py:59` 直接 `item["name"]` 下标访问。
    → 给 dict 才能**同时**满足。
    """
    m = _mgr([_FakePersona("p1", "糯米茨", description="水属性史莱姆")])
    asyncio.run(m.refresh())

    assert isinstance(m.personas_v3, list) and m.personas_v3, "personas_v3 不能为空"
    for item in m.personas_v3:
        assert isinstance(item, dict), f"元素必须是 dict，实际 {type(item)}"
        for key in ("name", "prompt", "begin_dialogs", "mood_imitation_dialogs"):
            assert key in item, f"缺 `Personality` 必需键：{key}"
        # 下标访问（真实现那条路）
        assert isinstance(item["name"], str) and item["name"]


def test_persona_prompt_uses_the_same_assembler_as_panel():
    """人设 prompt 必须走**同一个组装器**，不是另写一份拼接。

    ⚠️ 若这里手搓一份，"面板里看着是人设 A、插件拿到的是 A 的一部分"
       —— 同一语义两处实现，本项目的老坑。
       判据：组装器会产出 `【角色设定】` 这种分节标题（`domain/prompt.py:50`），
       手搓的简单拼接**不会**有这个特征（除非恰好也用了同款格式）。
    """
    m = _mgr([_FakePersona("p1", "甲", description="很长的角色设定内容")])
    asyncio.run(m.refresh())
    prompt = m.personas_v3[0]["prompt"]
    assert "很长的角色设定内容" in prompt, f"人设正文没进 prompt：{prompt!r}"
    assert "【角色设定】" in prompt, (
        "prompt 不像组装器的产物（缺分节标题）—— 可能有人另写了一份拼接"
    )


def test_nameless_persona_is_skipped_not_silently_kept():
    """没有名字的人设要**跳过**（并留日志），不能变成空串 id。

    🔴 为什么危险：插件的 `_sanitize_persona_id` 会把空名变成 `""`，
       而 `main.py:3354-3366` 一旦发现无效 id 就返回
       `f"{source_name}_item_invalid"` → **整个对账变成"不可验证"**
       → 等于白接（且完全没有可见症状）。
    """
    m = _mgr([_FakePersona("p1", ""), _FakePersona("p2", "有名字")])
    asyncio.run(m.refresh())
    names = [i["name"] for i in m.personas_v3]
    assert "" not in names, "空名字的人设不该进列表"
    assert "有名字" in names


# ---------------------------------------------------------------------------
# ⑤ 插件真的会调的成员（签名/返回值形状必须逐条对上）
# ---------------------------------------------------------------------------


def test_resolve_selected_persona_returns_four_tuple():
    """`resolve_selected_persona` **必须返回 4 元组**。

    🔴 两个调用方解包方式不同，少一个就挂一个：
       * `main.py:3526-3528` 按**下标**读 `[0]/[1]/[2]`（≥3 即可）；
       * `meme_manager/mixins/event_handlers.py:825` 按 **4 元解包**
         `(persona_id, _, _, _)` → 返回 3 元会 `ValueError`。
    """
    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id=None,
            platform_name="webchat",
            provider_settings={},
        )
    )
    assert isinstance(out, tuple), f"必须是 tuple，实际 {type(out)}"
    assert len(out) == 4, f"必须 4 元组（meme_manager 按 4 元解包），实际 {len(out)}"

    # 4 元解包（meme_manager 的读法）—— 这条会真的抛错，不是形式主义
    persona_id, persona, forced, special = out
    assert persona is not None, "有一个'甲'可用时不该解析出 None"


def test_resolve_selected_persona_honours_conversation_persona():
    """会话绑定的人格要**优先于**全局默认（照真实现 `persona_mgr.py:105-110`）。"""
    m = _mgr([_FakePersona("p1", "甲"), _FakePersona("p2", "乙")])
    asyncio.run(m.refresh())
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id="乙",
            platform_name="webchat",
            provider_settings={"default_personality": "甲"},
        )
    )
    assert out[0] == "乙", f"会话绑定的应胜出，实际 {out[0]!r}"
    assert (out[1] or {}).get("name") == "乙"


def test_resolve_selected_persona_keeps_explicit_none():
    """`"[%None]"` 是"这条会话**不要**人格"的哨兵，必须保留（真实现 `:107-110`）。"""
    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id="[%None]",
            platform_name="webchat",
            provider_settings={"default_personality": "甲"},
        )
    )
    assert out[0] == "[%None]", f"哨兵被覆盖了：{out[0]!r}"


def test_get_default_persona_v3_never_returns_none():
    """`get_default_persona_v3` **永不返回 None**（真实现兜底 `DEFAULT_PERSONALITY`）。

    🔴 返回 None 会让 `daily_state._extract_default_persona_prompt(None)`
       拿到空串 → 又走回那句"未读取到 AstrBot 默认人格"的误诊文案。
       这是**空仓库**下的关键路径（用户还没建人设时）。
    """
    empty = _mgr([])
    asyncio.run(empty.refresh())
    got = asyncio.run(empty.get_default_persona_v3())
    assert got is not None, "空仓库时也必须给兜底人格"
    assert str(got.get("prompt") or "").strip(), "兜底人格的 prompt 不能为空"
    assert got.get("name") == "default"

    # 三个人格三种调用姿势（实测插件三种都用）
    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    assert (asyncio.run(m.get_default_persona_v3()) or {}).get("name") == "甲"
    assert (asyncio.run(m.get_default_persona_v3("")) or {}).get("name") == "甲"
    assert (asyncio.run(m.get_default_persona_v3(umo="11:FriendMessage:1")) or {}).get("name") == "甲"


def test_default_persona_id_from_config_profile_is_honoured():
    """「设置 → 配置文件 → 会话 → 默认采用的人格」要**真的生效**。

    🔴 `config_profile.session.default_persona_id` 之前是**死配置**
       （全仓 0 个消费方）。现在它是"插件认为哪个是默认人格"的来源。
    """
    m = _mgr(
        [_FakePersona("p1", "甲"), _FakePersona("p2", "乙")],
        default_persona_id="p2",
    )
    asyncio.run(m.refresh())
    assert (m.selected_default_persona_v3 or {}).get("name") == "乙", (
        "default_persona_id 指了 p2，默认人格却不是乙"
    )


def test_get_persona_raises_for_unknown_id():
    """`get_persona` 查不到要**抛 ValueError**（真实现 `persona_mgr.py:44`）。

    🔴 抛错是**预期路径**：`daily_state.py:11104-11133` 专门为它写了一串
       `except`（含 CancelledError/sqlite3/TimeoutError）→ 记 warning 后
       **继续**去试 `get_default_persona_v3()`。静默返回 None 会绕过那条降级链。
    """
    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    got = asyncio.run(m.get_persona("甲"))
    assert (got or {}).get("name") == "甲"
    with pytest.raises(ValueError):
        asyncio.run(m.get_persona("不存在的人"))


def test_get_persona_v3_by_id_is_sync():
    """`get_persona_v3_by_id` 必须是**同步**的（`main.py:3310-3313` 不 await）。

    ⚠️ 做成 async 会让 `getter(pid) is not None` 判到**协程对象**（恒真）
       → `_astrbot_persona_exists()` 对**任何** id 都返回 True（静默谎言）。
    """
    import inspect

    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    assert not inspect.iscoroutinefunction(m.get_persona_v3_by_id), (
        "get_persona_v3_by_id 不能是 async —— 插件同步调用它"
    )
    assert (m.get_persona_v3_by_id("甲") or {}).get("name") == "甲"
    assert m.get_persona_v3_by_id("没有这个") is None
    assert m.get_persona_v3_by_id("") is None


def test_resolve_falls_back_to_our_default_persona():
    """没有会话人格、也没有 `provider_settings.default_personality` 时，
    **要回落到我们自己的默认人设**（这是有意偏离真实现的一处）。

    🔴 为什么必须偏离（这就是情绪报障的原话「陪伴那边获取不到平台的默认人设」）：
       真实现 `persona_mgr.py:105-115` 在这种情况下 `persona = None`
       → 插件在 `_validate_proactive_persona_delivery` 里判
       `astrbot_persona_unresolved` 而早退。照抄等于把诉求原样退回去。

    ⚠️ 这条判据守的是"**有数据时必须给出数据**"：
       我们库里明明有人设，插件不该拿到 None。
    """
    m = _mgr([_FakePersona("p1", "甲")])
    asyncio.run(m.refresh())
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id=None,
            platform_name="webchat",
            provider_settings={},        # ← 没有 default_personality
        )
    )
    assert out[1] is not None, "我们有人设，不该解析出 None"
    assert (out[1] or {}).get("name") == "甲", f"应回落到我们的默认人设，实际 {out[1]}"
    assert out[3] is False, "有真人格时不该走 webchat 专用默认（那等于没用上人设）"


def test_resolve_uses_synthetic_default_when_load_succeeded_empty():
    """**读成功但一条人设都没有** → 解析出内置 `default`（照真实现行为）。

    🔴 我第一版判据在这里断言 `_chatui_default_`，**是判据错了、代码对的**
       （本项目的老规矩：断言失败先查清哪个值才是对的，别反射性改代码）：
       真 AstrBot 的 `personas_v3` **永不为空** ——
       `persona_mgr.py:416-418` 在没人格时会 `append(DEFAULT_PERSONALITY)`，
       所以它按 `["name"]` 一找就能找到 `default`，
       **根本走不到** `_chatui_default_` 那条分支。

       → 我们补了合成 `default` 之后行为与真实现一致：这里给 `default` 才对。
    """
    m = _mgr([])
    asyncio.run(m.refresh())
    assert m.initialized is True
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id=None,
            platform_name="webchat",
            provider_settings={},
        )
    )
    assert out[0] == "default", f"实际 {out[0]!r}"
    assert (out[1] or {}).get("name") == "default"
    assert out[3] is False, "有可解析的 default 时不该标 webchat 专用默认"


def test_resolve_uses_webchat_default_only_when_truly_nothing():
    """**真的什么都没有**（人设库读失败/没仓库）才走 webchat 专用默认。

    ⚠️ 与上一条配对，两条一起才说明这条分支是**有条件**的：
       * 读成功（哪怕空）→ 有合成 `default` → 正常解析；
       * 读**失败** → 列表真空 → 才落到 `_chatui_default_` + `special=True`。

    🔴 这时 `persona is None`，插件会判 `exists: False`
       （`main.py:3536`）→ 如实降级。**这是对的**：我们确实读不到，
       不能为了让数字好看而伪造一个人格（作者判据③）。
    """
    m = _mgr(raises=RuntimeError("模拟人设库坏了"))
    asyncio.run(m.refresh())
    out = asyncio.run(
        m.resolve_selected_persona(
            umo="11:FriendMessage:1",
            conversation_persona_id=None,
            platform_name="webchat",
            provider_settings={},
        )
    )
    assert out[0] == "_chatui_default_", f"实际 {out[0]!r}"
    assert out[1] is None, "读失败时不该伪造人格对象"
    assert out[3] is True, "应如实标记用了 webchat 专用默认"


def test_synthetic_default_never_wins_over_a_real_persona():
    """补进列表的合成 `default` **不能**抢走默认人格（实测踩到的真 bug）。

    🔴 我第一版照真实现"按名字找 `default`"来挑默认人格，
       而我们为了让插件别误删 `default.db` 又**补**了一个合成 `default`
       （见 `test_publishing_persona_list_keeps_builtin_default`）——
       两者一撞，合成的空壳**永远胜出**：
       用户在面板上建的「糯米茨」永远当不上默认人格，插件拿到的是
       `You are a helpful and friendly assistant.`
       → 功能"通了"但用的是**假人格**，比报错更糟（静默错）。
    """
    m = _mgr([_FakePersona("p1", "糯米茨")])
    asyncio.run(m.refresh())
    default = asyncio.run(m.get_default_persona_v3()) or {}
    assert default.get("name") == "糯米茨", (
        f"合成 default 抢走了默认人格（拿到 {default.get('name')!r}）—— "
        "用户建的人设永远用不上"
    )
    # 但合成 default 仍要在列表里（那是防误删用的）
    assert "default" in [str(i.get("name") or "") for i in m.personas_v3]


# ---------------------------------------------------------------------------
# ⑥ 🔴🔴 数据安全：发布人格列表**不能**让插件删掉自己的档案
# ---------------------------------------------------------------------------


def test_publishing_persona_list_keeps_builtin_default():
    """🔴🔴 **本文件最重要的一条**：`personas_v3` 里必须**总有** `default`。

    理由（实测推演，不是理论）—— 见本文件模块头 §②：

        primary      = ""            ← plugin_specific_persona_id 为空，保护不了任何人
        candidates   = {"default"}   ← persona_profiles/default.db（**150 段真实数据**）
        official_ids = {"糯米茨"}     ← 桥报出去的名字
        → stale = ["default"] → 插件会**删掉 default.db**

    ✅ 真 AstrBot 上不会发生，因为它的 `personas_v3` **永远含 `default`**
       （`persona_mgr.py:416-418` 在没人格时会 `append(DEFAULT_PERSONALITY)`）。
       我们补上它就与真实现一致，同时天然堵住这个误删。

    ⚠️ 这条判据是**给我自己的修复上闸**：加桥之前对账根本跑不到
       （`persona_manager is None` → 直接跳过），所以它守的是"别修出新事故"。
    """
    m = _mgr([_FakePersona("p1", "糯米茨")])
    asyncio.run(m.refresh())
    names = [str(i.get("name") or "") for i in m.personas_v3]
    assert "default" in names, (
        "personas_v3 缺内置 `default` → 插件的「已删除人格对账」会把 "
        "persona_profiles/default.db 判为 stale 并**删掉它**（150 段用户数据）"
    )

    # 空仓库时同样要有（这是最容易漏的一格：没有人设 ≠ 没有 default）
    empty = _mgr([])
    asyncio.run(empty.refresh())
    assert "default" in [str(i.get("name") or "") for i in empty.personas_v3], (
        "一个我们的人设都没有时，插件会只看到空列表 → 同样会删 default.db"
    )


def test_reconcile_simulation_never_marks_default_stale():
    """把插件的 stale 算法**照着抄一遍**，证明它现在判不出 stale。

    🔴 为什么复制算法而不是只断言"有 default"：
       上面那条判据验的是**我们给了什么**；这条验的是**插件拿它会算出什么**
       —— 中间少一个环节（比如名字大小写、sanitize）就漏了。
       算法照抄 `main.py:3430-3443`（只读，不改生产代码）。
    """
    m = _mgr([_FakePersona("p1", "糯米茨")])
    asyncio.run(m.refresh())

    # ↓↓↓ 照抄 main.py:3434-3443
    official_ids = {
        (item.get("persona_id") or item.get("name") or item.get("id"))
        for item in m.personas_v3
    }
    primary = ""                       # 实测：plugin_specific_persona_id 为空
    candidates = {"default"}           # 实测：persona_profiles/ 下只有 default.db
    stale = sorted(
        pid for pid in candidates if pid and pid != primary and pid not in official_ids
    )
    # ↑↑↑ 照抄结束

    assert stale == [], (
        f"插件会把 {stale} 判为已删除并删掉它们的档案（default.db 是用户数据）"
    )


# ---------------------------------------------------------------------------
# ⑦ 如实降级：repo 坏了/缺席时**不能**假装"没人格"
# ---------------------------------------------------------------------------


def test_repo_failure_keeps_uninitialized_so_no_data_loss():
    """读人设**失败**时要保持"未初始化"，而不是报"空列表"。

    🔴 这是数据安全的**第二道闸**：
       `main.py:3341-3347` 用「`personas` 空 + 两个 `selected_default_*` 为 None」
       判"未初始化"→ **放弃对账**。
       若读失败时我们报"空列表但已初始化"，插件就会认为"一条人格都没有"
       → 删掉所有非主人格档案。**读失败 ≠ 一条都没有**。
    """
    m = _mgr(raises=RuntimeError("模拟数据库坏了"))
    asyncio.run(m.refresh())

    assert m.personas_v3 == [], "读失败时列表应为空"
    assert m.selected_default_persona is None, (
        "读失败时 `selected_default_persona` 必须是 None —— "
        "它是插件的「未初始化」判据，填了会让对账以为「权威空集」并删档案"
    )
    assert m.selected_default_persona_v3 is None
    assert m.initialized is False, "读失败时不能标记为已初始化"


def test_absent_repo_is_reported_not_faked():
    """没有人设仓库时：**如实**未初始化，绝不凭空造一个人格。"""
    from astrbot_host.persona_manager import HostPersonaManager

    m = HostPersonaManager(repo=None)
    asyncio.run(m.refresh())
    assert m.initialized is False
    assert m.personas_v3 == []
    assert m.selected_default_persona_v3 is None, "不许伪造默认人格（伪造能力）"
    # 仍要能给出兜底 prompt（否则插件面板又报那句误诊文案）
    assert (asyncio.run(m.get_default_persona_v3()) or {}).get("name") == "default"


def test_successful_empty_load_is_initialized():
    """**读成功但一条人设都没有** → 算"已初始化"（空集是**可信**的答案）。

    ⚠️ 这与上一条的区别很重要，别把两者混成一个：
       * 读**失败** → 不可信 → 不初始化（保护档案）；
       * 读**成功但空** → 可信 → 初始化（插件有权知道"真的没人格"）。
       本判据把这个边界**显式钉住**，免得将来有人"顺手统一"成一个分支。
    """
    m = _mgr([])
    asyncio.run(m.refresh())
    assert m.initialized is True, "读成功（空）应算已初始化"
    assert m.selected_default_persona_v3 is not None, (
        "已初始化时要有默认（至少是内置 default）—— 否则插件判 not_initialized"
    )


# ---------------------------------------------------------------------------
# ⑧ 装配：宿主服务必须真的把 repo 接上（"模块能工作 ≠ 被接上了"）
# ---------------------------------------------------------------------------


def test_plugin_host_service_passes_persona_manager_to_ports():
    """`PluginHostService.start()` 必须把 `persona_manager=` 传进 `HostPorts`。

    🔴 本项目最贵的一类 bug 就是"模块写好了但没绑线"
       （`tests/test_assembly.py` 的由来）。用源码断言守住这条线：
       光有 `HostPersonaManager` 类而没接上，插件照样读不到人格。
    """
    import inspect

    from emotion_app.web import plugin_host as ph

    src = inspect.getsource(ph.PluginHostService.start)
    assert "persona_manager=persona_manager" in src, (
        "start() 没把 persona_manager 传进 HostPorts —— 桥白写了"
    )
    assert "_build_persona_manager" in src, "start() 没调用装配方法"

    # 装配方法本身：必须把 repo 交给 HostPersonaManager，并从 config_profile 取默认人格
    build_src = inspect.getsource(ph.PluginHostService._build_persona_manager)
    assert "HostPersonaManager(" in build_src and "repo=self.personas" in build_src, (
        "`_build_persona_manager` 没把人设仓库交给桥"
    )
    assert "_default_persona_id" in build_src, (
        "没读 `default_persona_id` → 面板上「默认采用的人格」仍是死配置"
    )


def test_app_assembly_injects_personas_repo():
    """装配层必须把 `EMOTION_PERSONAS`（人设仓库）注进宿主服务。"""
    import inspect

    from emotion_app.web import app as app_mod

    src = inspect.getsource(app_mod._mount_plugin_routes)
    assert "service.personas" in src, (
        "`_mount_plugin_routes` 没注入 `service.personas` → "
        "`_build_persona_manager` 拿到 None → 插件读不到人格"
    )
    assert "EMOTION_PERSONAS" in src
