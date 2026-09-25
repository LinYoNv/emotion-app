# -*- coding: utf-8 -*-
"""判据：平台实例注册表（`platforms.py`）—— 「加第二个平台」必须安全。

## 背景（情绪 2026-09-21）

> 「**别写死了，以后可能会加入 onebot 机器人，那边也可以获取 umo。**」

改之前「我们是什么平台」写死在 `umo.py` 的三个常量 + 单实例管理器上，
想加平台要改多处、且改一处漏一处 = **umo 写进孤儿会话**（静默数据分裂）。

## 本文件守的三条硬约束（全部来自**实读插件源码**，不是推理）

| # | 约束 | 违反后果（实测出处） |
|---|---|---|
| ① | `name` 不能解析成 `generic` | `proactive.py:1056` 硬拒 generic → **主动消息整链失效**（约 25 个调用点），任何 capability 都补不回来 |
| ② | `description` 不能含平台别名子串 | `platform_compat.py:165-182` 拿 description 做**子串兜底** → 误判成 `onebot` → 插件去 `int(user_id)`（非数字 id **崩**）+ OneBot 原生兜底（**硬失败**） |
| ③ | `description` 不能是 `cronjob` | `tts_tool_sanitizer.py:54` 判成定时任务伪事件 → 跳过空正文守卫（`main.py:18293-18295`）→ **正文可能发两次** |

⚠️ 三条的失败后果**全是静默的**（不报错、只是行为不对），
所以校验必须在**注册时**做（配错就起不来），不能留到运行时。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

from astrbot_host.platforms import (  # noqa: E402
    DEFAULT_PLATFORM,
    DEFAULT_PLATFORM_ID,
    DEFAULT_PLATFORM_NAME,
    PlatformRegistry,
    PlatformSpec,
    PlatformSpecError,
    build_registry,
    normalize_platform_kind,
    spec_from_mapping,
)


# ---------------------------------------------------------------------------
# 0. 与插件的判定算法**必须一致**（照抄 `_normalize_platform_kind`）
# ---------------------------------------------------------------------------


def test_normalize_matches_plugin_algorithm():
    """我们的 `normalize_platform_kind` 必须与插件的判定**逐例一致**。

    ⚠️ 为什么这条排第一：另外三条约束都建立在"我们知道插件会判成什么"
       之上。这个函数一旦与插件漂移，**校验就会漏掉真误判**
       （最坏情况：校验通过、插件却判成 onebot → 崩）。

    样例来源：`platform_compat.py:17-37` 的别名表 + `:165-173` 的算法
    （先小写、去 `-`/空格，再精确查表、最后子串扫描）。
    """
    cases = {
        "webchat": "webchat",
        "WebChat": "webchat",
        "aiocqhttp": "onebot",
        "onebot": "onebot",
        "napcat": "onebot",
        "qq": "onebot",
        "qq_official": "qq_official",
        "telegram": "telegram",
        "tg": "telegram",
        "dingtalk": "dingtalk",
        # 🔴 关键反例：否定句 = 断言（子串扫描）
        "非 OneBot 适配器": "onebot",
        "不是 telegram": "telegram",
        "postgres-tg bridge": "telegram",
        # 认不出来
        "情感陪伴": "generic",
        "": "generic",
        None: "generic",
        "情绪app Web 端": "generic",
    }
    for raw, expected in cases.items():
        assert normalize_platform_kind(raw) == expected, (
            f"{raw!r} 应判成 {expected!r}（与插件算法不一致 → 校验会漏）"
        )


def test_the_old_dangerous_description_would_misclassify():
    """🔴 回归判据：**旧的那个 description 确实是危险的**（不是理论风险）。

    改之前 `OurPlatform` 的默认描述是
    `"情绪app 内置 Web 端（非 OneBot）"` —— 含 `onebot` 子串，
    插件用 description 做子串兜底时会判成 **onebot**。

    这条判据把"为什么必须改"钉死：如果哪天有人把描述改回去，
    它连同下面 `test_description_with_platform_alias_is_rejected` 一起红。
    """
    old = "情绪app 内置 Web 端（非 OneBot）"
    assert normalize_platform_kind(old) == "onebot", (
        "旧 description 会被插件判成 onebot —— 这正是必须改掉它的原因"
    )
    # 新值安全（且与 name 同类型）
    assert normalize_platform_kind("情绪app WebChat") == "webchat"


# ---------------------------------------------------------------------------
# 1. 约束①：name 不能是 generic
# ---------------------------------------------------------------------------


def test_name_resolving_to_generic_is_rejected():
    """`name` 解析成 `generic` 必须**注册即失败**。

    🔴 为什么最严重：插件 `proactive.py:1056` 的身份闸门硬拒 generic：

    ```python
    if not subject or not platform or platform == "generic" or not (adapter or bot_id):
        return False
    ```

    → **主动消息在所有调用点静默失效**。这不是"少个能力"，
      是"整条链断掉"，且 capability 表怎么配都补不回来。
    """
    with pytest.raises(PlatformSpecError) as e:
        PlatformSpec(id="99", name="情感陪伴")
    assert "generic" in str(e.value)
    assert "主动消息" in str(e.value), "报错要说清后果，否则读者不知道为什么不能这样配"


def test_empty_name_is_rejected():
    """空 `name` 也拒绝（空串会解析成 generic，同样断主动消息链）。"""
    with pytest.raises(PlatformSpecError):
        PlatformSpec(id="99", name="")


def test_known_kinds_are_all_accepted():
    """别名表值域里的平台类型都应该能注册（不能只放行 webchat）。"""
    for i, kind in enumerate(sorted({"webchat", "onebot", "qq_official", "telegram",
                                     "wechat_official", "dingtalk"})):
        spec = PlatformSpec(id=f"9{i}", name=kind)
        assert spec.kind == kind


# ---------------------------------------------------------------------------
# 2. 约束②：description 不能含平台别名子串
# ---------------------------------------------------------------------------


def test_description_with_platform_alias_is_rejected():
    """含**别的**平台别名子串的 description 必须被拒（否定句也不行）。"""
    dangerous = [
        "情绪app 内置 Web 端（非 OneBot）",  # ← 真实踩过的那句
        "QQ 官方机器人 adapter",
        "telEgram 转发",
        "napcat relay",
    ]
    for desc in dangerous:
        with pytest.raises(PlatformSpecError) as e:
            PlatformSpec(id="99", name="webchat", description=desc)
        assert "description" in str(e.value)


def test_description_matching_own_kind_is_allowed():
    """与**自己**同类型的描述是允许的（`"情绪app WebChat"` 就该过）。"""
    spec = PlatformSpec(id="11", name="webchat", description="情绪app WebChat")
    assert spec.kind == "webchat"
    # 中文里不含任何平台名的描述也可以
    PlatformSpec(id="21", name="aiocqhttp", description="QQ 机器人")


def test_description_with_plain_chinese_is_allowed():
    """纯中文描述（不含任何别名 token）必须放行 —— 别把校验做得过严。"""
    PlatformSpec(id="99", name="webchat", description="情绪app 内置网页端")
    PlatformSpec(id="98", name="webchat", description="")


# ---------------------------------------------------------------------------
# 3. 约束③：description 不能是 cronjob
# ---------------------------------------------------------------------------


def test_cronjob_description_is_rejected():
    """`cronjob` / `cron` 必须被拒 —— 它会让插件把真实消息当定时任务。

    插件判定是 `.strip().lower()` 后的**完全相等**（`tts_tool_sanitizer.py:54`），
    所以只有裸串危险；命中后走静默降级分支（正文可能发两次）。
    """
    for bad in ["cronjob", "CronJob", "  CRONJOB  ", "cron"]:
        with pytest.raises(PlatformSpecError):
            PlatformSpec(id="99", name="webchat", description=bad)


def test_cronjob_as_substring_is_allowed_because_plugin_uses_exact_match():
    """⚠️ **刻意放行**：`"cronjob handler"` 不是裸串，插件判不出来。

    为什么专门写一条：插件用的是**完全相等**（不是子串），所以
    `"CronJob scheduler"` **不会**触发那个分支。把它也拒掉是**过度校验**
    （本项目的老毛病：「过度处理也是 bug」、「宁可少杀不可错杀」）。
    这条判据把"我们是照实读的源码、不是想当然"钉住。
    """
    spec = PlatformSpec(id="99", name="webchat", description="cronjob handler")
    assert spec.description == "cronjob handler"


# ---------------------------------------------------------------------------
# 4. 形状校验
# ---------------------------------------------------------------------------


def test_platform_id_must_be_usable_as_umo_first_segment():
    """`id` 是 umo 第一段 ⇒ 不能空 / 不能含冒号 / 不能含空白。"""
    with pytest.raises(PlatformSpecError):
        PlatformSpec(id="", name="webchat")
    with pytest.raises(PlatformSpecError):
        PlatformSpec(id="1:1", name="webchat")
    with pytest.raises(PlatformSpecError):
        PlatformSpec(id="1 1", name="webchat")


def test_registry_rejects_duplicate_id():
    """重复 id 必须抛错 —— 两个平台共用 umo 第一段 = **用户数据串号**。

    ⚠️ 静默覆盖更糟：真实现 `context.send_message` 按 `meta().id` 查表
       只命中一个 ⇒ 另一个平台的消息**发不出去**，而且没人知道。
    """
    reg = PlatformRegistry([PlatformSpec(id="11", name="webchat")])
    with pytest.raises(PlatformSpecError) as e:
        reg.register(PlatformSpec(id="11", name="aiocqhttp"))
    assert "重复" in str(e.value)


def test_registry_get_does_not_fall_back_to_default():
    """🔴 `get()` 取不到**必须返回 None**，不许回落默认平台。

    为什么：把"未知平台的消息"静默写进默认平台的用户数据 = 串号。
    调用方应当如实失败（而不是"看起来成功了"）。
    """
    reg = PlatformRegistry()
    assert reg.get("999") is None
    assert reg.get("") is None
    with pytest.raises(PlatformSpecError):
        reg.require("999")


def test_registry_default_is_always_present():
    """默认平台**永远**在注册表里（哪怕配置里一个额外平台都没有）。"""
    reg = build_registry(None)
    assert len(reg) == 1
    assert reg.default.id == DEFAULT_PLATFORM_ID
    assert reg.default.name == DEFAULT_PLATFORM_NAME


# ---------------------------------------------------------------------------
# 5. 「加一个 onebot 平台」的端到端演练（这就是情绪要的那个能力）
# ---------------------------------------------------------------------------


def test_can_register_a_second_onebot_platform():
    """⭐ 情绪要的能力：**在不动默认平台的前提下加一个 onebot 平台**。

    要求：
      * 两个平台并存，`id` 不同（umo 第一段不同 ⇒ 插件里是两份用户数据）；
      * onebot 那个能真的判成 `onebot`（不是 generic）；
      * 默认平台**保持 webchat 语义**（不能被带成 onebot）。
    """
    reg = build_registry(
        [{"id": "21", "name": "aiocqhttp", "description": "QQ 机器人", "label": "QQ"}]
    )
    assert len(reg) == 2
    assert reg.default.id == DEFAULT_PLATFORM_ID, "加平台不该改默认平台"
    assert reg.default.kind == "webchat", "默认平台必须仍是 webchat"

    qq = reg.require("21")
    assert qq.kind == "onebot"
    assert qq.effective_self_id == "aiocqhttp"


def test_two_platforms_produce_different_umo_and_metadata():
    """两个平台产出的 umo 与平台元数据必须**各自一致**（同源，不漂移）。

    ⚠️ 这是"加平台"最容易出的错：umo 用 A 平台、`platform_name` 说 B 平台
       ⇒ 插件按 B 的能力表对待 A 的消息（能力给错），
       而真实现 `send_message` 按 `meta().id` 查表又找不到 A ⇒ 发不出去。
    """
    from astrbot_host.umo import build_inbound_event

    reg = build_registry([{"id": "21", "name": "aiocqhttp", "label": "QQ"}])
    web = build_inbound_event("你好", "u1", platform=reg.default)
    qq = build_inbound_event("你好", "u1", platform=reg.require("21"))

    # umo 第一段 = 平台 id
    assert web.unified_msg_origin.startswith(f"{DEFAULT_PLATFORM_ID}:")
    assert qq.unified_msg_origin.startswith("21:")

    # 同一个 user_id 在两个平台上是**不同的 umo**（= 插件里两份数据）
    assert web.unified_msg_origin != qq.unified_msg_origin

    # umo 第一段必须与 get_platform_id() 一致（真实现按它查表）
    assert web.get_platform_id() == DEFAULT_PLATFORM_ID
    assert qq.get_platform_id() == "21"
    # 类型名来自各自规格
    assert web.get_platform_name() == DEFAULT_PLATFORM_NAME
    assert qq.get_platform_name() == "aiocqhttp"


def test_platform_meta_matches_platform_manager_instances():
    """管理器里的实例元数据必须与规格**逐字一致**（"能构造事件也能发出去"）。

    真实现 `context.send_message` 按 `platform.meta().id == session.platform_name`
    查表（`core/star/context.py:641-643`）→ 事件里的 `platform_id`
    必须能在管理器里找到对应实例，否则**能构造事件但发不出去**。
    """
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.umo import build_inbound_event

    reg = build_registry([{"id": "21", "name": "aiocqhttp"}])
    mgr = build_platform_manager(registry=reg)

    for spec in reg.all():
        # ① 管理器能按 id 取到实例
        inst = mgr.get_inst(spec.id)
        assert inst is not None, f"管理器里没有平台 {spec.id}（事件发不出去）"
        # ② 实例元数据与规格一致
        meta = inst.meta()
        assert meta.id == spec.id
        assert meta.name == spec.name
        assert meta.description == spec.description
        # ③ 事件里的 platform_id 也能查到（真实现的查表路径）
        ev = build_inbound_event("x", "u1", platform=spec)
        assert mgr.get_inst(ev.get_platform_id()) is not None


def test_default_platform_values_are_unchanged():
    """🔴 **回归判据**：默认平台的三个取值必须**逐字不变**。

    它们是插件数据主键的一半 —— 改一个字 = 老用户数据全部对不上（换库）。
    这条是"以后重构别手滑"的保险。
    """
    assert DEFAULT_PLATFORM_ID == "11"
    assert DEFAULT_PLATFORM_NAME == "webchat"
    assert DEFAULT_PLATFORM.description == "情绪app WebChat"
    assert DEFAULT_PLATFORM.id == "11"
    assert DEFAULT_PLATFORM.name == "webchat"


# ---------------------------------------------------------------------------
# 6. 配置解析（`.env` / 设置页声明额外平台）
# ---------------------------------------------------------------------------


def test_spec_from_mapping_rejects_unknown_fields():
    """配置里拼错字段名**不许静默忽略**（否则用户以为配上了）。

    ⚠️ 与本项目"静默丢弃是最贵的 bug"一致：宁可启动失败，
       也不要"配了但没生效"。
    """
    with pytest.raises(PlatformSpecError) as e:
        spec_from_mapping({"id": "21", "name": "aiocqhttp", "nmae": "typo"})
    assert "未知字段" in str(e.value)


def test_spec_from_mapping_applies_the_same_validation():
    """从配置造规格时，三条硬约束**照样**执行（不能绕过校验）。"""
    with pytest.raises(PlatformSpecError):
        spec_from_mapping({"id": "21", "name": "随便"})  # → generic
    with pytest.raises(PlatformSpecError):
        spec_from_mapping({"id": "21", "name": "webchat", "description": "cronjob"})


def test_build_registry_rejects_non_list():
    """平台列表形状错也要响亮失败（不静默当空）。"""
    with pytest.raises(PlatformSpecError):
        build_registry({"id": "21"})
