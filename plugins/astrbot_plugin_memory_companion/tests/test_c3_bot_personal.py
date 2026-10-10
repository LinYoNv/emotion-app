from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

from core.bot_personal_consumer import BotPersonalConsumer
from core.bot_personal_contract import BOT_PERSONAL_MEMORY_DOMAIN, BOT_PERSONAL_MEMORY_TYPES, TYPE_CONTRACTS, WINDOW_SLUGS
from core.bot_personal_dto import (
    BOT_PERSONAL_MAX_RECORD_VERSION,
    BotPersonalValidationError,
    build_bot_personal_archive,
)
from core.bridge import MemoryCompanionBridge
from core.models import MemoryRecord
from core.service import MemoryCompanionService
from core.store import MemoryStore


def envelope(index: str = "1", **overrides):
    value = {
        "memory_type": "bot_observed_activity",
        "memory_domain": BOT_PERSONAL_MEMORY_DOMAIN,
        "subject": "bot_self",
        "date": "2026-07-30",
        "window": "afternoon",
        "occurred_at": "2026-07-30T15:00:00+08:00",
        "source_kind": "wrong_input_is_corrected",
        "source_refs": [f"companion:event:{index}"],
        "certainty": 0.9,
        "evidence_level": "L0",
        "status": "wrong_input_is_corrected",
        "version": 1,
        "idempotency_key": f"c3:test:{index}",
        "payload_schema_version": "1.0",
        "payload": {"summary": "safe archive summary", "activity": "rest"},
    }
    value.update(overrides)
    return value


def make_service(tmp_path: Path):
    service = object.__new__(MemoryCompanionService)
    service.store = MemoryStore(tmp_path / "memory.db")
    service.store.initialize()
    service._schedule_memory_embedding = lambda *args, **kwargs: None
    return service


def run(coro):
    return asyncio.run(coro)


def test_contract_windows_and_types_are_imported_from_single_contract():
    dto = build_bot_personal_archive(envelope())
    assert tuple(WINDOW_SLUGS) == ("late_night", "morning", "noon", "afternoon", "evening")
    assert set(BOT_PERSONAL_MEMORY_TYPES) == set(TYPE_CONTRACTS)
    assert dto.window in WINDOW_SLUGS
    assert dto.source_kind == TYPE_CONTRACTS[dto.memory_type][0]
    assert dto.evidence_level == TYPE_CONTRACTS[dto.memory_type][1]


def test_archive_version_has_a_bounded_semantic_range():
    at_limit = build_bot_personal_archive(envelope(version=BOT_PERSONAL_MAX_RECORD_VERSION))
    assert at_limit.version == BOT_PERSONAL_MAX_RECORD_VERSION

    try:
        build_bot_personal_archive(envelope(version=BOT_PERSONAL_MAX_RECORD_VERSION + 1))
    except BotPersonalValidationError as exc:
        assert exc.error_code == "invalid"
        assert exc.field == "version"
    else:
        raise AssertionError("out-of-range archive version was accepted")


def test_privacy_rejects_raw_prompt_credentials_binary_path_and_unsafe_key():
    for update in (
        {"payload": {"raw_prompt": "do not accept"}},
        {"payload": {"auth_token": "secret-value"}},
        {"payload": {"media_bytes": b"binary"}},
        {"payload": {"photo": "C:\\private\\photo.png"}},
        {"idempotency_key": "photo:C:\\private\\photo.png"},
    ):
        try:
            build_bot_personal_archive({**envelope(), **update})
        except BotPersonalValidationError as exc:
            assert exc.error_code == "privacy_rejected"
        else:
            raise AssertionError("privacy input was accepted")

    for update in (
        {"source_refs": [{"token": "secret-value"}]},
        {"idempotency_key": {"token": "secret-value"}},
        {"record_id": "unexpected-record-id"},
    ):
        try:
            build_bot_personal_archive({**envelope(), **update})
        except BotPersonalValidationError as exc:
            assert exc.error_code == "invalid"
        else:
            raise AssertionError("structured reference input was accepted")


