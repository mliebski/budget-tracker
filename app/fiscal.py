"""Date helpers for months and the fiscal (budget) year.

A fiscal year is identified by the calendar year it starts in: with the default
start month of October, FY 2025 runs from 1 Oct 2025 to 30 Sep 2026.
"""
import calendar
import os
from datetime import date

FY_START_MONTH = int(os.environ.get("FY_START_MONTH", "10"))


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    y, m = divmod(idx, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_between(a: date, b: date) -> int:
    """Whole calendar months from a's month to b's month."""
    return (b.year - a.year) * 12 + b.month - a.month


def month_range(start: date, end: date) -> list[date]:
    """First-of-month dates from start's month to end's month, inclusive."""
    first = month_start(start)
    return [add_months(first, i) for i in range(months_between(first, end) + 1)]


def fy_of(d: date) -> int:
    return d.year if d.month >= FY_START_MONTH else d.year - 1


def fy_start(fy: int) -> date:
    return date(fy, FY_START_MONTH, 1)


def fy_end(fy: int) -> date:
    return month_end(add_months(fy_start(fy), 11))


def fy_months(fy: int) -> list[date]:
    return [add_months(fy_start(fy), i) for i in range(12)]


def fy_label(fy: int) -> str:
    if FY_START_MONTH == 1:
        return str(fy)
    return f"FY {fy}–{(fy + 1) % 100:02d}"


def fiscal_month_index(d: date) -> int:
    """1-based position of d's month inside its fiscal year."""
    return months_between(fy_start(fy_of(d)), d) + 1


def fiscal_quarter(d: date) -> int:
    return (fiscal_month_index(d) - 1) // 3 + 1


def quarter_label(fy: int, q: int) -> str:
    months = fy_months(fy)[(q - 1) * 3 : q * 3]
    return f"Q{q} {fy_label(fy)} ({months[0]:%b}–{months[-1]:%b})"


def fy_month_numbers() -> list[int]:
    """Calendar month numbers in fiscal order, e.g. [10, 11, 12, 1, ..., 9]."""
    return [m.month for m in fy_months(2000)]
