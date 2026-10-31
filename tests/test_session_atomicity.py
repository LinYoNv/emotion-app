"""会话读改写必须在共享连接及独立 SQLite 连接间保持原子性。"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from emotion_app.domain.message import Message
from emotion_app.domain.session_repo import SessionNotFound, SessionRepository
from emotion_app.storage.sqlite_store import SqliteStore


@pytest.fixture(params=["shared_store", "separate_connections"])
async def repositories(tmp_path, request):
    stores = [SqliteStore(tmp_path / "atomic.sqlite")]
    await stores[0].open()
    try:
        if request.param == "separate_connections":
            stores.append(SqliteStore(tmp_path / "atomic.sqlite"))
            await stores[1].open()
        yield stores, [SessionRepository(stores[0]), SessionRepository(stores[-1])]
    finally:
        for store in reversed(stores):
            await store.close()


def pair(marker):
    return [Message(role="user", content=f"user-{marker}"),
            Message(role="assistant", content=f"assistant-{marker}")]


async def assert_session(repo, sid, markers):
    messages = await repo.messages(sid)
    expected = {(role, f"{role}-{marker}") for marker in markers for role in ("user", "assistant")}
    assert {(m.role, m.content) for m in messages} == expected
    assert len(messages) == len(expected)
    for user, assistant in zip(messages[::2], messages[1::2]):
        assert user.role == "user"
        assert assistant.role == "assistant"
        assert user.content.removeprefix("user-") == assistant.content.removeprefix("assistant-")
    metadata = await repo.get(sid)
    assert metadata.message_count == len(messages)
    assert metadata.unread == len(markers)


async def test_concurrent_creates_preserve_all_sessions_and_generated_users(repositories):
    _, repos = repositories
    created = await asyncio.gather(*(
        repos[i % 2].create(session_id=f"session-{i}", title=f"title-{i}") for i in range(12)
    ))
    stored = {s.id: s for s in await repos[0].list()}
    assert set(stored) == {f"session-{i}" for i in range(12)}
    assert len({s.user_id for s in created}) == 12
    for returned in created:
        assert stored[returned.id].to_dict() == returned.to_dict()


async def test_concurrent_ensure_does_not_replace_existing_session(repositories):
    _, repos = repositories

    async def ensure_and_append(i):
        repo = repos[i % 2]
        session = await repo.ensure("shared-session")
        await repo.append(session.id, pair(i), unread_delta=1)
        return session.user_id

    users = await asyncio.gather(*(ensure_and_append(i) for i in range(12)))
    assert len(set(users)) == 1
    assert (await repos[0].get("shared-session")).user_id == users[0]
    await assert_session(repos[0], "shared-session", range(12))


async def test_concurrent_appends_to_different_sessions_preserve_all_messages(repositories):
    _, repos = repositories
    for i in range(4):
        await repos[0].create(session_id=f"session-{i}")
    await asyncio.gather(*(
        repos[i % 2].append(f"session-{i % 4}", pair(i), unread_delta=1) for i in range(16)
    ))
    for i in range(4):
        await assert_session(repos[0], f"session-{i}", range(i, 16, 4))


async def test_concurrent_appends_to_same_session_preserve_pairs_counts_and_unread(repositories):
    _, repos = repositories
    await repos[0].create(session_id="shared-session")
    await asyncio.gather(*(
        repos[i % 2].append("shared-session", pair(i), unread_delta=1) for i in range(16)
    ))
    await assert_session(repos[0], "shared-session", range(16))
    assert (await repos[0].get("shared-session")).title in {f"user-{i}" for i in range(16)}


async def test_concurrent_metadata_patches_and_append_preserve_each_change(repositories):
    _, repos = repositories
    await repos[0].create(session_id="shared-session")
    patches = {"title": "chosen title", "persona_id": "chosen-persona", "model": "test/model",
               "summary": "chosen summary", "summarized_upto": 3, "user_id": "chosen-user"}
    await asyncio.gather(
        *(repos[i % 2].update("shared-session", {key: value})
          for i, (key, value) in enumerate(patches.items())),
        repos[1].append("shared-session", pair("new"), unread_delta=1),
    )
    session = await repos[0].get("shared-session")
    assert {key: getattr(session, key) for key in patches} == patches
    await assert_session(repos[0], "shared-session", ["new"])


@pytest.mark.parametrize("operation", ["clear_messages", "delete", "mark_read"])
async def test_session_mutations_do_not_erase_other_session_appends(repositories, operation):
    _, repos = repositories
    for sid in ("changed", "survivor"):
        await repos[0].create(session_id=sid)
        await repos[0].append(sid, pair("seed"), unread_delta=1)
    await asyncio.gather(
        getattr(repos[0], operation)("changed"),
        repos[1].append("survivor", pair("new"), unread_delta=1),
    )
    await assert_session(repos[0], "survivor", ["seed", "new"])
    if operation == "delete":
        with pytest.raises(SessionNotFound):
            await repos[0].get("changed")
        assert "changed" not in (await repos[0]._store.load_sections(["messages"]))["messages"]
    elif operation == "clear_messages":
        assert await repos[0].messages("changed") == []
        assert (await repos[0].get("changed")).message_count == 0
    else:
        assert (await repos[0].get("changed")).unread == 0
        assert (await repos[0].get("changed")).message_count == 2


async def test_failed_second_section_write_rolls_back_messages_metadata_and_revision(repositories):
    stores, repos = repositories
    store = stores[0]
    await repos[0].create(session_id="session")
    await repos[0].append("session", pair("seed"), unread_delta=1)
    before = await store.load_store()
    revision = await store.current_revision()
    conn = store._require_conn()
    await conn.execute("""
        CREATE TEMP TRIGGER fail_messages BEFORE UPDATE ON store
        WHEN NEW.section = 'messages'
        BEGIN SELECT RAISE(ABORT, 'injected message write failure'); END
    """)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected message write failure"):
            await repos[0].append("session", pair("failed"), unread_delta=1)
        assert await store.load_store() == before
        assert await stores[-1].load_store() == before
        assert await store.current_revision() == revision
    finally:
        await conn.execute("DROP TRIGGER fail_messages")
    await repos[0].append("session", pair("recovered"), unread_delta=1)
    await assert_session(repos[0], "session", ["seed", "recovered"])
    assert await store.current_revision() == revision + 1


async def test_atomic_mutator_exception_keeps_prior_sections_and_releases_transaction(repositories):
    stores, repos = repositories
    store = stores[0]
    await repos[0].create(session_id="session")
    before = await store.load_store()
    revision = await store.current_revision()

    def fail(sections):
        sections["sessions"]["session"]["title"] = "must roll back"
        raise ValueError("injected mutator failure")

    with pytest.raises(ValueError, match="injected mutator failure"):
        await store.update_sections(["sessions"], fail)
    assert await store.load_store() == before
    assert await store.current_revision() == revision
    await repos[1].append("session", pair("recovered"), unread_delta=1)
    await assert_session(repos[0], "session", ["recovered"])


async def test_cancelled_write_rolls_back_both_sections_and_allows_next_writer(repositories, monkeypatch):
    stores, repos = repositories
    store = stores[0]
    await repos[0].create(session_id="session")
    await repos[0].append("session", pair("seed"), unread_delta=1)
    before = await store.load_store()
    revision = await store.current_revision()
    entered = asyncio.Event()
    original = store._bump_revision

    async def pause(conn):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_bump_revision", pause)
    task = asyncio.create_task(repos[0].append("session", pair("cancelled"), unread_delta=1))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(store, "_bump_revision", original)
    assert await store.load_store() == before
    assert await stores[-1].load_store() == before
    assert await store.current_revision() == revision
    await repos[1].append("session", pair("recovered"), unread_delta=1)
    await assert_session(repos[0], "session", ["seed", "recovered"])


async def test_generated_user_collision_is_checked_inside_creation_transaction(repositories, monkeypatch):
    import emotion_app.domain.session_repo as session_module

    _, repos = repositories
    candidates = iter(["forced-collision", "forced-collision", "unique-retry"])
    monkeypatch.setattr(session_module, "_new_umo_user_id", lambda: next(candidates))
    created = await asyncio.gather(
        repos[0].create(session_id="a"), repos[1].create(session_id="b"),
    )
    assert {s.user_id for s in created} == {"forced-collision", "unique-retry"}
    assert {s.id for s in await repos[0].list()} == {"a", "b"}


async def test_duplicate_create_cannot_reset_existing_history_or_metadata(repositories):
    from emotion_app.storage.base import StorageError

    stores, repos = repositories
    await repos[0].create(session_id="session", title="keep title")
    await repos[0].append("session", pair("seed"), unread_delta=1)
    before = await stores[0].load_store()
    revision = await stores[0].current_revision()
    with pytest.raises(StorageError, match="会话已存在"):
        await repos[1].create(session_id="session", title="must not replace")
    assert await stores[0].load_store() == before
    assert await stores[0].current_revision() == revision


@pytest.mark.parametrize("writer_kind", ["save_sections", "save_store", "import_merge", "import_replace", "next_revision"])
async def test_public_writers_cannot_commit_a_pending_atomic_write(repositories, monkeypatch, writer_kind):
    stores, repos = repositories
    store, writer = stores[0], stores[-1]
    await repos[0].create(session_id="session")
    before = await store.load_store()
    revision = await store.current_revision()
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = store._bump_revision
    first = True

    async def fail_first(conn):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
            raise ValueError("isolated transaction failure")
        await original(conn)

    async def other_write():
        started.set()
        if writer_kind == "save_sections":
            await writer.save_sections({"external": "committed"})
        elif writer_kind == "save_store":
            await writer.save_store({**before, "external": "committed"})
        elif writer_kind.startswith("import_"):
            await writer.import_store(
                {"data": {**before, "external": "committed"}},
                mode=writer_kind.removeprefix("import_"),
            )
        else:
            return await writer.next_revision()

    monkeypatch.setattr(store, "_bump_revision", fail_first)
    pending = asyncio.create_task(repos[0].append("session", pair("failed"), unread_delta=1))
    other = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        other = asyncio.create_task(other_write())
        await started.wait()
        assert not other.done()
    finally:
        release.set()
        with pytest.raises(ValueError, match="isolated transaction failure"):
            await pending
        if other is not None:
            await other
    assert await store.load_store() == (before if writer_kind == "next_revision" else {**before, "external": "committed"})
    assert await store.current_revision() == revision + 1


@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_failed_import_rolls_back_every_section_and_revision(repositories, mode):
    stores, repos = repositories
    store = stores[0]
    await repos[0].create(session_id="session")
    await repos[0].append("session", pair("seed"), unread_delta=1)
    before = await store.load_store()
    revision = await store.current_revision()
    conn = store._require_conn()
    await conn.execute("""
        CREATE TEMP TRIGGER fail_import BEFORE INSERT ON store
        WHEN NEW.section = 'messages'
        BEGIN SELECT RAISE(ABORT, 'injected import failure'); END
    """)
    try:
        incoming = {"sessions": {}, "messages": {}}
        with pytest.raises(sqlite3.IntegrityError, match="injected import failure"):
            await store.import_store({"data": incoming}, mode=mode)
        assert await store.load_store() == before
        assert await stores[-1].load_store() == before
        assert await store.current_revision() == revision
    finally:
        await conn.execute("DROP TRIGGER fail_import")
    await repos[1].append("session", pair("recovered"), unread_delta=1)
    await assert_session(repos[0], "session", ["seed", "recovered"])


async def test_concurrent_next_revision_is_unique_across_repositories_and_connections(repositories):
    stores, _ = repositories
    revisions = await asyncio.gather(*(stores[i % len(stores)].next_revision() for i in range(16)))
    assert sorted(revisions) == list(range(1, 17))
    assert await stores[0].current_revision() == 16


async def test_atomic_mutator_returns_detached_result_and_rejects_awaitable(repositories):
    from emotion_app.storage.base import StorageError

    stores, _ = repositories
    store = stores[0]
    retained = {}

    def update(sections):
        sections["isolated"] = {"items": ["persisted"]}
        retained["snapshot"] = sections
        return sections["isolated"]

    result = await store.update_sections(["isolated"], update)
    result["items"].append("returned mutation")
    retained["snapshot"]["isolated"]["items"].append("retained mutation")
    assert await store.load_sections(["isolated"]) == {"isolated": {"items": ["persisted"]}}

    async def invalid(sections):
        sections["isolated"] = "must not run"

    revision = await store.current_revision()
    with pytest.raises(StorageError, match="同步执行"):
        await store.update_sections(["isolated"], invalid)
    assert await store.current_revision() == revision
    assert await store.load_sections(["isolated"]) == {"isolated": {"items": ["persisted"]}}


@pytest.mark.parametrize("reader_kind", ["load_sections", "export_store"])
async def test_readers_never_see_uncommitted_message_and_metadata_changes(repositories, monkeypatch, reader_kind):
    stores, repos = repositories
    store, observer = stores[0], stores[-1]
    await repos[0].create(session_id="session")
    await repos[0].append("session", pair("seed"), unread_delta=1)
    before = await store.load_store()
    revision = await store.current_revision()
    entered, started = asyncio.Event(), asyncio.Event()
    original = store._bump_revision

    async def pause(conn):
        entered.set()
        await asyncio.Event().wait()

    async def read_snapshot():
        started.set()
        if reader_kind == "load_sections":
            return await observer.load_sections(["sessions", "messages"])
        exported = await observer.export_store()
        assert exported["revision"] == revision
        return exported["data"]

    monkeypatch.setattr(store, "_bump_revision", pause)
    pending = asyncio.create_task(repos[0].append("session", pair("cancelled"), unread_delta=1))
    reader = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        reader = asyncio.create_task(read_snapshot())
        await started.wait()
        if observer is store:
            assert not reader.done()
        else:
            assert await asyncio.wait_for(reader, timeout=3) == before
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        monkeypatch.setattr(store, "_bump_revision", original)
        if reader is not None:
            snapshot = await asyncio.wait_for(reader, timeout=3)
    assert snapshot == before
    await assert_session(repos[0], "session", ["seed"])


async def test_summary_snapshot_is_one_select_and_cas_preserves_history_version(repositories, monkeypatch):
    from emotion_app.domain.context import ContextPolicy, MODE_ROLLING_SUMMARY

    stores, repos = repositories
    await repos[0].create(session_id="summary", policy=ContextPolicy(mode=MODE_ROLLING_SUMMARY))
    await repos[0].append("summary", pair("seed"))
    calls = []
    original = stores[0].load_sections

    async def capture(keys):
        calls.append(keys)
        return await original(keys)

    monkeypatch.setattr(stores[0], "load_sections", capture)
    snapshot, messages = await repos[0].summary_snapshot("summary")
    assert calls == [["sessions", "messages"]]
    assert snapshot.message_count == len(messages) == 2
    assert snapshot.history_revision > 0
    assert await repos[1].commit_summary(
        "summary", expected_history_revision=snapshot.history_revision,
        summary="committed summary", summarized_upto=1,
    )
    current = await repos[0].get("summary")
    assert (current.summary, current.summarized_upto) == ("committed summary", 1)
    assert current.history_revision == snapshot.history_revision
    before = await stores[0].load_store()
    for cursor in (0, 99):
        assert not await repos[1].commit_summary(
            "summary", expected_history_revision=snapshot.history_revision,
            summary="must not overwrite", summarized_upto=cursor,
        )
    assert await stores[0].load_store() == before


@pytest.mark.parametrize("mutation", ["append", "clear", "manual_summary", "manual_cursor", "policy_toggle", "delete"])
async def test_stale_summary_cas_cannot_overwrite_or_revive_session(repositories, mutation):
    from emotion_app.domain.context import ContextPolicy, MODE_ROLLING_SUMMARY

    stores, repos = repositories
    await repos[0].create(session_id="summary", policy=ContextPolicy(mode=MODE_ROLLING_SUMMARY))
    await repos[0].append("summary", pair("seed"))
    snapshot, _ = await repos[0].summary_snapshot("summary")
    if mutation == "append":
        await repos[1].append("summary", pair("new"))
    elif mutation == "clear":
        await repos[1].clear_messages("summary")
        await repos[1].clear_messages("summary")
    elif mutation == "manual_summary":
        await repos[1].update("summary", {"summary": ""})
    elif mutation == "manual_cursor":
        await repos[1].update("summary", {"summarized_upto": 0})
    elif mutation == "policy_toggle":
        await repos[1].update("summary", {"policy": {"mode": "recent_only"}})
        await repos[1].update("summary", {"policy": {"mode": MODE_ROLLING_SUMMARY}})
    else:
        await repos[1].delete("summary")
    before = await stores[0].load_store()
    assert not await repos[0].commit_summary(
        "summary", expected_history_revision=snapshot.history_revision,
        summary="stale summary must not return", summarized_upto=1,
    )
    assert await stores[0].load_store() == before
    if mutation == "delete":
        assert not await repos[0].exists("summary")
    else:
        assert (await repos[0].get("summary")).history_revision > snapshot.history_revision


async def test_summary_cas_rejects_disabled_policy_and_server_links_cannot_be_forged(repositories):
    from emotion_app.domain.session_repo import Session

    stores, repos = repositories
    session = await repos[0].create(session_id="ordinary")
    assert session.native_origin == session.native_conversation_id == ""
    assert session.history_revision == 0
    assert "native_origin" not in session.to_dict()
    assert "history_revision" not in Session.from_dict({"id": "legacy"}).to_dict()
    await repos[0].append("ordinary", pair("seed"))
    snapshot, _ = await repos[0].summary_snapshot("ordinary")
    assert not await repos[1].commit_summary(
        "ordinary", expected_history_revision=snapshot.history_revision,
        summary="policy is disabled", summarized_upto=1,
    )
    for key, value in {"native_origin": "fake", "native_conversation_id": "fake", "history_revision": 999,
                       "native_history_revision": 999, "native_reset_revision": 999}.items():
        before = await stores[0].load_store()
        with pytest.raises(ValueError, match="不能手动修改"):
            await repos[1].update("ordinary", {key: value})
        assert await stores[0].load_store() == before
    await repos[1].update("ordinary", {"persona_id": "normal persona", "summary": "manual allowed"})
    await repos[1].clear_messages("ordinary")
    await repos[1].delete("ordinary")
    assert not await repos[0].exists("ordinary")
