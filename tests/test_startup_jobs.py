"""Background jobs the server starts."""
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "zeus" / "options_analyzer" / "optionstrat_server.py"


def test_signal_backtest_job_is_not_started():
    # It trades SSP1 screener signals, which are not shipped, and has no page:
    # starting it would only download 264 option chains every weekday.
    src = SERVER.read_text(encoding="utf-8")
    assert "async def backtest_scheduler" in src
    assert "create_task(backtest_scheduler())" not in src
