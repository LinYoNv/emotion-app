# -*- coding: utf-8 -*-
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL_PATHS = (
    ROOT / "pages" / "companion-panel" / "app.js",
    ROOT / "pages" / "陪伴面板" / "app.js",
)
INDEX_PATHS = tuple(path.with_name("index.html") for path in PANEL_PATHS)


def test_persona_style_batch_load_failure_has_a_recovery_path() -> None:
    for path in PANEL_PATHS:
        source = path.read_text(encoding="utf-8")
        assert 'personaStandardizationStyleBatchError: ""' in source
        assert "if (state.personaStandardizationStyleBatchError) return false;" in source
        assert 'return await promiseWithTimeout(action(), 50000, "请求超时，请稍后重试");' in source
        assert "requestError = error;" in source
        assert 'state.personaStandardizationStyleBatchError = message || "请求未完成，请点击重试";' in source
        assert 'state.personaStandardizationStyleBatchError = message || "请求失败，请点击重试";' in source
        assert "重试加载下一组三个情景" in source


def test_persona_style_batch_recovery_asset_is_cache_busted() -> None:
    for path in INDEX_PATHS:
        source = path.read_text(encoding="utf-8")
        assert "persona-style-batch-recovery=v1" in source


def test_persona_style_panel_variants_stay_in_sync() -> None:
    first, second = (path.read_text(encoding="utf-8") for path in PANEL_PATHS)
    assert first == second
