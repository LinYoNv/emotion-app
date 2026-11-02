"""A recall question may add one bounded transcript section for a core admin."""

from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

try:
    from astrbot.core.agent.message import TextPart  # noqa: F401
    HAS_ASTRBOT = True
except ImportError:  # pragma: no cover - the real host installs AstrBot
    HAS_ASTRBOT = False

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.core_reply_perception import inject_core_reply_perception_context


class _Outcome:
    is_success = True
    failure = None

    def unwrap(self):
        return {"entries": [{"author": "member", "local_time": "12:00", "text": "被撤回的原话"}]}


class _Manager:
    def __init__(self, application, context):
        self.application, self.context = application, context

    def transcribe(self, operator, limit=None):
        return _Outcome()


@unittest.skipUnless(HAS_ASTRBOT, "recall injection needs the AstrBot message types")
class RecallInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_question_adds_the_bounded_transcript_to_context(self):
        context = NS(actor=NS(subject=NS(subject_id="admin")), scope=NS(kind="private"))
        prepared = {"context": context, "native_input": None}
        event = NS()
        event.get_extra = lambda key: {CORE_REPLY_CONTEXT_EXTRA: {"prepared": prepared}}.get(key)
        event.is_at_or_wake_command = False
        request = NS(extra_user_content_parts=[], prompt="刚才撤回了什么", system_prompt="", model=None)
        application = NS(role_attention=None, tasks=NS(now=lambda: None), messages=NS())

        with patch("companion_v2.conversation.recall_management.RecallManagement", _Manager):
            await inject_core_reply_perception_context(event, request, application=application)

        text = request.extra_user_content_parts[0].text
        self.assertIn('"recall_query"', text)
        self.assertIn("被撤回的原话", text)
        self.assertIn("不要提插件", text)

    async def test_bare_word_does_not_add_recall_context(self):
        prepared = {"context": NS(actor=NS(subject=NS(subject_id="admin")), scope=NS(kind="private")),
                    "native_input": None}
        event = NS()
        event.get_extra = lambda key: {CORE_REPLY_CONTEXT_EXTRA: {"prepared": prepared}}.get(key)
        event.is_at_or_wake_command = False
        request = NS(extra_user_content_parts=[], prompt="撤回功能怎么关", system_prompt="", model=None)

        await inject_core_reply_perception_context(event, request, application=NS(
            role_attention=None, tasks=NS(now=lambda: None), messages=NS()))

        self.assertEqual(request.extra_user_content_parts, [])


if __name__ == "__main__":
    unittest.main()
