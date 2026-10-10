# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

from .qzone_json import load_qzone_json


class _QzoneFeedHtmlParser(HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.repost_parts: list[str] = []
        self.image_urls: list[str] = []
        self.image_items: list[dict[str, str]] = []
        self._class_stack: list[set[str]] = []
        self._tag_stack: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for key, value in attrs:
            if str(key or "").lower() == "class":
                return {item.strip() for item in str(value or "").split() if item.strip()}
        return set()

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
        lowered = str(name or "").lower()
        for key, value in attrs:
            if str(key or "").lower() == lowered:
                return str(value or "")
        return ""

    @classmethod
    def _first_attr(cls, attrs: list[tuple[str, str | None]], *names: str) -> str:
        for name in names:
            value = cls._attr(attrs, name).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _is_content_image_url(value: str) -> bool:
        source = str(value or "").strip().lower()
        return bool(source) and not source.startswith("http://qzonestyle.gtimg.cn")

    def _record_image(self, attrs: list[tuple[str, str | None]]) -> None:
        preview_url = self._first_attr(attrs, "src", "data-src", "data-lazy-src", "data-original-src")
        full_url = self._first_attr(
            attrs,
            "data-original",
            "data-origin",
            "data-origin-url",
            "data-original-url",
            "data-originalurl",
            "data-big-url",
            "data-bigurl",
            "data-full-url",
            "data-fullurl",
            "data-raw-url",
            "data-rawurl",
            "origsrc",
            "original",
            "data-url",
        )
        if not self._is_content_image_url(full_url):
            full_url = ""
        if not self._is_content_image_url(preview_url):
            preview_url = ""
        source = full_url or preview_url
        if not source:
            return
        item = {"preview_url": preview_url or source, "full_url": source}
        if item not in self.image_items:
            self.image_items.append(item)
        if source not in self.image_urls:
            self.image_urls.append(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = str(tag or "").lower()
        classes = self._classes(attrs)
        if normalized_tag != "img":
            if normalized_tag not in self._VOID_TAGS:
                self._tag_stack.append(normalized_tag)
                self._class_stack.append(classes)
            elif classes:
                self._class_stack.append(classes)
                self._class_stack.pop()
            return
        active_classes = self._class_stack + [classes]
        if any({"img-box", "video-img"} & item for item in active_classes):
            self._record_image(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = str(tag or "").lower()
        if normalized_tag == "img":
            self.handle_starttag(tag, attrs)
            return
        classes = self._classes(attrs)
        if classes:
            self._class_stack.append(classes)
            self._class_stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = str(tag or "").lower()
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index] == normalized_tag:
                del self._tag_stack[index:]
                del self._class_stack[index:]
                return
        if self._tag_stack and self._class_stack:
            self._tag_stack.pop()
            self._class_stack.pop()

    def handle_data(self, data: str) -> None:
        text = unescape(str(data or "")).strip()
        if not text:
            return
        if any("f-info" in item for item in self._class_stack):
            self.text_parts.append(text)
        if any("txt-box" in item for item in self._class_stack):
            self.repost_parts.append(text)


def _parse_feed_html(html_content: str) -> tuple[str, str, list[str]]:
    text, repost, image_urls, _items = _parse_feed_html_details(html_content)
    return text, repost, image_urls


def _parse_feed_html_details(html_content: str) -> tuple[str, str, list[str], list[dict[str, str]]]:
    parser = _QzoneFeedHtmlParser()
    parser.feed(str(html_content or ""))
    text = "".join(parser.text_parts).strip()
    repost = "".join(parser.repost_parts).strip()
    if "：" in repost:
        repost = repost.split("：", 1)[1].strip()
    return text, repost, parser.image_urls, parser.image_items


def _html_attr_value(html_content: str, *names: str) -> str:
    source = str(html_content or "")
    for name in names:
        escaped = re.escape(str(name or ""))
        match = re.search(rf"""{escaped}\s*=\s*["']([^"']+)["']""", source, flags=re.IGNORECASE)
        if match:
            return unescape(match.group(1)).strip()
    return ""


def _normalized_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key or "").strip().lower().replace("-", "_"): item for key, item in value.items()}


