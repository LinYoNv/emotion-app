"""Automatic relationship anniversaries created from daily-reflection evidence."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

from companion_v2.foundation.refs import ScopeKind
from companion_v2.memory.reflection_output import OutputProblem, parse_output
from companion_v2.relationships import anniversaries
from companion_v2.storage.runtime import StorageRuntime


def _context():
    return SimpleNamespace(
        scope=SimpleNamespace(kind=ScopeKind.PRIVATE, installation_id="installation-a", bot_id="bot-a"),
        role_binding=SimpleNamespace(role=SimpleNamespace(subject_id="role-a")),
        actor=SimpleNamespace(subject=SimpleNamespace(subject_id="user-a")),
    )


def _evidence(*, speaker_id="user-a", issued_at=None):
    return SimpleNamespace(
        speaker_id=speaker_id,
        source=SimpleNamespace(issued_at=issued_at or datetime(2026, 9, 21, 15, 30, tzinfo=timezone.utc)),
    )


class RelationshipAnniversaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="companion-anniversary-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.root.mkdir()
        self.runtime = StorageRuntime.open(data_root=self.root, installation_id="installation-a")
        self.addCleanup(self.runtime.close)
        self.context = _context()
        self.at = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)

    def test_reflection_contract_allows_one_optional_anniversary(self):
        candidate = {
            "label": "第一次认真说喜欢",
            "reason": "用户原话明确说出了告白。",
            "confidence": 0.9,
            "evidence_handle": "s1",
        }
        output = {"summary": "记下了。", "proposals": [], "pending_decisions": [],
                  "anniversaries": [candidate]}
        self.assertEqual(parse_output({"text": json.dumps(output, ensure_ascii=False),
                                       "finish_reason": "stop"}), output)
        output["anniversaries"] = [candidate, candidate]
        with self.assertRaises(OutputProblem) as caught:
            parse_output({"text": json.dumps(output, ensure_ascii=False), "finish_reason": "stop"})
        self.assertEqual(caught.exception.code, "invalid_anniversaries")

    def test_validation_uses_the_user_message_date_and_rejects_bot_evidence(self):
        accepted, rejected = anniversaries.validate_candidates([{
            "label": "第一次认真说喜欢",
            "reason": "用户原话明确说出今天第一次向角色告白。",
            "confidence": 0.93,
            "evidence_handle": "s1",
        }], context=self.context, resolve_evidence=lambda handle: _evidence(),
            timezone_name="Asia/Shanghai", at=self.at)
        self.assertEqual(rejected, [])
        self.assertEqual((accepted[0]["month"], accepted[0]["day"]), (9, 21))
        self.assertEqual(accepted[0]["timezone"], "Asia/Shanghai")

        _, rejected = anniversaries.validate_candidates([{
            "label": "角色自己说的纪念日",
            "reason": "只有 Bot 的回复。",
            "confidence": 0.95,
            "evidence_handle": "s2",
        }], context=self.context, resolve_evidence=lambda handle: _evidence(speaker_id="role-a"),
            timezone_name="Asia/Shanghai", at=self.at)
        self.assertEqual(rejected[0]["problem"]["code"], "anniversary_user_evidence_required")

    def test_remember_deduplicates_and_projects_into_the_next_lookahead(self):
        at = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
        candidate = {
            "label": "第一次一起完成约定",
            "reason": "双方在这天完成了第一次共同约定。",
            "confidence": 0.91,
            "evidence_handle": "s1",
            "occurred_at": "2026-09-20T12:00:00+00:00",
            "month": 9,
            "day": 21,
            "timezone": "Asia/Shanghai",
        }
        created = anniversaries.remember(self.runtime, self.context, candidate,
                                        source_run_id="reflection-1", at=at)
        self.assertEqual(created["state"], "created")
        duplicate = anniversaries.remember(self.runtime, self.context, candidate,
                                          source_run_id="reflection-2", at=at)
        self.assertEqual(duplicate["state"], "duplicate")
        rows = anniversaries.upcoming(self.runtime, self.context, at,
                                     timezone_name="Asia/Shanghai", lookahead_days=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["label"], rows[0]["days_until"]), ("第一次一起完成约定", 0))
        self.assertEqual(rows[0]["basis"], "auto_relationship_anniversary")
        self.assertEqual(anniversaries.retract_by_label(self.runtime, self.context, "第一次一起完成约定"),
                         ("第一次一起完成约定",))
        self.assertEqual(anniversaries.upcoming(self.runtime, self.context, at,
                                                timezone_name="Asia/Shanghai", lookahead_days=1), ())
