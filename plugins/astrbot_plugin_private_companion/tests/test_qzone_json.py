# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from astrbot_plugin_private_companion.qzone_integration import QzoneMixin
from astrbot_plugin_private_companion.qzone_json import QzoneJsonDecodeError, load_qzone_json
from astrbot_plugin_private_companion.qzone_recent_parser import parse_qzone_h5_index_html


class QzoneJsonTests(unittest.TestCase):
    def test_common_javascript_object_syntax_works_without_json5(self) -> None:
        payload = """
        {
          code: 0,
          data: {
            text: '含有 undefined 的文本 \\uD83D\\uDE00',
            enabled: true,
            missing: undefined,
            score: NaN,
          }, // QQ 空间接口可能包含行注释
        }
        """

        with patch("astrbot_plugin_private_companion.qzone_json.import_module") as optional_import:
            parsed = load_qzone_json(payload)

        optional_import.assert_not_called()
        self.assertEqual(0, parsed["code"])
        self.assertEqual("含有 undefined 的文本 😀", parsed["data"]["text"])
        self.assertTrue(parsed["data"]["enabled"])
        self.assertIsNone(parsed["data"]["missing"])
        self.assertIsNone(parsed["data"]["score"])

    def test_missing_optional_json5_never_leaks_module_error(self) -> None:
        missing = ModuleNotFoundError("No module named 'json5'")
        with patch("astrbot_plugin_private_companion.qzone_json.import_module", side_effect=missing):
            with self.assertRaisesRegex(QzoneJsonDecodeError, "接口响应格式暂不受支持") as raised:
                load_qzone_json("{value: function () { return 1; }}")
            parsed = QzoneMixin._qzone_parse_response("callback({value: function () { return 1; }});")

        self.assertNotIn("json5", str(raised.exception))
        self.assertEqual(-1, parsed["code"])
        self.assertNotIn("json5", parsed["message"])

    def test_h5_parser_uses_same_relaxed_parser(self) -> None:
        html = """
        <script>
        window.shine0callback = function () { return 'token-123'; };
        var FrontPage = new Loader({data: {
          code: 0,
          data: {name: '好友动态', items: [1, 2,],},
        }});
        </script>
        """

        parsed = parse_qzone_h5_index_html(html)

        self.assertEqual("token-123", parsed["token"])
        self.assertEqual("好友动态", parsed["payload"]["data"]["name"])
        self.assertEqual([1, 2], parsed["payload"]["data"]["items"])


if __name__ == "__main__":
    unittest.main()