def _first_value(source: Any, *keys: str) -> Any:
    normalized = _normalized_dict(source)
    for key in keys:
        value = normalized.get(str(key or "").lower().replace("-", "_"))
        if value not in (None, ""):
            return value
    return None


def _safe_int(value: Any) -> int:
    text = str(value or "").strip().lstrip("oO")
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _clean_plain_text(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    source = re.sub(r"<br\s*/?>", "\n", source, flags=re.IGNORECASE)
    source = re.sub(r"<[^>]+>", "", source)
    source = unescape(source)
    return re.sub(r"[ \t\r\f\v]+", " ", source).strip()


def _looks_like_feed(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    normalized = _normalized_dict(value)
    if isinstance(normalized.get("comm"), dict) and isinstance(normalized.get("userinfo"), dict):
        return True
    def present(item: Any) -> bool:
        if isinstance(item, (dict, list, tuple, set)):
            return bool(item)
        return item not in (None, "")

    has_content = any(
        present(normalized.get(key))
        for key in ("html", "feedhtml", "feed_html", "content", "text", "summary", "con")
    )
    has_identity = any(
        present(normalized.get(key))
        for key in ("uin", "hostuin", "opuin", "user_uin", "useruin", "owner_uin", "owneruin", "userinfo")
    )
    has_id = any(
        present(normalized.get(key))
        for key in ("key", "tid", "fid", "topicid", "topic_id", "feedid", "feed_id", "cellid", "id")
    )
    return has_content and has_identity and has_id


_SKIPPED_FEED_BRANCHES = {
    "comment",
    "comments",
    "commentlist",
    "comment_list",
    "reply",
    "replys",
    "replies",
    "likemans",
    "like_list",
    "userinfo",
    "user",
}


def _collect_feed_candidates(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: list[dict[str, Any]] = []
    containers: list[str] = []
    seen: set[int] = set()

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(value, list):
            if value and path:
                containers.append(path)
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]" if path else f"[{index}]", depth + 1)
            return
        if not isinstance(value, dict):
            return
        object_id = id(value)
        if object_id in seen:
            return
        seen.add(object_id)
        if _looks_like_feed(value):
            candidates.append(value)
            return
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower().replace("-", "_")
            if normalized_key in _SKIPPED_FEED_BRANCHES:
                continue
            if isinstance(item, (dict, list)):
                child_path = f"{path}.{normalized_key}" if path else normalized_key
                walk(item, child_path, depth + 1)

    walk(data, "", 0)
    return candidates, list(dict.fromkeys(containers))


def _structured_feed_identity(feed: dict[str, Any], html_content: str) -> tuple[int, str, str]:
    userinfo = _first_value(feed, "userinfo", "user", "author", "owner")
    if not isinstance(userinfo, dict):
        userinfo = {}
    id_info = _first_value(feed, "id", "identity")
    if not isinstance(id_info, dict):
        id_info = {}
    uin = _safe_int(
        _first_value(feed, "uin", "hostuin", "opuin", "user_uin", "useruin", "owner_uin", "owneruin")
        or _first_value(userinfo, "uin", "qq", "id")
        or _html_attr_value(html_content, "data-uin", "data-hostuin", "data-opuin", "uin")
    )
    tid = str(
        _first_value(feed, "key", "tid", "fid", "topicid", "topic_id", "feedid", "feed_id", "cellid")
        or _first_value(id_info, "cellid", "tid", "fid", "key", "id")
        or _html_attr_value(
            html_content,
            "data-key",
            "data-tid",
            "data-fid",
            "data-topicid",
            "data-feedid",
        )
        or ""
    ).strip()
    name = str(
        _first_value(feed, "nickname", "name", "nick")
        or _first_value(userinfo, "nickname", "name", "nick")
        or _html_attr_value(html_content, "data-nickname", "data-name", "data-nick")
        or uin
        or "QQ空间用户"
    ).strip()
    return uin, tid, name


def is_official_qzone_promotion(name: Any) -> bool:
    """Return whether a feed author is the built-in Qzone promotion account."""
    normalized_name = re.sub(r"\s+", "", str(name or "")).casefold()
    return normalized_name == "官方qzone".casefold()


def _structured_feed_common(feed: dict[str, Any]) -> dict[str, Any]:
    common = _first_value(feed, "comm", "common", "cell_comm")
    return common if isinstance(common, dict) else {}


def _structured_feed_text(feed: dict[str, Any], html_content: str) -> tuple[str, str]:
    summary = _first_value(feed, "summary")
    if isinstance(summary, dict):
        text = _clean_plain_text(_first_value(summary, "summary", "content", "text"))
    else:
        text = _clean_plain_text(summary)
    text = text or _clean_plain_text(_first_value(feed, "content", "text", "con"))

    original = _first_value(feed, "original", "forward", "repost", "rt_con")
    rt_con = ""
    if isinstance(original, dict):
        original_summary = _first_value(original, "summary", "content", "text", "con")
        if isinstance(original_summary, dict):
            original_summary = _first_value(original_summary, "summary", "content", "text")
        rt_con = _clean_plain_text(original_summary)
    else:
        rt_con = _clean_plain_text(original)

    if html_content:
        html_text, html_repost, _urls, _items = _parse_feed_html_details(html_content)
        text = text or html_text or _clean_plain_text(html_content)
        rt_con = rt_con or html_repost
    if not text:
        operation = _first_value(feed, "operation")
        share_info = _first_value(operation, "share_info", "shareinfo") if isinstance(operation, dict) else None
        if isinstance(share_info, dict):
            text = _clean_plain_text(_first_value(share_info, "summary", "title"))
    return text, rt_con


def _valid_image_url(value: Any) -> str:
    source = unescape(str(value or "").strip())
    return source if source.lower().startswith(("https://", "http://", "//")) else ""


def _photo_variant_item(value: Any) -> dict[str, str] | None:
    variants: list[tuple[int, str]] = []

    def collect(item: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(item, list):
            for child in item:
                collect(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        direct = _valid_image_url(
            _first_value(
                item,
                "origin_url",
                "original_url",
                "raw_url",
                "large_url",
                "big_url",
                "url3",
                "url2",
                "url1",
                "smallurl",
                "pic_url",
                "coverurl",
                "url",
                "src",
            )
        )
        if direct:
            width = _safe_int(_first_value(item, "width", "w"))
            height = _safe_int(_first_value(item, "height", "h"))
            variants.append((max(1, width * height), direct))
        for child in item.values():
            if isinstance(child, (dict, list)):
                collect(child, depth + 1)

    collect(value)
    if not variants:
        return None
    variants = list(dict.fromkeys(variants))
    preview = min(variants, key=lambda item: item[0])[1]
    full = max(variants, key=lambda item: item[0])[1]
    return {"preview_url": preview, "full_url": full}


def _structured_feed_images(feed: dict[str, Any], html_content: str) -> tuple[list[str], list[dict[str, str]]]:
    items: list[dict[str, str]] = []
    for key in ("pic", "pics", "images", "photos", "video"):
        branch = _first_value(feed, key)
        if branch in (None, ""):
            continue
        if isinstance(branch, dict) and isinstance(_first_value(branch, "picdata", "pic_data"), list):
            sources = list(_first_value(branch, "picdata", "pic_data") or [])
        elif isinstance(branch, list):
            sources = branch
        else:
            sources = [branch]
        for source in sources:
            item = _photo_variant_item(source)
            if item and item not in items:
                items.append(item)
    if html_content:
        _text, _repost, _urls, html_items = _parse_feed_html_details(html_content)
        for item in html_items:
            if item not in items:
                items.append(item)
    return [item["full_url"] for item in items], items


def _balanced_object(source: str, start: int) -> str:
    if start < 0 or start >= len(source) or source[start] != "{":
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return ""


def parse_qzone_h5_index_html(html_content: str) -> dict[str, Any]:
    source = str(html_content or "")
    token_match = re.search(
        r"window\.shine0callback.*?return\s+[\"']([0-9a-zA-Z_-]+?)[\"']\s*;",
        source,
        flags=re.DOTALL,
    )
    token = token_match.group(1).strip() if token_match else ""
    data_match = re.search(r"var\s+FrontPage\s*=.*?\bdata\s*:\s*\{", source, flags=re.DOTALL)
    object_text = _balanced_object(source, data_match.end() - 1) if data_match else ""
    payload: dict[str, Any] = {}
    if object_text:
        try:
            parsed = load_qzone_json(object_text)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    return {"token": token, "payload": payload}


def parse_recent_feeds(data: dict[str, Any], diagnostics: dict[str, Any] | None = None) -> list[Any]:
    feeds, containers = _collect_feed_candidates(data)
    stats: dict[str, Any] = {
        "response_keys": sorted(str(key) for key in data)[:24] if isinstance(data, dict) else [],
        "containers": containers[:12],
        "candidate_count": len(feeds),
        "parsed_count": 0,
        "skipped_official_promotion": 0,
        "skipped_missing_identity": 0,
        "skipped_empty_content": 0,
        "skipped_duplicate": 0,
        "appids": [],
    }
    posts: list[Any] = []
    seen_posts: set[tuple[int, str]] = set()
    appids: set[str] = set()
    for feed in feeds:
        html_content = str(_first_value(feed, "html", "feedhtml", "feed_html", "html_content") or "")
        common = _structured_feed_common(feed)
        uin, tid, name = _structured_feed_identity(feed, html_content)
        if is_official_qzone_promotion(name):
            stats["skipped_official_promotion"] += 1
            continue
        if not uin or not tid:
            stats["skipped_missing_identity"] += 1
            continue
        post_key = (uin, tid)
        if post_key in seen_posts:
            stats["skipped_duplicate"] += 1
            continue
        text, rt_con = _structured_feed_text(feed, html_content)
        image_urls, image_items = _structured_feed_images(feed, html_content)
        if not (text or rt_con or image_urls):
            stats["skipped_empty_content"] += 1
            continue
        seen_posts.add(post_key)
        appid = str(_first_value(feed, "appid") or _first_value(common, "appid") or "311")
        typeid = str(
            _first_value(feed, "typeid", "type", "feedstype")
            or _first_value(common, "typeid", "type", "feedstype")
            or "0"
        )
        abstime = _safe_int(
            _first_value(feed, "abstime", "created_time", "create_time", "timestamp", "pubtime", "time")
            or _first_value(common, "abstime", "created_time", "create_time", "timestamp", "time")
        )
        fid = str(
            _first_value(feed, "fid", "key", "tid")
            or _first_value(_first_value(feed, "id"), "cellid", "fid", "tid")
            or _first_value(common, "ugcrightkey")
            or tid
        )
        unikey = str(
            _first_value(feed, "unikey", "likekey", "like_key", "orglikekey")
            or _first_value(common, "unikey", "likekey", "like_key", "orglikekey")
            or _html_attr_value(html_content, "data-unikey", "unikey")
            or ""
        )
        curkey = str(
            _first_value(feed, "curkey", "curlikekey", "likekey", "like_key")
            or _first_value(common, "curkey", "curlikekey", "likekey", "like_key")
            or _html_attr_value(html_content, "data-curkey", "curkey")
            or ""
        )
        like_info = _first_value(feed, "like")
        liked = bool(_first_value(like_info, "isliked", "liked")) if isinstance(like_info, dict) else False
        appids.add(appid)
        posts.append(
            SimpleNamespace(
                tid=tid,
                uin=uin,
                name=name,
                text=text,
                rt_con=rt_con,
                images=image_urls,
                image_items=image_items,
                comments=[],
                create_time=abstime,
                appid=appid,
                typeid=typeid,
                abstime=abstime,
                fid=fid,
                unikey=unikey,
                curkey=curkey,
                liked=liked,
                raw=feed,
                status="approved",
            )
        )
    stats["parsed_count"] = len(posts)
    stats["appids"] = sorted(appids)[:16]
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(stats)
    return posts
