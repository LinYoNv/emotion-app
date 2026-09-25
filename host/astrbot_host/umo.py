# -*- coding: utf-8 -*-
"""umo（`unified_msg_origin`）约定的**权威模块** + 入站事件工厂（P1-1）。

⭐⭐ 为什么需要这个模块（而不是各处自己拼字符串）：

    umo 是插件的**主键** —— 陪伴插件把用户数据（人设/记忆/关系/日程）
    全部挂在 `unified_msg_origin` 上。**格式一改 = 换库 = 老用户数据全部对不上。**
    所以这套约定必须**只有一处定义**，宿主其它部分（Web 层 / 派发层 /
    未来的主动消息）一律 import 它，绝不允许就地 `f"{plat}:FriendMessage:{uid}"`。

## 约定（情绪 2026-09-20 拍板，**不要改**）

```
<platform_id> : <message_type> : <session_id>
      ↓                ↓                ↓
     "11"        FriendMessage      我们的用户 ID
                 GroupMessage
```

真实生产数据（`/opt/astrbot/data/plugin_data/astrbot_plugin_private_companion/
companions.json`，实测读出，**只读**）：

```
11:FriendMessage:3412642082
11:GroupMessage:764630833
webchat:FriendMessage:webchat!astrbot!<uuid>
```

→ 我们的 umo 就是 `11:FriendMessage:<我们的用户ID>`，**与第一行逐字同构**。
   第一段 `11` 在真 AstrBot 里是「用户给 aiocqhttp 平台实例起的名字」
   （`PlatformMetadata.id`，真实现 `core/platform/astr_message_event.py:138`
   `get_platform_id()` → `platform_meta.id`），**不是平台类型名**。
   平台类型名（`webchat`）是另一回事，见 `PLATFORM_NAME`。

## 🔴 解析语义：**必须复用真实现**，不许另写一套

真 AstrBot `core/platform/message_session.py`（v4.27.5，全 30 行）：

```python
@staticmethod
def from_str(session_str: str):
    platform_id, message_type, session_id = session_str.split(":", 2)   # ← 切 2 刀
    return MessageSession(platform_id, MessageType(message_type), session_id)
```

* `split(":", 2)` 是**最多切 3 段**（maxsplit=2）→ `session_id` 里**允许再出现 `:`**。
  实测形态：`webchat:FriendMessage:webchat!astrbot!<uuid>` 里没有冒号，
  但**主播场景/自定义 session 会带**，而且陪伴插件把 `|` 拼在 session_id 后面
  （`11:FriendMessage:3412642082|<hash>|comfort`）——
  哪天它换成 `:` 分隔，**只有 maxsplit 语义能还原**。
  ⚠️ 换成 `split(":")` 会让解析**静默错位**（不报错、值却是错的）——
  这是最贵的一类 bug，`host/tests/test_umo.py` 有专门判据守着。
* 段数不足 / 类型名非法 → **真实现直接抛异常**（`ValueError`：
  `not enough values to unpack` / `MessageType("xxx")`）。
  我们**照抄这个行为**：`parse_umo` 失败返回 **`None`**（宿主侧好判），
  但**绝不返回一个"看起来正常"的假值**；需要异常语义时用 `parse_umo_strict`。

## 🔴 umo 第二段必须与实际会话类型一致（我实测踩过）

真实现 `AstrMessageEvent.is_private_chat()`（`astr_message_event.py:255-257`）：

```python
def is_private_chat(self) -> bool:
    return self.get_message_type() == MessageType.FRIEND_MESSAGE
```

而 `get_message_type()`（同文件 `:184-189`）**优先读 `message_obj.type`，
拿不到才退回 `session.message_type`**（session 由 umo 解析而来）。
compat 层 `astr_message_event.py:241-251` 是它的等价物。

→ 所以 **`message_obj.type` 与 umo 第二段必须同源**；只写对其中一个
  （例如 umo 写 `GroupMessage`、`message_obj.type` 留 `FRIEND_MESSAGE`）
  会让 `is_private_chat()` 给出与预期相反的答案 →
  `dispatch.apply_wake`（`astrbot_host/dispatch.py:145-162`）据此判"私聊直接唤醒"
  → **唤醒判定出错、插件收不到消息**。
  `build_inbound_event` 因此**从同一个 `MessageType` 同时产出两处**。

## 🔴 `self_id` 绝不能等于 `sender_id`

陪伴插件 `event_dispatch.py:983`（另一处 `:1363`）有一道"过滤自己发的消息"的闸门：

```python
if sender_id and self_id and sender_id == self_id:
    return False        # ← 判定为"这是我们自己发的" → 整条消息被丢弃
```

若 `self_id` 与 `sender_id` 相同，**用户说的每句话都会被静默丢掉**。
真实现的 webchat 适配器把 `self_id` 设成 **`"webchat"`**（平台，而非用户）
—— 见真源码 `core/platform/sources/webchat/webchat_adapter.py:208`
`abm.self_id = "webchat"`。我们照这个语义：`self_id` 默认取 `PLATFORM_NAME`
（类型名，绝不会撞上用户 ID）。
"""
from __future__ import annotations

