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

    # 🔴 **投影是单向的：它不许把「面板侧的历史/摘要」写回插件那边**。
    #
    #    ⚠️ 原断言是"整个 `plugin_conversations` 逐字不变"—— 那太绝对了。
    #       2026-10-31 起 `_project()` 会**刻意**给人设做同步
    #       （把面板的 UUID 翻译成插件认的 `name` 写进 `persona_id`），
    #       否则"面板绑了人设、QQ 那边完全无感"。
    #       所以这里改成：**除了 `persona_id`，其它字段一个都不许变**。
    #       单向性守的仍然是那件事（历史/摘要不许反向污染），
    #       只是把"人设同步"这个**有意为之**的例外排除在外。
    native_after = (await stores[0].load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
    assert set(native_after) == set(native_before)
    for key in native_before:
        strip = lambda d: {k: v for k, v in d.items() if k != "persona_id"}  # noqa: E731
        assert strip(native_after[key]) == strip(native_before[key]), (
            f"{key} 的插件侧记录被投影改动了（除 persona_id 外都不许动）"
        )


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


#: 🔴 `persona_id` **已从"必须拒绝"里移出**（2026-10-31）。
#:    它曾和 `summary` 一起被禁，但两者归属不同：
#:      * `summary` / `summarized_upto` —— 历史正文的摘要，归陪伴插件 ⇒ 必须拒绝
#:      * `user_id` —— **umo 第三段**，插件认人的键 ⇒ 必须拒绝
#:      * `persona_id` —— 我们聊天用哪个系统提示词，**插件不读它** ⇒ 应允许
#:    禁它的实际后果：设置页点保存 → 红字「历史和人格由陪伴管理」→
#:    用户以为"不能给人设"（情绪就是这么报的）。
#:    正向判据见 `test_native_projection_persona_binding_is_allowed_and_persists`。
@pytest.mark.parametrize("mutation", ["append", "clear_messages", "delete", "user_id", "summary", "summarized_upto"])
async def test_native_projection_history_and_identity_mutations_are_rejected(native, mutation):
    from emotion_app.domain.session_repo import NativeProjectionReadOnly

    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    before = await stores[0].load_store()
    # ⚠️ 匹配串必须与产品**真正会打的**一致；这里用 "陪伴插件维护"
    #    （旧文案是"陪伴管理"，2026-10-31 改文案时同步改了这条，否则它会
    #    永远绿 —— 否定式断言的字面串漂移是假判据的经典成因）
    with pytest.raises(NativeProjectionReadOnly, match="陪伴插件维护"):
        if mutation == "append":
            await sessions.append("projection", [Message(role="user", content="must not write")])
        elif mutation in ("clear_messages", "delete"):
            await getattr(sessions, mutation)("projection")
        else:
            await sessions.update("projection", {mutation: 0 if mutation == "summarized_upto" else "new"})
    assert await stores[0].load_store() == before
    assert await history(managers[0], cid) == pair("seed")


async def test_native_projection_persona_binding_is_allowed_and_persists(native):
    """原生会话绑人设**允许**，且**真的落盘**。

    🔴 为什么必须有这条正向判据：只把 `persona_id` 从拒绝列表里删掉，
       "能改"是观测不到的 —— 白名单里也允许它才行，而且必须确认**写进去了**
       （本项目最忌"面板说保存成功、实际没写"）。

    ⚠️ 同时确认一件容易误伤的事：改人设**不许**动 `history_revision`
       （那是历史版本，归插件；动它会让在途回复的 CAS 失效）。
    """
    stores, managers, sessions = native
    cid = await managers[0].new_conversation(UMO, content=pair("seed"))
    await sessions.ensure("projection")
    await managers[0].project_conversation(UMO, cid, "projection")
    before = await sessions.get("projection")

    updated = await sessions.update("projection", {"persona_id": "chosen-persona"})

    assert updated.persona_id == "chosen-persona"
    # 落盘判据：重新读回来还是它（不是只在内存里改了）
    assert (await sessions.get("projection")).persona_id == "chosen-persona"
    # 历史相关的版本号不许被这次改动带跑
    assert updated.history_revision == before.history_revision
    # 插件那边的历史也没被动
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


# ---------------------------------------------------------------------------
# 人设同步：面板绑的人设要能被插件读到（方案 A，2026-10-31）
# ---------------------------------------------------------------------------


async def test_project_syncs_persona_name_into_plugin_conversation(tmp_path):
    """`_project()` 要把面板绑的人设**翻译成 name** 写进 `plugin_conversations`。

    🔴 为什么这条必须存在（AstrBot 能改、我们不能的根因）：
       AstrBot 只有一套会话存储 —— 面板改 `persona_id`、插件读同一份 ⇒ 即改即生效。
       我们有**两套**（`sessions` vs `plugin_conversations`）且从来没打通：
       面板把人设存在 `sessions.<sid>.persona_id`（UUID），
       而陪伴插件读的是 `conversations.persona_id`
       （`native.py:397` 的 `persona_snapshot_lookup`）⇒ **绑了等于没绑**。

    🔴 而且**必须写 name 不能写 UUID**：`multi_controller.py:244` 校验
       `native["name"] == binding.persona_id`，`_persona_to_v3` 也写明
       「`name` 是插件的主键」。写 UUID 会被判 `host_persona_unavailable`。

    ⚠️ 判据要断言**具体值**（"糯米茨"），不能只断言"非空" ——
       写进 UUID 也是非空的，那样这条判据就抓不住真 bug。
    """
    store = SqliteStore(tmp_path / "sync.sqlite")
    await store.open()
    try:
        # 一个"人设 id → 名字"的极简映射：面板存 UUID，插件认 name
        names = {"uuid-liloth": "liloth", "uuid-nuomi": "糯米茨"}
        manager = StoreConversationManager(store, persona_name_of=names.get)
        sessions = SessionRepository(store)

        cid = await manager.new_conversation(UMO, content=pair("seed"))
        await sessions.ensure("projection")
        # 面板侧绑定（存的是 UUID）
        await sessions.update("projection", {"persona_id": "uuid-nuomi"})

        await manager.project_conversation(UMO, cid, "projection")

        conv = await manager.get_conversation(UMO, cid)
        assert conv.persona_id == "糯米茨", (
            f"插件侧读到的是 {conv.persona_id!r} —— 应该是人设**名字**，不是 UUID"
        )
    finally:
        await store.close()


async def test_project_clears_plugin_persona_when_panel_unbinds(tmp_path):
    """面板把绑定清空 ⇒ 插件侧也要清（否则它会继续用那个旧角色）。

    ⚠️ 这条守的是"只写不清"的半吊子实现：只在有值时同步，清空时什么也不做，
       插件就会一直拿着上一次的人设 —— 用户以为解绑了，实际没解。
    """
    store = SqliteStore(tmp_path / "sync2.sqlite")
    await store.open()
    try:
        names = {"uuid-liloth": "liloth"}
        manager = StoreConversationManager(store, persona_name_of=names.get)
        sessions = SessionRepository(store)

        cid = await manager.new_conversation(UMO, content=pair("seed"))
        await sessions.ensure("projection")
        await sessions.update("projection", {"persona_id": "uuid-liloth"})
        await manager.project_conversation(UMO, cid, "projection")
        assert (await manager.get_conversation(UMO, cid)).persona_id == "liloth"

        # 面板解绑
        await sessions.update("projection", {"persona_id": ""})
        await manager.project_conversation(UMO, cid, "projection")
        assert (await manager.get_conversation(UMO, cid)).persona_id == ""
    finally:
        await store.close()


async def test_project_writes_empty_when_persona_unknown_instead_of_guessing(tmp_path):
    """人设 id 查不到名字时**写空**，不要原样把 UUID 写进去。

    🔴 写 UUID 的后果不是报错，而是插件抛 `host_persona_unavailable`
       （"choose a persona that is currently loaded in AstrBot"）——
       一个**看起来与本操作无关**的错误。
    """
    store = SqliteStore(tmp_path / "sync3.sqlite")
    await store.open()
    try:
        # 空映射 = 谁都查不到（模拟人设已被删 / 缓存未预热）
        manager = StoreConversationManager(store, persona_name_of=lambda pid: "")
        sessions = SessionRepository(store)

        cid = await manager.new_conversation(UMO, content=pair("seed"))
        await sessions.ensure("projection")
        await sessions.update("projection", {"persona_id": "uuid-gone"})
        await manager.project_conversation(UMO, cid, "projection")

        conv = await manager.get_conversation(UMO, cid)
        assert conv.persona_id == "", (
            f"查不到 name 时不该写入 {conv.persona_id!r}（插件会因此报 host_persona_unavailable）"
        )
    finally:
        await store.close()


async def test_update_persona_syncs_to_plugin_side_immediately(tmp_path):
    """`sessions.update({persona_id})` 要**当场**把 name 写进 `plugin_conversations`。

    🔴 为什么不能等下一次聊天：`project_conversation()` 只在聊天请求链路里被调
       （`default_llm_reply.py:181`），所以"面板改完 → 下一轮 QQ 消息"之间
       两侧**不一致**：面板显示已绑，插件仍是旧角色。
       实测就是这么翻车的 —— 部署脚本 PATCH 后立即读，插件侧还是旧值 '莉莉丝'。

    ⚠️ 必须验**同一个事务**里写进去的（而不是"稍后会有"）：
       读完立刻断言，中间不做任何触发投影的动作。
    """
    store = SqliteStore(tmp_path / "sync4.sqlite")
    await store.open()
    try:
        names = {"uuid-nuomi": "糯米茨"}
        sessions = SessionRepository(store, persona_name_of=names.get)
        manager = StoreConversationManager(store, persona_name_of=names.get)

        # 造一条原生会话（cid + native_conversation_id 都齐）
        cid = await manager.new_conversation(UMO, content=pair("seed"))
        await sessions.ensure("projection")
        await manager.project_conversation(UMO, cid, "projection")

        # 面板改人设 —— **只做这一步**，不触发投影
        await sessions.update("projection", {"persona_id": "uuid-nuomi"})

        node = (await store.load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
        assert node[cid]["persona_id"] == "糯米茨", (
            f"插件侧没同步（读到 {node[cid]['persona_id']!r}）—— 用户会在面板看到已绑、"
            "而插件仍用旧角色"
        )
    finally:
        await store.close()


async def test_update_clearing_persona_clears_plugin_side_immediately(tmp_path):
    """面板解绑 ⇒ 插件侧**当场**清空（不等到下次聊天）。"""
    store = SqliteStore(tmp_path / "sync5.sqlite")
    await store.open()
    try:
        names = {"uuid-nuomi": "糯米茨"}
        sessions = SessionRepository(store, persona_name_of=names.get)
        manager = StoreConversationManager(store, persona_name_of=names.get)

        cid = await manager.new_conversation(UMO, content=pair("seed"))
        await sessions.ensure("projection")
        await manager.project_conversation(UMO, cid, "projection")
        await sessions.update("projection", {"persona_id": "uuid-nuomi"})
        node = (await store.load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
        assert node[cid]["persona_id"] == "糯米茨"

        await sessions.update("projection", {"persona_id": ""})

        node = (await store.load_sections([CONVERSATIONS_SECTION]))[CONVERSATIONS_SECTION]
        assert node[cid]["persona_id"] == "", (
            "解绑后插件侧仍有值 —— 插件会继续用那个已经不绑的角色"
        )
    finally:
        await store.close()


def test_section_names_match_between_modules():
    """两处 `plugin_conversations` 常量必须**同一个值**。

    ⚠️ `session_repo` 与 `conversation_manager` 各写了一份（前者不能 import 后者，
       会成环）。两份漂了就会出现"写进 A 节、插件读 B 节"——
       而那是**完全不报错**的静默失效。
    """
    from emotion_app.domain import conversation_manager as cm
    from emotion_app.domain import session_repo as sr

    assert sr.CONVERSATION_SECTION == cm.CONVERSATIONS_SECTION == "plugin_conversations"
