"""Focused contracts for the Xiaomi RC4 compatibility fallback."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import unittest

from astrbot_host.native_extensions.reality.mihome_arc4_compat import (
    _fallback_module,
    ensure_arc4_compat,
)


ROOT = Path(__file__).resolve().parents[1]


class Arc4FallbackTests(unittest.TestCase):
    def test_pure_python_fallback_matches_the_standard_rc4_vector(self):
        arc4 = _fallback_module()
        cipher = arc4.new(b"Key")
        self.assertEqual(cipher.encrypt(b"Plaintext").hex(), "bbf316e8d940af0ad3")

    def test_streaming_calls_keep_their_rc4_position(self):
        arc4 = _fallback_module()
        cipher = arc4.new(b"Key")
        self.assertEqual(
            (cipher.encrypt(b"Plain") + cipher.encrypt(b"text")).hex(),
            "bbf316e8d940af0ad3",
        )

    def test_native_module_stays_usable_when_available(self):
        reason = ensure_arc4_compat()
        self.assertIsInstance(reason, str)
        from Crypto.Cipher import ARC4

        self.assertEqual(ARC4.new(b"Key").encrypt(b"Plaintext").hex(), "bbf316e8d940af0ad3")

    def test_forced_fallback_is_importable_as_crypto_cipher_arc4(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from astrbot_host.native_extensions.reality.mihome_arc4_compat import ensure_arc4_compat\n"
            "assert ensure_arc4_compat(force_fallback=True) == 'forced'\n"
            "from Crypto.Cipher import ARC4\n"
            "assert ARC4.new(b'Key').encrypt(b'Plaintext').hex() == 'bbf316e8d940af0ad3'\n"
            "print('ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
