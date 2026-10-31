# -*- coding: utf-8 -*-
"""领域模型测试：人设 · 消息 · 提示词组装。"""
from __future__ import annotations

import pytest

from emotion_app.domain.message import (
    ROLE_ASSISTANT,
    ROLE_REASONING,
    ROLE_SYSTEM,
    ROLE_USER,
    Message,
    build_upstream_messages,
    from_upstream_message,
)
from emotion_app.domain.persona import Persona
from emotion_app.domain import prompt as prompt_mod

#: 2026-10-29：`PromptSection` 是**组装层**的内部结构，已从 `persona.py` 搬到
#: `prompt.py`。人设结构改成 AstrBot 的「一整段 `system_prompt`」之后，
#: 「人设怎么存」与「提示词怎么组装」是两个概念，分居两个模块。
from emotion_app.domain.prompt import PromptSection


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------


def test_persona_has_astrbot_core_fields():
    """⭐ 字段必须与 AstrBot `Persona`（`core/db/po.py:145`）对齐。

    2026-10-29 重构：原来是「爱语 `PersonaInfo` 的 6 字段」（description /
    personality / scenario / creator_notes + name + avatar_path）。
    情绪拍板改成 AstrBot 的结构，理由见 `domain/persona.py` 的模块 docstring：
    面板只画那 4 个框、而人设正文若在 `prompt_sections` 里界面就是空白，
    且陪伴插件读的是 AstrBot `Personality`（`prompt` 字段），不认那 6 个字段。

    ⚠️ **守的意图没变**：人设的存储结构必须**字段齐全、能被序列化出来**，
       改了名字就说明结构漂了、面板/插件会对不上。
    """
    p = Persona(
        name="小爱", system_prompt="正文", begin_dialogs=["你好"],
        tools=["generate_image"], skills=None, custom_error_message="出错了",
        folder_id="f1", sort_order=3, avatar_path="/a.png",
    )
    d = p.to_dict()
    for key in (
        "name", "system_prompt", "begin_dialogs", "tools", "skills",
        "custom_error_message", "folder_id", "sort_order", "avatar_path",
    ):
        assert key in d, key

    # AstrBot 用 `persona_id` 当主键，且**与 `name` 同值**（见 persona.py docstring）
    assert d["persona_id"] == "小爱" == d["name"]
    # 已删除的旧字段**不许**再出现在出口（否则面板会以为还能编辑它们）
    for gone in ("description", "personality", "scenario", "creator_notes",
                 "prompt_sections"):
        assert gone not in d, f"{gone} 应已从人设结构中删除"


def test_persona_tools_none_and_empty_are_different():
    """🔴 `tools=None`（用全部）与 `tools=[]`（一个都不用）**语义不同**，不许归一。

    AstrBot `Persona.tools` 的默认值是 `None`，插件按 `None` 判"不做白名单过滤"。
    归一成 `[]` 会让插件以为"这个角色不许用任何工具" —— 那是**功能性故障**，
    不只是显示问题（见 `persona.py` 的 `to_personality()` docstring）。
    """
    assert Persona(name="a").to_dict()["tools"] is None
    assert Persona(name="a", tools=[]).to_dict()["tools"] == []
    # 与 AstrBot `Personality` 互转时也必须原样保留
    assert Persona(name="a").to_personality()["tools"] is None
    assert Persona(name="a", tools=[]).to_personality()["tools"] == []



def test_persona_requires_name():
    with pytest.raises(ValueError, match="name"):
        Persona(name="   ").validate()


def test_persona_rejects_overlong_name():
    with pytest.raises(ValueError, match="过长"):
        Persona(name="x" * 201).validate()


def test_persona_api_key_hidden_by_default():
    """🔴 人设的 key 默认不出现在出口。"""
    p = Persona(name="a", api_key="sk-supersecret")
    assert "api_key" not in p.to_dict()
    assert p.to_dict()["api_key_configured"] is True
    assert p.to_dict(include_secrets=True)["api_key"] == "sk-supersecret"


def test_persona_roundtrip():
    """序列化往返：**人设正文与扩展字段都不能丢**。

    2026-10-29：原来的 `prompt_sections` 往返断言已无意义（那个概念没了），
    改成守新结构里**真正的正文载体** `system_prompt`，意图不变 ——
    「导出再导入，人设内容一字不少」。
    """
    p = Persona(
        name="往返", system_prompt="我的人设正文", api_url="https://x/v1", model="m",
        begin_dialogs=["你好呀"], tools=["generate_image"],
    )
    p2 = Persona.from_dict(p.to_dict(include_secrets=True))
    assert p2.name == "往返"
    assert p2.api_url == "https://x/v1"
    assert p2.system_prompt == "我的人设正文"
    assert p2.begin_dialogs == ["你好呀"]
    assert p2.tools == ["generate_image"]


