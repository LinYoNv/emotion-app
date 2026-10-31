from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDV8AAAAASUVORK5CYII="


@pytest.fixture
async def external_pipeline(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "host"))
    from astrbot_host.shadow import install_shadow_package
    install_shadow_package(force=True)
    from astrbot_host import llm_hooks
    from astrbot_host.adapters.onebot import OneBotAdapter
    from astrbot_host.compat.api.message_components import Plain
    from astrbot_host.compat.core.agent.tool import ToolSet
    from astrbot_host.compat.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.persona_manager import HostPersonaManager
    from emotion_app.domain.backends import OpenAIChatBackend
    from emotion_app.domain.chat import ChatService
    from emotion_app.domain.conversation_manager import StoreConversationManager
    from emotion_app.domain.persona import Persona
    from emotion_app.domain.persona_repo import PersonaRepository
    from emotion_app.domain.session_repo import SessionRepository
    from emotion_app.infra.config import Config
    from emotion_app.storage.sqlite_store import SqliteStore
    from emotion_app.web.app import create_app
    from emotion_app.web.auth import AuthManager
    from emotion_app.web.plugin_host import PluginHostService
    import emotion_app.infra.adapters_store as adapters_store

    class AdapterStore:
        def load(self):
            return None

        def all(self):
            return []

    monkeypatch.setattr(adapters_store, "AdapterStore", AdapterStore)
    monkeypatch.setattr(llm_hooks, "star_handlers_registry", SimpleNamespace(
        get_handlers_by_event_type=lambda *args, **kwargs: [],
    ))
    cfg = Config()
    cfg.plugins.enabled = False
    cfg.plugins.data_dir = str(tmp_path / "plugins")
    cfg.upstream.base_url = "https://isolated.invalid/v1"
    cfg.upstream.api_key = "isolated-key"
    cfg.upstream.model = "isolated-multimodal-model"
    store = SqliteStore(tmp_path / "isolated.db")
    await store.open()
    personas = PersonaRepository(store)
    adult = await personas.create(Persona(name="safe-adult", system_prompt="你是成年图书管理员，只作普通日常交流。"))
    sessions = SessionRepository(store)
    native = StoreConversationManager(store)
    requests = []
    sent = []
    sent_chains = []
    control = SimpleNamespace(before_response=None, after_send=None, send_result=True, tool_calls=None,
                              response_text=None, attachments=[], fail_followup=False)

    async def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        if control.before_response is not None:
            await control.before_response()
        if body.get("stream"):
            frame = {"model": "isolated-multimodal-model", "choices": [{"delta": {"content": "isolated stream reply"}}]}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
        if control.fail_followup and len(requests) > 1:
            return httpx.Response(503, json={"error": {"message": "synthetic followup failure"}})
        message = {"role": "assistant", "content": (f"isolated reply {len(requests)}"
                                                   if control.response_text is None else control.response_text)}
        if control.tool_calls and len(requests) == 1:
            message.update(content="", tool_calls=control.tool_calls)
        return httpx.Response(200, json={
            "model": "isolated-multimodal-model", "choices": [{"message": message, "finish_reason": "stop"}],
        })

    backend = OpenAIChatBackend(transport=httpx.MockTransport(upstream))
    await backend.open()
    original_backend_chat = backend.chat

    async def chat_with_attachments(*args, **kwargs):
        result = await original_backend_chat(*args, **kwargs)
        result.attachments = list(control.attachments)
        return result

    backend.chat = chat_with_attachments
    chat = ChatService(cfg, client=backend, personas=personas)
    chat.bind_sessions(sessions)
    chat.schedule_summarize = lambda session_id: None
    app = create_app(cfg, store, AuthManager(access_token="isolated-token"), chat=chat,
                     personas=personas, sessions=sessions, chat_backend=backend)
    runtime = SimpleNamespace(reload=AsyncMock(), specs=lambda: [], status=lambda: {}, stop_all=AsyncMock())

    async def send(session, message):
        from copy import deepcopy

        if control.send_result is False:
            return False
        if control.send_result == "exception":
            raise ConnectionError("synthetic delivery failure")
        sent.append((str(session), message.get_plain_text()))
        sent_chains.append(deepcopy(message.chain))
        if control.after_send is not None:
            await control.after_send()
        return True

    platform = SimpleNamespace(meta=lambda: SimpleNamespace(id="onebot-isolated"), send_by_session=send)
    plugins = PluginHostService(cfg)
    plugins.sessions = sessions
    plugins.personas = personas
    plugins.conversation_manager = native
    plugins.platform_manager = SimpleNamespace(get_insts=lambda: [platform])
    plugins.attach_platforms = lambda specs: None
    plugins.stop = AsyncMock()
    plugins.dispatch_event = AsyncMock(return_value=SimpleNamespace(allow_default_llm=True, emitted=[]))
    plugins.host = SimpleNamespace(context=SimpleNamespace(
        persona_manager=HostPersonaManager(personas, default_persona_id=adult.id),
        get_config=lambda umo: {"provider_settings": {"default_personality": adult.name}},
        get_llm_tool_manager=lambda: SimpleNamespace(get_full_tool_set=lambda: ToolSet()),
    ))
    chat.bind_llm_hook(plugins.apply_llm_hooks)
    app.config["EMOTION_PLUGIN_HOST"] = plugins
    app.config["EMOTION_ADAPTERS_RUNTIME"] = runtime
    app.config["EMOTION_CONFIG_PROFILE_PATH"] = tmp_path / "profiles.json"
    adapter = OneBotAdapter(adapter_id="onebot-isolated", config={})

    def event(text="ordinary input", components=None, sender="10001"):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "20002"
        message.session_id = sender
        message.message_id = f"isolated-{sender}-{len(requests)}"
        message.sender = MessageMember(user_id=sender, nickname="synthetic-user")
        message.message = [Plain(text=text)] if components is None else components
        message.message_str = text
        message.raw_message = {}
        result = adapter.create_event(message)
        result.is_wake = True
        return result

    try:
        async with app.test_app():
            yield SimpleNamespace(app=app, chat=chat, sessions=sessions, native=native, store=store,
                                  runtime=runtime, plugins=plugins, event=event, requests=requests,
                                  sent=sent, sent_chains=sent_chains, control=control)
    finally:
        await backend.close()


