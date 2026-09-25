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
from emotion_app.domain.persona import Persona, PromptSection
from emotion_app.domain import prompt as prompt_mod


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------


def test_persona_has_six_tavern_fields():
    """⭐ 6 个字段必须与爱语 PersonaInfo / SillyTavern 标准卡一致。"""
    p = Persona(
        name="小爱", description="描述", personality="性格",
        scenario="场景", creator_notes="备注", avatar_path="/a.png",
    )
    d = p.to_dict()
    for key in ("name", "description", "personality", "scenario", "creator_notes", "avatar_path"):
        assert key in d


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
    p = Persona(
        name="往返", description="d", api_url="https://x/v1", model="m",
        prompt_sections=[PromptSection(title="自定义", content="内容", order=50)],
    )
    p2 = Persona.from_dict(p.to_dict(include_secrets=True))
    assert p2.name == "往返"
    assert p2.api_url == "https://x/v1"
    assert p2.prompt_sections[0].title == "自定义"


def test_enabled_sections_sorted_stably():
    """⭐ 排序必须**确定性**（顺序抖动会破坏上游 prompt 缓存）。"""
    p = Persona(name="x", prompt_sections=[
        PromptSection(title="b", order=20),
        PromptSection(title="a", order=20),
        PromptSection(title="c", order=10),
    ])
    assert [s.title for s in p.enabled_sections()] == ["c", "a", "b"]
    # 再跑一次结果必须一致
    assert [s.title for s in p.enabled_sections()] == ["c", "a", "b"]


def test_disabled_sections_excluded():
    p = Persona(name="x", prompt_sections=[
        PromptSection(title="on", enabled=True),
        PromptSection(title="off", enabled=False),
    ])
    assert [s.title for s in p.enabled_sections()] == ["on"]


def test_section_rejects_bad_source():
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


def test_system_content_wrapped_with_markers():
    """🔴 实测：system 的 content 包 `<|SYSTEM_INJECT|>…<|END_INJECT|>`。"""
    msg = Message(role=ROLE_SYSTEM, content="你是助手")
    out = msg.to_upstream()
    assert out["content"] == "<|SYSTEM_INJECT|>你是助手<|END_INJECT|>"


def test_system_markers_can_be_disabled():
    msg = Message(role=ROLE_SYSTEM, content="x")
    assert msg.to_upstream(inject_system_markers=False)["content"] == "x"


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


def test_assemble_includes_persona_fields():
    p = Persona(name="小爱", description="描述", personality="性格", scenario="场景")
    out = prompt_mod.assemble(p)
    titles = out.titles()
    assert "角色设定" in titles
    assert "性格" in titles
    assert "场景" in titles


def test_assemble_skips_empty_fields():
    """空字段不产生片段 —— 避免空标题污染稳定前缀。"""
    p = Persona(name="只有名字", description="", personality="", scenario="")
    out = prompt_mod.assemble(p, include_time=False)
    assert out.titles() == []


def test_assemble_formats_sections_with_brackets():
    """分节用 `【】`（与爱语一致）。"""
    p = Persona(name="x", description="我是描述")
    text = prompt_mod.assemble(p, include_time=False).text
    assert "【角色设定】" in text
    assert "我是描述" in text


def test_assemble_stable_order():
    """⭐ 两次组装结果必须一致（P3：稳定前缀）。"""
    p = Persona(name="x", description="d", personality="p", scenario="s",
                prompt_sections=[PromptSection(title="附加", content="c", order=100)])
    a = prompt_mod.assemble(p).text
    b = prompt_mod.assemble(p).text
    # 时间片段会变，所以比较 section 顺序
    assert prompt_mod.assemble(p).titles() == prompt_mod.assemble(p).titles()


def test_assemble_puts_volatile_content_later():
    """⭐ 易变内容（记忆/模块注入）应排在稳定人设**之后**。"""
    p = Persona(name="x", description="稳定的")
    extra = [PromptSection(title="记忆", content="易变的", order=900, source="memory")]
    out = prompt_mod.assemble(p, include_time=False, extra_sections=extra)
    assert out.titles().index("角色设定") < out.titles().index("记忆")


def test_describe_is_structural_not_content():
    """describe() 给日志用，**不含正文**（避免日志过长/泄漏）。"""
    p = Persona(name="x", description="很长的描述" * 100)
    desc = prompt_mod.describe(prompt_mod.assemble(p))
    assert "很长的描述" not in str(desc)
    assert desc["section_count"] >= 1
    assert "builtin" in desc["by_source"]


def test_time_section_goes_last():
    """🔴 回归：时间片段必须**排在最后**。

    实测踩到：它最初排最前，而它每秒都变 → 整个 system 前缀被打乱
    → **上游 prompt 缓存永远命中不了**，正好毁掉爱语"记忆好"的机制。
    """
    p = Persona(name="x", description="稳定的人设")
    extra = [PromptSection(title="记忆", content="易变", order=900, source="memory")]
    out = prompt_mod.assemble(p, extra_sections=extra)
    titles = out.titles()
    assert titles[-1] == "当前时间"
    assert titles.index("角色设定") < titles.index("当前时间")


def test_time_section_order_is_large():
    assert prompt_mod.ORDER_TIME > prompt_mod.ORDER_CONTEXT


def test_prefix_is_stable_across_calls():
    """稳定前缀：除时间外，两次组装的前缀部分必须逐字相同。"""
    p = Persona(name="x", description="d", personality="p")
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
