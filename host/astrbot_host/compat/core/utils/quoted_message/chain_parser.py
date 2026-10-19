# -*- coding: utf-8 -*-
"""`OneBotPayloadParser` / `ReplyChainParser` —— 从 payload 抽文本与图片引用。

⭐ 依据：真 AstrBot `core/utils/quoted_message/chain_parser.py`（v4.27.5）。

## 契约（陪伴 `core_reply_media.py:122`）

```python
parsed = OneBotPayloadParser().parse_get_msg_payload(raw)
parsed["image_refs"]   # list[str]
parsed["text"]         # str | None
parsed["forward_ids"]  # list[str]
```

返回的是 **TypedDict 形状的普通 dict**（三个键都要有，缺一个就是 KeyError）。

## 与真实现的差异（如实记录）

* 真实现支持 **合并转发的递归展开**（`depth` 参数、嵌套 `node` / `nodes`）、
  QQ `multimsg` JSON 卡片提取、视频/文件占位文本。这些我们都保留
  —— 因为引用转发的图**很常见**，砍掉等于"引用转发里的图看不见"。
* 真实现用 `normalize_and_dedupe_strings` 去重图片引用；我们照做
  （重复引用同一张图会让上游收到重复图、白烧 token）。
* **`settings` 参数保留但不用**：真实现用它控制"图片地址要不要做白名单校验"。
  我们**不丢图片**（宁可多传也不静默丢 —— 项目铁律）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

logger = logging.getLogger("astrbot")

__all__ = ["ParsedOneBotPayload", "OneBotPayloadParser", "ReplyChainParser"]


class ParsedOneBotPayload(TypedDict):
    """解析结果（字段名逐字照真实现 `chain_parser.py:31-35`）。"""

    text: str | None
    forward_ids: list[str]
    image_refs: list[str]


def _build_parsed_payload(
    text: str | None,
    forward_ids: list[str] | None = None,
    image_refs: list[str] | None = None,
) -> ParsedOneBotPayload:
    return {
        "text": text,
        "forward_ids": forward_ids or [],
        "image_refs": image_refs or [],
    }


def _unwrap_onebot_data(payload: Any) -> dict[str, Any]:
    """剥一层 `{"data": {...}}`（照真实现 `:215-221`）。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _dedupe(items: list[str]) -> list[str]:
    """保序去重（照真实现 `normalize_and_dedupe_strings` 的语义）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _is_image_file_name(value: str) -> bool:
    lowered = value.lower().split("?")[0]
    return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _extract_text_from_multimsg_json(raw_json: str) -> str | None:
    """从 QQ 合并转发卡片 JSON 里抽文本（照真实现 `:224-262`）。"""
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("app") != "com.tencent.multimsg":
        return None
    config = parsed.get("config")
    if not isinstance(config, dict) or config.get("forward") != 1:
        return None
    meta = parsed.get("meta")
    detail = meta.get("detail") if isinstance(meta, dict) else None
    news_items = detail.get("news") if isinstance(detail, dict) else None
    if not isinstance(news_items, list):
        return None
    texts: list[str] = []
    for item in news_items:
        if not isinstance(item, dict):
            continue
        text_content = item.get("text")
        if isinstance(text_content, str):
            cleaned = text_content.strip().replace("[图片]", "").strip()
            if cleaned:
                texts.append(cleaned)
    joined = "\n".join(texts).strip()
    return joined or None


def _parse_segments(
    segments: list[Any], *, depth: int = 0
) -> ParsedOneBotPayload:
    """解析 OneBot 消息段数组（照真实现 `:264-346`）。"""
    text_parts: list[str] = []
    forward_ids: list[str] = []
    image_refs: list[str] = []

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

        if seg_type in ("text", "plain"):
            text = seg_data.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)

        elif seg_type == "image":
            text_parts.append("[Image]")
            candidate = seg_data.get("url") or seg_data.get("file")
            if isinstance(candidate, str) and candidate.strip():
                image_refs.append(candidate.strip())

        elif seg_type == "video":
            text_parts.append("[Video]")

        elif seg_type == "file":
            file_name = (
                seg_data.get("name")
                or seg_data.get("file_name")
                or seg_data.get("file")
                or "file"
            )
            text_parts.append(f"[File:{file_name}]")
            # 文件段里也可能是张图（QQ 把图当文件发）
            candidate_url = seg_data.get("url", "")
            if isinstance(candidate_url, str) and candidate_url.strip() and _is_image_file_name(
                candidate_url
            ):
                image_refs.append(candidate_url.strip())
            candidate_file = seg_data.get("file")
            if (
                isinstance(candidate_file, str)
                and candidate_file.strip()
                and _is_image_file_name(candidate_file)
            ):
                image_refs.append(candidate_file.strip())

        elif seg_type in ("forward", "node", "nodes"):
            # 合并转发：取 id 供上层再拉一次；同时**尝试就地展开**
            fid = seg_data.get("id") or seg_data.get("message_id")
            if isinstance(fid, str) and fid.strip():
                forward_ids.append(fid.strip())
            nested = seg_data.get("content") or seg_data.get("messages")
            if isinstance(nested, list) and depth < 2:
                nested_parsed = _parse_segments(nested, depth=depth + 1)
                if nested_parsed["text"]:
                    text_parts.append(nested_parsed["text"])
                forward_ids.extend(nested_parsed["forward_ids"])
                image_refs.extend(nested_parsed["image_refs"])

        elif seg_type == "json":
            raw_json = seg_data.get("data")
            if isinstance(raw_json, str) and raw_json.strip():
                multimsg_text = _extract_text_from_multimsg_json(
                    raw_json.replace("&#44;", ",")
                )
                if multimsg_text:
                    text_parts.append(multimsg_text)

    joined = "".join(text_parts).strip()
    return _build_parsed_payload(
        joined or None, forward_ids, _dedupe(image_refs)
    )


class ReplyChainParser:
    """引用组件解析（真实现 `:455-492`）。"""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    @staticmethod
    def find_first_reply_component(event: Any) -> Any:
        """找事件里的第一个 `Reply` 组件（照真实现 `:459-462`）。"""
        try:
            from astrbot_host.compat.core.message.components import Reply
        except ImportError:
            return None
        for comp in getattr(getattr(event, "message_obj", None), "message", ()) or ():
            if isinstance(comp, Reply):
                return comp
        return None

    @staticmethod
    def is_forward_placeholder_only_text(text: str | None) -> bool:
        """判断是不是"只有转发占位符"的文本（照真实现 `:463-465`）。"""
        if not isinstance(text, str):
            return False
        stripped = text.strip()
        return bool(stripped) and stripped in {
            "[转发消息]", "[聊天记录]", "[合并转发]",
        }

    def extract_text_from_reply_component(self, reply: Any, *, depth: int = 0) -> str | None:
        """从 `Reply` 组件里取文本（真实现 `:467-478`）。"""
        if reply is None:
            return None
        # Reply 组件的 id 指向被引用消息，**文本要再拉一次 get_msg** —— 这里只能
        # 从组件自身携带的字段读（真实现同样如此）。
        for attr in ("message_str", "text", "summary"):
            value = getattr(reply, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def extract_image_refs_from_reply_component(self, reply: Any) -> list[str]:
        """从 `Reply` 组件里取图片引用（真实现 `:479-492`）。"""
        if reply is None:
            return []
        refs: list[str] = []
        for attr in ("image_refs", "images", "url"):
            value = getattr(reply, attr, None)
            if isinstance(value, str) and value.strip():
                refs.append(value.strip())
            elif isinstance(value, (list, tuple)):
                refs.extend(str(v).strip() for v in value if isinstance(v, str) and v.strip())
        return _dedupe(refs)


class OneBotPayloadParser:
    """payload 解析器（真实现 `:492-504`）。"""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    def parse_get_msg_payload(self, payload: dict[str, Any]) -> ParsedOneBotPayload:
        """解析 `get_msg` 的返回（照真实现 `:411-428`）。"""
        data = _unwrap_onebot_data(payload)
        segments = data.get("message") or data.get("messages")
        if isinstance(segments, list):
            return _parse_segments(segments)

        text: str | None = None
        if isinstance(segments, str) and segments.strip():
            text = segments.strip()
        else:
            raw = data.get("raw_message")
            if isinstance(raw, str) and raw.strip():
                text = raw.strip()
        return _build_parsed_payload(text)

    def parse_get_forward_payload(self, payload: dict[str, Any]) -> ParsedOneBotPayload:
        """解析 `get_forward_msg` 的返回（照真实现 `:431-452`）。"""
        data = _unwrap_onebot_data(payload)
        nodes = (
            data.get("messages")
            or data.get("message")
            or data.get("nodes")
            or data.get("nodeList")
        )
        if not isinstance(nodes, list):
            return _build_parsed_payload(None)
        return _parse_segments(nodes)