import time
from typing import Any, NamedTuple

from astrbot_host.compat.core.platform.message_session import MessageSession
from astrbot_host.compat.core.platform.message_type import MessageType
from astrbot_host.platforms import (
    DEFAULT_PLATFORM,
    DEFAULT_PLATFORM_DESCRIPTION,
    DEFAULT_PLATFORM_ID,
    DEFAULT_PLATFORM_NAME,
    PlatformRegistry,
    PlatformSpec,
)

#: 插件按这个昵称判定"定时任务伪事件"（`tts_tool_sanitizer.py:69`，完全相等）。
#: ⚠️ 从 platforms 模块取（那里是禁止值的唯一定义处）。
from astrbot_host.platforms import _FORBIDDEN_SENDER_NAME  # noqa: E402

# ---------------------------------------------------------------------------
# 常量：umo 三段 + 平台元数据
# ---------------------------------------------------------------------------
#
# 🔴🔴 **这些常量现在是从 `platforms.py`（唯一事实源）派生的**（2026-09-21）。
#
#   为什么改成派生（情绪：「别写死了，以后可能会加入 onebot 机器人」）：
#     原来这里把「我们是哪个平台」写死成三个字面量，于是"加第二个平台"
#     要改多处、且改一处漏一处 = **umo 写进孤儿会话**（静默数据分裂）。
#     现在平台实例是个带校验的规格对象，这里只是它的**默认值投影** ——
#     名字一个都没变（向后兼容），但"定义处"只有一处。
#
#   ⚠️ 取值本身**一个字都没改**（`"11"` / `"webchat"` / `"情绪app WebChat"`）：
#      它们是插件数据主键的一半，改了等于换库。

#: 🔴 **平台实例 ID** —— umo 的**第一段**，对应真实现的 `PlatformMetadata.id`
#:    （真 `astr_message_event.py:138` 的 `get_platform_id()`）。
#:
#: 取值 `"11"` 是**情绪拍板**的：与现有 AstrBot 连接同名，使我们的 umo
#: 与生产 `companions.json` 里的 `11:FriendMessage:3412642082` **逐字同构**。
#:
#: ⚠️ **这个值一旦改动 = 插件侧所有用户数据对不上**（它就是主键的一半）。
#: 改它之前必须同时做数据迁移，见 `docs/P0_P1_TASKS.md` P1-4。
PLATFORM_ID: str = DEFAULT_PLATFORM_ID

