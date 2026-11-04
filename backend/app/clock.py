"""Stable calendar for the bundled ledgers.

The sample databases end in early November 2026, which is the hackathon week.
Questions such as "last quarter" are answered against this clock so a demo
on a later day still hits the rows the agent is meant to find.
"""

from __future__ import annotations

from datetime import date, timedelta

LEDGER_TODAY = date(2026, 11, 4)


def _quarter_start(year: int, quarter: int) -> date:
    return date(year, (quarter - 1) * 3 + 1, 1)


def previous_quarter(today: date | None = None) -> tuple[date, date, str]:
    today = today or LEDGER_TODAY
    quarter = (today.month - 1) // 3 + 1
    year = today.year
    if quarter == 1:
        year -= 1
        quarter = 4
    else:
        quarter -= 1
    start = _quarter_start(year, quarter)
    end = _quarter_start(year + 1, 1) if quarter == 4 else _quarter_start(year, quarter + 1)
    last_day = end - timedelta(days=1)
    label = f"Q{quarter} {year} ({start.isoformat()} to {last_day.isoformat()})"
    return start, end, label


def previous_month(today: date | None = None) -> tuple[date, date, str]:
    today = today or LEDGER_TODAY
    first_this = date(today.year, today.month, 1)
    end = first_this
    start = date(first_this.year - 1, 12, 1) if first_this.month == 1 else date(first_this.year, first_this.month - 1, 1)
    label = start.strftime("%B %Y")
    return start, end, label


def calendar_year(today: date | None = None) -> tuple[date, date, str]:
    today = today or LEDGER_TODAY
    start = date(today.year, 1, 1)
    end = date(today.year + 1, 1, 1)
    return start, end, str(today.year)