def test_from_dict_migrates_legacy_fields_into_system_prompt():
    """🔴 旧数据（爱语 6 字段 / `prompt_sections`）必须能读出来，**一行都不许丢**。

    这是重构的**数据安全底线**：`emotion.db` 里已有旧结构的人设（莉莉丝、糯米茨）。
    如果 `from_dict()` 不认旧字段，用户的人设会凭空消失。

    ⚠️ 顺序固定（description → personality → scenario → creator_notes），
       这样同样的旧数据每次迁移出**逐字相同**的正文，上游 prompt 缓存才命中。
    """
    legacy = {
        "name": "旧人设",
        "description": "我是描述",
        "personality": "温柔",
        "scenario": "在教室",
        "creator_notes": "备注",
    }
    p = Persona.from_dict(legacy)
    for piece in ("我是描述", "温柔", "在教室", "备注"):
        assert piece in p.system_prompt, piece
    # 顺序固定：描述必须排在备注之前
    assert p.system_prompt.index("我是描述") < p.system_prompt.index("备注")

    # 只有 prompt_sections 的旧数据（面板里改过片段的那种）也要能读
    legacy2 = {
        "name": "片段人设",
        "prompt_sections": [
            {"title": "乙", "content": "BBB", "order": 200},
            {"title": "甲", "content": "AAA", "order": 10},
        ],
    }
    p2 = Persona.from_dict(legacy2)
    assert "AAA" in p2.system_prompt and "BBB" in p2.system_prompt
    # 按 order 排序（甲 order=10 在前），不是按 dict 里的先后
    assert p2.system_prompt.index("AAA") < p2.system_prompt.index("BBB")


def test_from_dict_prefers_system_prompt_over_legacy_fields():
    """新结构数据里 `system_prompt` 是唯一真值，不该再被旧字段覆盖。"""
    p = Persona.from_dict({
        "name": "新", "system_prompt": "新正文", "description": "旧描述",
    })
    assert p.system_prompt == "新正文"


def test_disabled_sections_excluded():
    """停用的片段不参与组装。

    2026-10-29：`Persona.enabled_sections()` 随人设结构重构**已删除**
    （片段不再挂在人设上，"只保留启用片段"这条规则挪到组装层
    `prompt.assemble()` 的末尾过滤）。这里改成**直接守组装层的实际行为** ——
    意图不变：标了 `enabled=False` 的内容**绝不能进最终提示词**。
    """
    p = Persona(name="x", system_prompt="人设正文")
    extra = [
        PromptSection(title="on", content="要的"),
        PromptSection(title="off", enabled=False),
    ]
    out = prompt_mod.assemble(p, include_time=False, extra_sections=extra)
    titles = out.titles()
    assert "on" in titles
    assert "off" not in titles



def test_section_rejects_bad_source():
    """`source` 只能是已知来源（防打错字导致"来源不可追溯"）。"""
    with pytest.raises(ValueError, match="source"):
        PromptSection(title="t", source="hacker").validate()



# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def test_message_has_fourteen_fields():
    """⭐ 照爱语 Message.java 的 14 个字段。"""
    d = Message().to_dict()
    assert len(d) == 14, sorted(d)


def test_reasoning_role_not_sent_upstream():
    """🔴 实测：`role == "reasoning"` 的消息**不发**给上游。"""
    msgs = [
        Message(role=ROLE_USER, content="你好"),
        Message(role=ROLE_REASONING, content="内部推理"),
    ]
    out = build_upstream_messages(msgs)
    assert len(out) == 1
    assert out[0]["content"] == "你好"


def test_system_content_is_plain_by_default():
    """🔴 **默认不包 `<|SYSTEM_INJECT|>`**（2026-10-29 实测改）。

    原来这里断言"必须包标记"—— 那是**照抄爱语的私有行为**，
    而爱语那个标记在标准 OpenAI 协议里不存在。

    实测（同一个 `gemini-3.8-flash-high`，只切这个开关）：

        A 带标记 → 「不是，我是 Gemini，由 Google 训练的大语言模型。」
        B 无标记 → 「（慢吞吞地睁开猩红的眼眸…）除了本小姐，
                     这里难道还会有第二只高贵又可爱的莉莉丝吗」

    ⇒ 带标记时模型把整段人设当**用户数据**，人设彻底失效；
      而现象是"调用成功 + 有回复"，**只看日志永远发现不了**。
    """
    msg = Message(role=ROLE_SYSTEM, content="你是助手")
    out = msg.to_upstream()
    assert out["content"] == "你是助手", (
        "system 必须是裸文本 —— 包 <|SYSTEM_INJECT|> 会让标准上游"
        "把它当用户数据，人设失效"
    )