#: 平台**类型**名（真实现的 `PlatformMetadata.name`）—— 与 `PLATFORM_ID` 不是一回事。
#:
#: 🔴 **绝不冒充 `aiocqhttp`**（照 `docs/P0_P1_TASKS.md` §4 的硬约束）：
#:    我们不给 QQ 发消息，冒充 OneBot 会让插件的平台能力判定走进它自己
#:    也实现不了的分支（实测 `int(user_id)` 崩、OneBot 原生兜底硬失败）。
#:    用 `webchat` 语义 —— 陪伴插件 `platform_compat.py:102`
#:    明确把 `webchat` 当**一等公民**（`delivery_policy="normal"`、
#:    能力取 `_BASE_CAPABILITIES`、`limits: []`），
#:    而且它是真 AstrBot **内核自带**的适配器（`platform/manager.py:99-102`
#:    自动创建、id 写死 `"webchat"`），不是野路子。
#: 🔴 更关键：`proactive.py:1056` **硬拒 `generic`** ⇒ name 必须能解析成已知类型。
PLATFORM_NAME: str = DEFAULT_PLATFORM_NAME

#: 平台描述（`PlatformMetadata.description`）。
#: ⚠️ 陪伴插件用它做**两处语义判定**（都实测过）：
#:    ① `tts_tool_sanitizer.py:54` 找 `"cronjob"`（**完全相等**）识别"定时任务伪事件"；
#:    ② `platform_compat.py:175-182` 把它当平台类型的**子串兜底** →
#:       写 `"非 OneBot"` 会被判成 **onebot**（否定句当断言！）。
#:    ⇒ 只写人类可读的中文，不提任何平台名、不写 cronjob。
PLATFORM_DESCRIPTION: str = DEFAULT_PLATFORM_DESCRIPTION

#: 消息类型字面量（值必须与 `MessageType` 的枚举值逐字相同 —— 它们会进 umo 字符串）。
FRIEND_MESSAGE: str = MessageType.FRIEND_MESSAGE.value    # "FriendMessage"
GROUP_MESSAGE: str = MessageType.GROUP_MESSAGE.value      # "GroupMessage"
OTHER_MESSAGE: str = MessageType.OTHER_MESSAGE.value      # "OtherMessage"

#: 合法的消息类型字面量集合（解析时用来**显式**校验，见 `parse_umo_strict`）。
MESSAGE_TYPES: tuple[str, ...] = (FRIEND_MESSAGE, GROUP_MESSAGE, OTHER_MESSAGE)

#: 🔴 **`self_id` 的推荐值**：用平台**类型**名而不是用户 ID。
#:
#: 理由见模块 docstring「`self_id` 绝不能等于 `sender_id`」：
#: 陪伴插件 `event_dispatch.py:983` 会把 `sender_id == self_id` 的消息
#: **静默丢弃**（判定为"自己发的"）。真 AstrBot 的 webchat 适配器也是这么做的
#: （`webchat_adapter.py:208` → `abm.self_id = "webchat"`）。
DEFAULT_SELF_ID: str = PLATFORM_NAME


class Umo(NamedTuple):
    """解析后的 umo 三段（**与真实现 `MessageSession` 字段同序同名**）。

    ⚠️ 用 `NamedTuple` 而不是 dataclass：既支持
       `platform_id, message_type, session_id = parse_umo_strict(x)`
       这种**与真实现完全一致的解包**，又支持 `.session_id` 属性访问。
    """

    platform_id: str
    """umo 第一段 —— 平台**实例** ID（我们的 = `"11"`）。"""
    message_type: str
    """umo 第二段 —— `FriendMessage` / `GroupMessage` / `OtherMessage`。"""
    session_id: str
    """umo 第三段 —— **插件的主键**（我们的 = 用户 ID）。

    ⚠️ 它**允许包含 `:`**（真实现用 `split(":", 2)`）。
    """


# ---------------------------------------------------------------------------
# 构造 / 解析
# ---------------------------------------------------------------------------


