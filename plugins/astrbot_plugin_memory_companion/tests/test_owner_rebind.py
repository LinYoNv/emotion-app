from __future__ import annotations

import tempfile
from pathlib import Path

from .package_bootstrap import bootstrap_package

bootstrap_package()

from astrbot_plugin_memory_companion.core.identity import IdentityResolver
from astrbot_plugin_memory_companion.core.memory_lifecycle import evaluate_memory_lifecycle
from astrbot_plugin_memory_companion.core.models import EntityRef, MemoryRecord, SessionContext
from astrbot_plugin_memory_companion.core.store import MemoryStore
from astrbot_plugin_memory_companion.core.visibility import VisibilityPolicy


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_identity_fallback_fills_missing_self_id() -> None:
    class Event:
        unified_msg_origin = "qq:FriendMessage:user-1"

        async def get_sender_id(self):
            return "user-1"

        async def get_sender_name(self):
            return "User"

        async def get_message_str(self):
            return "hello"

    ctx = run(IdentityResolver(lambda _event: "bot-1").resolve_event_context(Event()))
    assert ctx.bot_id == "bot-1"


def test_missing_context_bot_id_has_distinct_lifecycle_reason() -> None:
    record = MemoryRecord(
        id="owned",
        memory_type="creative_work",
        subject=EntityRef.bot_self("bot-1"),
        object=EntityRef(kind="user", id="u1"),
        scope="private",
        visibility="bot_self",
        owner_bot_id="bot-1",
        validity_status="active",
        content="owned",
    ).ensure_defaults()
    result = evaluate_memory_lifecycle(record, SessionContext(scope="private", user_id="u1"))
    assert result.reason == "owner_bot_context_missing"


def test_visibility_reports_missing_context_bot_id() -> None:
    memory = MemoryRecord(
        id="owned-visible",
        scope="private",
        visibility="bot_self",
        subject=EntityRef.bot_self("bot-1"),
        metadata={"owner_bot_id": "bot-1"},
    )
    assert VisibilityPolicy().is_visible(memory, SessionContext(scope="private")) == (
        False,
        "owner_bot_context_missing",
    )


def test_store_owner_rebind_updates_column_and_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.db")
        store.initialize()
        try:
            record = MemoryRecord(
                id="legacy",
                memory_type="creative_work",
                subject=EntityRef.bot_self(),
                scope="unknown",
                session_id="private_companion:creative",
                visibility="bot_self",
                content="legacy creative memory",
                metadata={"owner_bot_id": ""},
            )
            run(store.insert_memory(record))
            assert run(store.update_memory_owner_bot("legacy", "bot-9"))
            refreshed = run(store.get_memory("legacy"))
            assert refreshed is not None
            assert refreshed.owner_bot_id == "bot-9"
            assert refreshed.metadata.get("owner_bot_id") == "bot-9"
        finally:
            store.close()
