

async def test_native_prepare_chat_payload_and_query_exist():
    """陪伴 7.0 的后台任务直接调 provider 的 `_prepare_chat_payload` / `_query`。

    生产实测（2026-10-30）：缺这两个方法 ⇒ 插件抛 `AttributeError` ⇒
    分类成 `request_preparation_failed` ⇒ 第 8 步「主动消息测试」报
    `model_not_started`，页面只显示「模型或投递链路失败」。
    """
    from astrbot_host.provider import Provider

    for name in ("_prepare_chat_payload", "_query"):
        assert callable(getattr(Provider, name, None)), f"缺少原生 provider 契约：{name}"


async def test_prepare_chat_payload_builds_messages_and_model():
    from astrbot_host.provider import Provider

    provider = Provider(provider_id="probe", model="probe-model", base_url="", api_key="")
    payloads, context_query = await provider._prepare_chat_payload(
        prompt="hello", contexts=[{"role": "user", "content": "prior"}],
    )
    assert payloads["model"] == "probe-model"
    assert payloads["messages"] == context_query
    assert any(m.get("content") == "prior" for m in payloads["messages"])
    assert any(m.get("content") == "hello" for m in payloads["messages"])


async def test_query_attaches_raw_completion_for_wire_validation():
    """插件按 `raw_completion.choices[0]` 校验；缺它就是 response_contract_rejected。"""
    from types import SimpleNamespace

    from astrbot_host.provider import Provider

    seen = {}

    class _LLM:
        async def generate(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(content="ok-body", finish_reason="stop",
                                   tool_calls=[], reasoning_content="", usage={}, raw={})

    provider = Provider(provider_id="probe", model="m", base_url="", api_key="", llm=_LLM())
    out = await provider._query({"messages": [{"role": "user", "content": "x"}], "model": "m"})
    raw = getattr(out, "raw_completion", None)
    assert raw is not None, "raw_completion 必须被构造（否则插件 wire 校验失败）"
    assert len(raw.choices) == 1
    assert raw.choices[0].finish_reason == "stop"
    assert raw.choices[0].message.content == "ok-body"


async def test_query_calls_parse_hook_like_official():
    """真 AstrBot `_query` 会回调 `_parse_openai_completion(completion, tools)`。

    陪伴 7.0 把这个方法替换成自己的 `parse()` 来拿**原始 completion**；
    漏调用会让它 `observed` 为空 ⇒ `completion_present=False` ⇒
    `response_contract_rejected` / `model_usage_unknown`（生产实测）。
    """
    from types import SimpleNamespace

    from astrbot_host.provider import Provider

    class _LLM:
        async def generate(self, **kwargs):
            return SimpleNamespace(content="body", finish_reason="stop", tool_calls=[],
                                   reasoning_content="", usage={}, raw={})

    provider = Provider(provider_id="probe", model="m", base_url="", api_key="", llm=_LLM())
    seen = {}

    async def parse(completion, tools):
        seen["completion"] = completion
        seen["tools"] = tools
        return "parsed-result"

    provider._parse_openai_completion = parse
    out = await provider._query({"messages": [{"role": "user", "content": "x"}], "model": "m"}, None)
    assert out == "parsed-result", "必须返回插件 parse 的产物"
    assert seen["completion"] is not None, "必须把原始 completion 交给 parse"
    assert seen["completion"].choices[0].finish_reason == "stop"


async def test_raw_completion_message_has_official_attributes():
    """插件 parse 会读 `message.refusal` 与 `model_fields_set`。

    缺了就是 `AttributeError` ⇒ `native_response_parse_failed`
    （生产实测 `completion_present=true` 但 `error_type=AttributeError`）。
    """
    from types import SimpleNamespace

    from astrbot_host.provider import Provider

    class _LLM:
        async def generate(self, **kwargs):
            return SimpleNamespace(content='{"text":"x","claims":[]}', finish_reason="stop",
                                   tool_calls=[], reasoning_content="", usage={}, raw={})

    provider = Provider(provider_id="probe", model="m", base_url="", api_key="", llm=_LLM())
    out = await provider._query({"messages": [{"role": "user", "content": "x"}]})
    message = out.raw_completion.choices[0].message
    assert message.refusal is None
    assert isinstance(message.model_fields_set, set)
    assert message.reasoning_content is None
    assert out.raw_completion.choices[0].finish_reason == "stop"


async def test_provider_official_class_has_parse_openai_completion():
    """插件显式调用 `provider_type._parse_openai_completion(...)`。

    类上缺这个方法 ⇒ `AttributeError` ⇒ `native_response_parse_failed`
    ⇒ 第 8 步 `model_usage_unknown`（生产实测）。
    """
    from astrbot_host.compat.core.provider.sources.providers import ProviderOpenAIOfficial

    assert callable(getattr(ProviderOpenAIOfficial, "_parse_openai_completion", None))


async def test_parse_openai_completion_extracts_text_and_tools():
    from types import SimpleNamespace

    from astrbot_host.compat.core.provider.sources.providers import ProviderOpenAIOfficial

    obj = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
    # 用 dict 形状（`to_llm_response` 的既有契约），与真实上游返回一致
    completion = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": '{"text":"hi","claims":[]}', "tool_calls": None}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    out = await obj._parse_openai_completion(completion, None)
    assert out is not None
    assert '{"text":"hi","claims":[]}' in (out.completion_text or "")