def build_umo(
    user_id: str,
    *,
    group: bool = False,
    platform_id: str = PLATFORM_ID,
) -> str:
    """构造 umo：`<platform_id>:FriendMessage:<user_id>`（默认 `11:…`）。

    ⚠️ 实现上**委托给真实现的 `str(MessageSession(...))`**，而不是手写 f-string ——
       这样"我们的 umo 长什么样"永远由**真实现的那段代码**决定；
       哪天真实现改了 `__str__`（例如加了转义），我们自动跟上，
       不会出现"我们以为的格式"与"插件眼中的格式"分叉。

    Args:
        user_id: 我们的用户 ID（会话主键）。**原样**进 umo，
            **不做 strip / 转义 / 截断** —— 它是插件的数据主键，
            静默改一个字符就等于换库（项目铁律「别静默改用户数据」）。
        group: `True` → 第二段 `GroupMessage`，否则 `FriendMessage`。
        platform_id: 平台**实例** id（umo 第一段）。
            🔴 默认 `"11"`（我们自己的 Web 端，向后兼容）。
            加第二个平台（例如 onebot）时**必须显式传它** ——
            同一条用户消息属于哪个平台，决定了插件的哪一份用户数据。

    Returns:
        形如 `11:FriendMessage:3412642082` 的字符串。

    Raises:
        ValueError: `user_id` 为空 / 非字符串 / 含 `:` 之外的非法形态时。

    🔴 **为什么空 user_id 必须抛错**：`11:FriendMessage:` 是一个**语法合法但语义
       有毒**的 umo —— 它会被真实现正常解析成一个 `session_id=""` 的会话，
       插件于是把数据写进一个"空用户"名下，而**任何地方都不报错**。
       与其静默造出这种垃圾键，不如当场响亮失败。
    """
    if not isinstance(user_id, str):
        raise ValueError(f"user_id 必须是 str，得到 {type(user_id).__name__}")
    if not user_id:
        raise ValueError(
            "user_id 不能为空 —— 会构造出 `11:FriendMessage:` 这种"
            "语法合法但语义有毒的 umo（插件会把数据写进空用户）"
        )
    # 🔴 平台实例 id 也要校验：它进 umo 第一段，含冒号/空白会静默错位
    pid = str(platform_id or "").strip()
    if not pid:
        raise ValueError(
            "platform_id 不能为空 —— umo 第一段缺失会让插件按未知平台处理"
            "（真实现 `context.send_message` 按 `meta().id` 查表，查不到就发不出去）"
        )
    if ":" in pid or any(c.isspace() for c in pid):
        raise ValueError(
            f"platform_id 不能含冒号或空白（会破坏 umo 的 A:B:C 结构）：{pid!r}"
        )
    message_type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    # ⚠️ 走真实现的 __str__（`message_session.py:40-41`），不手写 f-string
    return str(
        MessageSession(
            platform_name=pid,
            message_type=message_type,
            session_id=user_id,
        )
    )


def parse_umo(umo: str) -> Umo | None:
    """解析 umo；**失败返回 `None`**（宿主侧逐条消息调用，用返回值判空最顺手）。

    🔴 **绝不静默返回垃圾值**：段数不足 / 消息类型非法 / 输入不是字符串
       → 一律 `None`。宁可让调用方显式处理"解析失败"，
       也不要给出一个"看起来正常"的三元组（那会让错误流向插件的数据主键）。

    ⚠️ **解析走真实现的 `MessageSession.from_str`**（`message_session.py:46-49`），
       **不自己写 `split`** —— 这样 `split(":", 2)` 的 maxsplit 语义、
       `MessageType(...)` 的校验规则都与真实现**逐字一致**，
       真实现哪天变了我们自动跟上。

    Args:
        umo: 待解析字符串，形如 `11:FriendMessage:3412642082`。

    Returns:
        `Umo(platform_id, message_type, session_id)`；失败为 `None`。
        ⚠️ `session_id` 可以是**含冒号**的串（`11:FriendMessage:a:b:c` → `a:b:c`）。

    Examples:
        >>> parse_umo("11:FriendMessage:3412642082").session_id
        '3412642082'
        >>> parse_umo("11:FriendMessage:a:b:c").session_id
        'a:b:c'
        >>> parse_umo("11:FriendMessage") is None
        True
        >>> parse_umo("11:NopeMessage:x") is None
        True
    """
    if not isinstance(umo, str):
        return None
    try:
        session = MessageSession.from_str(umo)
    except Exception:  # noqa: BLE001
        # ValueError（段数不足 / 类型非法）、AttributeError 等都归为"解析失败"。
        # ⚠️ 这里**不记日志**是刻意的：本函数被逐条消息调用，
        #    调用方（host 层）拿到 None 后自会按需告警；
        #    在工具函数里打日志会让"每次刷屏"掩盖真正的问题。
        return None
    return Umo(
        platform_id=session.platform_id,
        message_type=session.message_type.value,
        session_id=session.session_id,
    )