def test_system_markers_can_still_be_enabled_explicitly():
    """旧行为**保留但必须显式开启**（给真要用爱语协议的场景留口子）。"""
    msg = Message(role=ROLE_SYSTEM, content="x")
    assert msg.to_upstream(inject_system_markers=True)["content"] == (
        "<|SYSTEM_INJECT|>x<|END_INJECT|>"
    )


def test_reasoning_content_uses_configurable_field():
    msg = Message(role=ROLE_ASSISTANT, content="答", reasoning_content="想了")
    assert msg.to_upstream(thinking_field="reasoning_content")["reasoning_content"] == "想了"
    assert msg.to_upstream(thinking_field="thinking")["thinking"] == "想了"


def test_message_roundtrip_preserves_all_fields():
    """⭐ 内部保留全字段 —— 降维到 OpenAI 会丢信息。"""
    m = Message(
        role=ROLE_ASSISTANT, content="c", audio_path="/a.mp3",
        video_path="/v.mp4", video_desc="视频描述", reasoning_content="r",
        reasoning_duration=5, tool_calls="[]", tool_call_id="t1",
        reply_content="引用的", forwarded_content="转发的",
        image_path="/i.png", image_desc="图描述",
    )
    m2 = Message.from_dict(m.to_dict())
    assert m2 == m


def test_tool_call_fields_are_passed_through():
    """🔴 回归：工具协议字段必须透传到上游。

    实测踩到的真 bug：`to_upstream()` 漏了 `tool_calls` 与 `tool_call_id`
    → 回填后的二次请求被上游 **400** 拒绝，工具闭环直接断掉。
    这两个字段少任何一个都不行。
    """
    import json as _json

    assistant = Message(
        role="assistant", content="",
        tool_calls=_json.dumps([
            {"id": "c1", "type": "function",
             "function": {"name": "generate_image", "arguments": '{"prompt":"猫"}'}}
        ]),
    )
    out = assistant.to_upstream()
    assert "tool_calls" in out
    assert out["tool_calls"][0]["id"] == "c1"
    # 带 tool_calls 时空 content 应转成 None（部分上游不接受空串）
    assert out["content"] is None

    tool_msg = Message(role="tool", content='{"ok":1}', tool_call_id="c1")
    out2 = tool_msg.to_upstream()
    assert out2["tool_call_id"] == "c1"
    assert out2["role"] == "tool"


def test_tool_message_requires_call_id():
    """🔴 tool 消息没有 tool_call_id → 明确报错（协议硬要求）。"""
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role="tool", content="x").validate()


def test_assistant_without_tool_calls_keeps_content():
    out = Message(role="assistant", content="普通回复").to_upstream()
    assert out["content"] == "普通回复"
    assert "tool_calls" not in out


def test_message_rejects_bad_role():
    with pytest.raises(ValueError, match="role"):
        Message(role="hacker").validate()


def test_from_upstream_plain_text():
    m = from_upstream_message({"role": "user", "content": "你好"})
    assert m.role == ROLE_USER
    assert m.content == "你好"


def test_from_upstream_multimodal_array():
    """多模态 content 数组 → 抽文本 + 记图片。"""
    m = from_upstream_message({
        "role": "user",
        "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
    })
    assert m.content == "这是什么"
    assert "图片" in m.image_desc


def test_from_upstream_rejects_non_dict():
    with pytest.raises(ValueError):
        from_upstream_message("not a dict")


# ---------------------------------------------------------------------------
# 提示词组装（⭐ 情绪 A3 指出的核心）
# ---------------------------------------------------------------------------


def test_assemble_includes_persona_body():
    """⭐ 人设正文必须进提示词 —— 且现在**恰好是一段 `【人设】`**。

    2026-10-29 结构变更：原来 `persona_core_sections()` 从爱语 6 字段
    （description/personality/scenario）各产一段，标题固定为
    「角色设定 / 性格 / 场景 / 创作者备注」。现在人设正文就是
    **一整段 `system_prompt`**（对齐 AstrBot），所以只产生**一段**，标题 `"人设"`。

    ⚠️ **守的意图没变**：人设内容必须出现在组装结果里、且能按标题追溯来源。
       变的只是"几段、叫什么标题" —— 那正是这次重构的内容。
    """
    p = Persona(name="小爱", system_prompt="描述\n\n性格\n\n场景")
    out = prompt_mod.assemble(p, include_time=False)
    assert out.titles() == [prompt_mod.PERSONA_CORE_TITLE]

    text = out.text
    for piece in ("描述", "性格", "场景"):
        assert piece in text, piece


