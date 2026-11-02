# -*- coding: utf-8 -*-
"""为 mijiaAPI 提供 RC4 兼容实现，避免原生扩展缺失阻断米家登录。

``mijiaAPI 4.1.3`` 只依赖 ``Crypto.Cipher.ARC4`` 处理小米账号协议里的
RC4 混淆。某些 AstrBot/Python 组合会出现 pycryptodome 原生模块加载失败，
即使运行时其他依赖都正常。这里优先继续使用 pycryptodome；只有原生模块
不可用时才注入同接口的纯 Python RC4 实现。RC4 仅用于复现上游协议，不得
用于本插件自己的加密或安全边界。
"""

from __future__ import annotations

import sys
import types


_FALLBACK_MODULE_NAME = "Crypto.Cipher.ARC4"
_FALLBACK_KEY_SIZES = (1, 256)


class ARC4Cipher:
    """与 pycryptodome ``ARC4.new()`` 兼容的有状态 RC4 流。"""

    block_size = 1

    def __init__(self, key, drop: int = 0):
        raw_key = bytes(key)
        if not _FALLBACK_KEY_SIZES[0] <= len(raw_key) <= _FALLBACK_KEY_SIZES[1]:
            raise ValueError("Incorrect ARC4 key length")
        if type(drop) is not int or drop < 0:
            raise ValueError("Incorrect ARC4 drop value")
        state = list(range(256))
        index = 0
        for position in range(256):
            index = (index + state[position] + raw_key[position % len(raw_key)]) & 0xFF
            state[position], state[index] = state[index], state[position]
        self._state = state
        self._i = 0
        self._j = 0
        self.key_size = len(raw_key)
        if drop:
            self.encrypt(b"\x00" * drop)

    def encrypt(self, plaintext):
        output = bytearray(len(plaintext))
        state, i, j = self._state, self._i, self._j
        for position, value in enumerate(plaintext):
            i = (i + 1) & 0xFF
            j = (j + state[i]) & 0xFF
            state[i], state[j] = state[j], state[i]
            output[position] = value ^ state[(state[i] + state[j]) & 0xFF]
        self._i, self._j = i, j
        return bytes(output)

    def decrypt(self, ciphertext):
        return self.encrypt(ciphertext)


def new(key, *args, **kwargs):
    """Create a stateful RC4 cipher, matching pycryptodome's public surface."""

    if len(args) > 1:
        raise TypeError("new() takes at most 2 arguments")
    drop = args[0] if args else kwargs.pop("drop", 0)
    if kwargs:
        raise TypeError(f"unexpected keyword argument: {next(iter(kwargs))}")
    return ARC4Cipher(key, drop)


def _fallback_module():
    module = types.ModuleType(_FALLBACK_MODULE_NAME)
    module.__doc__ = "Bounded pure-Python ARC4 fallback for mijiaAPI."
    module.ARC4Cipher = ARC4Cipher
    module.block_size = 1
    module.key_size = _FALLBACK_KEY_SIZES
    module.new = new
    return module


def _install_fallback():
    crypto = sys.modules.get("Crypto")
    if crypto is None:
        crypto = types.ModuleType("Crypto")
        crypto.__path__ = []
        sys.modules["Crypto"] = crypto
    cipher = sys.modules.get("Crypto.Cipher")
    if cipher is None:
        cipher = types.ModuleType("Crypto.Cipher")
        cipher.__path__ = []
        sys.modules["Crypto.Cipher"] = cipher
        setattr(crypto, "Cipher", cipher)
    arc4 = _fallback_module()
    sys.modules[_FALLBACK_MODULE_NAME] = arc4
    setattr(cipher, "ARC4", arc4)
    return arc4


def ensure_arc4_compat(*, force_fallback: bool = False) -> str:
    """Ensure ``from Crypto.Cipher import ARC4`` is usable.

    Returns the native failure type when the fallback was installed, or ``""``
    when the native pycryptodome module already works. The return value is
    deliberately a short class name so callers can log it without exposing
    environment paths or import-trace details.
    """

    native_error = ""
    if not force_fallback:
        try:
            from Crypto.Cipher import ARC4 as native_arc4

            native_arc4.new(b"companion-arc4-probe").encrypt(b"\x00")
            return ""
        except Exception as exc:  # noqa: BLE001 - any native load failure is recoverable here
            native_error = type(exc).__name__
    try:
        fallback = _install_fallback()
        fallback.new(b"companion-arc4-probe").encrypt(b"\x00")
    except Exception as exc:  # pragma: no cover - defensive bootstrap failure
        raise RuntimeError("ARC4 compatibility fallback is unavailable") from exc
    return native_error or "forced"