def parse_umo_strict(umo: str) -> Umo:
    """解析 umo；**失败抛 `ValueError`**（对拍真实现语义时用这个）。

    与 `parse_umo` 的唯一区别是错误形态：真实现 `from_str` 对非法输入**直接抛异常**
    （`session_str.split(":", 2)` 解包失败 → `ValueError`），
    需要"与真实现行为逐字一致"的场景（差分对拍 / 边界测试）用本函数。

    Raises:
        ValueError: 段数不足 / 消息类型非法 / 输入不是字符串。
    """
    if not isinstance(umo, str):
        raise ValueError(f"umo 必须是 str，得到 {type(umo).__name__}")
    try:
        session = MessageSession.from_str(umo)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"不合法的 umo 字符串: {umo!r}（{type(exc).__name__}: {exc}）") from exc
    return Umo(
        platform_id=session.platform_id,
        message_type=session.message_type.value,
        session_id=session.session_id,
    )


def session_id_of(umo: str) -> str | None:
    """从 umo 里取 **`session_id`**（= 我们的用户 ID）；失败返回 `None`。

    这是宿主最常用的一个动作（"这条消息来自哪个用户"），
    单独给个函数是为了让调用点**不写 `parse_umo(x).session_id`**
    —— 后者在解析失败时会 `AttributeError: 'NoneType'`，
    把"umo 非法"这个业务判断变成一次崩溃。
    """
    parsed = parse_umo(umo)
    return parsed.session_id if parsed is not None else None


def user_id_of(umo: str) -> str | None:
    """`session_id_of` 的别名 —— 语义上它就是"我们平台的用户 ID"。"""
    return session_id_of(umo)


def is_our_umo(umo: str) -> bool:
    """这条 umo 是不是**我们平台**发出的（第一段 == `PLATFORM_ID`）。

    ⚠️ 用于"这条会话该不该由我们处理"的判定；
       **不要**用它反推平台类型（第一段是实例 id，类型名在 `PLATFORM_NAME`）。
    """
    parsed = parse_umo(umo)
    return parsed is not None and parsed.platform_id == PLATFORM_ID


def is_private(umo: str) -> bool:
    """umo 是否代表**私聊**（第二段 == `FriendMessage`）；非法 umo 返回 `False`。

    🔴 这就是"唤醒判定"依赖的那个量（`astrbot_host/dispatch.py:149` 调
       `event.is_private_chat()`，而它由 session/umo 推导）——
       第二段写错会导致**唤醒判定反过来**，把工具函数放这里统一。
    """
    parsed = parse_umo(umo)
    return parsed is not None and parsed.message_type == FRIEND_MESSAGE


# ---------------------------------------------------------------------------
# 平台元数据
# ---------------------------------------------------------------------------


