# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NUMERIC_FID_MIN_LENGTH = 12

LATEST_ALIASES = {"", "0", "1", "latest", "newest", "最新", "最新一条", "最近", "第一条", "第1条"}
LAST_ALIASES = {"-1", "last", "最后", "最后一条", "末条"}

_QZONE_VIEW_TARGET_SCOPE_ALIASES = {
    "": "auto",
    "auto": "auto",
    "bot": "bot_self",
    "bot_self": "bot_self",
    "botself": "bot_self",
    "self": "bot_self",
    "me": "bot_self",
    "assistant": "bot_self",
    "persona": "bot_self",
    "自己": "bot_self",
    "你": "bot_self",
    "机器人": "bot_self",
    "current_user": "current_user",
    "sender": "current_user",
    "user": "current_user",
    "我": "current_user",
    "我的": "current_user",
    "当前用户": "current_user",
    "对方": "current_user",
    "用户": "current_user",
    "explicit": "explicit_uin",
    "explicit_user": "explicit_uin",
    "explicit_uin": "explicit_uin",
    "uin": "explicit_uin",
    "qq": "explicit_uin",
    "指定用户": "explicit_uin",
    "指定qq": "explicit_uin",
    "ambiguous": "ambiguous",
    "unknown": "ambiguous",
    "不明确": "ambiguous",
}


@dataclass(slots=True)
class QzoneViewTarget:
    """Resolved owner target for a Qzone view request.

    The target must be established before an API call.  A QQ nickname is not a
    stable identity here; only normalized UINs are authoritative.
    """

    scope: str = "ambiguous"
    target_uin: int = 0
    bot_uin: int = 0
    sender_uin: int = 0
    error: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.target_uin and not self.error)


def normalize_qzone_uin(value: Any) -> int:
    """Normalize common Qzone/OneBot UIN representations to an integer."""
    text = str(value or "").strip()
    if text[:1].lower() == "o":
        text = text[1:]
    if not re.fullmatch(r"\d{1,20}", text):
        return 0
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def normalize_qzone_view_target_scope(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _QZONE_VIEW_TARGET_SCOPE_ALIASES.get(text, "")


def resolve_qzone_view_target(
    *,
    target_scope: Any = "",
    target_uin: Any = "",
    legacy_user_id: Any = "",
    bot_uin: Any = 0,
    sender_uin: Any = 0,
) -> QzoneViewTarget:
    """Resolve a view target without silently choosing the event sender.

    ``legacy_user_id`` remains supported only as an explicit UIN so existing
    callers that already supplied an account continue to work.  Empty target
    arguments are deliberately ambiguous: guessing would let a model turn a
    user's or third party's post into a Bot-self observation.
    """
    normalized_scope = normalize_qzone_view_target_scope(target_scope)
    bot = normalize_qzone_uin(bot_uin)
    sender = normalize_qzone_uin(sender_uin)
    target_text = str(target_uin or "").strip()
    legacy_text = str(legacy_user_id or "").strip()
    explicit = normalize_qzone_uin(target_text)
    legacy = normalize_qzone_uin(legacy_text)

    if target_text and not explicit:
        return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="invalid_target_uin")
    if legacy_text and not legacy:
        return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="invalid_legacy_user_id")
    if explicit and legacy and explicit != legacy:
        return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="conflicting_target_uin")
    supplied = explicit or legacy

    if not normalized_scope:
        return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="invalid_target_scope")
    if normalized_scope == "auto":
        normalized_scope = "explicit_uin" if supplied else "ambiguous"

    if normalized_scope == "ambiguous":
        return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="missing_target")
    if normalized_scope == "explicit_uin":
        if not supplied:
            return QzoneViewTarget(normalized_scope, bot_uin=bot, sender_uin=sender, error="missing_target_uin")
        return QzoneViewTarget(normalized_scope, supplied, bot, sender)
    if normalized_scope == "bot_self":
        if not bot:
            return QzoneViewTarget(normalized_scope, bot_uin=bot, sender_uin=sender, error="bot_uin_unavailable")
        if supplied and supplied != bot:
            return QzoneViewTarget(normalized_scope, bot_uin=bot, sender_uin=sender, error="scope_target_conflict")
        return QzoneViewTarget(normalized_scope, bot, bot, sender)
    if normalized_scope == "current_user":
        if not sender:
            return QzoneViewTarget(normalized_scope, bot_uin=bot, sender_uin=sender, error="sender_uin_unavailable")
        if supplied and supplied != sender:
            return QzoneViewTarget(normalized_scope, bot_uin=bot, sender_uin=sender, error="scope_target_conflict")
        return QzoneViewTarget(normalized_scope, sender, bot, sender)
    return QzoneViewTarget("ambiguous", bot_uin=bot, sender_uin=sender, error="invalid_target_scope")