def test_service_success_idempotency_version_conflict_and_stale(tmp_path):
    service = make_service(tmp_path)
    try:
        first = run(service.record_bot_personal_archive(envelope("same")))
        duplicate = run(service.record_bot_personal_archive(envelope("same")))
        conflict = run(service.record_bot_personal_archive(envelope("same", payload={"summary": "changed"})))
        stale = run(service.record_bot_personal_archive(envelope("same", version=0)))
        newer = run(service.record_bot_personal_archive(envelope("same", version=2, payload={"summary": "new"})))
        assert first["ok"] and first["state"] == "sent"
        assert duplicate["ok"] and duplicate["deduplicated"] and duplicate["state"] == "deduplicated"
        assert conflict["error_code"] == "version_conflict"
        assert stale["error_code"] == "invalid"
        assert newer["ok"] and newer["version"] == 2
        stored = run(service.store.get_memory(first["record_id"]))
        assert stored.metadata["memory_domain"] == BOT_PERSONAL_MEMORY_DOMAIN
        assert stored.metadata["subject"] == "bot_self"
        assert stored.metadata["source_kind"] == TYPE_CONTRACTS["bot_observed_activity"][0]
        assert stored.metadata["evidence_level"] == TYPE_CONTRACTS["bot_observed_activity"][1]
    finally:
        service.store.close()


def test_bridge_missing_or_exception_degrades_and_consumer_dead_letters():
    missing = run(MemoryCompanionBridge(object()).record_bot_personal_archive(envelope()))
    assert missing["state"] == "forbidden"
    assert missing["error_code"] == "producer_capability_required"

    class Broken:
        async def record_bot_personal_archive(self, _envelope):
            raise RuntimeError("unavailable")

    consumer = BotPersonalConsumer(MemoryCompanionBridge(Broken()), max_attempts=2)
    first = run(consumer.consume_bot_personal_archive(envelope(), attempt=1))
    last = run(consumer.consume_bot_personal_archive(envelope(), attempt=2))
    assert first["state"] == "retry"
    assert last["state"] == "dead_letter"

    class BrokenLookup:
        def __getattr__(self, _name):
            raise RuntimeError("unavailable")

    lookup_failure = run(
        BotPersonalConsumer(BrokenLookup(), max_attempts=1).consume_bot_personal_archive(envelope())
    )
    assert lookup_failure["state"] == "dead_letter"
    assert lookup_failure["error_code"] == "bridge_method_unavailable"


def test_domain_isolation_and_read_only_profile_does_not_leak_payload(tmp_path):
    service = make_service(tmp_path)
    try:
        bot = run(service.record_bot_personal_archive(envelope("bot")))
        user = MemoryRecord(id="user-1", memory_type="user_fact", scope="private", session_id="private:user", visibility="private_pair", content="user record", metadata={"memory_domain": "user_memory"})
        group = MemoryRecord(id="group-1", memory_type="group_memory", scope="group", session_id="group:g", visibility="group", content="group record", metadata={"memory_domain": "group_memory"})
        run(service.store.insert_memory(user))
        run(service.store.insert_memory(group))
        profile = run(service.read_bot_personal_profile(limit=10))
        assert profile["read_only"] is True
        assert [item["record_id"] for item in profile["items"]] == [bot["record_id"]]
        assert all("payload" not in item and "content" not in item for item in profile["items"])
        assert "safe archive summary" not in str(profile)
    finally:
        service.store.close()


def test_distinct_bot_personal_idempotency_keys_remain_distinct_rows(tmp_path):
    service = make_service(tmp_path)
    try:
        first = run(service.record_bot_personal_archive(envelope("one")))
        second = run(service.record_bot_personal_archive(envelope("two")))
        assert first["record_id"] != second["record_id"]
        profile = run(service.read_bot_personal_profile(limit=10))
        assert {item["record_id"] for item in profile["items"]} == {first["record_id"], second["record_id"]}
    finally:
        service.store.close()


def test_profile_drops_unsafe_legacy_source_references(tmp_path):
    service = make_service(tmp_path)
    try:
        legacy = MemoryRecord(
            id="legacy-bot-ref",
            memory_type="bot_observed_activity",
            scope="private",
            session_id="bot_personal",
            visibility="bot_self",
            content="legacy bot record",
            metadata={
                "memory_domain": BOT_PERSONAL_MEMORY_DOMAIN,
                "source_refs": ["Bearer leaked-token", "companion:event:safe"],
                "payload": {"summary": "legacy summary"},
            },
        )
        run(service.store.insert_memory(legacy))

        profile = run(service.read_bot_personal_profile(limit=10))

        item = next(value for value in profile["items"] if value["record_id"] == legacy.id)
        assert item["source_refs"] == ["companion:event:safe"]
        assert "leaked-token" not in str(profile)
    finally:
        service.store.close()
