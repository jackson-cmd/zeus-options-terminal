"""The Positions toolbar keeps a way into each panel."""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parent.parent / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")


def test_checkup_panel_has_a_button():
    # the panel used to open only from the red position alert bar
    assert re.search(r'<button[^>]*onclick="openCheckup\(\)"[^>]*id="checkupBtn"', PAGE)
    assert re.search(r"^function openCheckup\(\)", PAGE, re.M)


def test_risk_and_refresh_buttons_still_there():
    for fn in ("openRiskView()", "refreshPositions()", "openAlertPanel()"):
        assert f'onclick="{fn}"' in PAGE


def test_standalone_pages_load_the_language_switch():
    server = (Path(__file__).resolve().parent.parent / "options_analyzer" / "optionstrat_server.py").read_text(encoding="utf-8")
    assert '<script src="/static/zeus.js"></script>' in server.split("_ZEUS_SHELL = ", 1)[1].split("\n", 1)[0]
    for page in ("PRETRADE_CHECKUP_HTML", "JOURNAL_REVIEW_HTML"):
        assert f'{page}.replace("</body>", _ZEUS_SHELL + "</body>", 1)' in server
