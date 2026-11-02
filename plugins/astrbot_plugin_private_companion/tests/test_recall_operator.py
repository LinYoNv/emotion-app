"""Withdrawal facts belong to the message route, not to the notice operator.

OneBot reports a group administrator in ``user_id`` when that administrator
recalls another member's message. V1 keyed its recalled-id cache by message and
scope, so the original author's withdrawal was still visible. V2 keyed the
marker by the full notice (including the operator), which made that case look
unwithdrawn.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from companion_v2.conversation.recalls import MessageRecall
from tests.test_host_application import HostFixture


class RecallOperatorTests(HostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_a_moderator_recall_withdraws_the_original_authors_input(self):
        host = await self.start()
        app = host.application
        batch = host.receive(self.event("event-moderated")).unwrap()
        context = self.context(app, batch)
        author = self.important.subject.subject_id

        notice = MessageRecall.from_scope(context.scope, "event-moderated", "group-admin-42")
        result = app.platform_recall(notice).unwrap()

        self.assertEqual(result["withdrawn_inputs"], 1)
        self.assertTrue(app.messages.recalls.recalled(context.scope, "event-moderated", author))
        with self.assertRaises(Exception) as raised:
            app.messages.recalls.check(context.scope, "event-moderated", author)
        self.assertEqual(getattr(raised.exception, "code", ""), "message_withdrawn")

    async def test_disabling_recall_silence_records_but_does_not_cancel_the_batch(self):
        host = await self.start()
        app = host.application
        batch = host.receive(self.event("event-unsilenced")).unwrap()
        context = self.context(app, batch)
        app.messages.reply_settings = SimpleNamespace(
            capture=lambda context, at: {"settings": {"recall_silence_enabled": False}})
        notice = MessageRecall.from_scope(context.scope, "event-unsilenced", "group-admin-42")

        result = app.platform_recall(notice).unwrap()

        self.assertEqual(result["withdrawn_inputs"], 1)
        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(app.runtime.read_one(
            "SELECT state FROM conversation_batches WHERE batch_id = ?", (batch,))["state"], "open")

    async def test_a_moderator_recall_marker_is_idempotent(self):
        host = await self.start()
        app = host.application
        batch = host.receive(self.event("event-replayed")).unwrap()
        context = self.context(app, batch)
        notice = MessageRecall.from_scope(context.scope, "event-replayed", "group-admin-42")

        first = app.platform_recall(notice).unwrap()
        second = app.platform_recall(notice).unwrap()

        self.assertTrue(first["replayed"] is False)
        self.assertTrue(second["replayed"] is True)
        self.assertEqual(second["withdrawn_inputs"], 0)


if __name__ == "__main__":
    unittest.main()