async def test_real_dispatch_accepts_image_without_text_and_persists_native_media(external_pipeline):
    from astrbot_host.compat.api.message_components import Image

    p = external_pipeline
    event = p.event("", [Image.fromURL(IMAGE)])
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == len(p.sent) == 1
    body = p.requests[0]
    assert any(part.get("image_url", {}).get("url") == IMAGE
               for message in body["messages"] if message["role"] == "user"
               for part in message["content"] if isinstance(part, dict))
    cid = await p.native.get_curr_conversation_id(event.unified_msg_origin)
    conversation = await p.native.get_conversation(event.unified_msg_origin, cid)
    history = json.loads(conversation.history)
    assert history[0]["content"] == [{"type": "image_url", "image_url": {"url": IMAGE}}]
    assert history[1]["content"] == "isolated reply 1"
    projection = f"{event.unified_msg_origin}:conversation:{cid}"
    assert (await p.sessions.messages(projection))[0].to_upstream()["content"] == history[0]["content"]


async def test_native_selection_controls_history_and_keeps_legacy_records(external_pipeline):
    from emotion_app.domain.message import Message

    p = external_pipeline
    event = p.event("first qq input")
    umo = event.unified_msg_origin
    await p.sessions.ensure(umo)
    await p.sessions.append(umo, [Message(role="user", content="legacy unmapped history")])
    c1 = await p.native.new_conversation(umo, content=[{"role": "user", "content": "native window one"}])
    await p.runtime.on_dispatch(event)
    c2 = await p.native.new_conversation(umo, content=[{"role": "user", "content": "native window two"}])
    await p.native.add_message_pair(c2, {"role": "user", "content": "room input"},
                                    {"role": "assistant", "content": "room reply"})
    await p.runtime.on_dispatch(p.event("second qq input"))
    first = json.dumps(p.requests[0]["messages"], ensure_ascii=False)
    second = json.dumps(p.requests[1]["messages"], ensure_ascii=False)
    assert "native window one" in first and "legacy unmapped history" not in first
    assert "native window two" in second and "room reply" in second
    assert "native window one" not in second and "first qq input" not in second
    assert [m.content for m in await p.sessions.messages(umo)] == ["legacy unmapped history"]
    assert (await p.sessions.messages(f"{umo}:conversation:{c1}"))[-1].content == "isolated reply 1"
    assert (await p.sessions.messages(f"{umo}:conversation:{c2}"))[-1].content == "isolated reply 2"


async def test_switch_before_send_cancels_without_recording_reply(external_pipeline):
    p = external_pipeline
    event = p.event()
    umo = event.unified_msg_origin
    c1 = await p.native.new_conversation(umo)
    c2 = []

    async def switch():
        c2.append(await p.native.new_conversation(umo))

    p.control.before_response = switch
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == 1 and p.sent == []
    assert json.loads((await p.native.get_conversation(umo, c1)).history) == []
    assert json.loads((await p.native.get_conversation(umo, c2[0])).history) == []
    assert await p.sessions.messages(f"{umo}:conversation:{c1}") == []


async def test_switch_after_send_records_only_captured_conversation(external_pipeline):
    p = external_pipeline
    event = p.event()
    umo = event.unified_msg_origin
    c1 = await p.native.new_conversation(umo)
    c2 = []

    async def switch():
        c2.append(await p.native.new_conversation(umo))

    p.control.after_send = switch
    await p.runtime.on_dispatch(event)
    assert len(p.sent) == 1
    assert await p.native.get_curr_conversation_id(umo) == c2[0]
    assert json.loads((await p.native.get_conversation(umo, c1)).history)[-1]["content"] == "isolated reply 1"
    assert json.loads((await p.native.get_conversation(umo, c2[0])).history) == []


async def test_failed_send_does_not_append_native_or_projection(external_pipeline):
    p = external_pipeline
    event = p.event()
    umo = event.unified_msg_origin
    c1 = await p.native.new_conversation(umo)
    p.control.send_result = False
    await p.runtime.on_dispatch(event)
    assert p.sent == []
    assert json.loads((await p.native.get_conversation(umo, c1)).history) == []
    assert await p.sessions.messages(f"{umo}:conversation:{c1}") == []


async def test_same_native_window_serializes_turns_and_reads_first_reply(external_pipeline):
    p = external_pipeline
    started = asyncio.Event()
    release = asyncio.Event()

    async def block_first():
        if len(p.requests) == 1:
            started.set()
            await release.wait()

    p.control.before_response = block_first
    first = asyncio.create_task(p.runtime.on_dispatch(p.event("first serial input")))
    await asyncio.wait_for(started.wait(), timeout=3)
    second = asyncio.create_task(p.runtime.on_dispatch(p.event("second serial input")))
    await asyncio.sleep(0)
    assert len(p.requests) == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
    assert len(p.sent) == len(p.requests) == 2
    assert "isolated reply 1" in json.dumps(p.requests[1]["messages"])


