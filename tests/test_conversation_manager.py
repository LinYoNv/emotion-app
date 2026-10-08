# -*- coding: utf-8 -*-
"""对话历史端口（`HostPorts.conversation_manager`）判据。

## 守什么（情绪 2026-09-27 明确要求修的那条）

真机日志里插件**每一轮主动消息**都在报：

    [主动消息] 主动消息主链生成失败: 宿主未接入会话历史能力
    [主动消息] 写入会话历史失败: 同上

后果：消息发得出去，但插件**存不下自己刚说过的话** ⇒ 下一轮读不到上文
⇒ **可能重复话题**。

根因**不是"忘了注入"**，而是 `ConversationManagerPort` 的**契约写错了**
（原来只有 `get_history`/`append`，而插件真正调的是真实现那 5 个方法）。
所以本文件的判据分三层：

  ① **契约层**：Protocol 的方法集必须与真实现一致（写错 = 谁也没法实现）
  ② **语义层**：5 个方法的行为照真实现（尤其中间那三个易错形状）
  ③ **装配层**：`start()` 真的把它传进 `HostPorts` 了（源码级，能失败）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# 🔴 `astrbot_host` 是 `host/` 下的顶层包 —— 主项目测试要显式加 path
#    （口径照 `tests/test_plugin_activation_api.py:32`）。
for _p in (str(ROOT), str(ROOT / "host")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ===========================================================================
# ① 契约层：Protocol 必须是真实现那 5 个方法
# ===========================================================================

REAL_METHODS = (
    "get_curr_conversation_id",
    "get_conversation",
    "new_conversation",
    "update_conversation",
    "add_message_pair",
)


def test_port_protocol_matches_real_implementation():
    """🔴 Protocol 的方法集 == 真实现 `ConversationManager` 的方法集。

    这条守的是**最初那个缺口**：契约里写了 `get_history`/`append`
    （插件**从来不调**，全库 0 调用点），而真实现的 5 个方法一个都没有
    ⇒ **谁也没法实现这个端口** ⇒ 它永远是 None。

    ⚠️ 断言写成"真实现那 5 个必须在" **且** "猜出来的那两个必须不在"：
       只断言前者的话，把猜错的方法留着也能过（那正是当初的状态）。
    """
    src = (ROOT / "host" / "astrbot_host" / "compat" / "core" / "star"
           / "context.py").read_text(encoding="utf-8")
    block = src.split("class ConversationManagerPort", 1)[1]
    block = block.split("class LLMPort", 1)[0]

    for name in REAL_METHODS:
        assert f"def {name}(" in block, (
            f"`ConversationManagerPort` 缺 `{name}()` —— "
            "插件的真实调用点会落到 __getattr__ / AttributeError"
        )
    for wrong in ("def get_history(", "def append("):
        assert wrong not in block, (
            f"`ConversationManagerPort` 还留着 `{wrong.strip()}` —— "
            "那是**猜**出来的方法（全库 0 调用点），留着会误导下一个人"
        )


def test_port_documents_the_three_easy_to_get_wrong_shapes():
    """🔴 三个**易错形状**必须写在契约的 docstring 里（照真实现）。

    不写下来，下一个人就会重犯（我就是这么犯的）：
      * `history` 是 **JSON 字符串**不是 list；
      * `new_conversation` 第一个位置参数是 `platform_id`（不是 title）；
      * `add_message_pair` 对不存在的 cid **要抛异常**。
    """
    src = (ROOT / "host" / "astrbot_host" / "compat" / "core" / "star"
           / "context.py").read_text(encoding="utf-8")
    block = src.split("class ConversationManagerPort", 1)[1].split("class LLMPort", 1)[0]

    assert "JSON 字符串" in block, "没写清 `history` 是 JSON 字符串（插件按 str 读它）"
    assert "platform_id" in block, "没写清 `new_conversation` 第一个位置参数是 platform_id"
    assert "not found" in block, "没写清 `add_message_pair` 对不存在的 cid 要抛异常"


# ===========================================================================
# ② 语义层：5 个方法的行为
# ===========================================================================


class _FakeStore:
    """最小的 store 替身（形状照 `StoreBackend.load_sections/save_sections`）。"""

    def __init__(self) -> None:
        self.sections: dict[str, dict] = {}
        self.save_count = 0

    async def load_sections(self, keys: list[str]) -> dict:
        return {k: self.sections.get(k) for k in keys}

    async def save_sections(self, sections: dict) -> None:
        self.save_count += 1
        self.sections.update(sections)


@pytest.fixture
def mgr():
    from emotion_app.domain.conversation_manager import StoreConversationManager

    return StoreConversationManager(_FakeStore())


UMO = "11:FriendMessage:default"


def test_get_curr_is_none_before_any_conversation(mgr):
    """没有对话时必须返回 `None`（**不许自动创建** —— 真实现也不创建）。"""
    assert asyncio.run(mgr.get_curr_conversation_id(UMO)) is None
    # 而且**真的没建**（不是"建了但没返回"）
    section = mgr._store.sections.get("plugin_conversations") or {}
    assert section == {}, f"不该创建任何对话，却写入了：{section}"


def test_new_conversation_sets_current_and_parses_platform(mgr):
    """`new_conversation` 返回 uuid、把该 umo 的当前对话切过去、并从 umo 解析平台。"""
    cid = asyncio.run(mgr.new_conversation(UMO))
    assert cid, "必须返回一个 cid"
    assert len(cid) == 36 and cid.count("-") == 4, f"cid 该是 uuid，得到 {cid!r}"
    assert asyncio.run(mgr.get_curr_conversation_id(UMO)) == cid

    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    assert conv is not None
    # 平台从 `11:FriendMessage:default` 的第一段解析
    assert conv.platform_id == "11", f"平台解析错了：{conv.platform_id!r}"
    assert conv.user_id == UMO
    assert conv.cid == cid


def test_platform_id_is_first_positional_arg(mgr):
    """🔴 `new_conversation` 的第一个**位置**参数必须是 `platform_id`。

    插件为此写了两层 `TypeError` 兜底
    （`proactive_message.py:4935-4944`：先试 `(umo, platform_id)`，
    失败再试 `(umo, title=…)`，再失败试 `(umo)`）——说明作者见过别人实现错。
    我们按真实现传位置参数，**不该让它走兜底**。
    """
    cid = asyncio.run(mgr.new_conversation(UMO, "webchat"))
    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    assert conv is not None
    assert conv.platform_id == "webchat", (
        "第一个位置参数没被当成 platform_id —— 插件的位置参数调用会被当成别的意思"
    )
    # 反向：`title` 走关键字，不该被当成 platform_id
    cid2 = asyncio.run(mgr.new_conversation(UMO, title="说话记录"))
    conv2 = asyncio.run(mgr.get_conversation(UMO, cid2))
    assert conv2 is not None
    assert conv2.title == "说话记录"
    assert conv2.platform_id == "11", f"title 串到 platform_id 了：{conv2.platform_id!r}"


def test_history_is_json_string_not_list(mgr):
    """🔴 `history` 必须是 **JSON 字符串**（真实现 `json.dumps`）。

    插件侧有一条 `isinstance(raw_history, str)` 的分支在读它
    （`proactive_message.py:18813`）——返回 list 会让那边**静默**当空历史
    （不报错、只是上一句白说，正是"最贵的那类 bug"）。
    """
    cid = asyncio.run(mgr.new_conversation(UMO))
    asyncio.run(mgr.add_message_pair(
        cid,
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
    ))
    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    assert conv is not None
    assert isinstance(conv.history, str), (
        f"history 是 {type(conv.history).__name__} —— 插件的 str 分支会把它当空历史"
    )
    parsed = json.loads(conv.history)
    assert parsed == [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
    ], f"history 内容不对：{parsed}"


def test_add_message_pair_raises_for_unknown_cid(mgr):
    """🔴 对话不存在时**必须抛**（照真实现），不能静默丢弃。

    静默丢弃会让插件的"存档成功"变成假象 —— 本项目最忌的那类 bug。
    """
    with pytest.raises(Exception) as ei:
        asyncio.run(mgr.add_message_pair("no-such-cid", {"role": "user"}, {"role": "assistant"}))
    assert "not found" in str(ei.value).lower(), f"报错信息没说清原因：{ei.value}"


def test_add_message_pair_accepts_model_dump_objects(mgr):
    """消息可以是 dict，也可以是带 `model_dump()` 的对象（真实现两种都收）。"""

    class Seg:
        def __init__(self, d): self._d = d
        def model_dump(self): return self._d

    cid = asyncio.run(mgr.new_conversation(UMO))
    asyncio.run(mgr.add_message_pair(
        cid, Seg({"role": "user", "content": "u"}), Seg({"role": "assistant", "content": "a"}),
    ))
    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    assert json.loads(conv.history) == [
        {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"},
    ]


def test_add_message_pair_rejects_garbage_instead_of_silently_empty(mgr):
    """收到不认识的消息类型 → **显式报错**（不静默塞空 dict）。"""
    cid = asyncio.run(mgr.new_conversation(UMO))
    with pytest.raises(TypeError):
        asyncio.run(mgr.add_message_pair(cid, object(), {"role": "assistant"}))


def test_update_conversation_replaces_history(mgr):
    """`update_conversation` 能整体替换 history（插件"只存上下文"那条路用它）。"""
    cid = asyncio.run(mgr.new_conversation(UMO))
    asyncio.run(mgr.update_conversation(UMO, cid, history=[{"role": "assistant", "content": "只有这一条"}]))
    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    assert json.loads(conv.history) == [{"role": "assistant", "content": "只有这一条"}]

    # 不传 conversation_id 时用"当前对话"（照真实现）
    asyncio.run(mgr.update_conversation(UMO, history=[{"role": "assistant", "content": "改当前"}]))
    conv2 = asyncio.run(mgr.get_conversation(UMO, cid))
    assert json.loads(conv2.history) == [{"role": "assistant", "content": "改当前"}]


def test_get_conversation_create_if_not_exists(mgr):
    """`create_if_not_exists=True` 时不存在就新建（照真实现）。"""
    assert asyncio.run(mgr.get_conversation(UMO, "nope")) is None

    conv = asyncio.run(mgr.get_conversation(UMO, "nope", True))
    assert conv is not None, "要求创建时却拿到 None"
    assert conv.cid != "nope", "不该把请求的假 cid 当成真的 cid"


def test_curr_id_is_none_when_record_vanished(mgr):
    """🔴 悬空引用要如实返回 `None`：`sel` 指向的对话记录没了 → 不返回那个 id。

    返回一个取不到的 id，插件会以为"有对话但读不出来"，比直接说"没有"
    更难排查（错误信息会撒谎那类）。
    """
    cid = asyncio.run(mgr.new_conversation(UMO))
    assert asyncio.run(mgr.get_curr_conversation_id(UMO)) == cid
    # 手工把对话记录删掉，只留 sel 指针
    section = mgr._store.sections["plugin_conversations"]
    section.pop(cid, None)

    assert asyncio.run(mgr.get_curr_conversation_id(UMO)) is None, (
        "对话记录已不存在，仍返回了那个 cid（悬空引用）"
    )


def test_corrupt_section_refuses_instead_of_wiping(mgr):
    """🔴 section 格式坏掉时**报错**，不能当空（当空会静默丢光全部历史）。"""
    mgr._store.sections["plugin_conversations"] = ["这不是 dict"]
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(mgr.get_curr_conversation_id(UMO))
    assert "格式错误" in str(ei.value)


def test_history_is_trimmed_not_unbounded(mgr):
    """历史超上限时**截断**（保留最近的），不能无限增长把库写爆。"""
    from emotion_app.domain.conversation_manager import MAX_MESSAGES

    cid = asyncio.run(mgr.new_conversation(UMO))
    for i in range(MAX_MESSAGES + 20):
        asyncio.run(mgr.add_message_pair(
            cid, {"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"},
        ))
    conv = asyncio.run(mgr.get_conversation(UMO, cid))
    parsed = json.loads(conv.history)
    assert len(parsed) <= MAX_MESSAGES, f"没截断，长度 {len(parsed)}"
    # 保留的必须是**最近**的（丢了最早的）
    assert parsed[-1]["content"].endswith(str(MAX_MESSAGES + 20 - 1)), (
        "截断方向错了 —— 应该丢最早的、留最近的"
    )


def test_conversations_are_isolated_by_umo(mgr):
    """不同 umo 的对话**互不串**（方案 B 下每个会话一个 umo）。"""
    a = "11:FriendMessage:sess-aaa"
    b = "11:FriendMessage:sess-bbb"
    ca = asyncio.run(mgr.new_conversation(a))
    cb = asyncio.run(mgr.new_conversation(b))
    assert ca != cb
    assert asyncio.run(mgr.get_curr_conversation_id(a)) == ca
    assert asyncio.run(mgr.get_curr_conversation_id(b)) == cb

    asyncio.run(mgr.add_message_pair(ca, {"role": "user", "content": "给A"}, {"role": "assistant", "content": "A"}))
    conv_b = asyncio.run(mgr.get_conversation(b, cb))
    assert json.loads(conv_b.history) == [], "A 的消息串到 B 的对话里了"


# ===========================================================================
# ③ 装配层：`start()` 真的传进去了
# ===========================================================================


def test_start_passes_conversation_manager_into_host_ports():
    """🔴 装配判据：`HostPorts(...)` 必须带 `conversation_manager=`。

    这条守的是**直接根因** —— 端口建得再好，`start()` 不传进去照样是 None
    （与 `llm=`/`delivery=` 那两条装配判据同款）。

    源码级断言（`start()` 要真加载 11 个插件，单测代价太大）；
    缺口的形态就是"少写一个关键字参数"，源码断言恰好精确覆盖且**能失败**。
    """
    src = (ROOT / "emotion_app" / "web" / "plugin_host.py").read_text(encoding="utf-8")
    assert "conversation_manager=conversation_manager," in src, (
        "`HostPorts(...)` 没有传 `conversation_manager=` —— "
        "插件会把「宿主未接入会话历史能力」继续刷下去"
    )
    assert "conversation_manager = self._build_conversation_manager()" in src, (
        "没有调用 `_build_conversation_manager()` 取端口"
    )


def test_app_injects_store_into_service():
    """🔴 装配判据：`create_app` 必须把 store 注入 service。

    缺了它 `_build_conversation_manager()` 会**如实降级**成 None
    （那是正确行为），但功能就没接上 —— 两层都要断言。
    """
    src = (ROOT / "emotion_app" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'service.store = app.config.get("EMOTION_STORE")' in src, (
        "`create_app` 没有把 store 注入 service —— 对话端口会降级成不接"
    )
    assert 'app.config["EMOTION_STORE"] = store' in src, (
        "`EMOTION_STORE` 根本没被放进 app.config"
    )


def test_service_degrades_honestly_without_store():
    """反向判据：**没有 store** 时必须返回 `None`（如实降级），不造假端口。"""
    from emotion_app.web.plugin_host import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)  # 不跑 __init__（省依赖）
    svc.store = None
    assert svc._build_conversation_manager() is None, (
        "没有 store 却造出了端口 —— 那会让插件以为能存档，实际写不进去"
    )


# ===========================================================================
# ④ 可观测性：启动时必须打能力自检
# ===========================================================================


def test_startup_logs_capability_selfcheck():
    """🔴 启动路径必须调用能力自检。

    守的是一个**元问题**：`HostPorts.capabilities()` 早就能如实列出"哪项没接"，
    但**生产启动路径从来没调用过它** ⇒ 这个"可观测"机制形同虚设。
    实证：`conversation_manager` 缺了好几轮才被从截图里翻出来。

    ⇒ 修完端口还不够，**要让下一个缺口能被立刻看见**。
    """
    src = (ROOT / "emotion_app" / "web" / "plugin_host.py").read_text(encoding="utf-8")
    assert "self._log_capability_selfcheck()" in src, (
        "启动路径没有调用能力自检 —— 下一个缺口又会藏起来"
    )
    assert "def _log_capability_selfcheck(self)" in src, "能力自检方法没实现"


def test_capability_selfcheck_logs_missing_items_by_name():
    """自检必须**逐条列出**缺失能力名（只给个数等于没给信息）。"""
    from emotion_app.web.plugin_host import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)
    # 造一个只缺 conversation_manager 的端口对象
    from astrbot_host.compat.core.star.context import HostPorts

    class _Ctx:
        _ports = HostPorts(llm=object(), delivery=object(), config={})

    class _Host:
        context = _Ctx()

    svc.host = _Host()
    # 直接调，靠 caplog 抓日志
    import logging

    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, rec):  # noqa: ANN001
            records.append(rec.getMessage())

    lg = logging.getLogger("emotion_app.plugins")
    h = _Cap()
    lg.addHandler(h)
    try:
        svc._log_capability_selfcheck()  # noqa: SLF001
    finally:
        lg.removeHandler(h)

    joined = " | ".join(records)
    assert "conversation_manager" in joined, (
        f"缺失的能力名没被打出来（只有个数没用）：{joined}"
    )
    assert "能力自检" in joined, f"没有自检日志：{joined}"


def test_capability_selfcheck_says_so_when_it_cannot_check():
    """拿不到 HostPorts 时要**如实说"没检查"**，不能假装检查过了。"""
    from emotion_app.web.plugin_host import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)

    class _Host:
        context = None

    svc.host = _Host()
    import logging

    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, rec):  # noqa: ANN001
            records.append(rec.getMessage())

    lg = logging.getLogger("emotion_app.plugins")
    h = _Cap()
    lg.addHandler(h)
    try:
        svc._log_capability_selfcheck()  # noqa: SLF001
    finally:
        lg.removeHandler(h)

    assert any("跳过" in r for r in records), (
        "拿不到端口时没说「跳过」，用户会以为检查过了：" + str(records)
    )
