"""ProviderRequest 的 OpenAI 内容组装；只依赖宿主及 Python 标准库。"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger("astrbot")


class MediaContentUnavailable(ValueError):
    """当前端口无法无损提供该媒体输入。"""


def _unavailable(reason: str) -> None:
    logger.error("provider media unavailable: %s", reason)
    raise MediaContentUnavailable(f"media unavailable: {reason}")


def _audio_part(locator: str) -> dict[str, Any]:
    if not isinstance(locator, str) or not locator:
        _unavailable("audio requires a wav/mp3 data URL or local file")
    if locator.startswith("data:"):
        header, separator, encoded = locator.partition(",")
        formats = {
            "data:audio/wav;base64": "wav", "data:audio/x-wav;base64": "wav",
            "data:audio/wave;base64": "wav", "data:audio/mpeg;base64": "mp3",
            "data:audio/mp3;base64": "mp3",
        }
        audio_format = formats.get(header.lower())
        if not separator or not audio_format:
            _unavailable("audio data URL must contain base64 wav/mp3")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _unavailable("audio data URL contains invalid base64")
    else:
        parsed = urlsplit(locator)
        if parsed.scheme and parsed.scheme != "file":
            _unavailable("remote audio requires downloading before wav/mp3 input")
        if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
            _unavailable("audio file URL must refer to a local file")
        path = Path(unquote(parsed.path) if parsed.scheme == "file" else locator)
        audio_format = path.suffix.lower().lstrip(".")
        if audio_format not in ("wav", "mp3"):
            _unavailable("local audio requires wav/mp3; other formats need conversion")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _unavailable(f"local audio cannot be read ({type(exc).__name__})")
    if not payload:
        _unavailable("audio input is empty")
    return {"type": "input_audio", "input_audio": {
        "data": base64.b64encode(payload).decode("ascii"), "format": audio_format,
    }}


def to_media_part(kind: str, locator: str) -> dict[str, Any]:
    """把图片或 wav/mp3 定位符转换为真实 OpenAI 输入，不执行下载/转码。"""
    if kind == "audio":
        return _audio_part(locator)
    if kind != "image":
        _unavailable("media kind requires explicit image/audio support; video needs conversion")
    if not isinstance(locator, str) or not locator:
        _unavailable("image requires an HTTP(S)/data URL or local file")
    if locator.startswith(("http://", "https://", "data:image/")):
        return {"type": "image_url", "image_url": {"url": locator}}
    parsed = urlsplit(locator)
    if parsed.scheme and parsed.scheme != "file":
        _unavailable("image requires an HTTP(S)/data URL or local file")
    if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
        _unavailable("image file URL must refer to a local file")
    path = Path(unquote(parsed.path) if parsed.scheme == "file" else locator)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower())
    if not mime:
        _unavailable("local image format requires conversion to png/jpeg/gif/webp")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _unavailable(f"local image cannot be read ({type(exc).__name__})")
    if not payload:
        _unavailable("image input is empty")
    encoded = base64.b64encode(payload).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _content_part(part: Any) -> dict[str, Any]:
    if not isinstance(part, dict):
        dump = getattr(part, "model_dump", None)
        if not callable(dump):
            _unavailable("content part must be an object or provide model_dump")
        part = dump()
    if not isinstance(part, dict):
        _unavailable("content part dump must be an object")
    out = deepcopy(part)
    out.pop("_no_save", None)
    kind = out.get("type")
    if kind == "audio_url":
        _unavailable("audio_url is not an OpenAI chat input; supply wav/mp3 input_audio")
    if kind in ("video", "video_url", "video_data", "input_video"):
        _unavailable("video requires explicit frame/audio conversion")
    if kind == "input_audio":
        audio = out.get("input_audio")
        if not isinstance(audio, dict) or audio.get("format") not in ("wav", "mp3"):
            _unavailable("input_audio requires wav/mp3 format")
        try:
            decoded = base64.b64decode(audio.get("data", ""), validate=True)
        except (TypeError, ValueError, binascii.Error):
            _unavailable("input_audio contains invalid base64")
        if not decoded:
            _unavailable("input_audio data is empty")
    return out


def _message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        dump = getattr(message, "to_openai", None)
        if not callable(dump):
            dump = getattr(message, "model_dump", None)
        if not callable(dump):
            _unavailable("context message must provide an OpenAI object")
        message = dump()
    out = deepcopy(message)
    if isinstance(out.get("content"), list):
        out["content"] = [_content_part(part) for part in out["content"]]
    return out


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _replace_prompt(content: Any, prompt: str | None) -> Any:
    text = prompt or ""
    if not isinstance(content, list):
        return text
    if content_text(content) == text:
        return content
    out = []
    inserted = False
    for part in content:
        if part.get("type") == "text":
            if not inserted:
                out.append({**part, "text": text})
                inserted = True
        else:
            out.append(part)
    if not inserted and text:
        out.insert(0, {"type": "text", "text": text})
    return out


def content_media_locators(content: Any) -> tuple[list[str], list[str]]:
    """给原生钩子镜像当前 multipart 的媒体定位符，保留重复项。"""
    images: list[str] = []
    audios: list[str] = []
    for original in content if isinstance(content, list) else []:
        part = _content_part(original)
        if part.get("type") == "image_url":
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                _unavailable("image_url requires an image URL object")
            images.append(image["url"])
        elif part.get("type") == "input_audio":
            audio = part["input_audio"]
            mime = "audio/wav" if audio["format"] == "wav" else "audio/mpeg"
            audios.append(f"data:{mime};base64,{audio['data']}")
    return images, audios


def _media_key(part: dict[str, Any]) -> tuple | None:
    if part.get("type") == "image_url":
        image = part.get("image_url")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            return "image", image["url"]
    elif part.get("type") == "input_audio":
        audio = part.get("input_audio")
        if isinstance(audio, dict):
            return "audio", audio.get("format"), audio.get("data")
    return None


def provider_request_to_messages(
    request: Any, *, current_user_index: int | None = None, include_system: bool = True,
) -> list[dict[str, Any]]:
    """历史模式追加本轮；已有本轮模式仅回写该 user，原媒体顺序不变。"""
    messages = [_message(item) for item in (getattr(request, "contexts", None) or [])]
    results = getattr(request, "tool_calls_result", None)
    if results is not None:
        for block in results if isinstance(results, (list, tuple)) else [results]:
            if block is None:
                continue
            try:
                messages.extend(_message(item) for item in block.to_openai_messages())
            except Exception:
                logger.error("provider tool result conversion failed", exc_info=True)
                raise
    prompt = getattr(request, "prompt", None)
    media = [to_media_part("image", url) for url in (getattr(request, "image_urls", None) or [])]
    media.extend(to_media_part("audio", url) for url in (getattr(request, "audio_urls", None) or []))
    if current_user_index is not None:
        if not (0 <= current_user_index < len(messages)) or messages[current_user_index].get("role") != "user":
            raise ValueError("current_user_index must identify a user message")
        current = messages[current_user_index]
        current["content"] = _replace_prompt(current.get("content"), prompt)
        content = current["content"]
        if isinstance(content, list):
            from collections import Counter

            mirrored = Counter(key for part in content if (key := _media_key(part)) is not None)
            appended = []
            for part in media:
                key = _media_key(part)
                if mirrored[key]:
                    mirrored[key] -= 1
                else:
                    appended.append(part)
            media = appended
    else:
        current = {"role": "user", "content": prompt or ""}
        if prompt or media:
            messages.append(current)
    if media:
        content = current.get("content")
        parts = content if isinstance(content, list) else ([{"type": "text", "text": content}] if content else [])
        current["content"] = [*parts, *media]
    extra = [_content_part(part) for part in (getattr(request, "extra_user_content_parts", None) or [])]
    if extra:
        extra_content = ("".join(part["text"] for part in extra)
                         if all(part.get("type") == "text" and set(part) == {"type", "text"}
                                and isinstance(part["text"], str) for part in extra) else extra)
        messages.append({"role": "user", "content": extra_content})
    system = getattr(request, "system_prompt", None)
    if include_system and system:
        messages.insert(0, {"role": "system", "content": system})
    return messages
