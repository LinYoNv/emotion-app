"""在回复进入发送链路前，只清理“整段一模一样”的自重复。

模型偶尔会把整段话原样写两遍，或者以“格式错了，重来”这类句子收尾。这里只用
两条不会误伤正常表达的判据：

* 整段（或整条消息）恰好是同一段文字的两份拷贝 —— 只有完全相等才合并；
* 最后一行是明确的自我重来说明 —— 只丢这一行，正文不动。

同句里的语气重复、刻意的叠词、对话里的“重来”都不碰。
"""

from __future__ import annotations

import re

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
# 只有明确在说「格式错了 → 重来」的整行才算元话语；裸“重来”可能只是台词。
_META_RETRY = re.compile(
    r"^(?:等等[，,。.！!]?\s*)?(?:格式|格式错|格式错了|说错|打错|写错)[^\n]{0,12}?"
    r"(?:重来|重新来|重发)[。.!！]?$")
_MIN_HALF_CHARS = 6


def _halved(text):
    """整段恰好是同一文字的两份拷贝时返回那一份，否则返回 None。"""
    stripped = text.strip()
    if len(stripped) < _MIN_HALF_CHARS * 2 or len(stripped) % 2:
        return None
    half = len(stripped) // 2
    first, second = stripped[:half], stripped[half:]
    return first if first == second else None


def clean_reply_text(text):
    """返回 (清理后的文本, 移除的片段数)。"""
    if not isinstance(text, str) or not text.strip():
        return text, 0
    removed = 0
    lines = text.split("\n")
    last = next((index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()), None)
    if last is not None and _META_RETRY.match(lines[last].strip()):
        del lines[last]
        removed += 1
    cleaned = "\n".join(lines).strip()
    whole = _halved(cleaned)
    if whole is not None:
        return whole, removed + 1
    paragraphs = _PARAGRAPH_SPLIT.split(cleaned) if cleaned else []
    result = []
    for paragraph in paragraphs:
        half = _halved(paragraph)
        if half is not None:
            removed += 1
            result.append(half)
            continue
        if result and paragraph.strip() and paragraph.strip() == result[-1].strip():
            removed += 1
            continue
        result.append(paragraph)
    return "\n\n".join(result).strip(), removed