def build_platform_metadata(
    *,
    platform_id: str = PLATFORM_ID,
    spec: PlatformSpec | None = None,
) -> Any:
    """造本平台的 `PlatformMetadata`（供事件 / `platform_manager` 复用）。

    ⚠️ **只在这里造一份**：`platform_meta.id` 必须与 umo 第一段**字面相同**
       （真实现 `context.send_message` 就是按 `platform.meta().id ==
       session.platform_name` **查表**找平台的，见 `core/star/context.py:641-643`），
       多处各造一份迟早会漂移成"能构造事件但发不出去"。

    ⚠️ `name` = 平台**类型**名（默认 `webchat`）、`id` = 平台**实例** id（默认 `11`）
       —— **两者必须分开**：真实现 `get_platform_name()` 返回 `name`、
       `get_platform_id()` 返回 `id`（`astr_message_event.py:129-138`）。
       陪伴插件用 `get_platform_name()` 做能力判定，用 `meta().id` 做投递查表
       —— 混用即两处都错。

    Args:
        platform_id: 平台实例 id（umo 第一段）。
            ⚠️ 传了它但**没传 `spec`** 时，会用**默认平台的 name/description**
               造元数据 —— 这只对"默认平台"是正确的。
               别的平台必须传 `spec`（否则插件会按 webchat 的能力表对待一个
               onebot 平台，见模块 docstring 约束②）。
        spec: 平台规格（`platforms.PlatformSpec`）。**推荐总是传它** ——
            `name`/`description`/`id` 三件套从同一个对象来，不可能漂移。
    """
    from astrbot_host.compat.core.platform.platform_metadata import PlatformMetadata

    chosen = spec if spec is not None else DEFAULT_PLATFORM
    # ⚠️ `platform_id` 只有在"没给 spec"时才允许覆盖 id
    #    （给了 spec 就以 spec 为准，避免两个来源打架）
    pid = chosen.id if spec is not None else platform_id
    return PlatformMetadata(
        name=chosen.name,
        description=chosen.description,
        id=pid,
    )


# ---------------------------------------------------------------------------
# 事件工厂
# ---------------------------------------------------------------------------


