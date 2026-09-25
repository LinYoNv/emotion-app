# -*- coding: utf-8 -*-
"""平台实例注册表 —— **「哪些平台」只有这一处定义**（情绪 2026-09-21 要求）。

## 为什么要有这个模块

情绪原话：

> 「**别写死了，以后可能会加入 onebot 机器人，那边也可以获取 umo。**」

改之前宿主把「我们是什么平台」写死在三个常量上
（`umo.PLATFORM_ID="11"` / `PLATFORM_NAME="webchat"` / 单实例
`build_platform_manager()`），于是：

* 「一个会话绑哪个平台的哪个用户」表达不出来 —— 全库只有一个平台；
* 想加 onebot（或任何第二平台）要改**多处**常量，改一处漏一处 =
  **umo 写进孤儿会话**（静默数据分裂，本项目最贵的一类 bug）。

→ 现在「平台实例」是一个**带校验的规格对象**，注册表是唯一事实源。

## 🔴 三条硬约束（都是**实测**出来的，不是推理）

### ① `name` 绝不能落到 `generic`

陪伴插件 `proactive.py:1056` 的主动消息身份闸门：

```python
if not subject or not platform or platform == "generic" or not (adapter or bot_id):
    return False
```

→ 平台类型解析成 `generic` ⇒ **主动消息在所有 ~25 个调用点静默失效**
（`proactive_engine.py:4227/4639`、`daily_state_tick.py:139/328`…），
而且**不是"能力少了一个"，是整条链断掉**：任何 capability 都补不回来。
✅ 本模块在**注册时**就拒绝一个会解析成 `generic` 的 `name`。

### ② `description` 里不能出现平台的**别名子串**

插件 `platform_compat.py:165-182` 解析平台类型的顺序是
`name` → `id` → `description`，且**先精确匹配、再子串扫描**别名表。
所以 `description="情绪app 内置 Web 端（非 OneBot）"` 会被判成 **onebot**
—— **否定句被当成断言**（"非 OneBot" 里含 `onebot`）。

⚠️ 这一条**极危险**：onebot 的能力表把 `onebot_actions` / `poke` /
`input_status` 置 True，而插件在这几处**会真的去用**：

* `int(user_id)`（`proactive_message.py:9663`、`:10079-10081`）——
  非数字 id（我们的会话 id 可以是任意串）→ **ValueError 崩**；
* `proactive_message.py:17701-17709` 走 OneBot 原生兜底 →
  `RuntimeError("当前平台不使用 OneBot 原生兜底")` **硬失败**。

✅ 本模块在**注册时**就拒绝含别名子串的 `description`。
（当前之所以没炸，只是因为 `name="webchat"` 先被检查到了 ——
 而"先被检查到"不是保证，改 `name` 就失效。所以必须显式禁止。）

### ③ `description` 不能恰好是 `"cronjob"`

插件 `tts_tool_sanitizer.py:47-69` 用它识别"定时任务伪事件"：

```python
if meta_name == "cron" or meta_description == "cronjob": return True
if sender_name == "scheduler": return True
```

命中后是**静默降级**：不推迟同会话文本、跳过空正文守卫
（`main.py:18293-18295` 让工具边界守卫整个返回 `[]`）→ **正文可能被发两次**。

⚠️ 判据是 `.strip().lower()` 后的**完全相等**（不是子串）→
只有裸串 `cronjob` 危险。✅ 一并禁止（顺手，零成本）。

## 与真实现的关系

真 AstrBot 的 `PlatformMetadata` 有三个字段（`id` / `name` / `description`），
且 `context.send_message` 是**按 `meta().id` 查表**找平台的
（`core/star/context.py:641-643`）；`context.get_platform(x)` 按 `meta().name` 查
（`:750-754`）。所以：

* **`id` 必须 == umo 第一段**（否则"能构造事件但发不出去"）；
* **`name` 是平台类型名**（插件的能力判定用它）。

两者**不能混**（`get_platform_id()` 返回 `id`、`get_platform_name()` 返回 `name`）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot")


class PlatformSpecError(ValueError):
    """平台规格不合法 —— **注册时就响亮失败**，绝不静默降级。

    🔴 为什么必须抛（而不是打日志继续）：这三条约束的失败后果
       都是**静默**的（主动消息失效 / 崩在 int() / 正文发两次），
       运行时根本看不出来。只有在注册时拒绝，才能保证"配错了根本起不来"。
    """


#: 插件 `platform_compat.py:17-37` 的别名表**照抄**（平台类型判定用）。
#:
#: ⚠️ 照抄而不是"凭印象写几个"：这张表决定了 `description` 里
#:    哪些子串是危险的，少一个就会漏掉一种误判。
_PLATFORM_KIND_ALIASES: dict[str, str] = {
    "aiocqhttp": "onebot",
    "onebot": "onebot",
    "napcat": "onebot",
    "llonebot": "onebot",
    "qq": "onebot",
    "qq_official": "qq_official",
    "qqofficial": "qq_official",
    "qqbot": "qq_official",
    "qq_bot": "qq_official",
    "qq官方": "qq_official",
    "官方qq": "qq_official",
    "webchat": "webchat",
    "telegram": "telegram",
    "telegram_bot": "telegram",
    "telegrambot": "telegram",
    "tg": "telegram",
    "weixin_official_account": "wechat_official",
    "wechat_official": "wechat_official",
    "dingtalk": "dingtalk",
}

#: 已知的"平台类型"取值（= 别名表的值域 + `generic`）。
#: `generic` **不在**允许注册的集合里（见约束①）。
KNOWN_PLATFORM_KINDS: frozenset[str] = frozenset(_PLATFORM_KIND_ALIASES.values())

#: 插件把"定时任务伪事件"认成这个字符串（`tts_tool_sanitizer.py:54`，完全相等）。
_FORBIDDEN_DESCRIPTION_EXACT = frozenset({"cronjob", "cron"})

#: 插件按 `sender_name == "scheduler"` 判定时任务（`:69`）。
_FORBIDDEN_SENDER_NAME = "scheduler"


def normalize_platform_kind(value: Any) -> str:
    """**照抄**插件的 `_normalize_platform_kind`（`platform_compat.py:165-173`）。

    ⚠️ 照抄而不"实现个差不多的"：这个函数的返回值决定了插件的**能力表**，
       我们用它做注册校验 —— 如果两边算法不一致，校验就会漏掉真误判。

    >>> normalize_platform_kind("webchat")
    'webchat'
    >>> normalize_platform_kind("情绪app 内置 Web 端（非 OneBot）")
    'onebot'
    >>> normalize_platform_kind("随便什么")
    'generic'
    """
    text = str(value or "").strip().lower()[:80]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in _PLATFORM_KIND_ALIASES:
        return _PLATFORM_KIND_ALIASES[compact]
    for token, kind in _PLATFORM_KIND_ALIASES.items():
        if token and token in compact:
            return kind
    return "generic"


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    """一个平台**实例**的规格（umo 第一段的来源 + 插件看到的三元组）。

    Attributes:
        id: 平台实例 id —— **umo 第一段**，也是真实现
            `context.send_message` 查表用的键（`meta().id`）。
            🔴 **它就是插件数据主键的一半，一旦上线不可改**（改了 = 换库）。
        name: 平台**类型**名（插件能力判定的依据，`meta().name`）。
            必须能解析成一个已知类型（`webchat`/`onebot`/`telegram`…），
            **不能是 `generic`**（见模块 docstring 约束①）。
        description: 给插件看的描述（`meta().description`）。
            ⚠️ 不能含平台别名子串、不能等于 `cronjob`（约束②③）。
        label: 给**人**看的名字（面板上显示，不参与任何判定）。
        self_id: bot 自己的 id（判"这条消息是不是我自己发的"）。
            默认取 `name`（照真实现 webchat 适配器 `abm.self_id = "webchat"`）。
            绝不要设成用户 id（插件会静默丢弃该用户的所有消息）。
    """

    id: str
    name: str
    description: str = ""
    label: str = ""
    self_id: str = ""

    def __post_init__(self) -> None:
        """注册即校验 —— 三条硬约束在这里挡住（见模块 docstring）。"""
        # ---- 基本形状 ----
        if not self.id or not str(self.id).strip():
            raise PlatformSpecError("平台实例 id 不能为空（它是 umo 第一段，不能缺失）")
        if ":" in self.id:
            raise PlatformSpecError(
                f"平台实例 id 不能含冒号（会破坏 umo 的 A:B:C 结构）：{self.id!r}"
            )
        if any(c.isspace() for c in self.id):
            raise PlatformSpecError(
                f"平台实例 id 不能含空白字符（umo 主键是逐字节比较）：{self.id!r}"
            )
        if not self.name or not str(self.name).strip():
            raise PlatformSpecError("平台类型名 name 不能为空（插件靠它判能力）")

        # ---- 约束①：name 绝不能被解析成 generic ----
        kind = normalize_platform_kind(self.name)
        if kind == "generic":
            raise PlatformSpecError(
                f"平台类型名 {self.name!r} 会被陪伴插件解析成 `generic` —— "
                "那会让【主动消息整条链失效】（`proactive.py:1056` 硬拒 generic，"
                "约 25 个调用点），且任何 capability 都补不回来。\n"
                f"→ 请用已知类型名之一：{sorted(KNOWN_PLATFORM_KINDS)}"
            )

        # ---- 约束③：description 不能是定时任务标记 ----
        desc_norm = str(self.description or "").strip().lower()
        if desc_norm in _FORBIDDEN_DESCRIPTION_EXACT:
            raise PlatformSpecError(
                f"description={self.description!r} 会被插件当成**定时任务伪事件**"
                "（`tts_tool_sanitizer.py:54` 完全相等判定）→ 同会话文本可能被发两次。"
            )

        # ---- 约束②：description 不能含平台别名子串（否定句会被当断言） ----
        if self.description:
            desc_kind = normalize_platform_kind(self.description)
            if desc_kind != "generic" and desc_kind != kind:
                raise PlatformSpecError(
                    f"description={self.description!r} 含平台别名子串，"
                    f"会被插件解析成 `{desc_kind}`（而 name 是 `{kind}`）——\n"
                    "⚠️ 插件解析顺序是 name → id → description，"
                    "现在靠 name 先命中才没出事；一旦 name 变了就会误判，"
                    "而误判成 onebot 会让插件去 `int(user_id)`（非数字 id 直接崩）"
                    "并走 OneBot 原生兜底（硬失败）。\n"
                    "→ description 请只写**人类可读**的中文，不要提任何平台名。"
                )

        # ---- self_id 不能等于用户 id 同款语义：至少不能为空 ----
        if self.self_id and ":" in self.self_id:
            raise PlatformSpecError(f"self_id 不能含冒号：{self.self_id!r}")

    # ---- 派生量（都不缓存，避免 frozen 对象里藏可变状态） ----

    @property
    def kind(self) -> str:
        """插件会把这个平台归到哪一类（照抄插件算法）。"""
        return normalize_platform_kind(self.name)

    @property
    def effective_self_id(self) -> str:
        """实际使用的 `self_id`（未指定则取平台类型名）。"""
        return self.self_id or self.name

    @property
    def display(self) -> str:
        """给人看的名字（面板/日志用）。"""
        return self.label or f"{self.name}({self.id})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "label": self.label,
            "self_id": self.effective_self_id,
        }


# ---------------------------------------------------------------------------
# 默认平台（= 当前生产在用的那个，**必须逐字保持不变**）
# ---------------------------------------------------------------------------

#: 默认平台实例 id。🔴 **情绪 2026-09-20 拍板，等于插件数据主键的一半。**
#: 与生产 `companions.json` 里那条私聊 umo 的第一段逐字同构 ——
#: ⚠️ 具体 umo 字面量只在 `umo.py` 里出现（那里的单一来源判据会扫本文件，
#:    连注释都扫），所以这里不写样例。
DEFAULT_PLATFORM_ID: str = "11"

#: 默认平台**类型**名（真实现 `PlatformMetadata.name`）。
#: ⚠️ 绝不冒充 `aiocqhttp`：插件把 `webchat` 当一等公民
#: （`platform_compat.py:102` `delivery_policy="normal"`、`limits: []`），
#: 而冒充 OneBot 会让它去用我们没有的原生能力（硬失败）。
DEFAULT_PLATFORM_NAME: str = "webchat"

#: 默认平台描述。
#: 🔴 **`WebChat` 这个词是刻意的**：它与 `name` 同类型，不会被判成别的平台；
#: 而旧值 `"情绪app 内置 Web 端（非 OneBot）"` 含 `onebot` 子串 ——
#: **否定句会被插件当断言**（详见模块 docstring 约束②）。
DEFAULT_PLATFORM_DESCRIPTION: str = "情绪app WebChat"

#: 默认平台规格（宿主不带参数时用的就是它）。
DEFAULT_PLATFORM: PlatformSpec = PlatformSpec(
    id=DEFAULT_PLATFORM_ID,
    name=DEFAULT_PLATFORM_NAME,
    description=DEFAULT_PLATFORM_DESCRIPTION,
    label="情绪app Web 端",
)


class PlatformRegistry:
    """平台实例注册表（**唯一事实源**）。

    用法::

        registry = PlatformRegistry()                    # 只含默认平台
        registry.register(PlatformSpec(id="21", name="aiocqhttp", label="QQ"))
        spec = registry.get("21")

    ⚠️ **不提供删除**：平台实例 id 是插件数据主键的一半，
       删掉一个已上线的平台实例会让它的用户数据变成孤儿。
       要下线一个平台，应该"不派发事件给它"，而不是从注册表抹掉。
    """

    def __init__(self, specs: list[PlatformSpec] | None = None) -> None:
        self._specs: dict[str, PlatformSpec] = {}
        #: 默认平台 id（`build_umo` 不带 platform 时用它）
        self._default_id: str = DEFAULT_PLATFORM_ID
        for spec in specs or [DEFAULT_PLATFORM]:
            self.register(spec)

    # ---- 增 / 查 ----

    def register(self, spec: PlatformSpec, *, make_default: bool = False) -> PlatformSpec:
        """注册一个平台实例。重复 id 视为**配置错误**（抛错，不静默覆盖）。

        🔴 为什么重复 id 要抛：两个不同平台共用同一个 umo 第一段 ⇒
           插件会把它们的用户数据**混进同一个键**（静默串号），
           而且真实现 `send_message` 按 id 查表只会命中一个
           ⇒ 另一个平台的消息**发不出去**。
        """
        if not isinstance(spec, PlatformSpec):
            raise PlatformSpecError(
                f"只接受 PlatformSpec，得到 {type(spec).__name__}"
            )
        existing = self._specs.get(spec.id)
        if existing is not None:
            raise PlatformSpecError(
                f"平台实例 id {spec.id!r} 重复注册（已有 {existing.name!r}，"
                f"又来 {spec.name!r}）—— 共用 umo 第一段会让两个平台的用户数据串号"
            )
        self._specs[spec.id] = spec
        if make_default:
            self._default_id = spec.id
        return spec

    def get(self, platform_id: str) -> PlatformSpec | None:
        """按 umo 第一段取平台规格；取不到返回 `None`（**不编造默认值**）。

        ⚠️ 取不到时**不要**回落成默认平台：那会把"未知平台的消息"
           静默写进默认平台的用户数据里（串号）。调用方应当如实失败。
        """
        return self._specs.get(str(platform_id or ""))

    def require(self, platform_id: str) -> PlatformSpec:
        """`get` 的抛错版（配置错误路径用）。"""
        spec = self.get(platform_id)
        if spec is None:
            raise PlatformSpecError(
                f"未知的平台实例 id {platform_id!r}；已注册：{sorted(self._specs)}"
            )
        return spec

    def all(self) -> list[PlatformSpec]:
        """全部平台规格（顺序稳定：默认平台在最前，其余按 id 排序）。"""
        rest = sorted(k for k in self._specs if k != self._default_id)
        order = ([self._default_id] if self._default_id in self._specs else []) + rest
        return [self._specs[k] for k in order]

    # ---- 默认平台 ----

    @property
    def default_id(self) -> str:
        return self._default_id

    @property
    def default(self) -> PlatformSpec:
        """默认平台规格（注册表非空时一定有值）。"""
        spec = self._specs.get(self._default_id)
        if spec is None:
            raise PlatformSpecError(
                f"默认平台 id {self._default_id!r} 不在注册表里（装配错误）"
            )
        return spec

    def set_default(self, platform_id: str) -> None:
        """改默认平台（面板上"新会话默认绑哪个平台"用）。"""
        self.require(platform_id)
        self._default_id = platform_id

    # ---- 诊断 ----

    def describe(self) -> list[dict[str, Any]]:
        """脱敏描述（供 `/api/v1/status` 与日志）。"""
        return [
            {**s.to_dict(), "is_default": s.id == self._default_id}
            for s in self.all()
        ]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, platform_id: object) -> bool:
        return str(platform_id or "") in self._specs


def spec_from_mapping(raw: Any) -> PlatformSpec:
    """从**配置**（dict）造一个平台规格 —— 供 `.env` / 设置页声明额外平台。

    ⚠️ 三项校验全部照走（构造 `PlatformSpec` 时自动执行），
       所以配错了会在**启动时**响亮失败，而不是运行中静默出怪事。
    """
    if isinstance(raw, PlatformSpec):
        return raw
    if not isinstance(raw, dict):
        raise PlatformSpecError(f"平台配置必须是 dict，得到 {type(raw).__name__}")
    unknown = set(raw) - {"id", "name", "description", "label", "self_id"}
    if unknown:
        raise PlatformSpecError(
            f"平台配置含未知字段 {sorted(unknown)}（拼错了不会静默忽略）"
        )
    return PlatformSpec(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        description=str(raw.get("description") or ""),
        label=str(raw.get("label") or ""),
        self_id=str(raw.get("self_id") or ""),
    )


def build_registry(raw_specs: Any = None) -> PlatformRegistry:
    """按配置造注册表（**默认平台永远在**，额外的追加在后面）。

    Args:
        raw_specs: 额外平台的配置列表（每项是 dict）；`None`/空 = 只有默认平台。

    Returns:
        至少含默认平台的注册表。
    """
    registry = PlatformRegistry([DEFAULT_PLATFORM])
    if not raw_specs:
        return registry
    if not isinstance(raw_specs, (list, tuple)):
        raise PlatformSpecError(
            f"平台列表必须是数组，得到 {type(raw_specs).__name__}"
        )
    for item in raw_specs:
        registry.register(spec_from_mapping(item))
    return registry


__all__ = [
    "DEFAULT_PLATFORM",
    "DEFAULT_PLATFORM_DESCRIPTION",
    "DEFAULT_PLATFORM_ID",
    "DEFAULT_PLATFORM_NAME",
    "KNOWN_PLATFORM_KINDS",
    "PlatformRegistry",
    "PlatformSpec",
    "PlatformSpecError",
    "build_registry",
    "normalize_platform_kind",
    "spec_from_mapping",
]
