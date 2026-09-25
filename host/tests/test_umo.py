# -*- coding: utf-8 -*-
"""P1-1 判据：**umo 约定定死** + 事件工厂产出正确的事件。

⭐ 这是插件世界与我们的用户之间的**唯一接口**：
   umo 是插件的数据主键（陪伴把人设/记忆/关系全挂在它上面），
   事件工厂是"用户在面板说了一句话"进入插件世界的入口。

设计原则（照项目铁律）：
  * 每条判据都能**失败**（下面每条都做过注入 bug 验证，见文件末尾注释）；
  * **不许静默**：解析失败要么 None 要么抛错，绝不返回"看起来正常"的假值。

⚠️ 跑法：`cd host && ../.venv/bin/python -m pytest -q`
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

#: 🔴 **生产实据**（只读抄录，不是猜的）。
#:
#: 来源：`/opt/astrbot/data/plugin_data/astrbot_plugin_private_companion/companions.json`
#: （AstrBot 生产目录，**只读、绝不写入**），用 json 解析后正则提取得到。
#: 本判据的作用 = 「我们的 umo 与生产数据**逐字同构**」。
PROD_QQ_PRIVATE_UMO = "11:FriendMessage:3412642082"
PROD_QQ_PRIVATE_UID = "3412642082"
PROD_QQ_GROUP_UMO = "11:GroupMessage:764630833"
PROD_QQ_GROUP_ID = "764630833"


# ---------------------------------------------------------------------------
# 1. umo 构造：与生产真实数据逐字一致
# ---------------------------------------------------------------------------


def test_build_umo_matches_production_data_verbatim():
    """⭐ 核心判据：`build_umo("3412642082")` **逐字**等于生产数据里的 umo。

    这是本任务存在的理由 —— 我们的 umo 必须与用户在真 AstrBot 里
    已经跑出来的格式一模一样，插件的用户数据才能对得上。
    """
    from astrbot_host.umo import build_umo

    assert build_umo(PROD_QQ_PRIVATE_UID) == PROD_QQ_PRIVATE_UMO


def test_build_umo_group_second_segment_is_group_message():
    """群聊 umo 的第二段必须是 `GroupMessage`（照生产群聊数据）。

    ⚠️ 第二段不是"装饰"：真实现 `is_private_chat()` 由它推导
       （`astr_message_event.py:255-257`），写错会让唤醒判定反过来。
    """
    from astrbot_host.umo import GROUP_MESSAGE, build_umo

    umo = build_umo(PROD_QQ_GROUP_ID, group=True)
    assert umo == PROD_QQ_GROUP_UMO
    assert umo.split(":")[1] == GROUP_MESSAGE == "GroupMessage"


def test_build_umo_empty_user_id_raises_not_silent():
    """空 user_id **必须抛错**，不许造出 `11:FriendMessage:` 这种有毒 umo。

    🔴 为什么这是判据而不是"防御性编程"：`11:FriendMessage:` 语法合法、
       真实现能正常解析成 `session_id=""` —— 插件会把用户数据写进一个
       "空用户"名下，而**全链路不报任何错**（静默数据分裂）。
    """
    from astrbot_host.umo import build_umo

    with pytest.raises(ValueError):
        build_umo("")
    with pytest.raises(ValueError):
        build_umo(None)  # type: ignore[arg-type]


def test_build_umo_does_not_mangle_user_id():
    """user_id **原样**进 umo：不 strip、不转义、不截断。

    ⚠️ 它是插件的数据主键，静默改一个字符 = 换库
       （项目通则「判断是否为空时可以 strip，存进去的必须是原文」）。
    """
    from astrbot_host.umo import build_umo, session_id_of

    weird = "  uid with spaces  "
    assert session_id_of(build_umo(weird)) == weird


# ---------------------------------------------------------------------------
# 2. 解析：复用真实现语义（maxsplit=2）
# ---------------------------------------------------------------------------


def test_parse_roundtrip():
    """构造 → 解析 往返能拿回原 user_id（私聊与群聊都要）。"""
    from astrbot_host.umo import FRIEND_MESSAGE, GROUP_MESSAGE, build_umo, parse_umo

    for uid, group, mt in (
        (PROD_QQ_PRIVATE_UID, False, FRIEND_MESSAGE),
        (PROD_QQ_GROUP_ID, True, GROUP_MESSAGE),
    ):
        parsed = parse_umo(build_umo(uid, group=group))
        assert parsed is not None
        assert parsed.session_id == uid
        assert parsed.message_type == mt
        assert parsed.platform_id == "11"


def test_parse_umo_session_id_with_colons_split_maxsplit_2():
    """🔴 `session_id` 含 `:` 时仍能正确解析（真实现是 `split(":", 2)`）。

    真源码 `core/platform/message_session.py:26`：
    `platform_id, message_type, session_id = session_str.split(":", 2)`
    —— **切 2 刀**，所以第三段可以再含冒号。

    ⚠️ 换成 `split(":")`（不带 maxsplit）会 **ValueError**（段数太多）；
       换成"多切后 join 前 N 段"之类会**静默错位**。
       本判据同时守住"能解析"与"值正确"。
    """
    from astrbot_host.umo import parse_umo, parse_umo_strict

    parsed = parse_umo_strict("11:FriendMessage:a:b:c")
    assert parsed.session_id == "a:b:c", f"第三段被切坏了：{parsed.session_id!r}"
    assert parsed.platform_id == "11"
    assert parsed.message_type == "FriendMessage"

    # 便利函数走同一条路
    parsed2 = parse_umo("11:FriendMessage:a:b:c")
    assert parsed2 is not None and parsed2.session_id == "a:b:c"

    # 真实形态之一：AstrBot 自带网页聊天的 session_id 带 `!`（同源数据里也有）
    webchat = "webchat:FriendMessage:webchat!astrbot!0f1e2d3c-4b5a-6978-8c9d-0e1f2a3b4c5d"
    assert parse_umo_strict(webchat).session_id == (
        "webchat!astrbot!0f1e2d3c-4b5a-6978-8c9d-0e1f2a3b4c5d"
    )

    # 陪伴插件把 `|hash|mood` 拼在 session_id 后面（实测生产数据形态）
    companion = "11:FriendMessage:3412642082|06a737fabfc93914|comfort"
    assert parse_umo_strict(companion).session_id == (
        "3412642082|06a737fabfc93914|comfort"
    )


def test_parse_reuses_real_message_session_from_str(monkeypatch):
    """🔴 **必须复用真实现的解析**：`parse_umo` 要真的走 `MessageSession.from_str`。

    判据形式：把 `MessageSession.from_str` 换成打桩，若 `parse_umo`
       自己写了一套 split（而不是调用真实现），打桩就**收不到调用** → 判据失败。

    ⚠️ 这条是"实现约束"判据，不是"行为"判据 —— 行为判据（上一组）
       在"另写一套等价 split"时**全都照样通过**，所以必须单独有一条
       把"不许另写一套"钉死（真实现哪天改了语义，我们才跟得上）。
    """
    from astrbot_host.compat.core.platform import message_session as ms_mod
    from astrbot_host.umo import parse_umo

    calls: list[str] = []
    real_from_str = ms_mod.MessageSession.from_str

    def spy(session_str: str):
        calls.append(session_str)
        return real_from_str(session_str)

    monkeypatch.setattr(ms_mod.MessageSession, "from_str", staticmethod(spy))
    parsed = parse_umo("11:FriendMessage:a:b:c")

    assert calls == ["11:FriendMessage:a:b:c"], (
        "parse_umo 没有走真实现的 MessageSession.from_str —— 它自己另写了一套解析"
    )
    assert parsed is not None and parsed.session_id == "a:b:c"


def test_parse_umo_invalid_input_is_none_never_fake_value():
    """🔴 非法输入**不静默**：`parse_umo` 返回 None，`.strict` 抛错。

    覆盖三类非法：段数不足、**段数过多**（不带 maxsplit 的实现会在这里翻车）、
    消息类型非法。另外确认**没有**返回"看起来正常的假值"。

    ⚠️ 特别注意"段数过多"这一条：`11:FriendMessage:a:b:c` 是**合法**的
       （第三段含冒号），所以判据里不能用它当非法输入 ——
       真正非法的是**前两段**就不完整的串。
    """
    from astrbot_host.umo import parse_umo, parse_umo_strict

    invalid = [
        "",                              # 空串
        "11",                            # 只有 1 段
        "11:FriendMessage",              # 只有 2 段（少 session_id）
        "11:NopeMessage:x",              # 消息类型非法
        "11:friendmessage:x",            # 大小写不符（MessageType 是精确匹配）
        "11::x",                         # 类型段为空
        "FriendMessage:11:x",            # 段序错（类型在第 1 段）→ 类型段是 "11" 非法
    ]
    for bad in invalid:
        got = parse_umo(bad)
        assert got is None, f"非法 umo {bad!r} 被解析成了 {got!r}（静默返回假值）"
        with pytest.raises(ValueError):
            parse_umo_strict(bad)

    # 非字符串同样不许"凑合"
    for bad_type in (None, 123, b"11:FriendMessage:x", ["11:FriendMessage:x"]):
        assert parse_umo(bad_type) is None  # type: ignore[arg-type]


def test_parse_umo_valid_never_returns_none():
    """反向判据：合法输入**必须**解析成功。

    ⚠️ 只有"非法→None"这一半的话，一个"永远返回 None"的实现也能全绿 ——
       所以必须配一条反向判据（项目通则：安全功能的测试要覆盖反向）。
    """
    from astrbot_host.umo import parse_umo

    for good in (
        PROD_QQ_PRIVATE_UMO,
        PROD_QQ_GROUP_UMO,
        "11:OtherMessage:x",
        "11:FriendMessage:a:b:c",
        "11:FriendMessage:",             # 语法合法（真实现也接受）——见下注
    ):
        assert parse_umo(good) is not None, f"合法 umo {good!r} 被判为非法"

    # ⚠️ 注意 `11:FriendMessage:` 的**有意差异**：
    #    `parse_umo` 照真实现接受它（真实现 from_str 不管 session_id 空不空），
    #    而 `build_umo` **拒绝造**它。两者不矛盾：
    #    "解析别人给的串"要宽容照抄真实现，"构造自己的键"要严格防数据分裂。


def test_session_id_of_helpers():
    """便利函数：非法 umo 返回 None（而不是 AttributeError 崩掉）。"""
    from astrbot_host.umo import (
        is_our_umo,
        is_private,
        parse_umo,
        session_id_of,
        user_id_of,
    )

    assert session_id_of(PROD_QQ_PRIVATE_UMO) == PROD_QQ_PRIVATE_UID
    assert user_id_of(PROD_QQ_PRIVATE_UMO) == PROD_QQ_PRIVATE_UID
    # 🔴 非法输入不能崩，要如实 None（调用方按"这条消息没有用户"处理）
    assert session_id_of("垃圾") is None
    assert session_id_of("11:FriendMessage:a:b:c") == "a:b:c"

    assert is_our_umo(PROD_QQ_PRIVATE_UMO) is True
    assert is_our_umo("webchat:FriendMessage:x") is False
    assert is_our_umo("垃圾") is False

    assert is_private(PROD_QQ_PRIVATE_UMO) is True
    assert is_private(PROD_QQ_GROUP_UMO) is False
    assert is_private("垃圾") is False

    assert parse_umo(PROD_QQ_PRIVATE_UMO) == (
        "11", "FriendMessage", PROD_QQ_PRIVATE_UID,
    )


# ---------------------------------------------------------------------------
# 3. 常量固化：格式不许漂移
# ---------------------------------------------------------------------------


def test_constants_are_frozen():
    """🔴 umo 三段 + 平台名/id 都是**常量**，值被钉死。

    ⚠️ 这条判据的意义：这些值是**数据兼容契约**（改 = 换库）。
       任何人（包括未来的我）顺手改一下 PLATFORM_ID，
       本判据立刻变红，逼着他先想清楚数据迁移。
    """
    from astrbot_host import umo as m

    assert m.PLATFORM_ID == "11"
    assert m.PLATFORM_NAME == "webchat"
    assert m.FRIEND_MESSAGE == "FriendMessage"
    assert m.GROUP_MESSAGE == "GroupMessage"
    assert m.OTHER_MESSAGE == "OtherMessage"
    # 类型名与实例 id **必须不同** —— 混用会让 get_platform_name/id 两处都错
    assert m.PLATFORM_NAME != m.PLATFORM_ID
    # 消息类型字面量必须与真实现枚举**同源**（不是抄来的字符串）
    from astrbot_host.compat.core.platform.message_type import MessageType

    assert m.FRIEND_MESSAGE == MessageType.FRIEND_MESSAGE.value
    assert m.GROUP_MESSAGE == MessageType.GROUP_MESSAGE.value
    assert m.OTHER_MESSAGE == MessageType.OTHER_MESSAGE.value
    assert set(m.MESSAGE_TYPES) == {mt.value for mt in MessageType}


def test_umo_format_is_defined_in_exactly_one_place():
    """🔴 umo 字面量只允许出现在 `umo.py`（+ compat 层）里。

    判据形式：扫描 `host/astrbot_host/**.py`，若**别处**出现了
       `FriendMessage:` / `GroupMessage:` 这种**手写 umo 片段** → 失败。

    为什么这条重要：宿主多处在用 umo（Web 层/派发层/未来的主动消息），
       一旦有人图省事就地 `f"11:FriendMessage:{uid}"`，
       格式就有了第二个定义源 —— 将来改一处漏一处，
       表现是"部分功能写进了孤儿会话"（**静默**数据分裂，
       项目里最贵的一类 bug）。把"只在一处定义"变成判据，才守得住。
    """
    pat = re.compile(r"""(FriendMessage|GroupMessage|OtherMessage)\s*:""")
    offenders: list[str] = []
    for path in sorted((HOST_DIR / "astrbot_host").rglob("*.py")):
        rel = path.relative_to(HOST_DIR).as_posix()
        if rel.startswith("astrbot_host/compat/") or rel == "astrbot_host/umo.py":
            continue  # compat 是真实现的迁移；umo.py 是唯一的权威定义处
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pat.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "umo 字面量出现在 umo.py 之外（格式有了第二个定义源）：\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 4. 事件工厂
# ---------------------------------------------------------------------------


def test_event_factory_sets_all_dispatch_relevant_fields():
    """⭐ 事件工厂产出的 event：所有派发链路会读的字段都正确。"""
    from astrbot_host.umo import build_inbound_event

    ev = build_inbound_event("你好", PROD_QQ_PRIVATE_UID)

    # --- umo（插件的主键，288 次引用） ---
    assert ev.unified_msg_origin == PROD_QQ_PRIVATE_UMO
    # --- 身份 ---
    assert ev.get_sender_id() == PROD_QQ_PRIVATE_UID
    assert ev.get_self_id() == "webchat"
    # 🔴 self_id 绝不能等于 sender_id（否则被插件当"自己发的"静默丢弃）
    assert ev.get_self_id() != ev.get_sender_id()
    # --- 会话类型：**与 umo 第二段一致** ---
    assert ev.is_private_chat() is True
    assert ev.get_session_id() == PROD_QQ_PRIVATE_UID
    # --- 平台名 / 实例 id（**两个不同的东西**） ---
    assert ev.get_platform_name() == "webchat"
    assert ev.get_platform_id() == "11"
    # --- 消息对象 ---
    obj = ev.message_obj
    assert obj is not None
    assert obj.session_id == PROD_QQ_PRIVATE_UID
    assert obj.self_id == "webchat"
    assert obj.sender.user_id == PROD_QQ_PRIVATE_UID
    assert obj.message_str == "你好"
    assert obj.message_id  # 非空（插件拿去重/引用）
    assert obj.group is None
    assert [c.text for c in obj.message] == ["你好"]
    # --- 文本 ---
    assert ev.message_str == "你好"


def test_event_factory_group_is_consistent_between_umo_and_message_obj():
    """🔴 群聊时 **umo 第二段** 与 **`message_obj.type`** 必须同为 GroupMessage。

    这是本任务点名的陷阱：真实现 `is_private_chat()` =
       `get_message_type() == FRIEND_MESSAGE`，而 `get_message_type()`
       （真 `:184-189`）**优先读 `message_obj.type`，缺失才退回 `session`**。
       两处不一致时，优先级更高的 `message_obj.type` 会盖掉 umo 的判定
       → 唤醒判定与实际会话类型**相反**（`dispatch.py:145-162` 据此判
       "私聊直接唤醒"）。

    ⚠️ 本判据**同时**查三处（umo 第二段 / `message_obj.type` /
       `is_private_chat()`），只查一处的话，另外两处写错也能通过。
    """
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.umo import build_inbound_event, parse_umo

    ev = build_inbound_event("群消息", PROD_QQ_GROUP_ID, group=True)

    assert parse_umo(ev.unified_msg_origin).message_type == "GroupMessage"
    assert ev.message_obj.type is MessageType.GROUP_MESSAGE
    assert ev.get_message_type() is MessageType.GROUP_MESSAGE
    assert ev.is_private_chat() is False, (
        "群聊事件被当成私聊 —— umo 第二段与 message_obj.type 至少有一处写错"
    )
    # 私聊一侧也要反向确认（防"恒 False"的实现假通过）
    ev_p = build_inbound_event("私聊", PROD_QQ_PRIVATE_UID)
    assert ev_p.message_obj.type is MessageType.FRIEND_MESSAGE
    assert ev_p.is_private_chat() is True


def test_event_factory_private_flag_follows_umo_not_constructor_arg():
    """🔴 `is_private_chat()` 由 **umo/session** 决定 —— 构造参数骗不过它。

    真实现 `:255-257` 只认 session/message_obj；compat 层
       `astr_message_event.py:241-251` 同理（session 优先）。
       本判据直接把 umo 改成群聊，确认判定**跟着 umo 走** ——
       这正是"umo 第二段必须与实际会话类型一致"的机制性证明。
    """
    from astrbot_host.umo import build_inbound_event

    ev = build_inbound_event("x", PROD_QQ_PRIVATE_UID)
    assert ev.is_private_chat() is True
    # 只改 umo（模拟"umo 第二段写错了"）→ 判定必须跟着变
    ev.unified_msg_origin = build_umo_group(PROD_QQ_PRIVATE_UID)
    assert ev.is_private_chat() is False, (
        "改了 umo 第二段但 is_private_chat() 没跟着变 —— 说明判定不是从 session 推导的"
    )


def build_umo_group(uid: str) -> str:
    """测试内联：造一个"第二段写错成群聊"的 umo（刻意不走 build_umo 的 group 参数）。"""
    return f"11:GroupMessage:{uid}"


def test_event_factory_private_fallback_agrees_with_umo():
    """🔴 连**退路值**都要与 umo 一致（session 丢失时也不能自相矛盾）。

    compat 的 `is_private_chat()`（`astr_message_event.py:241-251`）优先用
    session（由 umo 解析而来），**没有 session 时才退回构造参数 `_is_private`**。
    真实现那条退路不存在（它恒 `self.session.message_type`），
    但我们这条退路一旦与 umo 写反，就会出现"umo 说群聊、退路说私聊"的
    自相矛盾 → 一旦 umo 解析失败，唤醒判定就会静默反转。

    ⚠️ 没有这条判据的话，`is_private=not group` 写成 `is_private=True`
       在**所有其它用例里都不会被发现**（因为 session 一直在，永远走不到退路）
       —— 这是典型的"判据盲区"，所以必须显式把 session 摘掉再验一次。
    """
    from astrbot_host.umo import build_inbound_event

    for uid, group in ((PROD_QQ_PRIVATE_UID, False), (PROD_QQ_GROUP_ID, True)):
        ev = build_inbound_event("x", uid, group=group)
        expected = not group
        assert ev.is_private_chat() is expected           # 主路径（session）
        ev.session = None                                  # 摘掉 session → 走退路
        assert ev.is_private_chat() is expected, (
            f"退路值 `is_private` 与 umo 第二段不一致（group={group}）—— "
            "umo 一旦解析失败，唤醒判定就会静默反转"
        )


def test_event_factory_does_not_pre_wake():
    """🔴 工厂**不许**预设唤醒标志 —— 唤醒是 `dispatch.apply_wake` 的职责。

    真实现里唤醒发生在 `waking_check/stage.py`，事件对象出厂时
       `is_wake=False` / `is_at_or_wake_command=False`（真 `:53-55`）。
       工厂里预设 True 会让宿主的唤醒策略**永远生效不了**
       （表现：配了唤醒词的群聊依然句句触发）。
    """
    from astrbot_host.umo import build_inbound_event

    ev = build_inbound_event("你好", PROD_QQ_PRIVATE_UID)
    assert ev.is_wake is False
    assert ev.is_at_or_wake_command is False
    assert ev.is_stopped() is False


def test_event_factory_self_id_equal_user_id_is_rejected():
    """🔴 `self_id == user_id` 必须当场拒绝（否则消息被插件静默丢弃）。

    陪伴插件 `event_dispatch.py:983`：
    ```python
    if sender_id and self_id and sender_id == self_id:
        return False        # 判为"自己发的" → 整条消息丢弃
    ```
    这是**静默**的（不报错、没日志），所以要在工厂层就拦住。
    """
    from astrbot_host.umo import build_inbound_event

    with pytest.raises(ValueError):
        build_inbound_event("你好", PROD_QQ_PRIVATE_UID, self_id=PROD_QQ_PRIVATE_UID)


def test_event_factory_sets_platform_meta_with_split_name_and_id():
    """事件要带 `platform_meta`，且 **`name` 与 `id` 分离**。

    真实现事件上有 `platform_meta`（`astr_message_event.py:49`），
       插件会读它（`tts_tool_sanitizer.py:50-54` 用它判"是不是定时任务伪事件"）：
    ```python
    meta_name = platform_meta.name          # 期望 "webchat"，不能是 "cron"
    meta_description = platform_meta.description
    if meta_name == "cron" or meta_description == "cronjob": return True   # → 静默分支
    ```
    另外 `context.send_message` 按 `platform.meta().id == session.platform_name`
       **查表**投递（真 `core/star/context.py:641-643`）→ `id` 必须与 umo 第一段**字面相同**。
    """
    from astrbot_host.umo import PLATFORM_ID, PLATFORM_NAME, build_inbound_event

    ev = build_inbound_event("你好", PROD_QQ_PRIVATE_UID)
    meta = ev.platform_meta
    assert meta is not None
    assert meta.name == PLATFORM_NAME == "webchat"
    assert meta.id == PLATFORM_ID == "11"
    # ⚠️ description 绝不能含 cron 语义（会让插件把真实消息当定时任务）
    assert "cron" not in meta.description.lower()
    # 真实现的 back_compability 别名（`astr_message_event.py:103`）
    assert ev.platform is meta


def test_event_factory_sender_name_defaults_to_user_id():
    """昵称缺省回退成 user_id（照真实现 webchat：`MessageMember(username, username)`）。"""
    from astrbot_host.umo import build_inbound_event

    ev = build_inbound_event("你好", PROD_QQ_PRIVATE_UID)
    assert ev.get_sender_name() == PROD_QQ_PRIVATE_UID
    ev2 = build_inbound_event("你好", PROD_QQ_PRIVATE_UID, sender_name="小情绪")
    assert ev2.get_sender_name() == "小情绪"
    assert ev2.message_obj.sender.nickname == "小情绪"


# ---------------------------------------------------------------------------
# 5. 端到端：工厂产出的事件真的能被派发（不是"造出来好看"）
# ---------------------------------------------------------------------------


def test_event_factory_private_event_wakes_and_dispatches():
    """⭐ 端到端：工厂造的私聊事件 → `dispatch` → **被唤醒 + 命中指令 + 产出回复**。

    ⚠️ 这条判据的价值：前面所有字段判据都是"零件检查"，
       零件对不代表能跑（项目踩过"模块能工作 ≠ 被接上了"）。
       这里用真派发器跑一遍，确认 umo 第二段 → is_private_chat() →
       `apply_wake` 第三分支（私聊无条件唤醒）这条链真的通。

    注册表的隔离：handler 由 `probe_plugin` 式的临时插件提供太重，
       这里直接给注册表塞一个**真 handler 元数据**，跑完清掉。
    """
    import asyncio

    from astrbot_host.dispatch import dispatch
    from astrbot_host.registry import (
        EventType,
        StarHandlerMetadata,
        star_handlers_registry,
    )
    from astrbot_host.umo import build_inbound_event

    calls: list[str] = []

    async def probe_handler(event):
        calls.append(event.message_str)
        yield event.plain_result("插件答复")

    meta = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name="umo_probe_probe_handler",
        handler_name="probe_handler",
        handler_module_path="umo_probe",
        handler=probe_handler,
        # ⚠️ 空过滤器 → 真实现里**永不执行**（`waking_check/stage.py:182-183`）。
        #    所以给一个恒真过滤器，模拟 `@filter.command` 之外的最简形态。
        event_filters=[_AlwaysPassFilter()],
    )
    star_handlers_registry.clear()
    try:
        # ⚠️ 必须走 `append()`（内部维护 `star_handlers_map` 并按 priority 排序），
        #    直接塞 `_handlers` 会绕过登记 → 派发器看不到它（实测踩到）。
        star_handlers_registry.append(meta)

        ev = build_inbound_event("在吗", PROD_QQ_PRIVATE_UID)
        res = asyncio.run(dispatch(ev))
    finally:
        star_handlers_registry.clear()

    assert res.is_wake is True, "私聊事件没被唤醒 —— umo 第二段/is_private_chat 链路断了"
    assert calls == ["在吗"], f"handler 没被执行：{calls}"
    assert res.emitted, "派发没有产出任何回复"
    assert str(res.emitted[-1]) == "插件答复"


def test_event_factory_group_event_is_not_woken_without_prefix():
    """反向：群聊事件在**没有唤醒词**时**不该**被唤醒（照真实现第三分支的 `is_private` 前提）。

    ⚠️ 没有这条，"永远返回 is_wake=True"的假实现也能通过上一条。
    """
    import asyncio

    from astrbot_host.dispatch import apply_wake
    from astrbot_host.umo import build_inbound_event

    ev = build_inbound_event("普通群聊消息", PROD_QQ_GROUP_ID, group=True)
    woke = apply_wake(ev, [])

    assert woke is False, "群聊无唤醒词却被唤醒 —— is_private_chat() 判定错了"
    assert ev.is_wake is False
    # 真实现 `:247-248`：没唤醒 → 直接终止事件
    assert ev.is_stopped() is True

    # 反向：同一条文本在私聊下必须被唤醒
    ev_p = build_inbound_event("普通群聊消息", PROD_QQ_PRIVATE_UID)
    assert apply_wake(ev_p, []) is True


class _AlwaysPassFilter:
    """恒真过滤器（真实现要求 handler 至少有一个 filter，否则永不执行）。"""

    def filter(self, event, cfg=None) -> bool:  # noqa: ANN001
        return True


# ---------------------------------------------------------------------------
# 6. 注入 bug 验证记录（不是自动化判据，是给未来的人看的证据）
# ---------------------------------------------------------------------------
#
# 每个注入都在**改坏实现后跑全量**，确认对应用例变红、再改回来（详见交付报告）：
#
# | 注入的 bug | 应挂的判据 | 实测 |
# |---|---|---|
# | `PLATFORM_ID = "12"` | test_build_umo_matches_production_data_verbatim | ✅ 红 |
# | `build_umo(group=True)` 恒返回 FriendMessage | test_build_umo_group_second_segment… | ✅ 红 |
# | `build_umo` 空 user_id 返回 f-string（不抛） | test_build_umo_empty_user_id_raises… | ✅ 红 |
# | `build_umo` 里 `user_id.strip()` | test_build_umo_does_not_mangle_user_id | ✅ 红 |
# | `parse_umo` 用 `split(":")`（丢 maxsplit） | test_parse_umo_session_id_with_colons… | ✅ 红 |
# | `parse_umo` 失败返回 `Umo("","","")` | test_parse_umo_invalid_input_is_none… | ✅ 红 |
# | `parse_umo` 恒返回 None | test_parse_umo_valid_never_returns_none | ✅ 红 |
# | `parse_umo` 自己写 split（不走真实现） | test_parse_reuses_real_message_session… | ✅ 红 |
# | 别处手写 `f"11:FriendMessage:{uid}"` | test_umo_format_is_defined_in_exactly_one… | ✅ 红 |
# | 工厂里 `message_obj.type` 写死 FRIEND_MESSAGE | test_event_factory_group_is_consistent… | ✅ 红 |
# | 工厂里预设 `is_wake=True` | test_event_factory_does_not_pre_wake | ✅ 红 |
# | 工厂去掉 self_id==user_id 校验 | test_event_factory_self_id_equal_user_id… | ✅ 红 |
# | 工厂 `platform_id` 传成 PLATFORM_NAME | test_event_factory_sets_all… / …platform_meta… | ✅ 红 |
# | 工厂把 sender 留空 | test_event_factory_sets_all… | ✅ 红 |
# | 工厂不设 `platform_meta` | test_event_factory_sets_platform_meta… | ✅ 红 |
