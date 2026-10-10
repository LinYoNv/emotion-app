# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_private_companion.page_api_users_groups import (
    PrivateCompanionPageApiUsersGroupsMixin,
)


class _Harness(PrivateCompanionPageApiUsersGroupsMixin):
    def __init__(self) -> None:
        profiles = {"10001": {"name": "阿岚"}}
        self.plugin = SimpleNamespace(
            _group_member_identity_name=lambda user_id, fallback, limit=40: {
                "10001": "阿岚",
            }.get(user_id, fallback),
            _worldbook_profile_by_user_id=lambda user_id, include_observation=True: profiles.get(user_id),
        )

    @staticmethod
    def _single_line(value, limit=80):
        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _limited_list(value, limit):
        return list(value or [])[:limit]


def test_group_page_prefers_relationship_name_and_keeps_fallback() -> None:
    harness = _Harness()
    group = {
        "members": {
            "10001": {"name": "群名片甲"},
            "10002": {"name": "群名片乙"},
        },
        "recent_messages": [
            {"sender_id": "10001", "sender_name": "群名片甲", "text": "你好"},
            {"sender_id": "10002", "sender_name": "群名片乙", "text": "在吗"},
        ],
    }

    names = harness._group_page_identity_names(group)
    messages = harness._group_page_recent_messages(group, names)

    assert group["members"]["10001"]["identity_name"] == "阿岚"
    assert "identity_name" not in group["members"]["10002"]
    assert messages[0]["identity_name"] == "阿岚"
    assert "identity_name" not in messages[1]
