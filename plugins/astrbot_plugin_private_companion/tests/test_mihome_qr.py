"""The MiHome login page only accepts raster data URIs."""

import base64
import unittest

from astrbot_host.native_extensions.reality.mihome_web_api import _build_qr_data_uri


class MiHomeQrTests(unittest.TestCase):
    def test_login_qr_is_a_png_data_uri(self):
        uri = _build_qr_data_uri("https://account.xiaomi.com/login?ticket=abc")

        self.assertTrue(uri.startswith("data:image/png;base64,"), uri[:40])
        payload = base64.b64decode(uri.split(",", 1)[1])
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
