"""Isolated request-tool and response-hook bridge regressions."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from emotion_app.domain.chat import PreparedLLMRequest
from emotion_app.domain.message import Message
from emotion_app.domain.tools import ToolCall, ToolResult
from emotion_app.domain.upstream import ChatResult
from emotion_app.web.llm_turn import prepare_llm_turn


PARAMETERS = {
    "type": "object", "properties": {"key": {"type": "string"}},
    "required": ["key"], "additionalProperties": False,
}


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "host"))
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent, MessageChain
    from astrbot_host.compat.core.provider.entities import ProviderRequest
    from astrbot_host.compat.core.message.components import Plain
    import astrbot_host.llm_hooks as hooks

    return SimpleNamespace(
        Tool=FunctionTool, ToolSet=ToolSet, Request=ProviderRequest,
        Event=AstrMessageEvent, Chain=MessageChain, Plain=Plain, hooks=hooks,
    )


def _native(call):
    return {
        "id": call.id, "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def _messages(call):
    return [
        Message(role="user", content="look up the isolated value"),
        Message(role="assistant", tool_calls=json.dumps([_native(call)])),
    ]


def _prepare(bridge, tools, event=None, messages=None, context=None):
    event = event if event is not None else bridge.Event(message_str="isolated")
    req = bridge.Request(func_tool=tools)
    req.model = "isolated-model"
    return prepare_llm_turn(messages or [], req, event, context=context)


async def test_native_subclass_call_gets_original_event_context_and_protocol(bridge):
    event = bridge.Event(message_str="original event")
    context = object()
    call = ToolCall("call-current", "request_tool", '{"key":"value"}')
    seen = []

    class NativeTool(bridge.Tool):
        async def call(self, wrapper, **kwargs):
            assert wrapper.context.event is event
            assert wrapper.context.context is context
            assert wrapper.messages[-1]["tool_calls"] == [_native(call)]
            completed = {m.get("tool_call_id") for m in wrapper.messages if m["role"] == "tool"}
            assert completed == {"call-previous"}
            assert wrapper.messages[-3]["tool_calls"][0]["id"] == "call-previous"
            seen.append(wrapper)
            return ToolResult(message=kwargs["key"])

    tool = NativeTool(name=call.name, parameters=PARAMETERS)
    tool.handler = AsyncMock(side_effect=AssertionError("must prefer subclass call"))
    previous = ToolCall("call-previous", call.name, call.arguments)
    messages = [
        *_messages(previous),
        Message(role="tool", tool_call_id=previous.id, content="previous receipt"),
        _messages(call)[-1],
    ]
    turn = _prepare(bridge, bridge.ToolSet([tool]), event, messages, context)
    assert isinstance(turn, PreparedLLMRequest)
    assert turn.messages is messages
    assert turn.model == "isolated-model"
    assert turn.tools == bridge.ToolSet([tool]).openai_schema()
    result = await turn.execute_tool(call, messages)
    assert result.success and result.message == "value"
    assert len(seen) == 1
    tool.handler.assert_not_called()


async def test_concurrent_turns_do_not_share_event_wrapper_or_toolset(bridge):
    arrived = asyncio.Event()
    seen = []
    turns = []
    calls = []
    messages = []
    events = [bridge.Event(message_str=marker) for marker in ("a", "b")]

    class NativeTool(bridge.Tool):
        async def call(self, wrapper, **kwargs):
            seen.append(wrapper)
            if len(seen) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=3)
            assert wrapper.context.event.message_str == kwargs["key"]
            assert wrapper.messages[-1]["tool_calls"][0]["id"] == "call-" + kwargs["key"]
            return kwargs["key"]

    for marker, event in zip(("a", "b"), events):
        call = ToolCall("call-" + marker, "tool_" + marker, json.dumps({"key": marker}))
        msgs = _messages(call)
        turns.append(_prepare(bridge, bridge.ToolSet([
            NativeTool(name=call.name, parameters=PARAMETERS),
        ]), event, msgs))
        calls.append(call)
        messages.append(msgs)
    results = await asyncio.gather(*(
        turn.execute_tool(call, msgs) for turn, call, msgs in zip(turns, calls, messages)
    ))
    assert [r.message for r in results] == ["a", "b"]
    assert seen[0] is not seen[1]
    denied = await turns[0].execute_tool(calls[1], messages[1])
    assert not denied.success
    assert len(seen) == 2


async def test_same_event_consecutive_requests_use_distinct_wrappers_and_native_ids(bridge):
    event = bridge.Event(message_str="same admitted event")
    wrappers = []
    receipts = []

    class NativeTool(bridge.Tool):
        async def call(self, wrapper, **kwargs):
            assert wrapper.context.event is event
            completed = {m.get("tool_call_id") for m in wrapper.messages if m["role"] == "tool"}
            native = next(
                call for m in reversed(wrapper.messages) if m["role"] == "assistant"
                for call in m.get("tool_calls", []) if call["id"] not in completed
            )
            assert json.loads(native["function"]["arguments"]) == kwargs
            wrappers.append(wrapper)
            receipts.append(native["id"])
            return native["id"]

    tool = NativeTool(name="repeated", parameters=PARAMETERS)
    tools = bridge.ToolSet([tool])
    first_call = ToolCall("call-first", tool.name, '{"key":"same args"}')
    first_messages = _messages(first_call)
    first = _prepare(bridge, tools, event, first_messages)
    assert (await first.execute_tool(first_call, first_messages)).message == "call-first"
    first_messages.append(Message(role="tool", tool_call_id=first_call.id, content="first receipt"))
    second_call = ToolCall("call-second", tool.name, first_call.arguments)
    second_messages = [*first_messages, _messages(second_call)[-1]]
    second = _prepare(bridge, tools, event, second_messages)
    assert (await second.execute_tool(second_call, second_messages)).message == "call-second"
    assert wrappers[0] is not wrappers[1]
    assert wrappers[0].messages[-1]["tool_calls"][0]["id"] == first_call.id
    assert receipts == ["call-first", "call-second"]
    replay = await second.execute_tool(first_call, second_messages)
    assert not replay.success and replay.must_not_claim_sent
    assert receipts == ["call-first", "call-second"]


async def test_inactive_tool_schemas_are_not_offered(bridge):
    active = bridge.Tool(name="active")
    inactive = bridge.Tool(name="inactive", active=False)
    turn = _prepare(bridge, bridge.ToolSet([active, inactive]))
    assert [item["function"]["name"] for item in turn.tools] == ["active"]
    unavailable = _prepare(bridge, bridge.ToolSet([inactive]))
    assert unavailable.tools is None
    call = ToolCall("inactive-call", "inactive", "{}")
    result = await unavailable.execute_tool(call, _messages(call))
    assert not result.success and result.must_not_claim_sent


@pytest.mark.parametrize("mode", ["event", "keyword_event", "no_event", "sync"])
async def test_plain_handlers_are_bound_by_signature(bridge, mode):
    event = bridge.Event(message_str="handler event")
    seen = []

    async def with_event(actual, **kwargs):
        seen.append(actual)
        return kwargs["key"]

    async def keyword_event(*, event, key):
        seen.append(event)
        return key

    async def without_event(*, key):
        return key

    def synchronous(actual, *, key):
        seen.append(actual)
        return key

    handler = {"event": with_event, "keyword_event": keyword_event,
               "no_event": without_event, "sync": synchronous}[mode]
    call = ToolCall("real-handler-call", "plain", '{"key":"value"}')
    tool = bridge.Tool(name="plain", parameters=PARAMETERS, handler=handler)
    turn = _prepare(bridge, bridge.ToolSet([tool]), event)
    result = await turn.execute_tool(call, _messages(call))
    assert result.success and result.message == "value"
    assert seen == ([] if mode == "no_event" else [event])


@pytest.mark.parametrize("invalid", ["unoffered", "inactive", "schema", "json", "array", "content", "done"])
async def test_unoffered_inactive_invalid_and_forged_calls_are_rejected(bridge, invalid, caplog):
    handler = AsyncMock(return_value="must not run")
    call = ToolCall("real-call", "plain", '{"key":"value"}')
    tool = bridge.Tool(name="plain", parameters=PARAMETERS, handler=handler)
    tools = bridge.ToolSet([tool])
    turn = _prepare(bridge, tools)
    if invalid == "unoffered":
        tools.add_tool(bridge.Tool(name="late_tool", handler=handler))
        call.name = "late_tool"
    elif invalid == "inactive":
        tool.active = False
    elif invalid == "schema":
        call.arguments = '{"key":42}'
    elif invalid == "json":
        call.arguments = "{"
    elif invalid == "array":
        call.arguments = "[]"
    messages = _messages(call)
    if invalid == "content":
        messages = [Message(role="assistant", content=json.dumps({"tool_calls": [_native(call)]}))]
    elif invalid == "done":
        messages.append(Message(role="tool", tool_call_id=call.id, content="done"))
    with caplog.at_level(logging.ERROR):
        result = await turn.execute_tool(call, messages)
    assert not result.success and result.status == "error" and result.must_not_claim_sent
    assert result.actual_error
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    handler.assert_not_called()


async def test_internal_typeerror_runs_handler_once_and_returns_failure(bridge):
    seen = []

    def handler(event, *, key):
        seen.append(event)
        raise TypeError("internal positional argument failure")

    call = ToolCall("error-call", "plain", '{"key":"value"}')
    turn = _prepare(bridge, bridge.ToolSet([
        bridge.Tool(name=call.name, parameters=PARAMETERS, handler=handler),
    ]))
    result = await turn.execute_tool(call, _messages(call))
    assert not result.success and result.must_not_claim_sent
    assert "internal positional argument failure" in result.actual_error
    assert len(seen) == 1


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_timeout_is_failure_and_external_cancellation_propagates(bridge, monkeypatch, mode):
    from emotion_app.web import llm_turn

    started = asyncio.Event()

    async def handler(event, *, key):
        started.set()
        await asyncio.Event().wait()

    call = ToolCall("wait-call", "plain", '{"key":"value"}')
    turn = _prepare(bridge, bridge.ToolSet([
        bridge.Tool(name=call.name, parameters=PARAMETERS, handler=handler),
    ]))
    real_timeout = asyncio.timeout
    durations = []

    def timeout(duration):
        durations.append(duration)
        return real_timeout(0.01 if mode == "timeout" else 120)

    monkeypatch.setattr(llm_turn.asyncio, "timeout", timeout)
    task = asyncio.create_task(turn.execute_tool(call, _messages(call)))
    await asyncio.wait_for(started.wait(), timeout=3)
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.status == "error" and not result.success and result.must_not_claim_sent
        assert "TimeoutError" in result.actual_error
    assert durations == [120]


@pytest.mark.parametrize("mode", ["text", "structured", "mcp_error", "image", "unknown"])
async def test_mcp_results_use_content_and_never_repr_success(bridge, mode):
    from mcp.types import CallToolResult, ImageContent, TextContent

    value = {
        "text": CallToolResult(content=[TextContent(type="text", text="receipt")]),
        "structured": CallToolResult(content=[], structuredContent={"value": 42}),
        "mcp_error": CallToolResult(content=[TextContent(type="text", text="denied")], isError=True),
        "image": CallToolResult(content=[ImageContent(type="image", data="eA==", mimeType="image/png")]),
        "unknown": object(),
    }[mode]
    call = ToolCall("mcp-call", "plain", '{"key":"value"}')
    turn = _prepare(bridge, bridge.ToolSet([
        bridge.Tool(name=call.name, parameters=PARAMETERS, handler=AsyncMock(return_value=value)),
    ]))
    result = await turn.execute_tool(call, _messages(call))
    if mode in ("text", "structured"):
        assert result.success
        assert result.message == ("receipt" if mode == "text" else '{"value": 42}')
    else:
        assert not result.success and result.must_not_claim_sent
        if mode == "mcp_error":
            assert "denied" in result.actual_error


@pytest.mark.parametrize("with_tools", [False, True])
async def test_response_hook_consumes_chain_and_reasoning_preserving_identity(bridge, monkeypatch, with_tools):
    event = bridge.Event(message_str="response original")
    tools = bridge.ToolSet([bridge.Tool(name="plain")]) if with_tools else None
    call = ToolCall("response-call", "plain", "{}")
    result = ChatResult(
        content="raw model text", reasoning_content="raw reasoning",
        tool_calls=[_native(call)], usage={"input_tokens": 7}, raw={"marker": object()},
    )
    calls, usage, raw = result.tool_calls, result.usage, result.raw
    responses = []

    async def post(actual, response):
        assert actual is event
        assert response.completion_text == "raw model text"
        assert response.tools_call_ids == ([call.id] if with_tools else [])
        response.result_chain = bridge.Chain(chain=[bridge.Plain("processed chain")])
        response.reasoning_content = "processed reasoning"
        responses.append(response)

    monkeypatch.setattr(bridge.hooks, "star_handlers_registry", SimpleNamespace(
        get_handlers_by_event_type=lambda *args, **kwargs: [SimpleNamespace(
            handler=post, handler_full_name="isolated.response",
        )],
    ))
    turn = _prepare(bridge, tools, event)
    if not with_tools:
        assert turn.tools is None and turn.execute_tool is None
    assert await turn.response_hook(result) is result
    assert result.content == "processed chain"
    assert result.reasoning_content == "processed reasoning"
    assert result.tool_calls is calls and result.usage is usage and result.raw is raw
    assert event.get_extra("_emotion_llm_response") is responses[0]
    assert responses[0].result_chain.get_plain_text() == "processed chain"


@pytest.mark.parametrize("empty_chain", [False, True])
async def test_response_hooks_for_concurrent_events_keep_identity_and_clear_text(bridge, monkeypatch, empty_chain):
    arrived = asyncio.Event()
    events = [bridge.Event(message_str=marker) for marker in ("a", "b")]
    seen = []

    async def post(event, response):
        seen.append((event, response))
        if len(seen) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=3)
        response.completion_text = event.message_str if event is events[0] else ""
        if empty_chain and event is events[1]:
            response._completion_text = "stale fallback must not leak"
            response.result_chain = bridge.Chain(chain=[])
        response.reasoning_content = ""

    monkeypatch.setattr(bridge.hooks, "star_handlers_registry", SimpleNamespace(
        get_handlers_by_event_type=lambda *args, **kwargs: [SimpleNamespace(
            handler=post, handler_full_name="isolated.concurrent_response",
        )],
    ))
    turns = [_prepare(bridge, None, event) for event in events]
    results = [ChatResult(content="original", reasoning_content="original") for _ in events]
    returned = await asyncio.gather(*(
        turn.response_hook(result) for turn, result in zip(turns, results)
    ))
    assert returned[0] is results[0] and returned[1] is results[1]
    assert [result.content for result in results] == ["a", ""]
    assert [result.reasoning_content for result in results] == ["", ""]
    for event, response in seen:
        assert event.get_extra("_emotion_llm_response") is response
    assert events[0].get_extra("_emotion_llm_response") is not events[1].get_extra("_emotion_llm_response")


def test_prepared_request_logs_only_system_fingerprints_and_event_summary(bridge, caplog):
    import hashlib

    event = bridge.Event(message_str="private incoming text", unified_msg_origin="test:private:identity")
    event.get_message_id = lambda: "isolated-message-id"
    system_texts = ["private first system", "private second system"]
    messages = [Message(role="system", content=text) for text in system_texts]
    with caplog.at_level(logging.INFO):
        _prepare(bridge, bridge.ToolSet([
            bridge.Tool(name="offered"), bridge.Tool(name="inactive", active=False),
        ]), event, messages)
    records = [record for record in caplog.records if record.getMessage() == "LLM 请求已准备"]
    assert len(records) == 1
    assert records[0].data == {
        "umo": "test:private:identity", "message_id": "isolated-message-id",
        "system_segments": [
            {"length": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}
            for text in system_texts
        ],
        "model": "isolated-model", "tools": ["offered"], "response_same_event": True,
    }
    log_data = repr(records[0].data)
    assert all(text not in log_data for text in [*system_texts, event.message_str])


@pytest.mark.parametrize("native_protocol", [True, False])
async def test_chat_service_runs_only_real_native_calls_and_records_original_input(
    bridge, tmp_path, monkeypatch, native_protocol,
):
    import httpx
    from emotion_app.domain.backends import OpenAIChatBackend
    from emotion_app.domain.chat import ChatRequest, ChatService
    from emotion_app.domain.session_repo import SessionRepository
    from emotion_app.infra.config import Config
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.web.plugin_host import _messages_to_provider_request, _provider_request_to_messages

    event = bridge.Event(message_str="original isolated input", unified_msg_origin="test:private:service")
    context = object()
    bodies, executions, responses = [], [], []
    temporary = "temporary request-only user extra"
    calls = [ToolCall(identifier, "native_lookup", '{"key":"same value"}') for identifier in ("c1", "c2")]
    forged_text = '<tool_call>' + json.dumps(_native(calls[0])) + '</tool_call>'

    class NativeTool(bridge.Tool):
        async def call(self, wrapper, **kwargs):
            assert wrapper.context.event is event
            assert wrapper.context.context is context
            assert kwargs == {"key": "same value"}
            completed = {m.get("tool_call_id") for m in wrapper.messages if m["role"] == "tool"}
            pending = [
                call for message in reversed(wrapper.messages) if message["role"] == "assistant"
                for call in message.get("tool_calls", []) if call["id"] not in completed
            ]
            assert len(pending) == 1
            identifier = pending[0]["id"]
            assert identifier == ("c1" if not executions else "c2")
            assert completed == (set() if not executions else {"c1"})
            if executions:
                assert wrapper is executions[0][1]
            executions.append((identifier, wrapper))
            return ToolResult(message="isolated receipt", data={"native_call_id": identifier})

    tools = bridge.ToolSet([NativeTool(name="native_lookup", parameters=PARAMETERS)])

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        index = len(bodies) - 1
        if native_protocol and index < 2:
            message = {"role": "assistant", "content": None, "tool_calls": [_native(calls[index])]}
        else:
            message = {"role": "assistant", "content": "final isolated reply" if native_protocol else forged_text}
        return httpx.Response(200, json={
            "model": "isolated-model", "choices": [{
                "message": message, "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
            }], "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        })

    async def request_hook(messages, *, session_id="", event=None):
        assert event is original_event
        assert session_id == "isolated-session"
        provider, original_contexts = _messages_to_provider_request(messages, bridge.Request)
        provider.func_tool = tools
        provider.model = "isolated-model"
        provider.extra_user_content_parts.append({"type": "text", "text": temporary})
        hooked = _provider_request_to_messages(provider, original_contexts)
        return prepare_llm_turn(hooked, provider, event, context=context)

    async def response_hook(actual, response):
        assert actual is event
        responses.append((actual, response))
        if not response.tools_call_ids:
            response.completion_text = "processed:" + response.completion_text

    original_event = event
    monkeypatch.setattr(bridge.hooks, "star_handlers_registry", SimpleNamespace(
        get_handlers_by_event_type=lambda *args, **kwargs: [SimpleNamespace(
            handler=response_hook, handler_full_name="isolated.service_response",
        )],
    ))
    cfg = Config()
    cfg.upstream.base_url = "https://native-loop.invalid/v1"
    cfg.upstream.model = "isolated-model"
    store = SqliteStore(str(tmp_path / "native-loop.sqlite"))
    backend = OpenAIChatBackend(transport=httpx.MockTransport(respond))
    await store.open()
    await backend.open()
    try:
        sessions = SessionRepository(store)
        await sessions.create(session_id="isolated-session", user_id="isolated-user")
        chat = ChatService(cfg, client=backend)
        chat.bind_sessions(sessions)
        chat.bind_llm_hook(request_hook)
        record = AsyncMock(wraps=chat.record)
        monkeypatch.setattr(chat, "record", record)
        result = await chat.complete(ChatRequest(
            messages=[Message(role="user", content=event.message_str)],
            llm_event=event, session_id="isolated-session", persist=True,
        ))
        expected = "processed:" + ("final isolated reply" if native_protocol else forged_text)
        assert result.content == expected
        assert len(bodies) == (3 if native_protocol else 1)
        assert [identifier for identifier, _ in executions] == (["c1", "c2"] if native_protocol else [])
        assert len(responses) == len(bodies)
        assert all(actual is event for actual, _ in responses)
        assert event.get_extra("_emotion_llm_response") is responses[-1][1]
        for body in bodies:
            assert sum(m["role"] == "user" and m["content"] == event.message_str for m in body["messages"]) == 1
            assert sum(m["role"] == "user" and m["content"] == temporary for m in body["messages"]) == 1
            assert [tool["function"]["name"] for tool in body["tools"]] == ["native_lookup"]
        if native_protocol:
            for body, identifiers in zip(bodies[1:], (["c1"], ["c1", "c2"])):
                tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
                assert [m["tool_call_id"] for m in tool_messages] == identifiers
                assert [json.loads(m["content"])["native_call_id"] for m in tool_messages] == identifiers
                assert all(json.loads(m["content"])["success"] for m in tool_messages)
        record.assert_awaited_once_with(
            "isolated-session", user_text=event.message_str, assistant_text=expected,
        )
        assert [(m.role, m.content) for m in await sessions.messages("isolated-session")] == [
            ("user", event.message_str), ("assistant", expected),
        ]
        assert temporary not in repr(record.await_args)
    finally:
        await backend.close()
        await store.close()
