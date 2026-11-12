"""
Minimal NYSE trading-day check — no external deps.

Hard-coded holiday list for 2025-2030. Add years as needed.
Source: nyse.com/markets/hours-calendars (US market full-day closures).
"""

from __future__ import annotations

import datetime as dt

# Full-day market closures (no early-close days listed here — those still trade)
NYSE_HOLIDAYS: set[dt.date] = {
    # 2025
    dt.date(2025, 1, 1),    # New Year's Day
    dt.date(2025, 1, 9),    # National Day of Mourning (Carter)
    dt.date(2025, 1, 20),   # MLK Day
    dt.date(2025, 2, 17),   # Presidents' Day
    dt.date(2025, 4, 18),   # Good Friday
    dt.date(2025, 5, 26),   # Memorial Day
    dt.date(2025, 6, 19),   # Juneteenth
    dt.date(2025, 7, 4),    # Independence Day
    dt.date(2025, 9, 1),    # Labor Day
    dt.date(2025, 11, 27),  # Thanksgiving
    dt.date(2025, 12, 25),  # Christmas
    # 2026
    dt.date(2026, 1, 1),    # New Year's Day
    dt.date(2026, 1, 19),   # MLK Day
    dt.date(2026, 2, 16),   # Presidents' Day
    dt.date(2026, 4, 3),    # Good Friday
    dt.date(2026, 5, 25),   # Memorial Day
    dt.date(2026, 6, 19),   # Juneteenth
    dt.date(2026, 7, 3),    # Independence Day (observed; Jul 4 is Sat)
    dt.date(2026, 9, 7),    # Labor Day
    dt.date(2026, 11, 26),  # Thanksgiving
    dt.date(2026, 12, 25),  # Christmas
    # 2027
    dt.date(2027, 1, 1),
    dt.date(2027, 1, 18),
    dt.date(2027, 2, 15),
    dt.date(2027, 3, 26),
    dt.date(2027, 5, 31),
    dt.date(2027, 6, 18),   # Juneteenth observed (Jun 19 is Sat)
    dt.date(2027, 7, 5),    # Jul 4 is Sun → observed Mon
    dt.date(2027, 9, 6),
    dt.date(2027, 11, 25),
    dt.date(2027, 12, 24),  # Dec 25 is Sat → observed Fri
    # 2028
    dt.date(2028, 1, 17),
    dt.date(2028, 2, 21),
    dt.date(2028, 4, 14),
    dt.date(2028, 5, 29),
    dt.date(2028, 6, 19),
    dt.date(2028, 7, 4),
    dt.date(2028, 9, 4),
    dt.date(2028, 11, 23),
    dt.date(2028, 12, 25),
    # 2029
    dt.date(2029, 1, 1),
    dt.date(2029, 1, 15),
    dt.date(2029, 2, 19),
    dt.date(2029, 3, 30),
    dt.date(2029, 5, 28),
    dt.date(2029, 6, 19),
    dt.date(2029, 7, 4),
    dt.date(2029, 9, 3),
    dt.date(2029, 11, 22),
    dt.date(2029, 12, 25),
    # 2030
    dt.date(2030, 1, 1),
    dt.date(2030, 1, 21),
    dt.date(2030, 2, 18),
    dt.date(2030, 4, 19),
    dt.date(2030, 5, 27),
    dt.date(2030, 6, 19),
    dt.date(2030, 7, 4),
    dt.date(2030, 9, 2),
    dt.date(2030, 11, 28),
    dt.date(2030, 12, 25),
}


def is_trading_day(date: dt.date) -> bool:
    """True if NYSE is open on `date`."""
    if date.weekday() >= 5:  # Sat (5) or Sun (6)
        return False
    return date not in NYSE_HOLIDAYS
