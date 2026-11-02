"""网页理解设置：默认可用，但边界（只 http(s)、拒内网）不受这些字段放宽。"""

from __future__ import annotations

import unittest

from companion_v2.configuration.models import ConfigurationError
from companion_v2.conversation.web_understanding import (
    DEFAULTS,
    GROUP,
    configuration_fields,
    validate,
)


class WebUnderstandingSettingsTests(unittest.TestCase):
    def test_defaults_enable_reading_with_bounded_limits(self):
        self.assertTrue(DEFAULTS["enabled"])
        self.assertLessEqual(DEFAULTS["max_links"], 3)
        self.assertLessEqual(DEFAULTS["max_chars"], 8000)
        validate(dict(DEFAULTS))

    def test_switch_must_be_boolean(self):
        for broken in (1, "true", None):
            with self.assertRaises(ConfigurationError):
                validate({**DEFAULTS, "enabled": broken})

    def test_numbers_must_stay_inside_the_declared_range(self):
        for name, value in (("max_links", 0), ("max_links", 9), ("max_chars", 50),
                            ("timeout_seconds", 1), ("timeout_seconds", 60)):
            with self.assertRaises(ConfigurationError):
                validate({**DEFAULTS, name: value})

    def test_unknown_or_missing_fields_are_refused(self):
        with self.assertRaises(ConfigurationError):
            validate({**DEFAULTS, "extra": 1})
        incomplete = dict(DEFAULTS)
        incomplete.pop("max_links")
        with self.assertRaises(ConfigurationError):
            validate(incomplete)

    def test_fields_target_the_window_layer_only(self):
        fields = configuration_fields()
        self.assertTrue(all(field.field_id.startswith(GROUP + ".") for field in fields))
        self.assertEqual({field.layers for field in fields}, {("window",)})


if __name__ == "__main__":
    unittest.main()