def classify_qzone_view_owner(target: QzoneViewTarget, owner_uin: Any) -> str:
    """Classify the returned feed author against the resolved request target."""
    owner = normalize_qzone_uin(owner_uin)
    if not owner:
        return "identity_unverified"
    if not target.resolved or owner != target.target_uin:
        return "identity_mismatch"
    if target.bot_uin and target.sender_uin and target.bot_uin == target.sender_uin and owner == target.bot_uin:
        return "shared_identity"
    if target.bot_uin and owner == target.bot_uin:
        return "bot_self"
    if target.sender_uin and owner == target.sender_uin:
        return "current_user"
    return "third_party"


def qzone_view_owner_is_pronoun_safe(owner_role: str) -> bool:
    return str(owner_role or "") in {"bot_self", "current_user", "third_party"}


@dataclass(slots=True)
class QzonePostSelection:
    target_id: str = ""
    pos: int = 0
    limit: int = 1
    selector: str = "latest"
    fid: str = ""
    explicit_target: bool = False
    explicit_selector: bool = False

    @property
    def is_last(self) -> bool:
        return self.selector == "last"


def _looks_like_fid(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(rf"\d{{{NUMERIC_FID_MIN_LENGTH},}}", text):
        return True
    if _parse_index(text) is not None:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{6,}", text))


def _parse_index(value: str) -> int | None:
    text = str(value or "").strip()
    lowered = text.lower()
    if text in LATEST_ALIASES or lowered in LATEST_ALIASES:
        return 0
    if text in LAST_ALIASES or lowered in LAST_ALIASES:
        return -1
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.fullmatch(r"第\s*(\d+)\s*(?:条|條)?", normalized)
    if not match:
        match = re.fullmatch(r"(\d+)\s*(?:条|條)?", normalized)
    if not match:
        return None
    return max(0, int(match.group(1)) - 1)


def parse_qzone_post_selection(
    *,
    user_id: str = "",
    selector: str = "",
    pos: int = 0,
    fid: str = "",
) -> QzonePostSelection:
    target_id = str(user_id or "").strip()
    raw = str(selector or "").strip()
    explicit_target = bool(target_id)
    explicit_selector = bool(raw or fid)

    at_match = re.search(r"\[CQ:at,qq=(\d+)[^\]]*\]|@(\d{5,})", raw)
    if at_match:
        target_id = at_match.group(1) or at_match.group(2) or target_id
        explicit_target = True
        raw = re.sub(r"\[CQ:at,qq=\d+[^\]]*\]|@\d{5,}", " ", raw, count=1).strip()

    tokens = raw.split()
    if not target_id and tokens and re.fullmatch(r"\d{5,}", tokens[0]) and not _looks_like_fid(tokens[0]):
        target_id = tokens.pop(0)
        explicit_target = True

    explicit_fid = str(fid or "").strip()
    if explicit_fid:
        return QzonePostSelection(target_id=target_id, fid=explicit_fid, selector="fid", explicit_target=explicit_target, explicit_selector=True)

    if tokens and _looks_like_fid(tokens[0]):
        return QzonePostSelection(target_id=target_id, fid=tokens[0], selector="fid", explicit_target=explicit_target, explicit_selector=True)

    parsed = _parse_index(tokens[0]) if tokens else _parse_index(raw)
    if parsed is None:
        parsed = max(0, int(pos or 0))
        explicit_selector = explicit_selector or parsed > 0
    if parsed < 0:
        return QzonePostSelection(target_id=target_id, pos=0, limit=10, selector="last", explicit_target=explicit_target, explicit_selector=True)
    return QzonePostSelection(target_id=target_id, pos=parsed, limit=1, selector="index" if parsed > 0 else "latest", explicit_target=explicit_target, explicit_selector=explicit_selector)
