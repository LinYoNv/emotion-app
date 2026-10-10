# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_stubbed_astrbot = False
try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ModuleNotFoundError:
    _stubbed_astrbot = True
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot-test")
    astrbot_module.api = astrbot_api_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

from astrbot_plugin_private_companion.desktop_pet_bridge import DesktopPetBridge

if _stubbed_astrbot:
    sys.modules.pop("astrbot.api", None)
    sys.modules.pop("astrbot", None)


class _Response:
    status = 202
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DesktopPetBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_message_is_posted_to_configured_pet_endpoint(self):
        owner = SimpleNamespace(
            enable_desktop_pet_bridge=True,
            desktop_pet_bridge_url="http://127.0.0.1:18120",
            desktop_pet_bridge_timeout_ms=800,
            desktop_pet_bridge_duration_ms=6000,
            bot_name="星缘",
        )
        bridge = DesktopPetBridge(owner)
        with patch(
            "astrbot_plugin_private_companion.desktop_pet_bridge.urllib.request.urlopen",
            return_value=_Response(),
        ) as urlopen:
            self.assertTrue(bridge.enqueue("主动消息正文。", source="private_companion"))
            await asyncio.sleep(0.05)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual("http://127.0.0.1:18120/v1/say", request.full_url)
        self.assertIn("主动消息正文。", request.data.decode("utf-8"))
        self.assertEqual(1, bridge.status()["sent_count"])
        await bridge.stop()

    async def test_bridge_failure_is_degraded_and_does_not_raise(self):
        owner = SimpleNamespace(
            enable_desktop_pet_bridge=True,
            desktop_pet_bridge_url="http://127.0.0.1:18120",
            desktop_pet_bridge_timeout_ms=800,
            desktop_pet_bridge_duration_ms=6000,
            bot_name="星缘",
        )
        bridge = DesktopPetBridge(owner)
        with patch(
            "astrbot_plugin_private_companion.desktop_pet_bridge.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            self.assertTrue(bridge.enqueue("桌宠没启动也不应影响平台消息。"))
            await asyncio.sleep(0.05)
        self.assertEqual("桌宠未连接", bridge.status()["status"])
        await bridge.stop()


if __name__ == "__main__":
    unittest.main()