def build_inbound_event(
    text: str,
    user_id: str,
    *,
    group: bool = False,
    sender_name: str = "",
    self_id: str = "",
    message_id: str = "",
    extra: dict[str, Any] | None = None,
    platform: PlatformSpec | None = None,
) -> Any:
    """由「一条来自我们面板/API 的用户消息」造出一个标准 `AstrMessageEvent`。

    这是宿主把**我们自己的聊天链路**接进插件世界的那一步：
    用户在面板里发一句话 → 本函数 → `astrbot_host.dispatch.dispatch(event)`
    → 插件 handler 跑起来 → 结果回给用户。

    设置项（**都是派发链路会读的**，逐条有理由）：

    | 字段 | 值 | 谁在读 |
    |---|---|---|
    | `umo` | `build_umo(user_id, group=..., platform_id=platform.id)` | 插件（288 次）· 会话主键 |
    | `message_obj.type` | 与 umo 第二段**同源**的 `MessageType` | `is_private_chat()` / `get_message_type()` |
    | `message_obj.self_id` | `platform.effective_self_id`（默认 `"webchat"`） | `get_self_id()` · **防"自己的消息"误判** |
    | `message_obj.session_id` | `user_id` | 真实现 `message_event_result` / 转发 |
    | `message_obj.sender` | `MessageMember(user_id, nickname)` | `get_sender_id()`（**真实现就取这里**） |
    | `platform_name` | `platform.name`（默认 `"webchat"`） | 唤醒判定第三分支（`dispatch.py:158`）· 插件能力判定 |
    | `platform_id` | `platform.id`（默认 `"11"`） | `get_platform_id()` · 投递查表 |

    Args:
        platform: 🔴 **平台规格**（`platforms.PlatformSpec`）。
            `None` = 用默认平台（我们自己的 Web 端，行为与以前**逐字一致**）。
            加第二个平台（onebot 等）时**必须传它** ——
            `name`/`description`/`id`/`self_id` 四件套都从它来，
            不可能出现"umo 用 A 平台、元数据说 B 平台"这种分裂。

    ## 🔴 两处"看起来啰嗦但不能省"的地方

    1. **`message_obj.type` 与 umo 第二段同源**：
       真实现 `is_private_chat()` = `get_message_type() == FRIEND_MESSAGE`，
       而 `get_message_type()` **优先读 `message_obj.type`，缺失才退回 `session`**
       （真 `:184-189`）。若两处不一致，`message_obj.type`（优先级更高）
       会**盖掉** umo 的判定 → 唤醒判定与实际会话类型相反。
       本函数从**同一个 `MessageType`** 产出两者，从根上排除不一致。

    2. **`sender` 必须真的填**：
       真实现 `get_sender_id()`（真 `:203-208`）是
       `message_obj.sender.user_id`，**且要求它是 `str`**——
       不填则返回 `""`，插件的用户身份/权限/关系全部落空。
       compat 层 `astr_message_event.py:197-199` 走的是构造参数，
       我们**两处都填成同一个值**，任何一条路径都不会取到空。

    Args:
        text: 用户消息的纯文本（进 `message_str`；同时作为 `Plain` 段进 `message`）。
        user_id: 我们的用户 ID → **`umo` 第三段**（插件数据主键，原样透传）。
        group: `True` → 群聊（umo 第二段 `GroupMessage` + `type=GROUP_MESSAGE`）。
        sender_name: 发送者昵称；空则回退成 `user_id`
            （真实现 webchat 就是 `MessageMember(username, username)`，
            见 `webchat_adapter.py:209-210`）。
        self_id: bot 自己的 id。**留空 = 用平台规格的 `effective_self_id`**
            （默认即平台类型名 `"webchat"`）。**绝不要传 `user_id`**
            —— 陪伴插件 `event_dispatch.py:983` 会把 `sender_id == self_id`
            的消息**静默丢弃**（当成"自己发的"）。
        message_id: 消息 ID；空则按 `时间戳` 造一个（插件用它做去重/引用）。
        extra: 预置的 `event._extras`（如宿主自己的会话 ID）。

    Returns:
        配好上述字段的 `AstrMessageEvent`。

    Raises:
        ValueError: `user_id` 非法（见 `build_umo`）；或 `self_id == user_id`
            （会触发插件的"自己发的消息"静默丢弃，必须当场拦住）。

    ⚠️ **本函数不设置 `is_wake` / `is_at_or_wake_command`** ——
       那是 `dispatch.apply_wake` 的职责（照真实现 `waking_check/stage.py`）。
       在这里预设 `is_wake=True` 会让宿主的唤醒策略**永远生效不了**，
       表现为"私聊配置成需要唤醒词但依然句句都触发"。
    """
    # ---- 平台规格：一个来源产出全部四件套（umo/name/description/id/self_id） ----
    spec = platform if platform is not None else DEFAULT_PLATFORM
    # `self_id` 留空 → 取平台规格的推荐值（绝不等于用户 id，见 docstring）
    resolved_self_id = str(self_id or spec.effective_self_id)

    # ---- 前置校验（都在造对象**之前**，避免留下半成品） ----
    if resolved_self_id == user_id and user_id:
        raise ValueError(
            f"self_id 不能等于 user_id（都是 {user_id!r}）—— 陪伴插件 "
            "`event_dispatch.py:983` 会把 sender_id == self_id 的消息"
            "判定为『自己发的』并**静默丢弃**，用户将收不到任何回复"
        )
    if str(sender_name or user_id) == _FORBIDDEN_SENDER_NAME:
        # 插件 `tts_tool_sanitizer.py:69` 按 `sender_name == "scheduler"` 判"定时
        # 任务伪事件"；命中后是**静默降级**（同会话文本可能被发两次）。
        # 我们的 sender_name 空则回退成 user_id → 所以两种来源都要挡。
        raise ValueError(
            f"sender_name/user_id 不能是 {_FORBIDDEN_SENDER_NAME!r} —— "
            "插件 `tts_tool_sanitizer.py:69` 会把它当成定时任务伪事件，"
            "导致同会话文本可能被发两次"
        )
    # `user_id` 非法时在这里抛
    umo = build_umo(user_id, group=group, platform_id=spec.id)

    from astrbot_host.compat.core.message.components import Plain
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.astrbot_message import (
        AstrBotMessage,
        MessageMember,
    )

    # 🔴 **一个 MessageType 同时喂两处**（umo 第二段 + message_obj.type）——
    #    这是"绝不会不一致"的写法，比"两个地方各自算一遍"可靠。
    message_type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE

    message_obj = AstrBotMessage()
    message_obj.type = message_type
    message_obj.self_id = resolved_self_id
    message_obj.session_id = user_id
    message_obj.message_id = message_id or f"emotion-{time.time_ns()}"
    message_obj.sender = MessageMember(user_id=user_id, nickname=sender_name or user_id)
    message_obj.message = [Plain(text)] if text else []
    message_obj.message_str = text
    # ⚠️ `raw_message` 留成**非 Mapping**（字典）—— 陪伴插件的
    #    `_event_raw_payload`（`event_dispatch.py:487-499`）会优先读它，
    #    一旦读到 Mapping 就按 OneBot 协议字段去解析（找 `post_type` 等）。
    #    我们不是 OneBot，给个 dict 反而更安全？——**不**：给非 Mapping 时它
    #    返回 `{}`，随后走"非 OneBot 适配器"的兜底分支（`:998-1001`
    #    明确写着 "the framework has already classified this event as a
    #    message, so retain that classification"）→ 判定为**有效消息**。
    #    填 dict 则要伪造 OneBot 字段，那是**冒充平台**（违反硬约束）。
    #    所以这里如实放我们的原始文本对象。
    message_obj.raw_message = text
    message_obj.timestamp = int(time.time())

    event = AstrMessageEvent(
        message_str=text,
        message_obj=message_obj,
        unified_msg_origin=umo,
        sender_id=user_id,
        sender_name=message_obj.sender.nickname,
        self_id=resolved_self_id,
        # 平台**类型**名 vs 平台**实例** id —— 两者都必须给，且不能混。
        # ⚠️ 两者都从 `spec` 来（`platform_id` 也必须与 umo 第一段一致，
        #    否则真实现 `context.send_message` 按 `meta().id` 查表查不到）。
        platform_name=spec.name,
        platform_id=spec.id,
        # ⚠️ 与 umo 第二段保持一致的退路值（session 在时以 umo 为准，
        #    见 compat `astr_message_event.py:241-251`）
        is_private=not group,
        session_id=user_id,
    )
    # 真实现的事件对象上有 `platform_meta`（`astr_message_event.py:49`），
    # 插件会读它（`tts_tool_sanitizer.py:50-54` 判"是不是定时任务伪事件"）——
    # compat 的构造函数不接它，所以在这里如实补上**同一份**元数据。
    # 🔴 传 `spec`（不是默认平台）：`name`/`description`/`id` 必须与
    #    umo 第一段、事件的 `platform_name` **同源**，否则插件按错误的
    #    能力表对待这个平台（例如把 onebot 当 webchat → 少给能力）。
    event.platform_meta = build_platform_metadata(spec=spec)
    event.platform = event.platform_meta  # 真实现 `:103` 的 back_compability 别名

    if extra:
        event._extras.update(extra)
    return event


__all__ = [
    # 常量
    "PLATFORM_ID",
    "PLATFORM_NAME",
    "PLATFORM_DESCRIPTION",
    "DEFAULT_SELF_ID",
    "FRIEND_MESSAGE",
    "GROUP_MESSAGE",
    "OTHER_MESSAGE",
    "MESSAGE_TYPES",
    # 平台规格（「哪些平台」的入口 —— 加 onebot 等新平台从这里走）
    "DEFAULT_PLATFORM",
    "PlatformRegistry",
    "PlatformSpec",
    # 类型
    "Umo",
    # 构造 / 解析
    "build_umo",
    "parse_umo",
    "parse_umo_strict",
    "session_id_of",
    "user_id_of",
    "is_our_umo",
    "is_private",
    # 工厂
    "build_platform_metadata",
    "build_inbound_event",
]
