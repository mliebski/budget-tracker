"""Recurring items: schedule maths and turning due occurrences into entries."""
import threading
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .fiscal import add_months, months_between
from .models import Recurring, RecurringSkip, Transaction

# key -> (label, step in days, step in months)
FREQUENCIES = {
    "weekly": ("Weekly", 7, None),
    "biweekly": ("Every 2 weeks", 14, None),
    "monthly": ("Monthly", None, 1),
    "quarterly": ("Every 3 months", None, 3),
    "semiannual": ("Every 6 months", None, 6),
    "annual": ("Yearly", None, 12),
}
PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}


def occurrences(start: date, frequency: str, end: date | None, lo: date, hi: date) -> list[date]:
    """Scheduled dates within [lo, hi] for a series starting on `start`."""
    last = min(hi, end) if end else hi
    if last < lo or last < start:
        return []
    _, days, months = FREQUENCIES[frequency]
    out = []
    if days:
        k = max(0, (lo - start).days // days)
        d = start + timedelta(days=k * days)
        while d <= last:
            if d >= lo:
                out.append(d)
            k += 1
            d = start + timedelta(days=k * days)
    else:
        # Always step from the start date so a 31st doesn't drift to the 28th.
        k = max(0, months_between(start, lo) // months - 1)
        while (d := add_months(start, k * months)) <= last:
            if d >= lo:
                out.append(d)
            k += 1
    return out


def next_occurrence(r: Recurring, after: date) -> date | None:
    found = occurrences(r.start_date, r.frequency, r.end_date, after + timedelta(days=1),
                        after + timedelta(days=400))
    skipped = {s.date for s in r.skips}
    return next((d for d in found if d not in skipped), None)


def monthly_equivalent(amount: Decimal, frequency: str) -> Decimal:
    return amount * PER_YEAR[frequency] / 12


def _skipped(db: Session, rid: int) -> set[date]:
    return set(db.scalars(select(RecurringSkip.date).where(RecurringSkip.recurring_id == rid)))


def skip_past(db: Session, r: Recurring, today: date) -> None:
    """Mark every not-yet-generated occurrence up to today as skipped.

    Used when resuming a paused item or changing a schedule, so the change only
    affects the future instead of back-filling old entries.
    """
    existing = set(db.scalars(select(Transaction.occurrence_date)
                              .where(Transaction.recurring_id == r.id)))
    skipped = _skipped(db, r.id)
    for d in occurrences(r.start_date, r.frequency, r.end_date, r.start_date, today):
        if d not in existing and d not in skipped:
            r.skips.append(RecurringSkip(date=d))


def materialize(db: Session, today: date) -> int:
    """Create entries for all due occurrences up to and including today."""
    created = 0
    for r in db.scalars(select(Recurring).where(Recurring.active.is_(True))):
        existing = set(db.scalars(select(Transaction.occurrence_date)
                                  .where(Transaction.recurring_id == r.id)))
        skipped = _skipped(db, r.id)
        for d in occurrences(r.start_date, r.frequency, r.end_date, r.start_date, today):
            if d in existing or d in skipped:
                continue
            db.add(Transaction(date=d, amount=r.amount, category_id=r.category_id, note=r.name,
                               recurring_id=r.id, occurrence_date=d, tags=list(r.tags)))
            created += 1
    if created:
        try:
            db.commit()
        except IntegrityError:  # another request generated them at the same moment
            db.rollback()
            return 0
    return created


_lock = threading.Lock()
_last_run: date | None = None


def ensure_up_to_date(db: Session, today: date | None = None, force: bool = False) -> None:
    """Materialize due occurrences at most once per day (or immediately when forced)."""
    global _last_run
    today = today or date.today()
    with _lock:
        if force or _last_run != today:
            materialize(db, today)
            _last_run = today
