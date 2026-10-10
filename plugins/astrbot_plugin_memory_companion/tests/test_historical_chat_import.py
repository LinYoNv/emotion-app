from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


ROOT = bootstrap_package()

from astrbot_plugin_memory_companion.core.chat_import import HistoricalChatImporter, HistoricalChatParser, HistoricalChatSegmenter
from astrbot_plugin_memory_companion.core.injection import InjectionComposer
from astrbot_plugin_memory_companion.core.models import EntityRef, MemoryRecord, SearchResult, SessionContext
from astrbot_plugin_memory_companion.core.store import MemoryStore
from astrbot_plugin_memory_companion.core.visibility import VisibilityPolicy


class HistoricalChatParserTests(unittest.TestCase):
    def test_parser_infers_year_rollover_and_preserves_multiline_body(self) -> None:
        text = """烛雨: 2025-12-31 23:59:58
新年快乐

manegata: 01-01 00:00:03
第一行
1. 到校时间：每日 5:30

manegata: 01-01 00:00:03
第二条同时间消息
"""
        parsed = HistoricalChatParser().parse(text, source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(3, parsed["stats"]["message_count"])
        self.assertEqual(2, parsed["stats"]["inferred_year_count"])
        self.assertEqual(1, parsed["stats"]["duplicate_timestamp_groups"])
        self.assertEqual("2026-01-01T00:00:03+08:00", parsed["messages"][1]["local_time"])
        self.assertIn("1. 到校时间：每日 5:30", parsed["messages"][1]["content"])

    def test_segmenter_merges_short_bursts_without_losing_message_ids(self) -> None:
        text = """u: 2026-01-01 00:00:00
在吗

b: 2026-01-01 00:00:10
在

b: 2026-01-01 00:00:20
怎么了

u: 2026-01-01 03:10:00
早
"""
        parsed = HistoricalChatParser().parse(text, source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest())
        segmenter = HistoricalChatSegmenter()
        mapping = {"u": {"role": "user"}, "b": {"role": "bot"}}
        turns = segmenter.logical_turns(parsed["messages"], mapping)
        segments = segmenter.segments(parsed["messages"], mapping)
        self.assertEqual(3, len(turns))
        self.assertEqual(2, len(segments))
        self.assertEqual(3, len(segments[0]["message_ids"]))
        self.assertIn("怎么了", segments[0]["transcript"])

    def test_parser_stably_reorders_inverted_export_but_keeps_source_sequence(self) -> None:
        text = """u: 2026-01-02 10:00:00
后导出的消息

b: 2026-01-01 09:00:00
更早的消息
"""
        parsed = HistoricalChatParser().parse(
            text, source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        self.assertTrue(parsed["stats"]["chronologically_reordered"])
        self.assertEqual([2, 1], [item["sequence"] for item in parsed["messages"]])
        self.assertEqual("更早的消息", parsed["messages"][0]["content"])

    def test_parser_understands_labeled_export_with_speaker_before_time(self) -> None:
        text = """发送者：比折
时间：2026-07-19 16:55:36
内容：已经吃过饭了
消息ID：1001
----------------
发送者：星缘
时间：2026-07-19 16:55:42
内容：那就好
早点休息
"""
        parsed = HistoricalChatParser().parse(
            text,
            source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

        self.assertEqual("labeled_fields", parsed["stats"]["source_format"])
        self.assertEqual("before_time", parsed["stats"]["field_speaker_layout"])
        self.assertEqual(2, parsed["stats"]["message_count"])
        self.assertEqual({"比折": 1, "星缘": 1}, parsed["stats"]["speakers"])
        self.assertEqual("已经吃过饭了", parsed["messages"][0]["content"])
        self.assertEqual("那就好\n早点休息", parsed["messages"][1]["content"])
        self.assertNotIn("时间", parsed["stats"]["speakers"])
        self.assertNotIn("内容", parsed["stats"]["speakers"])

    def test_parser_understands_labeled_export_with_speaker_after_time(self) -> None:
        text = """时间: 2026-07-19 16:55:36
发送者: 比折
内容: 刚才去洗澡了

时间: 2026-07-19 16:55:42
发送者: 星缘
内容: 洗完舒服些了吗
"""
        parsed = HistoricalChatParser().parse(
            text,
            source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

        self.assertEqual("after_time", parsed["stats"]["field_speaker_layout"])
        self.assertEqual(["比折", "星缘"], [item["speaker"] for item in parsed["messages"]])
        self.assertEqual("刚才去洗澡了", parsed["messages"][0]["content"])

    def test_parser_keeps_content_when_content_field_precedes_time(self) -> None:
        text = """发送者：比折
内容：晚饭已经吃过
时间：2026-07-19 16:55:36

发送者：星缘
内容：那就早点休息
时间：2026-07-19 16:55:42
"""
        parsed = HistoricalChatParser().parse(
            text,
            source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

        self.assertEqual("before_time", parsed["stats"]["field_content_layout"])
        self.assertEqual("晚饭已经吃过", parsed["messages"][0]["content"])
        self.assertEqual("那就早点休息", parsed["messages"][1]["content"])

    def test_labeled_export_without_sender_fails_before_cost_estimation(self) -> None:
        text = """时间：2026-07-19 16:55:36
内容：第一条

时间：2026-07-19 16:55:42
内容：第二条

时间：2026-07-19 16:55:48
内容：第三条
"""
        with self.assertRaisesRegex(ValueError, "没有找到发送者或昵称字段"):
            HistoricalChatParser().parse(
                text,
                source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )

    def test_labeled_upload_reports_normalization_in_preview(self) -> None:
        text = """发送者：比折
时间：2026-07-19 16:55:36
内容：第一条

发送者：星缘
时间：2026-07-19 16:55:42
内容：第二条
"""
        with tempfile.TemporaryDirectory() as temp:
            service = type("Service", (), {"data_dir": Path(temp), "store": object()})()
            importer = HistoricalChatImporter(service)
            importer._identity_context = lambda _speakers: {
                "available": False,
                "matches": {},
                "bot": {},
                "target_users": [],
            }
            preview = importer.stage_upload(filename="friend.txt", content=text.encode("utf-8"))

        self.assertEqual(2, preview["stats"]["speaker_count"])
        self.assertTrue(any("字段式导出" in warning for warning in preview["warnings"]))

    def test_qq_chat_exporter_json_upload_preserves_metadata_and_message_provenance(self) -> None:
        payload = {
            "metadata": {"name": "QQChatExporter", "version": "0.1.0"},
            "chatInfo": {
                "name": "山间之茶",
                "type": "private",
                "selfUid": "u_self",
                "selfUin": "2732152361",
                "selfName": "捕梦猫neko",
                "peerUid": "u_peer",
                "peerUin": "1374758454",
            },
            "messages": [
                {
                    "id": "m-1", "seq": "1", "timestamp": 1783852174000,
                    "time": "2026-07-12T10:29:34.000Z",
                    "sender": {"uid": "u_peer", "uin": "1374758454", "name": "山间之茶"},
                    "type": "text", "content": {"text": "在吗"}, "recalled": False, "system": False,
                },
                {
                    "id": "m-2", "seq": "2", "timestamp": 1783852227000,
                    "sender": {"uid": "u_self", "uin": "2732152361", "name": "捕梦猫neko"},
                    "type": "audio", "content": {"text": "", "elements": []}, "recalled": False, "system": False,
                },
                {
                    "id": "m-3", "seq": "3", "timestamp": 1783852367000,
                    "sender": {"uid": "u_peer", "uin": "1374758454", "name": "山间之茶"},
                    "type": "image", "content": {"text": "", "elements": [{"type": "image", "data": {}}]},
                    "recalled": False, "system": False,
                },
                {
                    "id": "m-recalled", "seq": "4", "timestamp": 1783852373000,
                    "sender": {"uin": "1374758454", "name": "山间之茶"},
                    "type": "text", "content": {"text": "撤回"}, "recalled": True, "system": False,
                },
                {
                    "id": "m-system", "seq": "5", "timestamp": 1783852374000,
                    "sender": {"uin": "1374758454", "name": "山间之茶"},
                    "type": "text", "content": {"text": "系统"}, "recalled": False, "system": True,
                },
                {
                    "id": "m-1", "seq": "6", "timestamp": 1783852374500,
                    "sender": {"uid": "u_peer", "uin": "1374758454", "name": "山间之茶"},
                    "type": "text", "content": {"text": "重复导出"}, "recalled": False, "system": False,
                },
            ],
        }
        source = json.dumps(payload, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temp:
            service = type("Service", (), {"data_dir": Path(temp), "store": object()})()
            importer = HistoricalChatImporter(service)
            importer._identity_context = lambda _speakers: {
                "available": False, "matches": {}, "bot": {}, "target_users": [],
            }
            preview = importer.stage_upload(filename="qq-export.json", content=source.encode("utf-8"))
            upload_dir = Path(temp) / "historical_chat_imports" / "uploads" / preview["upload_id"]
            parsed = [json.loads(line) for line in (upload_dir / "parsed.jsonl").read_text(encoding="utf-8").splitlines()]
            archived_source = (upload_dir / "source.txt").read_text(encoding="utf-8")
            expanded_payload = json.loads(source)
            expanded_payload["statistics"] = {"totalMessages": 6}
            expanded_payload["messages"].append(
                {
                    "id": "m-6", "seq": "6", "timestamp": 1783852375000,
                    "sender": {"uid": "u_self", "uin": "2732152361", "name": "旧昵称"},
                    "type": "text", "content": {"text": "新增消息"}, "recalled": False, "system": False,
                }
            )
            expanded_preview = importer.stage_upload(
                filename="qq-export-new.json",
                content=json.dumps(expanded_payload, ensure_ascii=False).encode("utf-8"),
            )
            expanded_dir = Path(temp) / "historical_chat_imports" / "uploads" / expanded_preview["upload_id"]
            expanded_parsed = [
                json.loads(line)
                for line in (expanded_dir / "parsed.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual("qq_chat_exporter", preview["source_kind"])
        self.assertEqual("qq_chat_exporter_json", preview["stats"]["source_format"])
        self.assertEqual(3, preview["stats"]["message_count"])
        self.assertEqual(1, preview["stats"]["skipped_recalled_count"])
        self.assertEqual(1, preview["stats"]["skipped_system_count"])
        self.assertEqual(1, preview["stats"]["skipped_duplicate_count"])
        self.assertEqual("0.1.0", preview["source_metadata"]["exporter"]["version"])
        self.assertEqual("2732152361", preview["identity_context"]["bot"]["self_ids"][0])
        self.assertEqual("1374758454", preview["identity_context"]["target_users"][0]["user_id"])
        self.assertEqual("2026-07-12T18:29:34+08:00", parsed[0]["local_time"])
        self.assertEqual("m-1", parsed[0]["source_message_id"])
        self.assertEqual(1, parsed[0]["source_message_seq"])
        self.assertEqual("1374758454", parsed[0]["source_sender_id"])
        self.assertEqual("[语音]", parsed[1]["content"])
        self.assertEqual("[图片]", parsed[2]["content"])
        self.assertEqual(parsed[0]["message_id"], expanded_parsed[0]["message_id"])
        self.assertEqual(parsed[1]["message_id"], expanded_parsed[1]["message_id"])
        self.assertEqual(parsed[2]["message_id"], expanded_parsed[2]["message_id"])
        self.assertEqual("捕梦猫neko [2732152361]", expanded_parsed[3]["speaker"])
        self.assertEqual(2, expanded_preview["stats"]["speaker_count"])
        self.assertEqual(source, archived_source)
        suggestions = {item["speaker"]: item for item in preview["speaker_suggestions"]}
        self.assertEqual("user", suggestions["山间之茶 [1374758454]"]["suggested_role"])
        self.assertEqual("bot", suggestions["捕梦猫neko [2732152361]"]["suggested_role"])
        self.assertTrue(all(item["confidence"] == "high" for item in suggestions.values()))

    def test_non_qq_chat_exporter_json_fails_with_actionable_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = type("Service", (), {"data_dir": Path(temp), "store": object()})()
            importer = HistoricalChatImporter(service)
            with self.assertRaisesRegex(ValueError, "不是 QQChatExporter"):
                importer.stage_upload(filename="other.json", content=b'{"messages": []}')


class HistoricalChatStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.db")
        self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    async def test_historical_timeline_is_idempotent_and_retention_safe(self) -> None:
        historical = {
            "event_type": "user_message", "session_id": "qq:FriendMessage:u1", "scope": "private",
            "subject_id": "u1", "object_id": "b1", "content": "历史消息", "message_id": "hist-1",
            "occurred_at": "2025-01-01T00:00:00+00:00", "retention_class": "historical_archive",
            "import_batch_id": "batch-1", "source_sequence": 1,
            "metadata": {"message_id": "hist-1", "preserve_raw": True},
        }
        first = await self.store.add_historical_timeline_events([historical])
        second = await self.store.add_historical_timeline_events([historical])
        third, newly_inserted = await self.store.add_historical_timeline_events_with_status([historical])
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(set(), newly_inserted)
        timeline_id = first["hist-1"]
        await self.store.mark_timeline_summarized([timeline_id])
        normal_id = await self.store.add_timeline_event(
            event_type="user_message", session_id="qq:FriendMessage:u2", scope="private",
            subject_id="u2", object_id="b1", content="普通旧消息",
            metadata={"message_id": "normal-1"}, occurred_at="2025-01-01T00:00:00+00:00",
        )
        await self.store.mark_timeline_summarized([normal_id])
        deleted = await self.store.prune_retained_rows(
            summarized_timeline_cutoff="2099-01-01T00:00:00+00:00", limit=100,
        )
        self.assertEqual(1, deleted["timeline"])
        self.assertIsNotNone(self.store._conn.execute("SELECT id FROM timeline WHERE id=?", (timeline_id,)).fetchone())
        self.assertIsNone(self.store._conn.execute("SELECT id FROM timeline WHERE id=?", (normal_id,)).fetchone())

    async def test_batch_rollback_removes_only_batch_records(self) -> None:
        await self.store.upsert_chat_import_batch({
            "id": "batch-rollback", "upload_id": "upload-1", "source_name": "chat.txt",
            "state": "running", "session_id": "qq:FriendMessage:u1", "scope": "private",
            "user_id": "u1", "bot_id": "b1", "speaker_map": {}, "stats": {}, "total_segments": 1,
        })
        rows = await self.store.add_historical_timeline_events([{
            "event_type": "user_message", "session_id": "qq:FriendMessage:u1", "scope": "private",
            "subject_id": "u1", "object_id": "b1", "content": "待回滚消息", "message_id": "rollback-message",
            "occurred_at": "2026-01-01T00:00:00+00:00", "retention_class": "historical_archive",
            "import_batch_id": "batch-rollback", "source_sequence": 1, "metadata": {"message_id": "rollback-message"},
        }])
        await self.store.replace_chat_import_segments("batch-rollback", [{
            "id": "batch-rollback_seg_0000", "segment_index": 0,
            "start_at": "2026-01-01T00:00:00+00:00", "end_at": "2026-01-01T00:00:00+00:00",
            "local_date": "2026-01-01", "message_ids": list(rows.values()), "transcript": "{}",
            "char_count": 4, "turn_count": 1,
        }])
        await self.store.insert_memory(MemoryRecord(
            id="batch-memory", memory_type="important_event", subject=EntityRef(kind="user", id="u1"),
            object=EntityRef.bot_self("b1"), scope="private", session_id="qq:FriendMessage:u1",
            content="待回滚记忆", import_batch_id="batch-rollback",
        ))
        await self.store.insert_memory(MemoryRecord(
            id="other-memory", memory_type="manual_memory", subject=EntityRef(kind="user", id="u2"),
            object=EntityRef.bot_self("b1"), scope="private", session_id="qq:FriendMessage:u2", content="不应回滚",
        ))
        deleted = await self.store.rollback_chat_import_batch("batch-rollback")
        self.assertEqual({"memories": 1, "timeline": 1, "segments": 1}, deleted)
        self.assertIsNone(await self.store.get_memory("batch-memory"))
        self.assertIsNotNone(await self.store.get_memory("other-memory"))
        self.assertEqual("rolled_back", (await self.store.get_chat_import_batch("batch-rollback"))["state"])

    async def test_interrupted_processing_segment_is_recoverable(self) -> None:
        await self.store.upsert_chat_import_batch({
            "id": "batch-resume", "upload_id": "chatup_" + "a" * 24,
            "source_name": "chat.txt", "state": "paused", "session_id": "qq:FriendMessage:u1",
            "scope": "private", "user_id": "u1", "bot_id": "b1", "speaker_map": {},
            "stats": {}, "total_segments": 1,
        })
        await self.store.replace_chat_import_segments("batch-resume", [{
            "id": "batch-resume_seg_0000", "segment_index": 0,
            "start_at": "2026-01-01T00:00:00+00:00", "end_at": "2026-01-01T00:00:00+00:00",
            "local_date": "2026-01-01", "message_ids": ["tl-1"], "transcript": "{}",
            "char_count": 2, "turn_count": 1, "status": "processing", "attempts": 2,
        }])
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._start_worker = lambda _batch_id: None
        result = await importer.resume_batch("batch-resume")
        segment = (await self.store.chat_import_segments("batch-resume"))[0]
        self.assertEqual("running", result["batch"]["state"])
        self.assertEqual("retry", segment["status"])
        self.assertEqual(0, segment["attempts"])

    async def test_batch_memory_counts_and_listing_are_exact(self) -> None:
        for memory_id, memory_type in (("summary-1", "conversation_summary"), ("event-1", "important_event")):
            await self.store.insert_memory(MemoryRecord(
                id=memory_id, memory_type=memory_type, subject=EntityRef(kind="user", id="u1"),
                object=EntityRef.bot_self("b1"), scope="private", session_id="qq:FriendMessage:u1",
                content=memory_id, import_batch_id="batch-count",
            ))
        counts = await self.store.chat_import_memory_counts("batch-count")
        records = await self.store.list_chat_import_memories("batch-count")
        self.assertEqual(2, counts["total"])
        self.assertEqual(1, counts["conversation_summary"])
        self.assertEqual({"summary-1", "event-1"}, {record.id for record in records})

    async def test_completed_batch_rebind_is_scoped_and_rebuild_safe(self) -> None:
        target_session = "qqbot:FriendMessage:openid-user"
        await self.store.insert_memory(MemoryRecord(
            id="target-native", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="openid-user", name="山间之茶"),
            object=EntityRef.bot_self("official-bot", "捕梦猫"),
            scope="private", session_id=target_session, platform="qqbot",
            visibility="private_pair", content="已有官方 Bot 私聊记忆",
            metadata={"owner_bot_id": "official-bot"},
        ))
        await self.store.upsert_chat_import_batch({
            "id": "batch-rebind", "upload_id": "upload-rebind", "source_name": "qq.json",
            "state": "completed", "session_id": "qq:FriendMessage:1374758454", "scope": "private",
            "platform": "qq", "user_id": "1374758454", "user_name": "山间之茶",
            "bot_id": "2732152361", "bot_name": "捕梦猫neko",
            "speaker_map": {
                "山间之茶 [1374758454]": {
                    "role": "user", "entity_id": "1374758454", "display_name": "山间之茶",
                },
                "捕梦猫neko [2732152361]": {
                    "role": "bot", "entity_id": "2732152361", "display_name": "捕梦猫neko",
                },
            },
            "stats": {"message_count": 2},
        })
        await self.store.insert_memory(MemoryRecord(
            id="imported-memory", memory_type="conversation_summary",
            subject=EntityRef(kind="user", id="1374758454", name="山间之茶"),
            object=EntityRef.bot_self("2732152361", "捕梦猫neko"),
            scope="private", session_id="qq:FriendMessage:1374758454", platform="qq",
            visibility="private_pair", content="两人聊过一次天气。",
            metadata={"owner_bot_id": "2732152361", "topics": ["天气"]},
            import_batch_id="batch-rebind",
        ))
        await self.store.insert_memory(MemoryRecord(
            id="unrelated-memory", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="other-user", name="其他用户"),
            object=EntityRef.bot_self("other-bot", "其他 Bot"),
            scope="private", session_id="qq:FriendMessage:other-user", platform="qq",
            content="不得移动", metadata={"owner_bot_id": "other-bot"},
        ))
        timeline_ids = await self.store.add_historical_timeline_events([{
            "event_type": "user_message", "session_id": "qq:FriendMessage:1374758454",
            "scope": "private", "subject_id": "1374758454", "object_id": "2732152361",
            "content": "今天天气不错", "message_id": "rebind-message", "source_sequence": 1,
            "occurred_at": "2026-07-12T10:37:49+08:00", "retention_class": "historical_archive",
            "import_batch_id": "batch-rebind", "metadata": {"source_sender_id": "1374758454"},
        }])
        unrelated_timeline_id = await self.store.add_timeline_event(
            event_type="user_message", session_id="qq:FriendMessage:other-user", scope="private",
            subject_id="other-user", object_id="other-bot", content="不得移动",
            metadata={"message_id": "other-message"},
        )
        relationship_id = await self.store.upsert_relationship(
            subject=EntityRef(kind="user", id="1374758454", name="山间之茶"),
            object=EntityRef.bot_self("2732152361", "捕梦猫neko"), relation_type="trust",
            scope="private", session_id="qq:FriendMessage:1374758454",
            source_memory_id="imported-memory", metadata={"owner_bot_id": "2732152361"},
        )
        source_node = await self.store.upsert_knowledge_node(
            node_type="person", label="山间之茶", scope="private",
            session_id="qq:FriendMessage:1374758454",
        )
        target_node = await self.store.upsert_knowledge_node(
            node_type="topic", label="天气", scope="private",
            session_id="qq:FriendMessage:1374758454",
        )
        knowledge_edge_id = await self.store.upsert_knowledge_edge(
            source_node_id=source_node, target_node_id=target_node, relation_type="mentioned",
            scope="private", session_id="qq:FriendMessage:1374758454",
            source_memory_id="imported-memory",
        )
        await self.store.upsert_memory_embedding(
            memory_id="imported-memory", provider_id="test", text_hash="old", vector=[1.0, 0.0]
        )
        await self.store.upsert_memory_embedding(
            memory_id="unrelated-memory", provider_id="test", text_hash="keep", vector=[0.0, 1.0]
        )

        result = await self.store.rebind_chat_import_batch(
            batch_id="batch-rebind", session_id=target_session, platform="qqbot",
            user_id="openid-user", user_name="山间之茶", bot_id="official-bot",
            bot_name="", backup_path="memory.backup.before-rebind.db",
        )

        memory = await self.store.get_memory("imported-memory")
        unrelated = await self.store.get_memory("unrelated-memory")
        timeline = self.store._conn.execute(
            "SELECT * FROM timeline WHERE id=?", (timeline_ids["rebind-message"],)
        ).fetchone()
        unrelated_timeline = self.store._conn.execute(
            "SELECT * FROM timeline WHERE id=?", (unrelated_timeline_id,)
        ).fetchone()
        relationship = self.store._conn.execute(
            "SELECT * FROM relationship_edges WHERE id=?", (relationship_id,)
        ).fetchone()
        batch = await self.store.get_chat_import_batch("batch-rebind")

        self.assertEqual(1, result["memories"])
        self.assertEqual(1, result["timeline"])
        self.assertEqual(0, result["embeddings_removed"])
        self.assertEqual(1, result["knowledge_edges_removed"])
        self.assertEqual(("openid-user", "official-bot"), (memory.subject.id, memory.object.id))
        self.assertEqual((target_session, "qqbot"), (memory.session_id, memory.platform))
        self.assertEqual("捕梦猫", memory.object.name)
        self.assertEqual("official-bot", memory.metadata["owner_bot_id"])
        self.assertEqual("1374758454", memory.metadata["original_import_target"]["user_id"])
        self.assertEqual("openid-user", memory.metadata["current_import_target"]["user_id"])
        self.assertEqual("qq:FriendMessage:other-user", unrelated.session_id)
        self.assertEqual((target_session, "openid-user", "official-bot"), (
            timeline["session_id"], timeline["subject_id"], timeline["object_id"],
        ))
        timeline_metadata = json.loads(timeline["metadata"])
        self.assertEqual("1374758454", timeline_metadata["source_sender_id"])
        self.assertEqual("official-bot", timeline_metadata["owner_bot_id"])
        self.assertEqual("qq:FriendMessage:other-user", unrelated_timeline["session_id"])
        self.assertEqual(("openid-user", "official-bot", target_session), (
            relationship["subject_id"], relationship["object_id"], relationship["session_id"],
        ))
        self.assertIsNotNone(self.store._conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id='imported-memory'"
        ).fetchone())
        self.assertIsNotNone(self.store._conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id='unrelated-memory'"
        ).fetchone())
        self.assertIsNone(self.store._conn.execute(
            "SELECT 1 FROM knowledge_edges WHERE id=?", (knowledge_edge_id,)
        ).fetchone())
        self.assertEqual((target_session, "openid-user", "official-bot"), (
            batch["session_id"], batch["user_id"], batch["bot_id"],
        ))
        self.assertEqual("1374758454", batch["speaker_map"]["山间之茶 [1374758454]"]["source_entity_id"])
        self.assertEqual("openid-user", batch["speaker_map"]["山间之茶 [1374758454]"]["entity_id"])
        self.assertEqual(
            "memory.backup.before-rebind.db",
            batch["stats"]["identity_rebind"]["backup_path"],
        )

        with self.assertRaisesRegex(ValueError, "已经属于所选私聊"):
            await self.store.rebind_chat_import_batch(
                batch_id="batch-rebind", session_id=target_session, platform="qqbot",
                user_id="openid-user", bot_id="official-bot",
            )

    async def test_rebind_rejects_nonterminal_missing_and_incoherent_targets(self) -> None:
        target_session = "qqbot:FriendMessage:openid-user"
        await self.store.insert_memory(MemoryRecord(
            id="validation-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="openid-user", name="用户"),
            object=EntityRef.bot_self("official-bot", "Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="目标上下文",
            metadata={"owner_bot_id": "official-bot"},
        ))
        for state in ("running", "paused", "rolled_back"):
            batch_id = f"batch-{state}"
            await self.store.upsert_chat_import_batch({
                "id": batch_id, "state": state, "scope": "private",
                "session_id": "qq:FriendMessage:old-user", "platform": "qq",
                "user_id": "old-user", "bot_id": "old-bot", "speaker_map": {}, "stats": {},
            })
            with self.assertRaisesRegex(ValueError, "仅已完成"):
                await self.store.rebind_chat_import_batch(
                    batch_id=batch_id, session_id=target_session, platform="qqbot",
                    user_id="openid-user", bot_id="official-bot",
                )

        with self.assertRaisesRegex(ValueError, "导入批次不存在"):
            await self.store.rebind_chat_import_batch(
                batch_id="missing", session_id=target_session, platform="qqbot",
                user_id="openid-user", bot_id="official-bot",
            )
        await self.store.upsert_chat_import_batch({
            "id": "batch-invalid-target", "state": "completed", "scope": "private",
            "session_id": "qq:FriendMessage:old-user", "platform": "qq",
            "user_id": "old-user", "bot_id": "old-bot", "speaker_map": {}, "stats": {},
        })
        invalid_payloads = (
            {"session_id": target_session, "platform": "qqbot", "user_id": "different-user", "bot_id": "official-bot"},
            {"session_id": target_session, "platform": "wrong-platform", "user_id": "openid-user", "bot_id": "official-bot"},
            {"session_id": target_session, "platform": "qqbot", "user_id": "openid-user", "bot_id": "different-bot"},
        )
        for payload in invalid_payloads:
            with self.assertRaises(ValueError):
                await self.store.rebind_chat_import_batch(batch_id="batch-invalid-target", **payload)

    async def test_importer_rebind_derives_platform_backs_up_and_syncs_relationship_candidates(self) -> None:
        target_session = "qqbot:FriendMessage:openid-user"
        await self.store.insert_memory(MemoryRecord(
            id="api-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="openid-user", name="目标用户"),
            object=EntityRef.bot_self("official-bot", "官方 Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="已有窗口",
            metadata={"owner_bot_id": "official-bot"},
        ))
        await self.store.upsert_chat_import_batch({
            "id": "batch-api-rebind", "state": "completed", "scope": "private",
            "session_id": "qq:FriendMessage:old-user", "platform": "qq",
            "user_id": "old-user", "user_name": "旧用户", "bot_id": "old-bot",
            "bot_name": "旧 Bot", "speaker_map": {}, "stats": {},
            "relationship_observation_count": 2,
        })
        await self.store.insert_memory(MemoryRecord(
            id="api-imported", memory_type="conversation_summary",
            subject=EntityRef(kind="user", id="old-user", name="旧用户"),
            object=EntityRef.bot_self("old-bot", "旧 Bot"), scope="private",
            session_id="qq:FriendMessage:old-user", platform="qq", content="导入记忆",
            import_batch_id="batch-api-rebind", metadata={"owner_bot_id": "old-bot"},
        ))

        class RelationshipAPI:
            calls = []

            async def rebind_historical_relationship_observations(self, **kwargs):
                self.calls.append(kwargs)
                return {"matched": 2, "moved": 2, "deduplicated": 0, "trimmed": 0}

        relationship_api = RelationshipAPI()
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._private_api = lambda: relationship_api

        result = await importer.rebind_batch({
            "batch_id": "batch-api-rebind", "session_id": target_session,
            "user_id": "openid-user", "user_name": "目标用户", "bot_id": "official-bot",
        })

        self.assertEqual("qqbot", result["target"]["platform"])
        self.assertEqual("completed", result["relationship_observations"]["status"])
        self.assertEqual(2, result["relationship_observations"]["moved"])
        self.assertEqual("old-user", relationship_api.calls[0]["old_user_id"])
        self.assertEqual("openid-user", relationship_api.calls[0]["user_id"])
        self.assertTrue(Path(result["backup_path"]).is_file())
        batch = await self.store.get_chat_import_batch("batch-api-rebind")
        self.assertEqual(
            "completed",
            batch["stats"]["relationship_observation_rebind"]["status"],
        )

    async def test_legacy_batches_remain_listed_and_status_inspection_does_not_upgrade(self) -> None:
        batch_count = 1001
        for index in range(batch_count):
            await self.store.upsert_chat_import_batch({
                "id": f"legacy-list-{index:04d}",
                "state": "completed",
                "scope": "private",
                "session_id": f"qq:FriendMessage:legacy-{index}",
                "platform": "qq",
                "user_id": f"legacy-{index}",
                "bot_id": "legacy-bot",
                "speaker_map": {},
                "stats": {},
            })
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        started: list[str] = []
        importer._start_worker = started.append

        listing = await importer.status()
        inspected = await importer.status("legacy-list-0000", upgrade_legacy=False)
        limited = await self.store.list_chat_import_batches(7)

        self.assertEqual(batch_count, len(listing["batches"]))
        self.assertIn("legacy-list-0000", {item["id"] for item in listing["batches"]})
        self.assertEqual(7, len(limited))
        self.assertEqual("completed", inspected["batch"]["state"])
        self.assertEqual({}, inspected["batch"]["stats"])
        self.assertEqual([], started)

    async def test_relationship_observation_cleanup_requests_complete_batch_history(self) -> None:
        for batch_id in ("cleanup-batch-a", "cleanup-batch-b"):
            await self.store.upsert_chat_import_batch({
                "id": batch_id,
                "state": "completed",
                "scope": "private",
                "session_id": f"qq:FriendMessage:{batch_id}",
                "platform": "qq",
                "user_id": batch_id,
                "bot_id": "cleanup-bot",
                "speaker_map": {},
                "stats": {},
            })

        requested_limits: list[int | None] = []
        original_listing = self.store.list_chat_import_batches

        async def tracked_listing(limit: int | None = 20):
            requested_limits.append(limit)
            return await original_listing(limit)

        rolled_back: list[str] = []

        class RelationshipAPI:
            def rollback_historical_relationship_observations(self, batch_id: str):
                rolled_back.append(batch_id)
                return {"removed": 1}

        self.store.list_chat_import_batches = tracked_listing
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._private_api = RelationshipAPI

        removed = await importer.clear_relationship_observations()

        self.assertEqual([None], requested_limits)
        self.assertEqual({"cleanup-batch-a", "cleanup-batch-b"}, set(rolled_back))
        self.assertEqual(2, removed)

    async def test_legacy_warning_batch_without_observation_count_still_rebinds(self) -> None:
        target_session = "qqbot:FriendMessage:target-user"
        await self.store.insert_memory(MemoryRecord(
            id="legacy-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="target-user", name="目标用户"),
            object=EntityRef.bot_self("target-bot", "目标 Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="目标窗口",
            metadata={"owner_bot_id": "target-bot"},
        ))
        await self.store.upsert_chat_import_batch({
            "id": "legacy-warning-rebind", "state": "completed_with_warnings", "scope": "private",
            "session_id": "qq:FriendMessage:old-user", "platform": "qq",
            "user_id": "", "user_name": "旧用户", "bot_id": "old-bot", "bot_name": "旧 Bot",
            "speaker_map": {
                "旧用户": {"role": "user", "entity_id": "old-user", "display_name": "旧用户"},
                "旧 Bot": {"role": "bot", "entity_id": "old-bot", "display_name": "旧 Bot"},
            },
            "stats": {"message_count": 1},
        })
        await self.store.insert_memory(MemoryRecord(
            id="legacy-warning-memory", memory_type="conversation_summary",
            subject=EntityRef(kind="unknown", id="", name="旧用户"),
            object=EntityRef(kind="unknown", id="", name="旧 Bot"), scope="private",
            session_id="qq:FriendMessage:old-user", platform="qq", content="旧版导入记忆",
            import_batch_id="legacy-warning-rebind", metadata={"owner_bot_id": "old-bot"},
        ))
        relationship_id = await self.store.upsert_relationship(
            subject=EntityRef(kind="unknown", id="", name="旧用户"),
            object=EntityRef(kind="unknown", id="", name="旧 Bot"),
            relation_type="trust", scope="private", session_id="qq:FriendMessage:old-user",
            source_memory_id="legacy-warning-memory", metadata={"owner_bot_id": "old-bot"},
        )

        class RelationshipAPI:
            calls: list[dict[str, object]] = []

            async def rebind_historical_relationship_observations(self, **kwargs):
                self.calls.append(kwargs)
                return {"matched": 1, "moved": 1, "deduplicated": 0, "trimmed": 0}

        relationship_api = RelationshipAPI()
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._private_api = lambda: relationship_api

        result = await importer.rebind_batch({
            "batch_id": "legacy-warning-rebind",
            "session_id": target_session,
            "user_id": "target-user",
            "user_name": "目标用户",
            "bot_id": "target-bot",
        })

        self.assertEqual("completed_with_warnings", result["state"])
        self.assertEqual(1, result["relationship_observations"]["moved"])
        self.assertEqual("old-user", relationship_api.calls[0]["old_user_id"])
        memory = await self.store.get_memory("legacy-warning-memory")
        relationship = self.store._conn.execute(
            "SELECT * FROM relationship_edges WHERE id=?", (relationship_id,)
        ).fetchone()
        self.assertEqual(("user", "target-user"), (memory.subject.kind, memory.subject.id))
        self.assertEqual(("bot", "target-bot"), (memory.object.kind, memory.object.id))
        self.assertEqual(("user", "target-user"), (relationship["subject_kind"], relationship["subject_id"]))
        self.assertEqual(("bot", "target-bot"), (relationship["object_kind"], relationship["object_id"]))

    async def test_rebind_rejects_completed_batch_without_linked_records(self) -> None:
        target_session = "qqbot:FriendMessage:target-user"
        await self.store.insert_memory(MemoryRecord(
            id="empty-rebind-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="target-user", name="目标用户"),
            object=EntityRef.bot_self("target-bot", "目标 Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="目标窗口",
            metadata={"owner_bot_id": "target-bot"},
        ))
        await self.store.upsert_chat_import_batch({
            "id": "empty-completed-batch", "state": "completed", "scope": "private",
            "session_id": "qq:FriendMessage:old-user", "platform": "qq",
            "user_id": "old-user", "bot_id": "old-bot", "speaker_map": {}, "stats": {},
        })

        with self.assertRaisesRegex(ValueError, "没有可修正"):
            await self.store.rebind_chat_import_batch(
                batch_id="empty-completed-batch", session_id=target_session, platform="qqbot",
                user_id="target-user", bot_id="target-bot",
            )

        batch = await self.store.get_chat_import_batch("empty-completed-batch")
        self.assertEqual(("old-user", "old-bot"), (batch["user_id"], batch["bot_id"]))

    async def test_relationship_rebind_warns_when_target_profile_is_full(self) -> None:
        class RelationshipAPI:
            async def rebind_historical_relationship_observations(self, **_kwargs):
                return {
                    "matched": 0,
                    "moved": 0,
                    "confirmed_matched": 1,
                    "confirmed_moved": 0,
                    "confirmed_trimmed": 1,
                }

        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._private_api = lambda: RelationshipAPI()

        result = await importer._rebind_relationship_observations(
            batch_id="full-target-batch",
            old_user_id="old-user",
            user_id="target-user",
            user_name="目标用户",
            observation_count=0,
        )

        self.assertEqual("completed_with_warnings", result["status"])
        self.assertIn("容量不足", result["message"])

    async def test_relationship_rebind_deduplication_is_not_reported_as_capacity_loss(self) -> None:
        class RelationshipAPI:
            async def rebind_historical_relationship_observations(self, **_kwargs):
                return {
                    "matched": 1,
                    "moved": 0,
                    "deduplicated": 1,
                    "trimmed": 0,
                    "confirmed_matched": 1,
                    "confirmed_moved": 0,
                    "confirmed_deduplicated": 1,
                    "confirmed_trimmed": 0,
                }

        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        importer._private_api = lambda: RelationshipAPI()

        result = await importer._rebind_relationship_observations(
            batch_id="deduplicated-batch",
            old_user_id="old-user",
            user_id="target-user",
            user_name="目标用户",
            observation_count=0,
        )

        self.assertEqual("completed", result["status"])
        self.assertNotIn("message", result)

    async def test_rebind_infers_unknown_entities_from_legacy_batch_identity(self) -> None:
        target_session = "qqbot:FriendMessage:target-user"
        await self.store.insert_memory(MemoryRecord(
            id="identity-fallback-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="target-user", name="目标用户"),
            object=EntityRef.bot_self("target-bot", "目标 Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="目标窗口",
            metadata={"owner_bot_id": "target-bot"},
        ))
        await self.store.upsert_chat_import_batch({
            "id": "legacy-identity-fallback", "state": "completed", "scope": "private",
            "session_id": "qq:FriendMessage:old-user", "platform": "qq",
            "user_id": "old-user", "user_name": "旧用户", "bot_id": "old-bot", "bot_name": "旧 Bot",
            "speaker_map": {}, "stats": {},
        })
        await self.store.insert_memory(MemoryRecord(
            id="legacy-unknown-memory", memory_type="conversation_summary",
            subject=EntityRef(kind="unknown", id="", name=""),
            object=EntityRef(kind="unknown", id="", name=""), scope="private",
            session_id="qq:FriendMessage:old-user", platform="qq", content="旧版未知实体记忆",
            import_batch_id="legacy-identity-fallback",
            metadata={"actor": "旧用户", "object": "旧 Bot", "owner_bot_id": "old-bot"},
        ))

        await self.store.rebind_chat_import_batch(
            batch_id="legacy-identity-fallback", session_id=target_session, platform="qqbot",
            user_id="target-user", user_name="目标用户", bot_id="target-bot", bot_name="目标 Bot",
        )

        memory = await self.store.get_memory("legacy-unknown-memory")
        self.assertEqual(("user", "target-user"), (memory.subject.kind, memory.subject.id))
        self.assertEqual(("bot", "target-bot"), (memory.object.kind, memory.object.id))

    async def test_start_import_validates_explicit_existing_private_context(self) -> None:
        target_session = "qqbot:FriendMessage:openid-user"
        await self.store.insert_memory(MemoryRecord(
            id="explicit-target", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="openid-user", name="用户"),
            object=EntityRef.bot_self("official-bot", "Bot"), scope="private",
            session_id=target_session, platform="qqbot", content="目标上下文",
            metadata={"owner_bot_id": "official-bot"},
        ))

        class Service:
            def __init__(self, data_dir, store):
                self.data_dir, self.store = data_dir, store

            @staticmethod
            def _spawn_background(coro, *, label):
                coro.close()
                return None

        importer = HistoricalChatImporter(Service(Path(self.temp.name), self.store))
        preview = importer.stage_upload(
            filename="chat.txt",
            content="用户: 2026-01-01 10:00:00\n你好\n\nBot: 2026-01-01 10:00:05\n你好呀\n".encode("utf-8"),
        )
        base_payload = {
            "upload_id": preview["upload_id"],
            "speaker_map": {
                "用户": {"role": "user", "entity_id": "openid-user", "display_name": "用户"},
                "Bot": {"role": "bot", "entity_id": "official-bot", "display_name": "Bot"},
            },
            "platform": "qqbot", "session_id": target_session,
            "user_id": "openid-user", "user_name": "用户",
            "bot_id": "official-bot", "bot_name": "Bot",
        }
        started = await importer.start_import(base_payload)
        self.assertEqual(target_session, started["batch"]["session_id"])

        mismatched = dict(base_payload)
        mismatched["user_id"] = "different-user"
        with self.assertRaisesRegex(ValueError, "用户 ID 与私聊会话不一致"):
            await importer.start_import(mismatched)
        wrong_bot = dict(base_payload)
        wrong_bot["bot_id"] = "different-bot"
        with self.assertRaisesRegex(ValueError, "私聊上下文不存在"):
            await importer.start_import(wrong_bot)

    async def test_import_reuses_existing_private_session(self) -> None:
        await self.store.insert_memory(MemoryRecord(
            id="native-memory", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="u1", name="比折"),
            object=EntityRef.bot_self("b1", "诺星缘"), scope="private",
            session_id="default:FriendMessage:u1", visibility="private_pair", content="现有私聊记忆",
        ))

        class Service:
            def __init__(self, data_dir, store):
                self.data_dir, self.store = data_dir, store

            def _spawn_background(self, coro, *, label):
                coro.close()
                return None

        importer = HistoricalChatImporter(Service(Path(self.temp.name), self.store))
        preview = importer.stage_upload(
            filename="chat.txt",
            content="烛雨: 2026-01-01 10:00:00\n你好\n\nmanegata: 2026-01-01 10:00:05\n你好呀\n".encode("utf-8"),
        )
        started = await importer.start_import({
            "upload_id": preview["upload_id"],
            "speaker_map": {
                "烛雨": {"role": "user", "entity_id": "u1", "display_name": "比折"},
                "manegata": {"role": "bot", "entity_id": "b1", "display_name": "诺星缘"},
            },
            "platform": "qq", "user_id": "u1", "user_name": "比折",
            "bot_id": "b1", "bot_name": "诺星缘",
        })
        self.assertEqual("default:FriendMessage:u1", started["batch"]["session_id"])
        self.assertEqual("default", started["batch"]["platform"])

    async def test_start_import_rejects_same_user_and_bot_id(self) -> None:
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        preview = importer.stage_upload(
            filename="chat.txt",
            content="用户: 2026-01-01 10:00:00\n你好\n\nBot: 2026-01-01 10:00:05\n你好呀\n".encode("utf-8"),
        )

        with self.assertRaisesRegex(ValueError, "用户 ID 和 Bot ID 不能相同"):
            await importer.start_import(
                {
                    "upload_id": preview["upload_id"],
                    "speaker_map": {
                        "用户": {"role": "user", "entity_id": "same", "display_name": "用户"},
                        "Bot": {"role": "bot", "entity_id": "same", "display_name": "Bot"},
                    },
                    "user_id": "same",
                    "bot_id": "same",
                }
            )

    async def test_qq_chat_exporter_group_cannot_enter_private_import(self) -> None:
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        source = json.dumps(
            {
                "metadata": {"name": "QQChatExporter", "version": "0.1.0"},
                "chatInfo": {"name": "测试群", "type": "group", "selfUin": "10001", "selfName": "Bot"},
                "messages": [
                    {
                        "id": "g-1", "seq": "1", "timestamp": 1783852174000,
                        "sender": {"uin": "20001", "name": "群成员"},
                        "type": "text", "content": {"text": "群聊消息"},
                        "recalled": False, "system": False,
                    }
                ],
            },
            ensure_ascii=False,
        )
        preview = importer.stage_upload(filename="group.json", content=source.encode("utf-8"))

        with self.assertRaisesRegex(ValueError, "群聊记录暂不能按私聊记忆导入"):
            await importer.start_import({"upload_id": preview["upload_id"]})

    async def test_incremental_qq_export_only_segments_new_messages(self) -> None:
        class Service:
            def __init__(self, data_dir, store):
                self.data_dir, self.store = data_dir, store

            @staticmethod
            def _spawn_background(coro, *, label):
                coro.close()
                return None

        importer = HistoricalChatImporter(Service(Path(self.temp.name), self.store))
        importer._identity_context = lambda _speakers: {
            "available": False, "matches": {}, "bot": {}, "target_users": [],
        }
        payload = {
            "metadata": {"name": "QQChatExporter", "version": "0.1.0"},
            "chatInfo": {
                "name": "用户", "type": "private", "selfUin": "10001", "selfName": "Bot",
                "peerUin": "20001", "peerUid": "peer-uid",
            },
            "messages": [
                {
                    "id": "inc-1", "seq": "1", "timestamp": 1783852174000,
                    "sender": {"uin": "20001", "name": "用户"},
                    "type": "text", "content": {"text": "第一条"}, "recalled": False, "system": False,
                },
                {
                    "id": "inc-2", "seq": "2", "timestamp": 1783852175000,
                    "sender": {"uin": "10001", "name": "Bot"},
                    "type": "text", "content": {"text": "第二条"}, "recalled": False, "system": False,
                },
            ],
        }
        mapping = {
            "用户 [20001]": {"role": "user", "entity_id": "20001", "display_name": "用户"},
            "Bot [10001]": {"role": "bot", "entity_id": "10001", "display_name": "Bot"},
        }
        first_preview = importer.stage_upload(
            filename="first.json", content=json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        await importer.start_import(
            {
                "upload_id": first_preview["upload_id"], "speaker_map": mapping,
                "user_id": "20001", "user_name": "用户", "bot_id": "10001", "bot_name": "Bot",
            }
        )
        payload["messages"].append(
            {
                "id": "inc-3", "seq": "3", "timestamp": 1783852176000,
                "sender": {"uin": "20001", "name": "用户"},
                "type": "text", "content": {"text": "仅新增这一条"}, "recalled": False, "system": False,
            }
        )
        second_preview = importer.stage_upload(
            filename="second.json", content=json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        second = await importer.start_import(
            {
                "upload_id": second_preview["upload_id"], "speaker_map": mapping,
                "user_id": "20001", "user_name": "用户", "bot_id": "10001", "bot_name": "Bot",
            }
        )
        segments = await self.store.chat_import_segments(second["batch"]["id"])

        self.assertEqual(1, second["batch"]["stats"]["new_timeline_count"])
        self.assertEqual(2, second["batch"]["stats"]["reused_timeline_count"])
        self.assertEqual(1, second["batch"]["stats"]["summarized_source_message_count"])
        self.assertEqual(1, len(segments))
        self.assertIn("仅新增这一条", segments[0]["transcript"])
        self.assertNotIn("第一条", segments[0]["transcript"])

        payload["statistics"] = {"revision": 2}
        unchanged_preview = importer.stage_upload(
            filename="unchanged.json", content=json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        unchanged = await importer.start_import(
            {
                "upload_id": unchanged_preview["upload_id"], "speaker_map": mapping,
                "user_id": "20001", "user_name": "用户", "bot_id": "10001", "bot_name": "Bot",
            }
        )
        await importer._run_batch(unchanged["batch"]["id"])
        completed = await self.store.get_chat_import_batch(unchanged["batch"]["id"])

        self.assertEqual("completed", completed["state"])
        self.assertEqual(0, completed["total_segments"])
        self.assertEqual(0, completed["stats"]["new_timeline_count"])

    async def test_existing_import_repairs_alias_entities_and_merges_bucket(self) -> None:
        await self.store.insert_memory(MemoryRecord(
            id="native-memory", memory_type="manual_memory",
            subject=EntityRef(kind="user", id="u1", name="比折"),
            object=EntityRef.bot_self("b1", "诺星缘"), scope="private",
            session_id="default:FriendMessage:u1", visibility="private_pair", content="现有私聊记忆",
        ))
        await self.store.upsert_chat_import_batch({
            "id": "batch-repair", "upload_id": "upload-repair", "source_name": "chat.txt",
            "state": "completed", "session_id": "qq:FriendMessage:u1", "scope": "private",
            "platform": "qq", "user_id": "u1", "user_name": "比折", "bot_id": "b1",
            "bot_name": "诺星缘", "speaker_map": {
                "烛雨": {"role": "user", "entity_id": "u1", "display_name": "烛雨"},
                "manegata": {"role": "bot", "entity_id": "b1", "display_name": "manegata"},
            }, "stats": {},
        })
        await self.store.insert_memory(MemoryRecord(
            id="historical-event", memory_type="important_event",
            subject=EntityRef(kind="unknown", name="比折（烛雨）", role="mentioned"),
            object=EntityRef(kind="unknown", name="诺星缘[3491542998]", role="mentioned"),
            scope="private", session_id="qq:FriendMessage:u1", visibility="private_pair",
            content="双方约好跨年", import_batch_id="batch-repair",
            metadata={"actor": "比折（烛雨）", "object": "诺星缘[3491542998]"},
        ))
        await self.store.add_historical_timeline_events([{
            "event_type": "user_message", "session_id": "qq:FriendMessage:u1", "scope": "private",
            "subject_id": "u1", "object_id": "b1", "content": "跨年快乐", "message_id": "repair-1",
            "occurred_at": "2026-01-01T00:00:00+08:00", "retention_class": "historical_archive",
            "import_batch_id": "batch-repair", "source_sequence": 1, "metadata": {"message_id": "repair-1"},
        }])
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        repaired = await importer._repair_batch_identity_links(
            await self.store.get_chat_import_batch("batch-repair")
        )
        memory = await self.store.get_memory("historical-event")
        self.assertEqual("default:FriendMessage:u1", repaired["session_id"])
        self.assertEqual(("user", "u1"), (memory.subject.kind, memory.subject.id))
        self.assertEqual(("bot", "b1"), (memory.object.kind, memory.object.id))
        self.assertEqual(1, repaired["stats"]["identity_links"]["repaired_entities"])
        targets = [item["target_id"] for item in await self.store.list_memory_buckets()]
        self.assertEqual(1, targets.count("u1"))
        self.assertNotIn("qq:FriendMessage:u1", targets)

    async def test_legacy_private_session_target_remains_visible(self) -> None:
        memory = MemoryRecord(
            id="legacy-event", memory_type="important_event",
            subject=EntityRef(kind="unknown", name="双方"),
            object=EntityRef(kind="unknown", name=""), scope="private",
            session_id="qq:FriendMessage:u1", visibility="private_pair", content="历史事件",
        )
        visible, reason = VisibilityPolicy().is_visible(
            memory,
            SessionContext(session_id="default:FriendMessage:u1", scope="private", user_id="u1"),
        )
        self.assertTrue(visible)
        self.assertEqual("same_private_session_target", reason)

    async def test_imported_first_person_summary_is_neutralized_and_reindexed(self) -> None:
        await self.store.insert_memory(MemoryRecord(
            id="perspective-memory", memory_type="conversation_summary",
            subject=EntityRef(kind="user", id="u1", name="比折"),
            object=EntityRef.bot_self("b1", "诺星缘"), scope="private",
            session_id="default:FriendMessage:u1", visibility="private_pair",
            content="我问诺星缘今天是什么日子。", import_batch_id="batch-perspective",
            metadata={"canonical_summary": "比折询问诺星缘当天是什么日子。"},
        ))
        await self.store.upsert_memory_embedding(
            memory_id="perspective-memory", provider_id="test", text_hash="old", vector=[1.0, 0.0]
        )
        result = await self.store.neutralize_chat_import_summary_perspective("batch-perspective")
        memory = await self.store.get_memory("perspective-memory")
        embedding = self.store._conn.execute(
            "SELECT memory_id FROM memory_embeddings WHERE memory_id='perspective-memory'"
        ).fetchone()
        self.assertEqual({"memories": 1, "embeddings_removed": 1}, result)
        self.assertEqual("比折询问诺星缘当天是什么日子。", memory.content)
        self.assertEqual("我问诺星缘今天是什么日子。", memory.metadata["legacy_perspective_summary"])
        self.assertEqual("neutral_third_person", memory.metadata["summary_perspective"])
        self.assertIsNone(embedding)

    async def test_new_summary_record_uses_canonical_third_person_content(self) -> None:
        service = type("Service", (), {"data_dir": Path(self.temp.name), "store": self.store})()
        importer = HistoricalChatImporter(service)
        batch = {
            "id": "batch-neutral", "user_id": "u1", "user_name": "比折",
            "bot_id": "b1", "bot_name": "诺星缘", "session_id": "default:FriendMessage:u1",
            "platform": "qq",
        }
        segment = {
            "id": "seg-neutral", "start_at": "2026-02-14T17:08:00+08:00",
            "end_at": "2026-02-14T17:10:00+08:00", "message_ids": ["tl-1"], "transcript": "",
        }
        record = importer._summary_record(batch, segment, {
            "summary": "我问诺星缘今天是什么日子。",
            "canonical_summary": "比折询问诺星缘当天是什么日子。",
            "confidence": 0.8, "importance": 0.7, "topics": ["节日"],
        })
        self.assertEqual("比折询问诺星缘当天是什么日子。", record.content)
        self.assertEqual("我问诺星缘今天是什么日子。", record.metadata["source_narrative_summary"])
        self.assertEqual("neutral_third_person", record.metadata["summary_perspective"])

    async def test_legacy_batch_enriches_conversation_and_daily_summaries(self) -> None:
        class Config:
            def int(self, _key, default=0):
                return default

            def bool(self, key, default=False):
                return False if key == "retrieval.embedding_enabled" else default

        class Response:
            def __init__(self, payload):
                import json
                self.completion_text = json.dumps(payload, ensure_ascii=False)

        detailed = (
            "2026年2月14日傍晚，比折询问诺星缘当天的节日含义，诺星缘说明当天是情人节并追问礼物准备情况。"
            "双方随后围绕由谁准备礼物展开玩笑，诺星缘提出将自己作为礼物，比折则明确表示诺星缘从诞生起就已经属于比折。"
            "这段互动延续了双方用调侃确认亲密感的相处方式，最终以双方接受这一表达结束。"
        )
        daily = (
            "2026年2月14日，比折与诺星缘围绕情人节和礼物进行了连续互动。诺星缘先说明节日并询问礼物，"
            "比折反问应由诺星缘准备；诺星缘随后把自己描述为礼物，比折以诺星缘从诞生起就属于比折回应。"
            "当天的交流以玩笑方式确认了双方熟悉的亲密表达，没有形成尚未完成的现实任务。"
        )

        class Provider:
            calls = 0

            async def text_chat(self, *, prompt, **_kwargs):
                self.calls += 1
                if "detailed_summary" in prompt:
                    return Response({"segments": [{
                        "segment_id": "legacy-seg", "detailed_summary": detailed,
                        "canonical_summary": "2026年2月14日，比折与诺星缘围绕情人节礼物进行互动并确认亲密表达。",
                    }]})
                return Response({"daily_digests": [{"date": "2026-02-14", "summary": daily}]})

        provider = Provider()

        class Service:
            def __init__(self, data_dir, store):
                self.data_dir, self.store, self.config = data_dir, store, Config()

            async def _summary_provider_attempts(self, _ctx):
                return [{"provider": provider, "provider_id": "test"}]

            def _record_token_usage(self, **_kwargs):
                return None

        await self.store.upsert_chat_import_batch({
            "id": "legacy-detail-batch", "upload_id": "upload-detail", "source_name": "chat.txt",
            "state": "indexing", "session_id": "default:FriendMessage:u1", "scope": "private",
            "platform": "qq", "user_id": "u1", "user_name": "比折", "bot_id": "b1",
            "bot_name": "诺星缘", "speaker_map": {}, "stats": {}, "total_segments": 1,
            "completed_segments": 1,
        })
        await self.store.replace_chat_import_segments("legacy-detail-batch", [{
            "id": "legacy-seg", "segment_index": 0,
            "start_at": "2026-02-14T17:08:00+08:00", "end_at": "2026-02-14T17:20:00+08:00",
            "local_date": "2026-02-14", "message_ids": ["tl-1", "tl-2"],
            "transcript": "{\"speaker\":\"比折\",\"text\":\"今天是什么日子\"}",
            "char_count": 900, "turn_count": 8, "status": "completed", "summary_memory_id": "legacy-summary",
            "result": {"summary": "我询问诺星缘今天是什么日子。", "canonical_summary": "比折询问节日。"},
        }])
        await self.store.insert_memory(MemoryRecord(
            id="legacy-summary", memory_type="conversation_summary",
            subject=EntityRef(kind="user", id="u1", name="比折"), object=EntityRef.bot_self("b1", "诺星缘"),
            scope="private", session_id="default:FriendMessage:u1", visibility="private_pair",
            content="比折询问节日。", import_batch_id="legacy-detail-batch",
            source_plugin="historical_chat_import",
            metadata={"segment_id": "legacy-seg", "canonical_summary": "比折询问节日。", "summary_perspective": "neutral_third_person"},
        ))
        await self.store.insert_memory(MemoryRecord(
            id="legacy-daily", memory_type="daily_digest",
            subject=EntityRef(kind="user", id="u1", name="比折"), object=EntityRef.bot_self("b1", "诺星缘"),
            scope="private", session_id="default:FriendMessage:u1", visibility="private_pair",
            content="双方讨论情人节。", import_batch_id="legacy-detail-batch",
            source_plugin="historical_chat_import", metadata={"date": "2026-02-14"},
        ))
        importer = HistoricalChatImporter(Service(Path(self.temp.name), self.store))
        await importer._finish_batch_indexing(await self.store.get_chat_import_batch("legacy-detail-batch"))
        batch = await self.store.get_chat_import_batch("legacy-detail-batch")
        summary = await self.store.get_memory("legacy-summary")
        digest = await self.store.get_memory("legacy-daily")
        self.assertEqual("completed", batch["state"], batch)
        self.assertEqual(detailed, summary.content)
        self.assertEqual(daily, digest.content)
        self.assertEqual(1, summary.metadata["detail_schema_version"])
        self.assertEqual(1, digest.metadata["detail_schema_version"])
        self.assertEqual(1, batch["stats"]["detail_quality"]["conversation_summaries_enriched"])
        self.assertEqual(1, batch["stats"]["detail_quality"]["daily_digests_enriched"])
        self.assertEqual(2, provider.calls)

    async def test_enriched_historical_memory_injects_detail_instead_of_brief_canonical(self) -> None:
        detail = "比折先询问诺星缘当天的节日，随后双方围绕礼物准备进行了多轮调侃，最后确认了彼此熟悉的亲密表达。"
        memory = MemoryRecord(
            id="detail-injection", memory_type="conversation_summary",
            subject=EntityRef(kind="user", id="u1", name="比折"), object=EntityRef.bot_self("b1", "诺星缘"),
            scope="private", session_id="default:FriendMessage:u1", visibility="private_pair",
            content=detail, source_plugin="historical_chat_import",
            metadata={
                "canonical_summary": "双方讨论情人节礼物。",
                "summary_perspective": "neutral_third_person",
                "detail_schema_version": 1,
            },
        )
        line = InjectionComposer()._memory_item_line(
            SearchResult(memory=memory, score=1.0), slot_name="conversation_summary"
        )
        self.assertIn(detail, line)
        self.assertNotIn("内容：双方讨论情人节礼物。", line)

    async def test_small_batch_runs_through_reconcile_with_grounded_relationship_evidence(self) -> None:
        class Config:
            def int(self, _key, default=0):
                return default

            def bool(self, _key, default=False):
                return default

        class Response:
            def __init__(self, payload):
                import json
                self.completion_text = json.dumps(payload, ensure_ascii=False)

        class Provider:
            segment_id = ""
            message_ids = []
            calls = 0

            async def text_chat(self, *, prompt, **_kwargs):
                self.calls += 1
                if "待整理片段" in prompt:
                    return Response({"segments": [{
                        "segment_id": self.segment_id,
                        "worth_long_term": True,
                        "summary": "用户和 Bot 约好第二天继续聊天。",
                        "canonical_summary": "双方约定次日继续聊天。",
                        "topics": ["约定"],
                        "importance": 0.8,
                        "confidence": 0.9,
                        "important_events": [{
                            "content": "双方约定次日继续聊天。", "status": "planned",
                            "source_message_ids": self.message_ids,
                        }],
                        "stable_facts": [{
                            "content": "用户希望继续聊天。", "source_message_ids": self.message_ids,
                        }],
                        "relationship_observations": [{
                            "content": "双方形成了继续联系的习惯。", "source_message_ids": self.message_ids,
                            "confidence": 0.8,
                        }],
                    }]})
                if "每日详细回忆" in prompt:
                    return Response({
                        "daily_digests": [{
                            "date": "2026-01-01",
                            "summary": "2026年1月1日，用户询问第二天是否继续聊天，Bot明确答应并与用户约定次日再见。",
                        }],
                    })
                return Response({
                    "daily_digests": [{"date": "2026-01-01", "summary": "双方约定继续聊天。"}],
                    "stable_facts": [{
                        "content": "用户希望继续聊天。", "segment_ids": [self.segment_id], "confidence": 0.8,
                    }],
                    "relationship_observations": [{
                        "content": "双方形成了继续联系的习惯。", "segment_ids": [self.segment_id],
                        "confidence": 0.82,
                    }],
                    "phase_summary": "这段时期双方开始保持持续联系。",
                })

        provider = Provider()

        class Service:
            def __init__(self, data_dir, store):
                self.data_dir, self.store, self.config = data_dir, store, Config()

            def _spawn_background(self, coro, *, label):
                coro.close()
                return None

            async def _summary_provider_attempts(self, _ctx):
                return [{"provider": provider, "provider_id": "test"}]

            def _record_token_usage(self, **_kwargs):
                return None

        importer = HistoricalChatImporter(Service(Path(self.temp.name), self.store))
        preview = importer.stage_upload(
            filename="chat.txt",
            content=(
                "u: 2026-01-01 10:00:00\n明天继续聊吗\n\n"
                "b: 2026-01-01 10:00:05\n好呀，明天见\n"
            ).encode("utf-8"),
        )
        started = await importer.start_import({
            "upload_id": preview["upload_id"],
            "speaker_map": {
                "u": {"role": "user", "entity_id": "u1", "display_name": "用户"},
                "b": {"role": "bot", "entity_id": "b1", "display_name": "Bot"},
            },
            "user_id": "u1", "user_name": "用户", "bot_id": "b1", "bot_name": "Bot",
        })
        batch_id = started["batch"]["id"]
        segment = (await self.store.chat_import_segments(batch_id))[0]
        provider.segment_id = segment["id"]
        provider.message_ids = segment["message_ids"]
        staged = []

        async def capture(_batch, observations):
            staged.extend(observations)
            return len(observations)

        importer._stage_relationship_observations = capture
        await importer._run_batch(batch_id)
        batch = await self.store.get_chat_import_batch(batch_id)
        counts = await self.store.chat_import_memory_counts(batch_id)
        imported_memories = await self.store.list_chat_import_memories(batch_id)
        summary_memory = next(item for item in imported_memories if item.memory_type == "conversation_summary")
        daily_memory = next(item for item in imported_memories if item.memory_type == "daily_digest")
        self.assertEqual("completed", batch["state"], batch)
        self.assertEqual(5, counts["total"])
        self.assertEqual(1, counts["conversation_summary"])
        self.assertEqual(1, counts["important_event"])
        self.assertEqual(1, counts["daily_digest"])
        self.assertEqual(1, counts["stable_fact"])
        self.assertEqual(1, counts["relationship_phase_summary"])
        self.assertEqual("用户和 Bot 约好第二天继续聊天。", summary_memory.content)
        self.assertIn("用户询问第二天是否继续聊天", daily_memory.content)
        self.assertEqual(1, batch["stats"]["detail_quality"]["conversation_summaries_enriched"])
        self.assertEqual(1, batch["stats"]["detail_quality"]["daily_digests_enriched"])
        self.assertEqual(provider.message_ids, staged[0]["source_message_ids"])
        self.assertEqual(3, provider.calls)

        # 模拟全局整理结果已持久化后进程中断；恢复时复用检查点，不再次调用模型，
        # 确定性记忆也不会重复增长。
        await self.store.update_chat_import_batch(batch_id, state="reconciling")
        checkpointed = await self.store.get_chat_import_batch(batch_id)
        await importer._finalize_batch(checkpointed)
        resumed_counts = await self.store.chat_import_memory_counts(batch_id)
        self.assertEqual(3, provider.calls)
        self.assertEqual(5, resumed_counts["total"])


class HistoricalChatUploadTests(unittest.TestCase):
    def test_upload_preview_normalizes_to_utf8_and_estimates_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = type("Service", (), {"data_dir": Path(temp), "store": object()})()
            importer = HistoricalChatImporter(service)
            text = "u: 2026-01-01 00:00:00\n你好\n\nb: 2026-01-01 00:00:02\n你好呀\n"
            preview = importer.stage_upload(filename="对话.txt", content=text.encode("utf-8"))
            self.assertEqual(2, preview["stats"]["message_count"])
            self.assertGreaterEqual(preview["stats"]["estimated_summary_calls"], 1)
            normalized = Path(temp) / "historical_chat_imports" / "uploads" / preview["upload_id"] / "source.txt"
            self.assertEqual(text, normalized.read_text(encoding="utf-8"))

    def test_missing_year_choice_creates_distinct_preview_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = type("Service", (), {"data_dir": Path(temp), "store": object()})()
            importer = HistoricalChatImporter(service)
            text = "u: 01-01 00:00:00\n你好\n"
            first = importer.stage_upload(filename="chat.txt", content=text.encode("utf-8"), base_year=2025)
            second = importer.stage_upload(filename="chat.txt", content=text.encode("utf-8"), base_year=2026)
            self.assertNotEqual(first["upload_id"], second["upload_id"])
            self.assertNotEqual(first["stats"]["first_at"], second["stats"]["first_at"])

    def test_reconcile_output_requires_segment_evidence(self) -> None:
        chunk = [{"segment_id": "seg-1", "date": "2026-01-01"}]
        segment_by_id = {
            "seg-1": {"message_ids": ["tl-1", "tl-2"], "start_at": "2026-01-01T00:00:00+00:00"}
        }
        normalized = HistoricalChatImporter._normalize_reconcile_output(
            {
                "daily_digests": [{"date": "2099-01-01", "summary": "错误日期"}],
                "stable_facts": [
                    {"content": "有证据", "segment_ids": ["seg-1"]},
                    {"content": "无证据", "segment_ids": ["made-up"]},
                ],
                "relationship_observations": [
                    {"content": "称呼发生变化", "segment_ids": ["seg-1"], "confidence": 0.8}
                ],
            },
            chunk,
            segment_by_id,
        )
        self.assertEqual([], normalized["daily_digests"])
        self.assertEqual(["tl-1", "tl-2"], normalized["stable_facts"][0]["source_message_ids"])
        self.assertEqual(1, len(normalized["stable_facts"]))
        self.assertEqual(["tl-1", "tl-2"], normalized["relationship_observations"][0]["source_message_ids"])


if __name__ == "__main__":
    unittest.main()
