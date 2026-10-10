from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


ROOT = bootstrap_package()

if "quart" not in sys.modules:
    quart_stub = types.ModuleType("quart")
    quart_stub.jsonify = lambda payload=None, **kwargs: payload or kwargs
    quart_stub.request = SimpleNamespace(args={}, method="GET")
    quart_stub.send_file = AsyncMock()
    sys.modules["quart"] = quart_stub

PACKAGE_NAME = "astrbot_plugin_memory_companion"
page_api_module = importlib.import_module(f"{PACKAGE_NAME}.page_api")
PluginPageApi = page_api_module.PluginPageApi


class _JsonResponse(dict):
    status_code = 200


class _Bridge:
    def __init__(self) -> None:
        self.capability = object()
        self.context = object()
        self.get_emotion_trace_diagnostic = AsyncMock(
            return_value={"state": "ready", "read_only": True, "items": []}
        )

    def bind_emotion_page_api(self, _page_api):
        return self.capability

    def create_emotion_admin_context(self, capability, *, bot_id, scope, session_id):
        if capability is not self.capability:
            return None
        if (bot_id, scope, session_id) != ("bot-1", "private", "session-1"):
            return None
        return self.context


class EmotionE9PageAdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, bound_username: str, query: dict[str, str]):
        bridge = _Bridge()
        plugin = SimpleNamespace(
            service=SimpleNamespace(),
            context=SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}}),
            memory_companion=bridge,
        )
        api = PluginPageApi(plugin)
        fake_request = SimpleNamespace(
            args=query,
            headers={"X-Emotion-Admin": "true"},
            cookies={"emotion_admin": "true"},
        )
        with (
            patch.object(page_api_module, "request", fake_request),
            patch.object(page_api_module, "astrbot_web_request", SimpleNamespace(username=bound_username)),
            patch.object(page_api_module, "jsonify", side_effect=lambda body: _JsonResponse(body)),
        ):
            response = await api.emotion_trace()
        return response, bridge

    async def test_query_header_and_cookie_cannot_claim_admin(self) -> None:
        response, bridge = await self.invoke(
            bound_username="attacker",
            query={
                "trace_id": "trace-1",
                "bot_id": "bot-1",
                "scope": "private",
                "session_id": "session-1",
                "is_admin": "true",
            },
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("admin_required", response["error"])
        bridge.get_emotion_trace_diagnostic.assert_not_awaited()

    async def test_framework_bound_dashboard_admin_reaches_bridge(self) -> None:
        response, bridge = await self.invoke(
            bound_username="admin",
            query={
                "trace_id": "trace-1",
                "bot_id": "bot-1",
                "scope": "private",
                "session_id": "session-1",
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response["success"])
        bridge.get_emotion_trace_diagnostic.assert_awaited_once_with(
            "trace-1", bridge.context, limit=100
        )


if __name__ == "__main__":
    unittest.main()
