"""SEC 13F-HR filing deadline calendar.

13F-HR must be filed within 45 calendar days of quarter-end.
Official SEC deadlines for 2026/2027 (adjusted for weekends/holidays):

  Q4 2025 → 2026-02-17  (Presidents' Day pushes from 2026-02-14)
  Q1 2026 → 2026-05-15
  Q2 2026 → 2026-08-14
  Q3 2026 → 2026-11-16  (Veterans' Day 2026-11-11)
  Q4 2026 → 2027-02-16  (Presidents' Day 2027)

The hot window starts HOT_BEFORE days before the deadline and ends
HOT_AFTER days after — this is when the intensive fetch workflow runs.
"""
from __future__ import annotations

import datetime

_DEADLINES: list[tuple[datetime.date, datetime.date]] = [
    (datetime.date(2025, 12, 31), datetime.date(2026, 2, 17)),
    (datetime.date(2026, 3, 31),  datetime.date(2026, 5, 15)),
    (datetime.date(2026, 6, 30),  datetime.date(2026, 8, 14)),
    (datetime.date(2026, 9, 30),  datetime.date(2026, 11, 16)),
    (datetime.date(2026, 12, 31), datetime.date(2027, 2, 16)),
]

HOT_BEFORE = 3   # days before deadline
HOT_AFTER  = 2   # days after deadline (late filers)


def in_hot_window(today: datetime.date | None = None) -> bool:
    """Return True if today is within a 13F filing deadline hot window."""
    if today is None:
        today = datetime.date.today()
    for _q_end, deadline in _DEADLINES:
        window_start = deadline - datetime.timedelta(days=HOT_BEFORE)
        window_end   = deadline + datetime.timedelta(days=HOT_AFTER)
        if window_start <= today <= window_end:
            return True
    return False


def next_deadline() -> tuple[datetime.date, datetime.date] | None:
    """Return (quarter_end, deadline) for the next upcoming filing deadline."""
    today = datetime.date.today()
    future = [(qe, d) for qe, d in _DEADLINES if d >= today]
    return min(future, key=lambda x: x[1]) if future else None


def days_until_next_deadline() -> int | None:
    """Days until the next 13F filing deadline, or None if none known."""
    nd = next_deadline()
    if nd is None:
        return None
    return (nd[1] - datetime.date.today()).days