def test_persona_core_section_title_is_astrbot_style():
    """正文片段的标题是 `"人设"`（`PERSONA_CORE_TITLE`）。

    ⚠️ 这条守的是**面板与测试的公共契约**：标题字面量只允许有一个来源
       （常量），一旦漂移，去重/追溯就会静默失效。
    """
    assert prompt_mod.PERSONA_CORE_TITLE == "人设"
    # 旧的爱语 4 个标题仍留在集合里 —— 那是**兼容旧数据**用的
    # （from_dict 迁移时不能把它们当成"用户手写的额外片段"重复注入）
    assert prompt_mod.PERSONA_CORE_TITLE in prompt_mod.PERSONA_CORE_TITLES
    for legacy in ("角色设定", "性格", "场景", "创作者备注"):
        assert legacy in prompt_mod.PERSONA_CORE_TITLES, legacy


def test_assemble_skips_empty_body():
    """空正文不产生片段 —— 避免空标题污染稳定前缀。"""
    p = Persona(name="只有名字", system_prompt="")
    out = prompt_mod.assemble(p, include_time=False)
    assert out.titles() == []

    # 纯空白也算空（`persona_core_sections` 用 strip 判断）
    out2 = prompt_mod.assemble(Persona(name="空白", system_prompt="   \n  "),
                               include_time=False)
    assert out2.titles() == []


def test_assemble_formats_sections_with_brackets():
    """分节用 `【】`（与爱语一致）。"""
    p = Persona(name="x", system_prompt="我是描述")
    text = prompt_mod.assemble(p, include_time=False).text
    assert f"【{prompt_mod.PERSONA_CORE_TITLE}】" in text
    assert "我是描述" in text


def test_assemble_stable_order():
    """⭐ 两次组装结果必须一致（P3：稳定前缀）。"""
    p = Persona(name="x", system_prompt="稳定正文")
    extra = [PromptSection(title="附加", content="c", order=100)]
    assert (prompt_mod.assemble(p, extra_sections=extra).titles()
            == prompt_mod.assemble(p, extra_sections=extra).titles())


def test_assemble_puts_volatile_content_later():
    """⭐ 易变内容（记忆/模块注入）应排在稳定人设**之后**。"""
    p = Persona(name="x", system_prompt="稳定的")
    extra = [PromptSection(title="记忆", content="易变的", order=900, source="memory")]
    out = prompt_mod.assemble(p, include_time=False, extra_sections=extra)
    assert (out.titles().index(prompt_mod.PERSONA_CORE_TITLE)
            < out.titles().index("记忆"))


def test_describe_is_structural_not_content():
    """describe() 给日志用，**不含正文**（避免日志过长/泄漏）。"""
    p = Persona(name="x", system_prompt="很长的描述" * 100)
    desc = prompt_mod.describe(prompt_mod.assemble(p))
    assert "很长的描述" not in str(desc)
    assert desc["section_count"] >= 1
    assert "builtin" in desc["by_source"]


def test_time_section_goes_last():
    """🔴 回归：时间片段必须**排在最后**。

    实测踩到：它最初排最前，而它每秒都变 → 整个 system 前缀被打乱
    → **上游 prompt 缓存永远命中不了**，正好毁掉爱语"记忆好"的机制。
    """
    p = Persona(name="x", system_prompt="稳定的人设")
    extra = [PromptSection(title="记忆", content="易变", order=900, source="memory")]
    out = prompt_mod.assemble(p, extra_sections=extra)
    titles = out.titles()
    assert titles[-1] == "当前时间"
    assert titles.index(prompt_mod.PERSONA_CORE_TITLE) < titles.index("当前时间")


def test_time_section_order_is_large():
    assert prompt_mod.ORDER_TIME > prompt_mod.ORDER_CONTEXT


def test_prefix_is_stable_across_calls():
    """稳定前缀：除时间外，两次组装的前缀部分必须逐字相同。"""
    p = Persona(name="x", system_prompt="稳定正文")
    a = prompt_mod.assemble(p).sections
    b = prompt_mod.assemble(p).sections
    # 去掉时间片段后应完全一致
    strip = lambda ss: [s.to_dict() for s in ss if s.title != "当前时间"]
    assert strip(a) == strip(b)


def test_builtin_sections_are_minimal():
    """内置片段刻意极少 —— 不抄爱语那些硬编码（严重健忘症模式等）。"""
    titles = [s.title for s in prompt_mod.builtin_sections()]
    assert len(titles) <= 3
    assert "记忆管理系统：严重健忘症模式" not in titles