async def test_blank_message_is_not_submitted_to_model(external_pipeline):
    p = external_pipeline
    await p.runtime.on_dispatch(p.event("  \n "))
    assert p.requests == p.sent == []


async def test_plugin_shortened_text_keeps_media_and_does_not_restore_wake_prefix(external_pipeline):
    from astrbot_host.compat.api.message_components import Image, Plain

    p = external_pipeline
    event = p.event("actual input", [Plain(text="wake-name actual input"), Image.fromURL(IMAGE)])
    await p.runtime.on_dispatch(event)
    current = p.requests[0]["messages"][-1]["content"]
    assert current == [{"type": "text", "text": "actual input"},
                       {"type": "image_url", "image_url": {"url": IMAGE}}]


async def test_switch_during_request_hook_prevents_first_model_request(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks

    p = external_pipeline
    event = p.event()
    await p.native.new_conversation(event.unified_msg_origin)

    async def switch(actual_event, request):
        await p.native.new_conversation(actual_event.unified_msg_origin)
        return request, False

    monkeypatch.setattr(llm_hooks, "run_llm_request_hooks", switch)
    await p.runtime.on_dispatch(event)
    assert p.requests == p.sent == []


async def test_switch_during_model_prevents_ordinary_tool_side_effect(external_pipeline):
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    p = external_pipeline
    event = p.event()
    await p.native.new_conversation(event.unified_msg_origin)
    handler = AsyncMock(return_value="side effect must not happen")
    tools = ToolSet([FunctionTool(name="isolated_write", handler=handler,
                                 parameters={"type": "object", "properties": {}})])
    p.plugins.host.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
    p.control.tool_calls = [{"id": "pending-write", "type": "function",
                            "function": {"name": "isolated_write", "arguments": "{}"}}]

    async def switch():
        await p.native.new_conversation(event.unified_msg_origin)

    p.control.before_response = switch
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == 1 and p.sent == []
    handler.assert_not_awaited()


async def test_switch_inside_first_tool_prevents_second_tool_and_followup_model(external_pipeline):
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    p = external_pipeline
    event = p.event()
    await p.native.new_conversation(event.unified_msg_origin)
    first = AsyncMock()
    second = AsyncMock(return_value="must not run")

    async def switch(*args, **kwargs):
        await p.native.new_conversation(event.unified_msg_origin)
        return "first finished"

    first.side_effect = switch
    tools = ToolSet([FunctionTool(name="first", handler=first, parameters={"type": "object"}),
                     FunctionTool(name="second", handler=second, parameters={"type": "object"})])
    p.plugins.host.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
    p.control.tool_calls = [{"id": name, "type": "function",
                            "function": {"name": name, "arguments": "{}"}} for name in ("first", "second")]
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == 1 and p.sent == []
    first.assert_awaited_once()
    second.assert_not_awaited()


async def test_native_tool_cancellation_is_not_rendered_as_tool_result(external_pipeline):
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet
    from emotion_app.domain.chat import LLMRequestCancelled

    p = external_pipeline
    handler = AsyncMock(side_effect=LLMRequestCancelled("authorization changed"))
    tools = ToolSet([FunctionTool(name="cancelled", handler=handler, parameters={"type": "object"})])
    p.plugins.host.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
    p.control.tool_calls = [{"id": "cancelled", "type": "function",
                            "function": {"name": "cancelled", "arguments": "{}"}}]
    await p.runtime.on_dispatch(p.event())
    assert len(p.requests) == 1 and p.sent == []
    handler.assert_awaited_once()


async def test_checkpoint_and_native_tool_history_reach_model_without_internal_role(external_pipeline):
    p = external_pipeline
    event = p.event()
    umo = event.unified_msg_origin
    call = {"id": "historic-read", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    history = [{"role": "user", "content": "previous input"},
               {"role": "assistant", "content": None, "tool_calls": [call]},
               {"role": "_checkpoint", "content": {"id": "internal-checkpoint"}},
               {"role": "tool", "content": "previous tool result", "tool_call_id": "historic-read"},
               {"role": "assistant", "content": "previous answer"}]
    cid = await p.native.new_conversation(umo, content=history)
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == len(p.sent) == 1
    messages = p.requests[0]["messages"]
    assert all(message["role"] != "_checkpoint" for message in messages)
    assert next(message for message in messages if message.get("tool_calls"))["tool_calls"] == [call]
    assert next(message for message in messages if message["role"] == "tool")["tool_call_id"] == "historic-read"
    stored = json.loads((await p.native.get_conversation(umo, cid)).history)
    assert history == stored[:len(history)]


@pytest.mark.parametrize("method,path_suffix,body", [
    ("delete", "", None),
    ("post", "/clear", {}),
    ("patch", "", {"persona_id": "another-persona"}),
    ("patch", "", {"user_id": "another-user"}),
])
async def test_http_cannot_fake_mutating_native_history(external_pipeline, method, path_suffix, body):
    from emotion_app.web.default_llm_reply import build_external_chat_request

    p = external_pipeline
    event = p.event()
    request = await build_external_chat_request(p.chat, p.plugins, event)
    before = await p.store.load_store()
    client = p.app.test_client()
    response = await getattr(client, method)(f"/api/v1/sessions/{request.session_id}{path_suffix}",
                                            json=body, headers={"Authorization": "Bearer isolated-token"})
    assert response.status_code == 409
    payload = await response.get_json()
    assert payload["error"]["type"] == "native_projection_read_only"
    assert await p.store.load_store() == before
    assert p.requests == p.sent == []


@pytest.mark.parametrize("stream", [False, True])
async def test_http_chat_rejects_native_projection_before_plugins_and_model(external_pipeline, stream):
    from emotion_app.web.default_llm_reply import build_external_chat_request

    p = external_pipeline
    request = await build_external_chat_request(p.chat, p.plugins, p.event())
    before = await p.store.load_store()
    response = await p.app.test_client().post("/v1/chat/completions", json={
        "session_id": request.session_id, "messages": [{"role": "user", "content": "must not be silently overwritten"}],
        "stream": stream,
    }, headers={"Authorization": "Bearer isolated-token"})
    assert response.status_code == 409
    assert (await response.get_json())["error"]["type"] == "native_projection_read_only"
    p.plugins.dispatch_event.assert_not_awaited()
    assert p.requests == p.sent == []
    assert await p.store.load_store() == before


async def test_native_projection_remains_readable_and_view_settings_can_change(external_pipeline):
    from emotion_app.web.default_llm_reply import build_external_chat_request

    p = external_pipeline
    request = await build_external_chat_request(p.chat, p.plugins, p.event())
    client = p.app.test_client()
    headers = {"Authorization": "Bearer isolated-token"}
    response = await client.patch(f"/api/v1/sessions/{request.session_id}",
                                  json={"title": "local view title", "model": "", "policy": {"mode": "recent_only"}}, headers=headers)
    assert response.status_code == 200
    assert (await client.get(f"/api/v1/sessions/{request.session_id}/messages", headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/sessions/{request.session_id}/read", headers=headers)).status_code == 200
    assert p.requests == p.sent == []


@pytest.mark.parametrize("refresh_projection", [False, True])
async def test_stale_background_summary_cannot_restore_reset_history(external_pipeline, refresh_projection):
    from emotion_app.web.default_llm_reply import build_external_chat_request

    p = external_pipeline
    event = p.event()
    umo = event.unified_msg_origin
    cid = await p.native.new_conversation(umo, content=[{"role": "user", "content": "obsolete before reset"}])
    request = await build_external_chat_request(p.chat, p.plugins, event)
    await p.sessions.update(request.session_id, {"policy": {"mode": "rolling_summary"}})
    started = asyncio.Event()
    release = asyncio.Event()

    async def summarize(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(ran=True, ok=True, summary="obsolete summary", cursor=1, summary_chars=16)

    p.chat.bind_summarizer(SimpleNamespace(maybe_summarize=summarize))
    task = asyncio.create_task(p.chat._summarize_bg(request.session_id))
    await asyncio.wait_for(started.wait(), timeout=3)
    await p.native.update_conversation(umo, cid, history=[])
    if refresh_projection:
        await p.native.project_conversation(umo, cid, request.session_id)
    release.set()
    await asyncio.wait_for(task, timeout=3)
    await p.native.project_conversation(umo, cid, request.session_id)
    assert (await p.sessions.get(request.session_id)).summary == ""
    assert await p.sessions.messages(request.session_id) == []


@pytest.mark.parametrize("method", ["complete", "stream"])
async def test_ordinary_web_media_history_survives_next_turn(external_pipeline, method):
    from emotion_app.domain.chat import ChatRequest
    from emotion_app.domain.message import from_upstream_message

    p = external_pipeline
    incoming = from_upstream_message({"role": "user", "content": [
        {"type": "text", "text": "inspect this"}, {"type": "image_url", "image_url": {"url": IMAGE}},
    ]})
    request = ChatRequest(session_id="ordinary-web", messages=[incoming])
    if method == "complete":
        await p.chat.complete(request)
    else:
        chunks = [chunk async for chunk in p.chat.stream(request)]
        assert chunks
    stored = await p.sessions.messages(request.session_id)
    assert stored[0].to_upstream()["content"] == incoming.to_upstream()["content"]
    assert stored[0].content_parts is not None


async def _native_delivery_history(p, event, cid=None):
    cid = cid or await p.native.get_curr_conversation_id(event.unified_msg_origin)
    history = json.loads((await p.native.get_conversation(event.unified_msg_origin, cid)).history)
    projection = await p.sessions.messages(f"{event.unified_msg_origin}:conversation:{cid}")
    return history, projection


@pytest.mark.parametrize("kind", ["image", "record", "video"])
async def test_response_hook_pure_media_is_sent_and_projected_without_provider_image(external_pipeline, monkeypatch, kind):
    from astrbot_host import llm_hooks
    from astrbot_host.compat.api.message_components import Image, Record, Video

    p = external_pipeline
    event = p.event()
    locator = IMAGE if kind == "image" else _silent_audio_url() if kind == "record" else "data:video/mp4;base64,AAAA"
    component = Image.fromURL(locator) if kind == "image" else Record(file=locator) if kind == "record" else Video(file=locator)
    field, label = {"image": ("image_path", "[图片]"), "record": ("audio_path", "[语音]"),
                    "video": ("video_path", "[视频]")}[kind]

    async def response(actual, value):
        assert actual is event
        value.result_chain = event.chain_result([component])

    monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", response)
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == len(p.sent_chains) == 1
    assert p.sent_chains[0][0].type.value == component.type.value
    assert p.sent[0][1] == ""
    history, projection = await _native_delivery_history(p, event)
    assert history[-1] == {"role": "assistant", "content": label, field: locator}
    assert getattr(projection[-1], field) == locator
    assert projection[-1].to_upstream() == {"role": "assistant", "content": label}
    assert projection[-1].content_parts is None
    assert event.get_extra("_emotion_delivery_recorded") is True


async def test_chatresult_attachments_become_real_image_components(external_pipeline):
    p = external_pipeline
    event = p.event()
    p.control.response_text = ""
    p.control.attachments = [{"type": "image", "url": IMAGE}]
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == len(p.sent_chains) == 1
    image = p.sent_chains[0][0]
    assert image.type.value.lower() == "image" and image.url == IMAGE
    history, projection = await _native_delivery_history(p, event)
    assert history[-1] == {"role": "assistant", "content": "[图片]", "image_path": IMAGE}
    assert projection[-1].image_path == IMAGE


async def test_response_hook_empty_chain_suppresses_text_and_attachments(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks

    p = external_pipeline
    event = p.event()
    p.control.attachments = [{"type": "image", "url": IMAGE}]

    async def suppress(actual, response):
        response.result_chain = actual.chain_result([])

    monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", suppress)
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == 1 and p.sent_chains == []
    history, projection = await _native_delivery_history(p, event)
    assert history == projection == []
    assert not event.get_extra("_emotion_delivered_messages")


async def test_plain_delivery_preserves_whitespace_exactly(external_pipeline):
    p = external_pipeline
    event = p.event()
    p.control.response_text = "  exact ordinary reply\n\t"
    await p.runtime.on_dispatch(event)
    assert p.sent[0][1] == p.control.response_text
    history, projection = await _native_delivery_history(p, event)
    assert history[-1]["content"] == projection[-1].content == p.control.response_text


@pytest.mark.parametrize("outcome", ["success", "switch", "model_failure", "response_failure"])
async def test_tool_delivered_media_is_recorded_once_to_captured_window(external_pipeline, monkeypatch, outcome):
    from astrbot_host import llm_hooks
    from astrbot_host.compat.api.message_components import Image, Plain, Record
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet
    from emotion_app.web.default_llm_reply import record_external_chat_reply

    p = external_pipeline
    event = p.event()
    old_cid = await p.native.new_conversation(event.unified_msg_origin)
    new_cids = []
    original_complete = p.chat.complete
    request_capture = []

    async def complete(request):
        request_capture.append(request)
        return await original_complete(request)

    monkeypatch.setattr(p.chat, "complete", complete)
    local_audio = "/private/synthetic/unknown-audio-format"

    async def send_media(actual):
        assert actual is event
        await actual.send(actual.chain_result([Image.fromURL(IMAGE)]))
        if outcome == "switch":
            new_cids.append(await p.native.new_conversation(actual.unified_msg_origin))
        else:
            await actual.send(actual.chain_result([Plain(" raw tool text\n"), Record(file=local_audio)]))
        return "tool receipt, not sent text"

    tools = ToolSet([FunctionTool(name="send_media", handler=send_media, parameters={"type": "object"})])
    p.plugins.host.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
    p.control.tool_calls = [{"id": "actual-send", "type": "function", "function": {"name": "send_media", "arguments": "{}"}}]
    p.control.fail_followup = outcome == "model_failure"
    if outcome == "response_failure":
        async def response(actual, value):
            if not value.tools_call_ids:
                raise RuntimeError("synthetic response failure after tool delivery")
        monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", response)
    await p.runtime.on_dispatch(event)
    expected = "[图片]" if outcome == "switch" else "[图片] raw tool text\n[语音]"
    assert len(p.sent_chains) == (1 if outcome == "switch" else 2)
    assert len(p.requests) == (1 if outcome == "switch" else 2)
    history, projection = await _native_delivery_history(p, event, old_cid)
    assert len(history) == len(projection) == 2
    assert history[-1]["content"] == projection[-1].content == expected
    assert history[-1]["image_path"] == projection[-1].image_path == IMAGE
    if outcome != "switch":
        assert history[-1]["audio_path"] == projection[-1].audio_path == local_audio
    assert local_audio not in history[-1]["content"]
    assert "isolated reply 2" not in history[-1]["content"]
    assert "tool receipt" not in history[-1]["content"]
    await record_external_chat_reply(p.chat, p.plugins, event, request_capture[0])
    again, _ = await _native_delivery_history(p, event, old_cid)
    assert again == history
    if new_cids:
        from emotion_app.domain.session_repo import SessionNotFound

        new_history = json.loads((await p.native.get_conversation(event.unified_msg_origin, new_cids[0])).history)
        assert new_history == []
        with pytest.raises(SessionNotFound):
            await p.sessions.messages(f"{event.unified_msg_origin}:conversation:{new_cids[0]}")


@pytest.mark.parametrize("kind", ["image", "record"])
@pytest.mark.parametrize("failure", [False, "exception"])
async def test_failed_pure_media_send_has_no_capture_or_history(external_pipeline, monkeypatch, kind, failure):
    from astrbot_host import llm_hooks
    from astrbot_host.compat.api.message_components import Image, Record

    p = external_pipeline
    event = p.event()
    p.control.send_result = failure

    async def response(actual, value):
        component = Image.fromURL(IMAGE) if kind == "image" else Record(file="/private/fake.wav")
        value.result_chain = actual.chain_result([component])

    monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", response)
    await p.runtime.on_dispatch(event)
    assert p.sent_chains == []
    assert not event._has_send_oper
    assert not event.get_extra("_emotion_delivered_messages")
    assert not event.get_extra("_emotion_delivery_recorded")
    history, projection = await _native_delivery_history(p, event)
    assert history == projection == []


def _silent_audio_url():
    import base64
    import io
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(bytes(160))
    return "data:audio/wav;base64," + base64.b64encode(output.getvalue()).decode("ascii")


async def test_after_send_hook_failure_still_records_successful_chain(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.compat.api.message_components import Image

    p = external_pipeline
    event = p.event()

    async def response(actual, value):
        value.result_chain = actual.chain_result([Image.fromURL(IMAGE)])

    async def after(actual, kind, *args, **kwargs):
        if kind.name == "OnAfterMessageSentEvent":
            raise RuntimeError("synthetic after-send failure")
        return False

    monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", response)
    monkeypatch.setattr(llm_hooks, "call_event_hook", after)
    await p.runtime.on_dispatch(event)
    assert len(p.sent_chains) == 1
    history, projection = await _native_delivery_history(p, event)
    assert len(history) == 2
    assert projection[-1].image_path == IMAGE
    assert event.get_extra("_emotion_delivery_recorded") is True


async def test_delivered_media_history_reaches_next_model_as_assistant_text_only(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.compat.api.message_components import Image

    p = external_pipeline
    first = p.event()

    async def response(actual, value):
        value.result_chain = actual.chain_result([Image.fromURL(IMAGE)])

    monkeypatch.setattr(llm_hooks, "run_llm_response_hooks", response)
    await p.runtime.on_dispatch(first)
    await p.runtime.on_dispatch(p.event("second ordinary input"))
    assert len(p.requests) == 2
    assistants = [item for item in p.requests[1]["messages"] if item["role"] == "assistant"]
    assert assistants == [{"role": "assistant", "content": "[图片]"}]
    assert IMAGE not in json.dumps(assistants)


@pytest.mark.parametrize("failure", [False, "exception"])
async def test_tool_failed_send_is_not_captured_or_persisted(external_pipeline, failure):
    from astrbot_host.compat.api.message_components import Image
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    p = external_pipeline
    event = p.event()
    p.control.send_result = failure

    async def send_image(actual):
        await actual.send(actual.chain_result([Image.fromURL(IMAGE)]))
        return "must not claim success"

    tools = ToolSet([FunctionTool(name="send_image", handler=send_image, parameters={"type": "object"})])
    p.plugins.host.context.get_llm_tool_manager = lambda: SimpleNamespace(get_full_tool_set=lambda: tools)
    p.control.tool_calls = [{"id": "failed-send", "type": "function", "function": {"name": "send_image", "arguments": "{}"}}]
    await p.runtime.on_dispatch(event)
    assert len(p.requests) == 2
    assert p.sent_chains == []
    assert not event.get_extra("_emotion_delivered_messages")
    history, projection = await _native_delivery_history(p, event)
    assert history == projection == []


@pytest.mark.parametrize("had_history", [False, True])
async def test_same_cid_reset_during_model_cancels_reply_without_restoring_history(external_pipeline, had_history):
    p = external_pipeline
    event = p.event("input captured before same-cid reset")
    umo = event.unified_msg_origin
    initial = [{"role": "user", "content": "obsolete history before reset"}] if had_history else []
    captured_cid = await p.native.new_conversation(umo, content=initial)
    reset_completed = []

    async def reset_same_conversation():
        assert await p.native.get_curr_conversation_id(umo) == captured_cid
        await p.native.update_conversation(umo, captured_cid, history=[])
        assert json.loads((await p.native.get_conversation(umo, captured_cid)).history) == []
        reset_completed.append(True)

    p.control.before_response = reset_same_conversation
    await p.runtime.on_dispatch(event)
    stored = json.loads((await p.native.get_conversation(umo, captured_cid)).history)
    assert reset_completed == [True]
    assert await p.native.get_curr_conversation_id(umo) == captured_cid
    assert len(p.requests) == 1
    assert p.sent == [], "the old model reply must be cancelled after a reset even when the selected cid is unchanged"
    assert stored == [], "the old turn must not repopulate history cleared during its model request"


async def test_same_cid_reset_after_send_does_not_reappend_delivered_turn_in_finally(external_pipeline):
    p = external_pipeline
    event = p.event("input delivered before same-cid reset")
    umo = event.unified_msg_origin
    captured_cid = await p.native.new_conversation(umo, content=[
        {"role": "user", "content": "obsolete history before delivery"},
    ])
    reset_completed = []

    async def reset_same_conversation_after_delivery():
        assert await p.native.get_curr_conversation_id(umo) == captured_cid
        await p.native.update_conversation(umo, captured_cid, history=[])
        assert json.loads((await p.native.get_conversation(umo, captured_cid)).history) == []
        reset_completed.append(True)

    p.control.after_send = reset_same_conversation_after_delivery
    await p.runtime.on_dispatch(event)
    stored = json.loads((await p.native.get_conversation(umo, captured_cid)).history)
    assert reset_completed == [True]
    assert await p.native.get_curr_conversation_id(umo) == captured_cid
    assert len(p.requests) == len(p.sent) == 1
    assert p.sent[0][1] == "isolated reply 1"
    assert stored == [], "finally must not reappend an already delivered turn after its native conversation was reset"
    session_id = f"{umo}:conversation:{captured_cid}"
    await p.native.project_conversation(umo, captured_cid, session_id)
    assert await p.sessions.messages(session_id) == []


async def test_agent_lifecycle_hooks_fire_exactly_once_per_turn(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType

    p = external_pipeline
    seen = []

    async def hook(event, hook_type, *args, **kwargs):
        if hook_type in (EventType.OnAgentBeginEvent, EventType.OnAgentDoneEvent):
            seen.append((hook_type.name, len(args)))
        return False

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    await p.runtime.on_dispatch(p.event())
    assert len(p.sent) == 1
    names = [name for name, _ in seen]
    assert names.count("OnAgentBeginEvent") == 1, names
    assert names.count("OnAgentDoneEvent") == 1, names
    assert names.index("OnAgentBeginEvent") < names.index("OnAgentDoneEvent")


async def test_agent_done_still_fires_once_when_model_fails(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType

    p = external_pipeline
    seen = []

    async def hook(event, hook_type, *args, **kwargs):
        if hook_type in (EventType.OnAgentBeginEvent, EventType.OnAgentDoneEvent):
            seen.append(hook_type.name)
        return False

    async def fail(*args, **kwargs):
        raise RuntimeError("isolated model failure")

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    monkeypatch.setattr(p.chat, "complete", fail)
    await p.runtime.on_dispatch(p.event())
    assert seen.count("OnAgentDoneEvent") == 1, seen
    assert seen.count("OnAgentBeginEvent") == 1, seen
    assert p.sent == []


async def test_agent_done_fires_when_turn_is_cancelled_by_window_switch(external_pipeline, monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType

    p = external_pipeline
    seen = []
    event = p.event()
    await p.native.new_conversation(event.unified_msg_origin)

    async def switch(actual_event, request):
        await p.native.new_conversation(actual_event.unified_msg_origin)
        return request, False

    async def hook(actual_event, hook_type, *args, **kwargs):
        if hook_type in (EventType.OnAgentBeginEvent, EventType.OnAgentDoneEvent):
            seen.append(hook_type.name)
        return False

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    monkeypatch.setattr(llm_hooks, "run_llm_request_hooks", switch)
    await p.runtime.on_dispatch(event)
    assert seen.count("OnAgentDoneEvent") == 1, seen
    assert p.sent == []


async def test_agent_done_receives_assistant_role_response(external_pipeline, monkeypatch):
    """官方 `main.py:1106` 只在 response.role == 'assistant' 时才写 completed。"""
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType

    p = external_pipeline
    seen = []

    async def hook(event, hook_type, *args, **kwargs):
        if hook_type is EventType.OnAgentDoneEvent:
            seen.append(args)
        return False

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    await p.runtime.on_dispatch(p.event())
    assert len(seen) == 1, seen
    response = seen[0][1]
    assert response is not None, "done hook must receive a response object"
    assert response.role == "assistant", response.role


async def _stream_lifecycle_request(p, event):
    from emotion_app.web.default_llm_reply import build_external_chat_request

    return await build_external_chat_request(p.chat, p.plugins, event)


def _capture_lifecycle(monkeypatch):
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType

    seen = []

    async def hook(event, hook_type, *args, **kwargs):
        if hook_type in (EventType.OnAgentBeginEvent, EventType.OnAgentDoneEvent):
            seen.append((event, hook_type, args))
        return False

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    return seen


def _assert_one_lifecycle(seen, event, *, assistant=False):
    from astrbot_host.registry import EventType

    assert [item[1] for item in seen] == [EventType.OnAgentBeginEvent, EventType.OnAgentDoneEvent]
    assert all(item[0] is event for item in seen)
    assert seen[0][2] == (None,)
    assert len(seen[1][2]) == 2 and seen[1][2][0] is None
    if assistant:
        assert seen[1][2][1] is not None
        assert seen[1][2][1].role == "assistant"
    return seen[1][2][1]


@pytest.mark.parametrize("consume", ["full", "break_aclosing", "break_then_aclose", "aclose_other_task"])
async def test_stream_lifecycle_real_event_finishes_once_after_consumption_or_close(external_pipeline, monkeypatch, consume):
    from contextlib import aclosing
    from astrbot_host.registry import EventType

    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)
    iterator = p.chat.stream(request)
    assert seen == []
    chunks = []
    if consume == "full":
        async for chunk in iterator:
            chunks.append(chunk)
            assert [item[1] for item in seen] == [EventType.OnAgentBeginEvent]
    elif consume == "break_aclosing":
        async with aclosing(iterator):
            async for chunk in iterator:
                chunks.append(chunk)
                break
            assert [item[1] for item in seen] == [EventType.OnAgentBeginEvent]
    else:
        async for chunk in iterator:
            chunks.append(chunk)
            break
        assert [item[1] for item in seen] == [EventType.OnAgentBeginEvent]
        if consume == "aclose_other_task":
            await asyncio.create_task(iterator.aclose())
        else:
            await iterator.aclose()
    assert "".join(chunk.delta for chunk in chunks) == "isolated reply 1"
    response = _assert_one_lifecycle(seen, event, assistant=True)
    assert response.completion_text == "isolated reply 1"
    assert len(p.requests) == 1
    assert p.sent == []
    assert request._lifecycle_run is None
    await iterator.aclose()
    _assert_one_lifecycle(seen, event, assistant=True)


async def test_stream_lifecycle_real_event_model_error_finishes_once(external_pipeline, monkeypatch):
    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)

    async def fail(*args, **kwargs):
        raise RuntimeError("isolated stream generation failure")

    monkeypatch.setattr(p.chat._client, "chat", fail)
    iterator = p.chat.stream(request)
    with pytest.raises(RuntimeError, match="isolated stream generation failure"):
        await anext(iterator)
    _assert_one_lifecycle(seen, event)
    assert request._lifecycle_run is None and p.sent == []
    await iterator.aclose()
    _assert_one_lifecycle(seen, event)


async def test_stream_lifecycle_real_event_llm_cancellation_finishes_once(external_pipeline, monkeypatch):
    from emotion_app.domain.chat import LLMRequestCancelled

    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)

    async def reset():
        await p.native.update_conversation(event.unified_msg_origin, request.conversation_id, history=[])

    p.control.before_response = reset
    iterator = p.chat.stream(request)
    with pytest.raises(LLMRequestCancelled):
        await anext(iterator)
    _assert_one_lifecycle(seen, event)
    assert len(p.requests) == 1 and p.sent == []
    assert request._lifecycle_run is None
    await iterator.aclose()
    _assert_one_lifecycle(seen, event)


async def test_stream_lifecycle_real_event_task_cancellation_finishes_once(external_pipeline, monkeypatch):
    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def wait_for_cancellation():
        started.set()
        await release.wait()

    p.control.before_response = wait_for_cancellation
    iterator = p.chat.stream(request)
    task = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await iterator.aclose()
    _assert_one_lifecycle(seen, event)
    assert request._lifecycle_run is None and p.sent == []


async def test_stream_lifecycle_without_real_event_never_dispatches_host_events(external_pipeline, monkeypatch):
    from emotion_app.domain.chat import ChatRequest
    from emotion_app.domain.message import Message

    p = external_pipeline
    seen = _capture_lifecycle(monkeypatch)
    request = ChatRequest(messages=[Message(content="ordinary web stream")], persist=False)
    chunks = [chunk async for chunk in p.chat.stream(request)]
    assert "".join(chunk.delta for chunk in chunks) == "isolated stream reply"
    assert request.llm_event is None and request._lifecycle_run is None
    assert seen == []
    assert p.sent == []


async def test_stream_lifecycle_never_started_generator_does_not_dispatch(external_pipeline, monkeypatch):
    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)
    iterator = p.chat.stream(request)
    await iterator.aclose()
    assert seen == [] and p.requests == [] and request._lifecycle_run is None


async def test_complete_lifecycle_direct_call_uses_injected_port_once(external_pipeline, monkeypatch):
    p = external_pipeline
    event = p.event()
    request = await _stream_lifecycle_request(p, event)
    seen = _capture_lifecycle(monkeypatch)
    result = await p.chat.complete(request)
    assert result.content == "isolated reply 1"
    _assert_one_lifecycle(seen, event, assistant=True)
    assert request._lifecycle_run is None and p.sent == []


async def test_stream_lifecycle_independent_events_do_not_share_completion(external_pipeline, monkeypatch):
    p = external_pipeline
    first_event, second_event = p.event(sender="10001"), p.event(sender="10002")
    first = await _stream_lifecycle_request(p, first_event)
    second = await _stream_lifecycle_request(p, second_event)
    seen = _capture_lifecycle(monkeypatch)
    first_stream = p.chat.stream(first)
    await anext(first_stream)
    await p.chat.complete(second)
    assert first._lifecycle_run is not None and second._lifecycle_run is None
    await first_stream.aclose()
    _assert_one_lifecycle([item for item in seen if item[0] is first_event], first_event, assistant=True)
    _assert_one_lifecycle([item for item in seen if item[0] is second_event], second_event, assistant=True)


async def test_cancelled_tool_turn_reports_error_role_not_stale_tool(external_pipeline, monkeypatch):
    """取消/异常轮不得复用上一轮残留的 `role="tool"`。

    机理：有工具调用的轮次里 `response_hook` 会把 `_emotion_llm_response`
    写成 `role="tool"`（`llm_turn.py:159`）。若取消发生在之后的模型调用上，
    复用那份 extra 就会把**上一轮**当成本轮结果上报。官方只认
    `role=="assistant"` 写 completed，用 "err" 才能明确表达"本轮无可用回复"。
    """
    from astrbot_host import llm_hooks
    from astrbot_host.registry import EventType
    from emotion_app.domain.chat import LLMRequestCancelled

    p = external_pipeline
    seen = []

    async def hook(event, hook_type, *args, **kwargs):
        if hook_type is EventType.OnAgentDoneEvent:
            seen.append(args[1])
        return False

    async def boom(*args, **kwargs):
        raise LLMRequestCancelled("cancelled during tool turn")

    monkeypatch.setattr(llm_hooks, "call_event_hook", hook)
    # 让真正的模型调用失败（走 complete 内部路径），而不是替换内部方法
    monkeypatch.setattr(p.chat, "_chat_with_fallback", boom)
    await p.runtime.on_dispatch(p.event())
    assert len(seen) == 1, seen
    response = seen[0]
    assert response is not None
    assert response.role != "assistant", "cancelled turn must not claim assistant"
    assert response.role == "err", response.role
