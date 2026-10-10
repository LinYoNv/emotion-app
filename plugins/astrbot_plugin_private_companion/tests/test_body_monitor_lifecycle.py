from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _Logger()
api.AstrBotConfig = object
astrbot.api = api


def _stub_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


_Dummy = type("_Dummy", (), {})
_ASTRBOT_STUBS = {
    "astrbot": astrbot,
    "astrbot.api": api,
    "astrbot.api.event": _stub_module(
        "astrbot.api.event",
        AstrMessageEvent=_Dummy,
        MessageChain=_Dummy,
        filter=_Dummy(),
    ),
    "astrbot.api.message_components": _stub_module(
        "astrbot.api.message_components",
        At=_Dummy,
        Image=_Dummy,
        Plain=_Dummy,
        Record=_Dummy,
        Reply=_Dummy,
    ),
    "astrbot.api.provider": _stub_module("astrbot.api.provider", ProviderRequest=_Dummy),
    "astrbot.api.star": _stub_module(
        "astrbot.api.star",
        Context=_Dummy,
        Star=_Dummy,
        StarTools=_Dummy,
        register=lambda *args, **kwargs: (lambda value: value),
    ),
    "astrbot.core": _stub_module("astrbot.core", file_token_service=_Dummy()),
    "astrbot.core.astr_main_agent": _stub_module(
        "astrbot.core.astr_main_agent",
        MainAgentBuildConfig=_Dummy,
        build_main_agent=lambda *args, **kwargs: None,
    ),
    "astrbot.core.agent": _stub_module("astrbot.core.agent"),
    "astrbot.core.agent.message": _stub_module(
        "astrbot.core.agent.message",
        AssistantMessageSegment=_Dummy,
        TextPart=_Dummy,
        UserMessageSegment=_Dummy,
    ),
    "astrbot.core.db": _stub_module("astrbot.core.db"),
    "astrbot.core.db.po": _stub_module("astrbot.core.db.po", Conversation=_Dummy),
    "astrbot.core.platform": _stub_module("astrbot.core.platform"),
    "astrbot.core.platform.astrbot_message": _stub_module(
        "astrbot.core.platform.astrbot_message",
        AstrBotMessage=_Dummy,
        MessageMember=_Dummy,
    ),
    "astrbot.core.platform.message_session": _stub_module(
        "astrbot.core.platform.message_session",
        MessageSession=_Dummy,
    ),
    "astrbot.core.platform.message_type": _stub_module(
        "astrbot.core.platform.message_type",
        MessageType=_Dummy,
    ),
    "astrbot.core.platform.platform": _stub_module(
        "astrbot.core.platform.platform",
        PlatformStatus=_Dummy,
    ),
    "astrbot.core.platform.platform_metadata": _stub_module(
        "astrbot.core.platform.platform_metadata",
        PlatformMetadata=_Dummy,
    ),
    "astrbot.core.provider": _stub_module("astrbot.core.provider"),
    "astrbot.core.provider.entities": _stub_module(
        "astrbot.core.provider.entities",
        LLMResponse=_Dummy,
    ),
    "astrbot.core.star": _stub_module("astrbot.core.star"),
    "astrbot.core.star.star_handler": _stub_module(
        "astrbot.core.star.star_handler",
        EventType=_Dummy,
        star_handlers_registry=_Dummy(),
    ),
    "astrbot.core.utils": _stub_module("astrbot.core.utils"),
    "astrbot.core.utils.astrbot_path": _stub_module(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: "",
    ),
}

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_private_companion"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    package.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = package

with mock.patch.dict(sys.modules, _ASTRBOT_STUBS):
    from astrbot_plugin_private_companion.busy_reply_gate import BusyReplyGateMixin
    from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class _BusyHarness(BusyReplyGateMixin):
    def __init__(self) -> None:
        self.marked = []
        self.cleared = 0

    def _mark_planned_candidate_status(self, _user, status, note):
        self.marked.append((status, note))

    def _clear_pending_proactive_plan(self, user):
        self.cleared += 1
        user.pop("body_monitor_health_context", None)
        user["next_proactive_at"] = 0


