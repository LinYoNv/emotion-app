# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from quart import Quart

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


class _PluginHarness:
    def __init__(self) -> None:
        self._data_lock = asyncio.Lock()
        self.data = {
            "worldbook_member_profiles": {
                "member-1": {
                    "name": "比折",
                    "linked_qq_user_id": "995051631",
                    "aliases": ["珝环"],
                }
            },
            "users": {
                "995051631": {
                    "enabled": True,
                    "umo": "default:FriendMessage:995051631",
                }
            },
        }
        self.enable_livingmemory_integration = True

    @staticmethod
    def _livingmemory_available() -> bool:
        return False

    @staticmethod
    def _livingmemory_plugin_dir() -> Path:
        return Path()

    @staticmethod
    def _format_livingmemory_status() -> str:
        return "当前使用：我会牢牢记住你"

    @staticmethod
    def _memory_companion_bridge():
        return SimpleNamespace(display_name="我会牢牢记住你")

    @staticmethod
    def _memory_companion_presence() -> dict:
        return {"detected": True, "loaded": True, "activated": True, "display_name": "我会牢牢记住你"}

    @staticmethod
    async def _memory_companion_read_user_memory_summary(user_id: str, *, limit: int = 3) -> dict:
        if user_id != "995051631":
            return {"available": False, "state": "forbidden", "reason_code": "private_identity_untrusted"}
        return {
            "available": True,
            "state": "ready",
            "counts": {"profile": 2, "preference": 3, "relationship": 4, "private_chat": 5},
            "summaries": {
                "profile": "用户画像摘要",
                "relationship": "关系记忆摘要",
            },
        }


def test_worldbook_member_prefers_memory_companion_summary() -> None:
    app = Quart(__name__)
    api = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
    api.plugin = _PluginHarness()

    async def run() -> dict:
        async with app.test_request_context("/worldbook/member/livingmemory?user_id=member-1&limit=24"):
            return await api.get_worldbook_member_livingmemory()

    response = asyncio.run(run())
    result = response["data"]

    assert result["available"] is True
    assert result["source_type"] == "memory_companion"
    assert result["source_label"] == "我会牢牢记住你"
    assert result["matched_identity"] == "995051631"
    assert result["total"] == 14
    assert [item["category"] for item in result["items"]] == [
        "profile",
        "preference",
        "relationship",
        "private_chat",
    ]
    assert result["items"][0]["preview"] == "用户画像摘要"
