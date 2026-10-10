from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from astrbot_plugin_private_companion.tts_enhancement import TtsEnhancementMixin


ROOT = Path(__file__).resolve().parents[1]


class _ConfigHarness(TtsEnhancementMixin):
    @staticmethod
    def _cfg_raw(config: dict[str, Any], key: str, default: Any = None) -> Any:
        return config.get(key, default)

    @staticmethod
    def _cfg_str(config: dict[str, Any], key: str, default: str = "", fallback: str = "") -> str:
        value = config.get(key, default)
        return fallback if value is None or not str(value).strip() else str(value)

    @staticmethod
    def _cfg_bool(config: dict[str, Any], key: str, default: bool = True) -> bool:
        value = config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        return bool(value)

    @staticmethod
    def _cfg_int(config: dict[str, Any], key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        return min(maximum, value) if maximum is not None else value

    @staticmethod
    def _cfg_float(config: dict[str, Any], key: str, default: float, minimum: float = 0.0) -> float:
        try:
            value = float(config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    @staticmethod
    def _parse_text_list_config(value: Any, *, limit: int = 80) -> list[str]:
        return [item.strip() for item in str(value or "").split(",") if item.strip()][:limit]


class TtsDefaultLanguageTests(unittest.TestCase):
    def test_missing_or_invalid_config_defaults_to_chinese(self) -> None:
        harness = _ConfigHarness()
        harness._load_tts_enhancement_config({})
        self.assertEqual("zh", harness.tts_voice_language)
        self.assertEqual("zh", harness._tts_voice_language_for_event())
        self.assertEqual("中文", harness._tts_language_label())

        harness._load_tts_enhancement_config({"tts_voice_language": "unsupported"})
        self.assertEqual("zh", harness.tts_voice_language)

    def test_explicit_japanese_config_remains_supported(self) -> None:
        harness = _ConfigHarness()
        harness._load_tts_enhancement_config({"tts_voice_language": "ja"})
        self.assertEqual("ja", harness.tts_voice_language)

    def test_schema_defaults_are_chinese(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual("zh", schema["tts_voice_language"]["default"])
        self.assertEqual("zh", schema["voice_action_config"]["items"]["tts_voice_language"]["default"])


if __name__ == "__main__":
    unittest.main()
