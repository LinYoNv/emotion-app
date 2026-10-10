# -*- coding: utf-8 -*-
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_calendar_is_embedded_in_observation_and_has_full_month_grid() -> None:
    for panel_root in (ROOT / "pages" / "companion-panel", ROOT / "pages" / "陪伴面板"):
        html = (panel_root / "index.html").read_text(encoding="utf-8")
        script = (panel_root / "app.js").read_text(encoding="utf-8")
        css = (panel_root / "app.css").read_text(encoding="utf-8")

        assert 'data-tab="calendar"' not in html
        assert 'id="panel-calendar"' not in html
        assert html.index('id="panel-memory"') < html.index('id="calendarWorkspace"')
        assert html.count('id="calendarMonthGrid"') == 1
        assert 'renderCalendar();' in script[script.index("function renderMemory"):script.index("function foodMenuFeaturePanelHtml")]
        assert 'tabName === "calendar"' in script and 'tabName = "memory"' in script
        assert 'calendar-grid-weekdays' in script
        assert '.calendar-grid-cells' in css
        assert '.calendar-grid-cell.is-today' in css
