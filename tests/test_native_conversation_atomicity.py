"""原生对话并发写入、捕获 cid 与 UI 历史投影的事务边界。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from emotion_app.domain.conversation_manager import CONVERSATIONS_SECTION, StoreConversationManager
from emotion_app.domain.message import Message
from emotion_app.domain.session_repo import SessionNotFound, SessionRepository
from emotion_app.storage.sqlite_store import SqliteStore

UMO = "test-bot:FriendMessage:test-user"
OTHER_UMO = "other-bot:FriendMessage:test-user"


@pytest.fixture(params=["shared_store", "separate_connections"])
async def native(tmp_path, request):
    stores = [SqliteStore(tmp_path / "native.sqlite")]
    await stores[0].open()
    try:
        if request.param == "separate_connections":
            stores.append(SqliteStore(tmp_path / "native.sqlite"))
            await stores[-1].open()
        yield stores, [StoreConversationManager(stores[0]), StoreConversationManager(stores[-1])], SessionRepository(stores[0])
    finally:
        for store in reversed(stores):
            await store.close()


def pair(marker):
    return [{"role": "user", "content": f"u-{marker}"},
            {"role": "assistant", "content": f"a-{marker}"}]


async def history(manager, cid, umo=UMO):
    conversation = await manager.get_conversation(umo, cid)
    assert conversation is not None
    return json.loads(conversation.history)


async def projection(repo, sid):
    return [m.to_upstream() for m in await repo.messages(sid)]


async def test_concurrent_native_creates_keep_every_record_and_selection(native):
    stores, managers, _ = native
    cids = await asyncio.gather(*(
        managers[i % 2].new_conversation(f"bot:FriendMessage:user-{i}") for i in range(12)
    ))
    section = (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
    assert {key for key, value in section.items() if "cid" in value} == set(cids)
    for i, cid in enumerate(cids):
        assert await managers[0].get_curr_conversation_id(f"bot:FriendMessage:user-{i}") == cid


async def test_get_or_create_builds_only_one_selected_conversation(native):
    stores, managers, _ = native
    results = await asyncio.gather(*(
        managers[i % 2].get_or_create_conversation(UMO, persona_id="chosen-persona") for i in range(12)
    ))
    assert len({c.cid for c in results}) == 1
    section = (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
    assert set(section) == {UMO, results[0].cid}
    assert section[UMO]["sel"] == results[0].cid
    for conv in results:
        assert conv.platform_id == "test-bot"
        assert conv.persona_id == "chosen-persona"
        assert json.loads(conv.history) == []
    retained = await managers[1].get_or_create_conversation(UMO, persona_id="must-not-replace")
    assert retained.persona_id == "chosen-persona"


async def test_dangling_or_foreign_selection_does_not_expose_other_owner(native):
    stores, managers, _ = native
    foreign = await managers[0].new_conversation(OTHER_UMO, content=pair("foreign"))
    raw = (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
    raw[UMO] = {"sel": foreign}
    await stores[0].save_sections({CONVERSATIONS_SECTION: raw})
    assert await managers[0].get_curr_conversation_id(UMO) is None
    created = await managers[1].get_or_create_conversation(UMO)
    assert created.cid != foreign
    assert json.loads(created.history) == []
    assert await history(managers[0], foreign, OTHER_UMO) == pair("foreign")
    assert await managers[0].get_conversation(UMO, foreign) is None


async def test_concurrent_native_appends_do_not_drop_pairs(native):
    _, managers, _ = native
    cid = await managers[0].new_conversation(UMO)
    await asyncio.gather(*(managers[i % 2].add_message_pair(cid, *pair(i)) for i in range(16)))
    messages = await history(managers[0], cid)
    assert len(messages) == 32
    assert {m["content"] for m in messages} == {f"{role}-{i}" for role in ("u", "a") for i in range(16)}
    for user, assistant in zip(messages[::2], messages[1::2]):
        assert user["content"].removeprefix("u-") == assistant["content"].removeprefix("a-")


async def test_native_metadata_updates_do_not_erase_concurrent_append_or_other_cid(native):
    _, managers, _ = native
    c1 = await managers[0].new_conversation(UMO)
    c2 = await managers[0].new_conversation(OTHER_UMO)
    await asyncio.gather(
        managers[0].update_conversation(UMO, c1, title="kept title"),
        managers[1].update_conversation(UMO, c1, persona_id="kept persona"),
        managers[0].update_conversation(UMO, c1, token_usage=42),
        managers[1].add_message_pair(c1, *pair("first")),
        managers[0].add_message_pair(c2, *pair("second")),
    )
    conv = await managers[0].get_conversation(UMO, c1)
    assert (conv.title, conv.persona_id, conv.token_usage) == ("kept title", "kept persona", 42)
    assert json.loads(conv.history) == pair("first")
    assert await history(managers[0], c2, OTHER_UMO) == pair("second")


async def test_selected_cids_have_independent_projections_and_keep_legacy_history(native):
    stores, managers, sessions = native
    for legacy in (UMO, "test-user"):
        await sessions.ensure(legacy)
        await sessions.append(legacy, [Message(role="user", content="legacy must stay")])
    legacy_before = {sid: [m.to_dict() for m in await sessions.messages(sid)] for sid in (UMO, "test-user")}
    c1 = await managers[0].new_conversation(UMO, content=pair("first"))
    await sessions.ensure("projection-first")
    conv1 = await managers[0].project_conversation(UMO, c1, "projection-first")
    c2 = await managers[1].new_conversation(UMO, content=pair("second"))
    await sessions.ensure("projection-second")
    conv2 = await managers[1].project_conversation(UMO, c2, "projection-second")
    assert json.loads(conv1.history) == await projection(sessions, "projection-first") == pair("first")
    assert json.loads(conv2.history) == await projection(sessions, "projection-second") == pair("second")
    before = await stores[0].load_store()
    with pytest.raises(RuntimeError, match="Selected conversation changed"):
        await managers[0].project_conversation(UMO, c1, "projection-first")
    assert await stores[0].load_store() == before
    for sid in (UMO, "test-user"):
        assert [m.to_dict() for m in await sessions.messages(sid)] == legacy_before[sid]


async def test_projection_is_one_way_and_preserves_protocol_and_media_parts(native):
    stores, managers, sessions = native
    tool_calls = [{"id": "call-native", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
    native_history = [
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "https://media.invalid/test.png"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": tool_calls, "reasoning_content": "test reasoning"},
        {"role": "tool", "content": "tool result", "tool_call_id": "call-native"},
    ]
    cid = await managers[0].new_conversation(UMO, content=native_history)
    await sessions.ensure("projection")
    await sessions.append("projection", [Message(role="user", content="UI-only content must disappear")])
    await sessions.update("projection", {"summary": "UI-only summary", "summarized_upto": 1})
    native_before = (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
    conv = await managers[1].project_conversation(UMO, cid, "projection")
    assert json.loads(conv.history) == native_history
    assert await projection(sessions, "projection") == native_history
    assert (await sessions.get("projection")).message_count == 3
    assert (await sessions.get("projection")).summary == ""
    assert (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION] == native_before


@pytest.mark.parametrize("change", ["extend", "replace", "truncate"])
async def test_projection_reuses_prefix_timestamps_and_invalidates_diverged_summary(native, change):
    _, managers, sessions = native
    initial = pair("one") + pair("two")
    cid = await managers[0].new_conversation(UMO, content=initial)
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    old = await sessions.messages("projection")
    await sessions.update("projection", {"policy": {"mode": "rolling_summary"}})
    snap, _ = await sessions.summary_snapshot("projection")
    assert await sessions.commit_summary(
        "projection", expected_history_revision=snap.history_revision,
        summary="kept only for extension", summarized_upto=2,
    )
    changed = {"extend": initial + pair("three"), "replace": pair("other"), "truncate": pair("one")}[change]
    if change == "extend":
        await managers[1].add_message_pair(cid, *pair("three"))
    else:
        await managers[1].update_conversation(UMO, cid, history=changed)
    await managers[0].project_conversation(UMO, cid, "projection")
    metadata = await sessions.get("projection")
    messages = await sessions.messages("projection")
    assert await projection(sessions, "projection") == changed
    assert metadata.message_count == len(changed)
    if change == "extend":
        assert metadata.summary == "kept only for extension"
        assert metadata.summarized_upto == 2
        assert [m.timestamp for m in messages[:4]] == [m.timestamp for m in old]
    else:
        assert metadata.summary == ""
        assert metadata.summarized_upto == 0
        if change == "truncate":
            assert [m.timestamp for m in messages] == [m.timestamp for m in old[:2]]


async def test_record_reply_targets_captured_cid_after_selected_conversation_switch(native):
    stores, managers, sessions = native
    c1 = await managers[0].new_conversation(UMO, content=pair("first"))
    await sessions.ensure("projection-first")
    await managers[0].project_conversation(UMO, c1, "projection-first")
    c2 = await managers[1].new_conversation(UMO, content=pair("second"))
    await sessions.ensure("projection-second")
    await managers[1].project_conversation(UMO, c2, "projection-second")
    before_second = [m.to_dict() for m in await sessions.messages("projection-second")]
    await managers[0].record_reply(UMO, c1, "projection-first", *pair("sent"))
    assert await managers[0].get_curr_conversation_id(UMO) == c2
    assert await history(managers[0], c1) == pair("first") + pair("sent")
    assert await projection(sessions, "projection-first") == pair("first") + pair("sent")
    assert await history(managers[0], c2) == pair("second")
    assert [m.to_dict() for m in await sessions.messages("projection-second")] == before_second


async def test_concurrent_record_reply_preserves_native_and_projection_in_one_transaction(native):
    _, managers, sessions = native
    cid = await managers[0].new_conversation(UMO)
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    await asyncio.gather(*(
        managers[i % 2].record_reply(UMO, cid, "projection", *pair(i)) for i in range(12)
    ))
    native_history = await history(managers[0], cid)
    assert len(native_history) == 24
    assert {m["content"] for m in native_history} == {f"{role}-{i}" for role in ("u", "a") for i in range(12)}
    assert await projection(sessions, "projection") == native_history
    assert (await sessions.get("projection")).message_count == 24


@pytest.mark.parametrize("operation", ["project_conversation", "record_reply"])
async def test_projection_requires_existing_session_and_correct_owner(native, operation):
    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    before = await stores[0].load_store()
    args = () if operation == "project_conversation" else tuple(pair("must-not-record"))
    method = getattr(managers[0], operation)
    with pytest.raises(SessionNotFound):
        await method(UMO, cid, "missing-projection", *args)
    assert await stores[0].load_store() == before
    await sessions.ensure("projection")
    before = await stores[0].load_store()
    with pytest.raises(RuntimeError, match="does not belong"):
        await method(OTHER_UMO, cid, "projection", *args)
    assert await stores[0].load_store() == before


async def test_record_failure_rolls_back_native_messages_projection_and_metadata(native):
    stores, managers, sessions = native
    store = stores[0]
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    before = await store.load_store()
    revision = await store.current_revision()
    conn = store._require_conn()
    await conn.execute("""
        CREATE TEMP TRIGGER fail_projection BEFORE UPDATE ON store
        WHEN NEW.section = 'sessions'
        BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END
    """)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected projection failure"):
            await managers[0].record_reply(UMO, cid, "projection", *pair("failed"))
        assert await store.load_store() == before
        assert await stores[-1].load_store() == before
        assert await store.current_revision() == revision
    finally:
        await conn.execute("DROP TRIGGER fail_projection")
    await managers[1].record_reply(UMO, cid, "projection", *pair("recovered"))
    assert await history(managers[0], cid) == await projection(sessions, "projection") == pair("seed") + pair("recovered")


@pytest.mark.parametrize("bad_field", ["native_content", "sessions", "messages", "projection_row"])
@pytest.mark.parametrize("operation", ["project_conversation", "record_reply"])
async def test_invalid_projection_input_is_not_silently_replaced(native, bad_field, operation):
    from emotion_app.storage.base import StorageError

    stores, managers, sessions = native
    store = stores[0]
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    raw = await store.load_store()
    if bad_field == "native_content":
        raw[CONVERSATIONS_SECTION][cid]["content"] = {}
    elif bad_field == "projection_row":
        raw["messages"]["projection"] = {}
    else:
        raw[bad_field] = []
    await store.save_sections(raw)
    before = await store.load_store()
    revision = await store.current_revision()
    args = () if operation == "project_conversation" else tuple(pair("must-not-record"))
    with pytest.raises(StorageError, match="格式错误"):
        await getattr(managers[0], operation)(UMO, cid, "projection", *args)
    assert await store.load_store() == before
    assert await store.current_revision() == revision


async def test_get_or_create_rejects_invalid_empty_native_section(native):
    stores, managers, _ = native
    await stores[0].save_sections({CONVERSATIONS_SECTION: []})
    before = await stores[0].load_store()
    with pytest.raises(RuntimeError, match="格式错误"):
        await managers[0].get_or_create_conversation(UMO)
    assert await stores[0].load_store() == before


@pytest.mark.parametrize("reset_kind", ["same_content", "clear", "replace"])
async def test_native_reset_rejects_stale_summary_before_and_after_projection(native, reset_kind):
    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    await sessions.update("projection", {"policy": {"mode": "rolling_summary"}})
    snapshot, _ = await sessions.summary_snapshot("projection")
    assert await sessions.commit_summary(
        "projection", expected_history_revision=snapshot.history_revision,
        summary="valid before reset", summarized_upto=1,
    )
    reset = {"same_content": pair("seed"), "clear": [], "replace": pair("replacement")}[reset_kind]
    await managers[1].update_conversation(UMO, cid, history=reset)
    assert not await sessions.commit_summary(
        "projection", expected_history_revision=snapshot.history_revision,
        summary="stale poison", summarized_upto=2,
    )
    await managers[0].project_conversation(UMO, cid, "projection")
    current = await sessions.get("projection")
    assert current.history_revision > snapshot.history_revision
    assert current.summary == ""
    assert current.summarized_upto == 0
    assert await projection(sessions, "projection") == reset
    assert not await sessions.commit_summary(
        "projection", expected_history_revision=snapshot.history_revision,
        summary="stale poison", summarized_upto=1,
    )


@pytest.mark.parametrize("mutation", ["append", "clear_messages", "delete", "persona_id", "user_id", "summary", "summarized_upto"])
async def test_native_projection_history_and_persona_mutations_are_rejected(native, mutation):
    from emotion_app.domain.session_repo import NativeProjectionReadOnly

    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    before = await stores[0].load_store()
    with pytest.raises(NativeProjectionReadOnly, match="陪伴管理"):
        if mutation == "append":
            await sessions.append("projection", [Message(role="user", content="must not write")])
        elif mutation in ("clear_messages", "delete"):
            await getattr(sessions, mutation)("projection")
        else:
            await sessions.update("projection", {mutation: 0 if mutation == "summarized_upto" else "new"})
    assert await stores[0].load_store() == before
    assert await history(managers[0], cid) == pair("seed")


@pytest.mark.parametrize("operation", ["project_conversation", "record_reply"])
async def test_existing_native_projection_cannot_be_rebound(native, operation):
    from emotion_app.domain.session_repo import NativeProjectionReadOnly

    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("first"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    new_cid = await managers[1].new_conversation(UMO, content=pair("second"))
    before = await stores[0].load_store()
    args = () if operation == "project_conversation" else tuple(pair("must not record"))
    with pytest.raises(NativeProjectionReadOnly):
        await getattr(managers[0], operation)(UMO, new_cid, "projection", *args)
    assert await stores[0].load_store() == before


async def test_native_projection_links_survive_allowed_settings_and_captured_reply(native):
    _, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    changed = await sessions.update("projection", {
        "title": "local title", "model": "test/model", "policy": {"mode": "rolling_summary"},
    })
    assert (changed.native_origin, changed.native_conversation_id) == (UMO, cid)
    await sessions.mark_read("projection")
    await managers[1].new_conversation(UMO, content=pair("other selected"))
    await managers[0].record_reply(UMO, cid, "projection", *pair("sent"))
    current = await sessions.get("projection")
    assert (current.title, current.model, current.policy.mode) == ("local title", "test/model", "rolling_summary")
    assert (current.native_origin, current.native_conversation_id) == (UMO, cid)
    assert current.history_revision > changed.history_revision
    assert await projection(sessions, "projection") == pair("seed") + pair("sent")


@pytest.mark.parametrize("as_json", [False, True])
def test_conversation_messages_filters_only_checkpoint_and_keeps_protocol(as_json):
    from copy import deepcopy
    from emotion_app.domain.conversation_manager import conversation_messages

    calls = [{"id": "call-checkpoint", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
    protocol = [
        {"role": "user", "content": [
            {"type": "text", "text": "input"},
            {"type": "image_url", "image_url": {"url": "https://media.invalid/checkpoint.png"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "content": "tool result", "tool_call_id": "call-checkpoint"},
    ]
    authoritative = [{"role": "_checkpoint", "content": {"id": "checkpoint-id"}}, *protocol]
    before = deepcopy(authoritative)
    converted = conversation_messages(json.dumps(authoritative) if as_json else authoritative)
    assert [message.to_upstream() for message in converted] == protocol
    assert authoritative == before


@pytest.mark.parametrize("bad", ["{}", "null", "[1]", "invalid json", {}, [None], [1]])
def test_conversation_messages_rejects_invalid_structure(bad):
    from emotion_app.domain.conversation_manager import conversation_messages

    with pytest.raises(ValueError):
        conversation_messages(bad)


async def test_checkpoint_stays_in_authoritative_history_but_not_projection(native):
    stores, managers, sessions = native
    checkpoint = {"role": "_checkpoint", "content": {"id": "checkpoint-id"}}
    authoritative = [checkpoint, *pair("seed")]
    cid = await managers[0].new_conversation(UMO, content=authoritative)
    got = await managers[0].get_conversation(UMO, cid)
    assert json.loads(got.history) == authoritative
    await sessions.ensure("projection")
    projected = await managers[0].project_conversation(UMO, cid, "projection")
    assert json.loads(projected.history) == authoritative
    assert await projection(sessions, "projection") == pair("seed")
    assert (await sessions.get("projection")).message_count == 2
    await managers[1].record_reply(UMO, cid, "projection", *pair("sent"))
    assert await history(managers[0], cid) == [checkpoint, *pair("seed"), *pair("sent")]
    assert await projection(sessions, "projection") == pair("seed") + pair("sent")


def _trim_tool_block(checkpoints=False):
    calls = [
        {"id": f"trim-call-{i}", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        for i in range(3)
    ]
    block = [{"role": "assistant", "content": None, "tool_calls": calls}]
    for i in range(3):
        if checkpoints and i:
            block.append({"role": "_checkpoint", "content": {"id": f"checkpoint-{i}"}})
        block.append({"role": "tool", "content": f"result-{i}", "tool_call_id": f"trim-call-{i}"})
    return block


@pytest.mark.parametrize("boundary", ["tool_middle", "checkpoint_middle", "assistant_start"])
def test_native_trim_does_not_create_orphan_tool_results(boundary, caplog):
    from copy import deepcopy
    from emotion_app.domain.context import ContextPolicy, build_context
    from emotion_app.domain.conversation_manager import conversation_messages, MAX_MESSAGES

    tail = [{"role": "user", "content": f"tail-{i}"} for i in range(398)]
    if boundary == "assistant_start":
        expected = _trim_tool_block() + tail[:396]
        source = pair("old") + expected
    else:
        source = _trim_tool_block(checkpoints=boundary == "checkpoint_middle") + tail
        expected = tail
    original = deepcopy(source)
    policy = ContextPolicy(protected_tail=1000, budget_chars=1000000)
    build_context(conversation_messages(source), policy)
    with caplog.at_level("WARNING"):
        trimmed = StoreConversationManager._trim(source)
    assert trimmed == expected
    assert len(trimmed) <= MAX_MESSAGES
    assert source == original
    assert [m.to_upstream() for m in build_context(conversation_messages(trimmed), policy).history] == [
        m.to_upstream() for m in conversation_messages(expected)
    ]
    assert "对话历史超过上限" in caplog.text
    if boundary != "assistant_start":
        assert "协议完整" in caplog.text
        assert "额外" in caplog.text


def test_native_trim_leaves_exact_limit_and_short_history_unchanged(caplog):
    from emotion_app.domain.context import ContextPolicy, build_context
    from emotion_app.domain.conversation_manager import conversation_messages, MAX_MESSAGES

    exact = _trim_tool_block(checkpoints=True) + [
        {"role": "user", "content": f"tail-{i}"} for i in range(MAX_MESSAGES - 6)
    ]
    assert len(exact) == MAX_MESSAGES
    assert StoreConversationManager._trim(exact) is exact
    short = pair("unchanged")
    assert StoreConversationManager._trim(short) is short
    build_context(conversation_messages(exact), ContextPolicy(protected_tail=1000, budget_chars=1000000))
    assert "对话历史超过上限" not in caplog.text


async def test_native_append_trims_interrupted_tool_block_before_projection(native):
    from emotion_app.domain.context import ContextPolicy, build_context
    from emotion_app.domain.conversation_manager import conversation_messages

    _, managers, sessions = native
    tail = [{"role": "user", "content": f"tail-{i}"} for i in range(396)]
    cid = await managers[0].new_conversation(UMO, content=_trim_tool_block(checkpoints=True) + tail)
    await managers[1].add_message_pair(cid, *pair("new"))
    expected = tail + pair("new")
    assert await history(managers[0], cid) == expected
    await sessions.ensure("trim-projection")
    result = await managers[1].project_conversation(UMO, cid, "trim-projection")
    context = build_context(conversation_messages(result.history), ContextPolicy(protected_tail=1000, budget_chars=1000000))
    assert [m.to_upstream() for m in context.history] == expected
    assert await projection(sessions, "trim-projection") == expected


def test_conversation_reset_token_defaults_for_legacy_snapshots():
    from emotion_app.domain.conversation_manager import Conversation

    assert Conversation().reset_revision == 0
    assert StoreConversationManager._to_conv({"cid": "legacy", "content": []}).reset_revision == 0


@pytest.mark.parametrize("reset_kind", ["same_content", "clear", "replace"])
async def test_record_reset_token_rejects_stale_reply_without_any_write(native, monkeypatch, reset_kind):
    from unittest.mock import Mock

    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    captured = await managers[0].project_conversation(UMO, cid, "projection")
    reset = {"same_content": pair("seed"), "clear": [], "replace": pair("replacement")}[reset_kind]
    await managers[1].update_conversation(UMO, cid, history=reset)
    current = await managers[1].get_conversation(UMO, cid)
    assert current.reset_revision > captured.reset_revision
    assert json.loads(current.history) == reset
    assert json.loads(captured.history) == pair("seed")
    before = await stores[0].load_store()
    revision = await stores[0].current_revision()
    append = Mock(wraps=managers[0]._append_pair)
    project = Mock(wraps=managers[0]._project)
    monkeypatch.setattr(managers[0], "_append_pair", append)
    monkeypatch.setattr(managers[0], "_project", project)
    assert await managers[0].record_reply(
        UMO, cid, "projection", *pair("must-not-return"),
        expected_reset_revision=captured.reset_revision,
    ) is False
    append.assert_not_called()
    project.assert_not_called()
    assert await stores[0].load_store() == before
    assert await stores[0].current_revision() == revision


async def test_normal_native_append_does_not_invalidate_captured_reset_token(native):
    _, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    captured = await managers[0].project_conversation(UMO, cid, "projection")
    await managers[1].add_message_pair(cid, *pair("concurrent"))
    assert (await managers[1].get_conversation(UMO, cid)).reset_revision == captured.reset_revision
    assert await managers[0].record_reply(
        UMO, cid, "projection", *pair("sent"), expected_reset_revision=captured.reset_revision,
    ) is True
    expected = pair("seed") + pair("concurrent") + pair("sent")
    assert await history(managers[0], cid) == await projection(sessions, "projection") == expected


async def test_captured_reset_token_allows_old_cid_after_selection_switch(native):
    _, managers, sessions = native
    c1 = await managers[0].new_conversation(UMO, content=pair("first"))
    await sessions.ensure("projection-first")
    captured = await managers[0].project_conversation(UMO, c1, "projection-first")
    c2 = await managers[1].new_conversation(UMO, content=pair("selected second"))
    await sessions.ensure("projection-second")
    await managers[1].project_conversation(UMO, c2, "projection-second")
    before_second = [message.to_dict() for message in await sessions.messages("projection-second")]
    assert await managers[0].record_reply(
        UMO, c1, "projection-first", *pair("sent"), expected_reset_revision=captured.reset_revision,
    ) is True
    assert await managers[0].get_curr_conversation_id(UMO) == c2
    assert await history(managers[0], c1) == pair("first") + pair("sent")
    assert await history(managers[0], c2) == pair("selected second")
    assert [message.to_dict() for message in await sessions.messages("projection-second")] == before_second


async def test_record_without_reset_token_keeps_legacy_call_compatible(native):
    _, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    await managers[1].update_conversation(UMO, cid, history=pair("reset"))
    assert await managers[0].record_reply(UMO, cid, "projection", *pair("legacy")) is True
    assert await history(managers[0], cid) == await projection(sessions, "projection") == pair("reset") + pair("legacy")


async def test_reset_token_is_checked_after_pending_reset_transaction_commits(native, monkeypatch):
    from unittest.mock import Mock

    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    captured = await managers[0].project_conversation(UMO, cid, "projection")
    before = await stores[0].load_store()
    revision = await stores[0].current_revision()
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resetting_store = stores[-1]
    original = resetting_store._bump_revision

    async def pause_reset_commit(conn):
        entered.set()
        await release.wait()
        await original(conn)

    async def record_stale():
        started.set()
        return await managers[0].record_reply(
            UMO, cid, "projection", *pair("stale"), expected_reset_revision=captured.reset_revision,
        )

    append = Mock(wraps=managers[0]._append_pair)
    project = Mock(wraps=managers[0]._project)
    monkeypatch.setattr(managers[0], "_append_pair", append)
    monkeypatch.setattr(managers[0], "_project", project)
    monkeypatch.setattr(resetting_store, "_bump_revision", pause_reset_commit)
    resetting = asyncio.create_task(managers[1].update_conversation(UMO, cid, history=pair("seed")))
    recording = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        recording = asyncio.create_task(record_stale())
        await started.wait()
        assert not recording.done()
    finally:
        release.set()
        await resetting
        if recording is not None:
            recorded = await asyncio.wait_for(recording, timeout=3)
    assert recorded is False
    append.assert_not_called()
    project.assert_not_called()
    after = await stores[0].load_store()
    assert after["sessions"] == before["sessions"]
    assert after["messages"] == before["messages"]
    assert after[CONVERSATIONS_SECTION][cid]["content"] == pair("seed")
    assert after[CONVERSATIONS_SECTION][cid]["reset_revision"] > captured.reset_revision
    assert await stores[0].current_revision() == revision + 1
