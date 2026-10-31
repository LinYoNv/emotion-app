from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def host_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "host"))


def test_temporary_part_keeps_text_and_context_only_marker(host_path):
    from astrbot_host.compat.core.agent.message import TextPart

    part = TextPart(text="  temporary native perception\n")
    assert part.mark_as_temp() is part
    assert part._no_save is True
    assert part.model_dump() == {"type": "text", "text": "  temporary native perception\n"}
    assert part.model_dump_for_context() == {
        "type": "text", "text": "  temporary native perception\n", "_no_save": True,
    }
    normal = TextPart(text="persistent")
    assert normal.model_dump_for_context() == {"type": "text", "text": "persistent"}


def test_native_temporary_part_is_user_content_and_keeps_persona(host_path):
    from astrbot_host.compat.core.agent.message import TextPart
    from astrbot_host.compat.core.provider.entities import ProviderRequest
    from emotion_app.domain.message import Message
    from emotion_app.web.plugin_host import _messages_to_provider_request, _provider_request_to_messages

    request, contexts = _messages_to_provider_request([
        Message(role="system", content="unaltered fictional adult librarian persona"),
        Message(role="user", content="original input"),
    ], ProviderRequest)
    request.extra_user_content_parts.append(TextPart(text="temporary turn background").mark_as_temp())
    result = _provider_request_to_messages(request, contexts)
    assert [(item.role, item.content) for item in result] == [
        ("system", "unaltered fictional adult librarian persona"),
        ("user", "original input"),
        ("user", "temporary turn background"),
    ]


async def test_onebot_closes_connections_before_awaiting_server_task(host_path):
    from astrbot_host.adapters.onebot import OneBotAdapter

    adapter = OneBotAdapter(adapter_id="isolated-stop", config={})
    closed = asyncio.Event()
    trace = []

    class Client:
        async def close(self, code=1000, reason=""):
            trace.append("connection_closed")
            closed.set()

    client = Client()
    adapter._bot = SimpleNamespace(_wsr_api_clients={"bot": client}, _wsr_event_clients={client})
    adapter._shutdown = asyncio.Event()
    adapter._running = True

    async def server():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            trace.append("server_cancelled")
            assert closed.is_set(), "live connection would keep the server waiting during shutdown"
            raise

    adapter._task = asyncio.create_task(server())
    await asyncio.sleep(0)
    await adapter.stop()
    assert trace == ["connection_closed", "server_cancelled"]
    assert adapter._task is None and adapter._bot is None
    assert adapter._running is False
    await adapter.stop()
    assert trace == ["connection_closed", "server_cancelled"]


async def test_onebot_live_reverse_ws_releases_listener_on_stop(host_path):
    import socket
    from websockets.asyncio.client import connect
    from astrbot_host.adapters.onebot import OneBotAdapter

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    adapter = OneBotAdapter(adapter_id="isolated-live-stop", config={"ws_reverse_host": "127.0.0.1", "ws_reverse_port": port})
    client = None
    await adapter.start()
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while client is None:
            try:
                client = await connect(
                    f"ws://127.0.0.1:{port}/ws",
                    additional_headers={"X-Self-ID": "10001", "X-Client-Role": "universal"},
                    proxy=None, open_timeout=1,
                )
            except OSError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.01)
        while not adapter.status()["connected"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        await asyncio.wait_for(adapter.stop(), timeout=3)
        await asyncio.wait_for(client.wait_closed(), timeout=1)
        with pytest.raises(OSError):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
    finally:
        if client is not None:
            await client.close()
        await asyncio.wait_for(adapter.stop(), timeout=3)
