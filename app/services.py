"""Budget, actual, forecast and net-worth calculations.

Data volumes for a household are small (thousands of rows), so aggregation is
done in Python; that keeps the SQL portable between MariaDB and SQLite.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .fiscal import add_months, fy_end, fy_of, month_end, month_range, month_start, months_between
from .models import (
    EXPENSE, INCOME, LIQUID_TYPES, Asset, AssetValue, Budget, Category, Item, Planned, Recurring,
    Setting, Tag, Transaction,
)
from .recurring import occurrences

ZERO = Decimal("0")

DEFAULT_SETTINGS = {"avg_months": "6", "warranty_days": "60", "ai_url": "", "ai_model": ""}


# --- settings -----------------------------------------------------------------

def get_setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS[key]


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def avg_window(db: Session) -> int:
    return int(get_setting(db, "avg_months"))


def warranty_days(db: Session) -> int:
    """How many days before a warranty ends it shows up on the Home page."""
    return int(get_setting(db, "warranty_days"))


def warranties_ending(db: Session, today: date) -> list[Item]:
    hi = today + timedelta(days=warranty_days(db))
    return list(db.scalars(select(Item).where(Item.warranty_end >= today, Item.warranty_end <= hi)
                           .order_by(Item.warranty_end)))


# --- categories ---------------------------------------------------------------

def category_map(db: Session) -> dict[int, Category]:
    return {c.id: c for c in db.scalars(select(Category))}


def category_tree(db: Session, kind: str | None = None, include_archived: bool = False):
    """[(top_category, [subcategories])] sorted by name."""
    cats = [c for c in db.scalars(select(Category).order_by(Category.name))
            if (include_archived or not c.archived) and (kind is None or c.kind == kind)]
    tops = [c for c in cats if c.parent_id is None]
    return [(t, [c for c in cats if c.parent_id == t.id]) for t in tops]


def category_choices(db: Session) -> list[dict]:
    """Flat list for the JS category picker."""
    out = []
    for kind in (EXPENSE, INCOME):
        for top, subs in category_tree(db, kind):
            out.append({"id": top.id, "name": top.name, "kind": kind, "group": top.name, "top": True,
                        "has_children": bool(subs)})
            out += [{"id": s.id, "name": s.name, "kind": kind, "group": top.name, "top": False}
                    for s in subs]
    return out


def recent_categories(db: Session, today: date, per_kind: int = 8) -> list[Category]:
    rows = db.execute(
        select(Transaction.category_id, func.count())
        .where(Transaction.date >= today - timedelta(days=90), Transaction.recurring_id.is_(None))
        .group_by(Transaction.category_id).order_by(func.count().desc())
    ).all()
    cats = category_map(db)
    out, counts = [], defaultdict(int)
    for cid, _ in rows:
        c = cats.get(cid)
        if c and not c.archived and counts[c.kind] < per_kind:
            out.append(c)
            counts[c.kind] += 1
    return out


def tags_from_text(db: Session, text: str) -> list[Tag]:
    names = []
    for raw in (text or "").split(","):
        name = " ".join(raw.split())[:50]
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    existing = {t.name.lower(): t for t in db.scalars(select(Tag))}
    tags = []
    for name in names:
        tag = existing.get(name.lower())
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            existing[name.lower()] = tag
        tags.append(tag)
    return tags


# --- actuals & budgets ----------------------------------------------------------

def actual_by_month(db: Session, start: date, end: date, tag_id: int | None = None):
    """{month_start: {category_id: total}} for entries dated within [start, end]."""
    q = select(Transaction.date, Transaction.category_id, Transaction.amount).where(
        Transaction.date >= start, Transaction.date <= end)
    if tag_id:
        q = q.where(Transaction.tags.any(Tag.id == tag_id))
    out = defaultdict(lambda: defaultdict(Decimal))
    for d, cid, amount in db.execute(q):
        out[month_start(d)][cid] += amount
    return out


def budget_for_month(b: Budget, month: date) -> Decimal:
    amount = b.monthly or ZERO
    if b.annual:
        if b.annual_month is None:
            amount += b.annual / 12
        elif b.annual_month == month.month:
            amount += b.annual
    return amount


def budget_by_month(db: Session, months: list[date]):
    """{month_start: {category_id: budget}}."""
    fys = {fy_of(m) for m in months}
    rows = defaultdict(list)
    for b in db.scalars(select(Budget).where(Budget.fiscal_year.in_(fys))):
        rows[b.fiscal_year].append(b)
    out = {}
    for m in months:
        out[m] = defaultdict(Decimal)
        for b in rows[fy_of(m)]:
            out[m][b.category_id] += budget_for_month(b, m)
    return out


def sum_months(by_month, months) -> dict[int, Decimal]:
    total = defaultdict(Decimal)
    for m in months:
        for cid, v in by_month.get(m, {}).items():
            total[cid] += v
    return total


def variable_average(db: Session, month: date, window: int) -> dict[int, Decimal]:
    """Average monthly spend per category over the `window` full months before `month`,
    excluding recurring, planned and one-off entries. Uses fewer months while history is short."""
    first = db.scalar(select(func.min(Transaction.date)))
    if not first:
        return {}
    n = min(window, months_between(first, month))
    if n <= 0:
        return {}
    q = select(Transaction.category_id, Transaction.amount).where(
        Transaction.date >= add_months(month, -n), Transaction.date < month,
        Transaction.recurring_id.is_(None), Transaction.planned_id.is_(None),
        Transaction.one_off.is_(False))
    total = defaultdict(Decimal)
    for cid, amount in db.execute(q):
        total[cid] += amount
    return {cid: v / n for cid, v in total.items()}


# --- forecast -------------------------------------------------------------------

@dataclass
class Cell:
    actual: Decimal = ZERO
    recurring: Decimal = ZERO
    variable: Decimal = ZERO
    planned: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.actual + self.recurring + self.variable + self.planned


@dataclass
class Forecast:
    months: list[date]
    cells: dict = field(default_factory=dict)  # {month: {category_id: Cell}}
    averages: dict = field(default_factory=dict)

    def total(self, month: date, cid: int) -> Decimal:
        cell = self.cells.get(month, {}).get(cid)
        return cell.total if cell else ZERO


def forecast(db: Session, today: date, months: list[date]) -> Forecast:
    """Projected totals per month and category.

    Past months: actuals. Current month: actuals so far + remaining recurring +
    open planned items (including overdue ones) + the variable average scaled to
    the days left. Future months: recurring + planned + variable average.
    """
    current = month_start(today)
    fc = Forecast(months=months, averages=variable_average(db, current, avg_window(db)))
    if not months:
        return fc
    actuals = actual_by_month(db, months[0], min(month_end(months[-1]), today))
    recurring = list(db.scalars(select(Recurring).where(Recurring.active.is_(True))))
    skips = {r.id: {s.date for s in r.skips} for r in recurring}
    planned = list(db.scalars(select(Planned).where(Planned.status == "planned")))

    for m in months:
        cells = defaultdict(Cell)
        fc.cells[m] = cells
        if m <= current:
            for cid, v in actuals.get(m, {}).items():
                cells[cid].actual = v
        if m < current:
            continue
        lo = today + timedelta(days=1) if m == current else m
        end = month_end(m)
        for r in recurring:
            for d in occurrences(r.start_date, r.frequency, r.end_date, lo, end):
                if d not in skips[r.id]:
                    cells[r.category_id].recurring += r.amount
        for p in planned:
            if p.date <= end and (p.date >= m or m == current):
                cells[p.category_id].planned += p.amount
        if m == current:
            fraction = Decimal(end.day - today.day) / end.day
        else:
            fraction = Decimal(1)
        for cid, avg in fc.averages.items():
            cells[cid].variable += avg * fraction
    return fc


def forecast_months(today: date) -> list[date]:
    """Rest of the current fiscal year plus the whole next one."""
    return month_range(today, fy_end(fy_of(today) + 1))


# --- report rows ----------------------------------------------------------------

def build_rows(db: Session, kind: str, columns: dict[str, dict[int, Decimal]], show_all=False):
    """Category rows (top level with nested subcategories) for a table.

    Returns (rows, totals); each row is {"cat", "vals", "children"} where vals
    maps column name -> amount. A top row includes its subcategories.
    """
    names = list(columns)
    rows = []
    totals = {n: ZERO for n in names}
    for top, subs in category_tree(db, kind, include_archived=True):
        children = []
        for c in [top] + subs:
            values = {n: columns[n].get(c.id, ZERO) for n in names}
            if any(values.values()) or (show_all and not c.archived):
                children.append({"cat": c, "vals": values, "own": c is top})
        if not children:
            continue
        values = {n: sum((ch["vals"][n] for ch in children), ZERO) for n in names}
        # Only list subrows when there is a real breakdown.
        if len(children) == 1 and children[0]["own"]:
            children = []
        rows.append({"cat": top, "vals": values, "children": children})
        for n in names:
            totals[n] += values[n]
    return rows, totals


def rollup(values: dict[int, Decimal], cats: dict[int, Category]) -> dict[int, Decimal]:
    out = defaultdict(Decimal)
    for cid, v in values.items():
        c = cats.get(cid)
        if c:
            out[c.top_id] += v
    return out


def kind_total(values: dict[int, Decimal], cats: dict[int, Category], kind: str) -> Decimal:
    return sum((v for cid, v in values.items() if cid in cats and cats[cid].kind == kind), ZERO)


# --- upcoming -------------------------------------------------------------------

def upcoming(db: Session, today: date, days: int = 30) -> list[dict]:
    hi = today + timedelta(days=days)
    items = []
    for r in db.scalars(select(Recurring).where(Recurring.active.is_(True))):
        skipped = {s.date for s in r.skips}
        for d in occurrences(r.start_date, r.frequency, r.end_date, today + timedelta(days=1), hi):
            if d not in skipped:
                items.append({"date": d, "name": r.name, "amount": r.amount, "category": r.category,
                              "source": "recurring", "link": f"/recurring/{r.id}"})
    for p in db.scalars(select(Planned).where(Planned.status == "planned", Planned.date <= hi)):
        items.append({"date": p.date, "name": p.name, "amount": p.amount, "category": p.category,
                      "source": "planned", "link": "/planned", "overdue": p.date < today})
    return sorted(items, key=lambda i: i["date"])


# --- assets ---------------------------------------------------------------------

def asset_values_at(assets: list[Asset], as_of: date) -> dict[int, AssetValue]:
    """Latest recorded value on or before as_of for each asset."""
    out = {}
    for a in assets:
        latest = None
        for v in a.values:
            if v.date <= as_of:
                latest = v
        if latest:
            out[a.id] = latest
    return out


def net_worth(assets: list[Asset], as_of: date) -> dict[str, Decimal]:
    by_id = {a.id: a for a in assets}
    vals = asset_values_at(assets, as_of)
    total_assets = sum((v.value for aid, v in vals.items() if not by_id[aid].is_liability), ZERO)
    total_liab = sum((v.value for aid, v in vals.items() if by_id[aid].is_liability), ZERO)
    return {"assets": total_assets, "liabilities": total_liab, "net": total_assets - total_liab}


def net_worth_series(db: Session, today: date) -> list[dict]:
    assets = list(db.scalars(select(Asset)))
    first = db.scalar(select(func.min(AssetValue.date)))
    if not first:
        return []
    series = []
    for m in month_range(first, today):
        point = net_worth(assets, min(month_end(m), today))
        point["month"] = m
        series.append(point)
    return series


def liquid_balance(db: Session, today: date) -> Decimal | None:
    assets = list(db.scalars(select(Asset).where(Asset.type.in_(LIQUID_TYPES),
                                                 Asset.archived.is_(False))))
    vals = asset_values_at(assets, today)
    return sum((v.value for v in vals.values()), ZERO) if vals else None
