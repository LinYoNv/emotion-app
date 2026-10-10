# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from importlib import import_module
from typing import Any


class QzoneJsonDecodeError(ValueError):
    """QQ 空间接口返回了当前无法识别的 JavaScript 对象。"""


def _skip_ignored(source: str, index: int) -> int:
    """跳过空白和 JavaScript 注释，返回下一个有效字符的位置。"""
    length = len(source)
    while index < length:
        if source[index].isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            return length if newline < 0 else _skip_ignored(source, newline + 1)
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            return length if end < 0 else _skip_ignored(source, end + 2)
        break
    return index


def _quoted_string(source: str, start: int) -> tuple[str, int]:
    """把 JavaScript 单/双引号字符串转换为严格 JSON 字符串。"""
    quote = source[start]
    chars: list[str] = []
    index = start + 1
    escapes = {
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    while index < len(source):
        char = source[index]
        if char == quote:
            return json.dumps("".join(chars), ensure_ascii=True), index + 1
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        index += 1
        if index >= len(source):
            break
        escaped = source[index]
        if escaped == "\r" or escaped == "\n":
            if escaped == "\r" and index + 1 < len(source) and source[index + 1] == "\n":
                index += 1
            index += 1
            continue
        if escaped == "x" and index + 2 < len(source):
            digits = source[index + 1 : index + 3]
            try:
                chars.append(chr(int(digits, 16)))
                index += 3
                continue
            except ValueError:
                pass
        if escaped == "u" and index + 4 < len(source):
            digits = source[index + 1 : index + 5]
            try:
                codepoint = int(digits, 16)
                next_escape = source[index + 5 : index + 11]
                if 0xD800 <= codepoint <= 0xDBFF and next_escape.startswith("\\u"):
                    low = int(next_escape[2:], 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
                        index += 6
                chars.append(chr(codepoint))
                index += 5
                continue
            except ValueError:
                pass
        if escaped == "0" and (index + 1 >= len(source) or not source[index + 1].isdigit()):
            chars.append("\0")
        else:
            chars.append(escapes.get(escaped, escaped))
        index += 1
    raise QzoneJsonDecodeError("接口响应包含未闭合的字符串")


def _javascript_object_to_json(source: str) -> str:
    """将 QQ 空间常见的 JSON5 子集转换为严格 JSON。"""
    output: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in {'"', "'"}:
            value, index = _quoted_string(source, index)
            output.append(value)
            continue
        if source.startswith("//", index) or source.startswith("/*", index):
            next_index = _skip_ignored(source, index)
            output.append(" ")
            index = next_index
            continue
        if char == ",":
            next_index = _skip_ignored(source, index + 1)
            if next_index < length and source[next_index] in "}]":
                index += 1
                continue
        if char in "+-" and source.startswith("Infinity", index + 1):
            end = index + 9
            if end >= length or not (source[end].isalnum() or source[end] in "_$"):
                output.append("null")
                index = end
                continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            token = source[index:end]
            next_index = _skip_ignored(source, end)
            if next_index < length and source[next_index] == ":":
                output.append(json.dumps(token, ensure_ascii=True))
            elif token in {"undefined", "NaN", "Infinity"}:
                output.append("null")
            else:
                output.append(token)
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def load_qzone_json(payload: str) -> Any:
    """解析 QQ 空间返回；json5 仅作为可选的最后回退。"""
    source = str(payload or "")
    try:
        return json.loads(source, parse_constant=lambda _value: None)
    except (TypeError, ValueError):
        pass

    try:
        normalized = _javascript_object_to_json(source)
        return json.loads(normalized, parse_constant=lambda _value: None)
    except (TypeError, ValueError) as relaxed_error:
        try:
            json5 = import_module("json5")
        except (ImportError, ModuleNotFoundError):
            raise QzoneJsonDecodeError("接口响应格式暂不受支持") from relaxed_error
        try:
            return json5.loads(source)
        except Exception as json5_error:
            raise QzoneJsonDecodeError("接口响应格式暂不受支持") from json5_error