class _EngineHarness(ProactiveEngineMixin):
    def __init__(self) -> None:
        self.data = {"proactive_candidate_pool": []}


class BodyMonitorLifecycleTests(unittest.TestCase):
    def test_busy_defer_cannot_extend_body_monitor_hard_expiry(self) -> None:
        harness = _BusyHarness()
        user = {
            "planned_proactive_source": "body_monitor",
            "next_proactive_at": 100,
            "planned_proactive_window_start_at": 100,
            "planned_proactive_best_until_at": 180,
            "planned_proactive_expire_at": 200,
            "body_monitor_health_context": {"metric": "heart_rate", "value": 108},
        }

        changed = harness._defer_proactive_for_busy(user, now=110, until=250)

        self.assertTrue(changed)
        self.assertEqual(harness.cleared, 1)
        self.assertEqual(harness.marked[0][0], "blocked")
        self.assertNotIn("body_monitor_health_context", user)

    def test_busy_defer_inside_window_keeps_original_expiry(self) -> None:
        harness = _BusyHarness()
        user = {
            "planned_proactive_source": "body_monitor",
            "next_proactive_at": 100,
            "planned_proactive_window_start_at": 100,
            "planned_proactive_best_until_at": 170,
            "planned_proactive_expire_at": 200,
        }

        changed = harness._defer_proactive_for_busy(user, now=110, until=150)

        self.assertTrue(changed)
        self.assertEqual(user["next_proactive_at"], 150)
        self.assertEqual(user["planned_proactive_expire_at"], 200)
        self.assertLessEqual(user["planned_proactive_best_until_at"], 200)

    def test_deferred_status_keeps_body_monitor_impulse_hard_expiry(self) -> None:
        harness = _EngineHarness()
        user = {
            "user_id": "10001",
            "next_proactive_at": 150,
            "planned_proactive_source": "body_monitor",
            "planned_proactive_impulse_id": "health-1",
            "planned_proactive_expire_at": 200,
            "proactive_impulses": [
                {
                    "id": "health-1",
                    "source": "body_monitor",
                    "state": "queued",
                    "created_ts": 100,
                    "updated_ts": 100,
                    "window_start_at": 100,
                    "preferred_ts": 100,
                    "best_until_at": 180,
                    "expire_at": 200,
                }
            ],
        }

        harness._mark_planned_candidate_status(user, "deferred", "繁忙顺延")

        impulse = user["proactive_impulses"][0]
        self.assertEqual(impulse["window_start_at"], 150)
        self.assertEqual(impulse["best_until_at"], 180)
        self.assertEqual(impulse["expire_at"], 200)

    def test_terminal_body_monitor_impulse_scrubs_health_context(self) -> None:
        user = {
            "proactive_impulses": [
                {
                    "id": "health-sent",
                    "source": "body_monitor",
                    "state": "sent",
                    "created_ts": 100,
                    "updated_ts": 190,
                    "expire_at": 200,
                    "context_key": "body_monitor_health_context",
                    "context": {"metric": "heart_rate", "value": 108},
                }
            ]
        }

        kept = _EngineHarness()._cleanup_proactive_impulses(user, now=210)

        self.assertEqual(len(kept), 1)
        self.assertNotIn("context", kept[0])
        self.assertEqual(kept[0]["context_key"], "")

    def test_expired_body_monitor_impulse_scrubs_health_context(self) -> None:
        user = {
            "proactive_impulses": [
                {
                    "id": "health-expired",
                    "source": "body_monitor",
                    "state": "queued",
                    "created_ts": 100,
                    "updated_ts": 100,
                    "window_start_at": 100,
                    "preferred_ts": 120,
                    "best_until_at": 180,
                    "expire_at": 200,
                    "context_key": "body_monitor_health_context",
                    "context": {"metric": "heart_rate", "value": 108},
                }
            ]
        }

        kept = _EngineHarness()._cleanup_proactive_impulses(user, now=210)

        self.assertEqual(kept[0]["state"], "blocked")
        self.assertNotIn("context", kept[0])
        self.assertEqual(kept[0]["context_key"], "")


if __name__ == "__main__":
    unittest.main()
