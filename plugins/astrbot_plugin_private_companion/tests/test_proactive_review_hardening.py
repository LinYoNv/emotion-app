from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _RewriteHarness(ProactiveMessageMixin):
    @staticmethod
    def _sanitize_action_boundaries(text, **_kwargs):
        return str(text or "").strip()

    @staticmethod
    def _repair_proactive_recipient_address(text, _user, _name=""):
        return str(text or ""), ""

    @staticmethod
    def _wrong_proactive_recipient_address(_text, _user, _name=""):
        return ""

    @staticmethod
    def _framework_agent_meta_summary_leak(_text):
        return False

    @staticmethod
    def _strip_internal_identity_anchors(text):
        return str(text or "")

    @staticmethod
    def _external_share_source_consistency_decision(*_args, **_kwargs):
        return None


class _AuditHarness(ProactiveEngineMixin):
    def __init__(self):
        self.data = {
            "proactive_audit_log": [
                {"status": "sent", "reason": "quiet_care", "updated_ts": 1000},
                {"status": "deferred", "reason": "environment_change", "updated_ts": 1001},
                {"status": "cancelled", "reason": "内部流程泄漏", "updated_ts": 1002},
            ],
        }


class ProactiveReviewHardeningTests(unittest.TestCase):
    def test_ordinary_system_and_model_words_are_not_blacklisted(self):
        harness = _RewriteHarness()
        accepted = harness._accept_proactive_rewrite(
            "学校系统刚恢复，我还在拼模型，晚点再和你说。",
            original_text="学校系统刚恢复。",
            user={"nickname": "小林"},
        )
        self.assertIsNotNone(accepted)

    def test_structured_model_leak_is_rejected(self):
        harness = _RewriteHarness()
        rejected = harness._accept_proactive_rewrite(
            '{"decision":"rewrite","text":"你好"}',
            original_text="我刚想到一件小事，想和你说。",
            user={"nickname": "小林"},
        )
        self.assertIsNone(rejected)

    def test_defer_policy_keeps_delay(self):
        harness = _RewriteHarness()
        result = harness._normalize_proactive_review_decision_policy(
            {"nickname": "小林"},
            {"decision": "defer", "reason": "现在不合适", "delay_minutes": 75},
            strength="balanced",
        )
        self.assertEqual(result["decision"], "defer")
        self.assertEqual(result["delay_minutes"], 75)

    def test_context_truncation_stops_at_sentence_boundary(self):
        text = "第一句完整。第二句完整。第三句完整。" + "第四句" * 100
        result = _RewriteHarness._truncate_proactive_context(text, 20)
        self.assertTrue(result.endswith("。"))
        self.assertNotIn("第四句", result)

    def test_sentence_flow_does_not_crush_extra_sentences_into_last_line(self):
        harness = _RewriteHarness()
        result = harness._normalize_proactive_sentence_flow(
            "第一句。第二句。第三句。第四句。第五句。"
        )

        self.assertEqual(["第一句。", "第二句。", "第三句。", "第四句。"], result.splitlines())
        self.assertNotIn("第五句", result)

    def test_review_audit_summary_aggregates_recent_outcomes(self):
        harness = _AuditHarness()
        result = harness._proactive_review_audit_summary(now=1002, window_days=30)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["decision_counts"]["sent"], 1)
        self.assertEqual(result["decision_counts"]["deferred"], 1)
        self.assertEqual(result["top_reasons"][0]["count"], 1)

    def test_expired_environment_candidate_is_marked_stale(self):
        harness = _RewriteHarness()
        result = harness._stale_proactive_review_defer_release_reason(
            {
                "planned_proactive_window_start_at": 100,
                "planned_proactive_expire_at": 500,
            },
            reason="environment_change",
            now=501,
        )
        self.assertIn("有效窗口已结束", result)

    def test_reply_outcome_is_linked_to_expected_proactive_audit(self):
        harness = _AuditHarness()
        harness.data["proactive_audit_log"].append(
            {
                "id": "audit-reply",
                "status": "sent",
                "expects_reply": True,
                "sent_ts": 1000,
                "updated_ts": 1000,
            }
        )
        user = {
            "last_proactive_reply_audit_id": "audit-reply",
            "last_proactive_reply_audit_sent_at": 1000,
        }

        recorded = harness._mark_proactive_audit_reply_outcome(
            user,
            received_at=1060,
            message_id="message-1",
        )

        self.assertTrue(recorded)
        audit = harness.data["proactive_audit_log"][-1]
        self.assertEqual("replied_24h", audit["outcome"])
        self.assertEqual(60, audit["outcome_latency_seconds"])

    def test_summary_marks_mature_unanswered_expected_send(self):
        harness = _AuditHarness()
        harness.data["proactive_audit_log"].append(
            {
                "id": "audit-unanswered",
                "status": "sent",
                "reason": "quiet_care",
                "expects_reply": True,
                "sent_ts": 1000,
                "updated_ts": 1000,
            }
        )

        result = harness._proactive_review_audit_summary(now=1000 + 25 * 3600, window_days=7)

        self.assertEqual(1, result["reply_outcomes"]["no_reply_24h"])
        self.assertEqual(0.0, result["reply_rate_24h"])


if __name__ == "__main__":
    unittest.main()
