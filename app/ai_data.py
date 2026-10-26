"""What the AI is told: a compact snapshot of the budget, the two reports, and the
app's own numbers shown next to the AI's as a check.

The snapshot is plain text with CSV-style tables; small local models read that
more reliably than nested JSON, and it keeps the prompt short.
"""
import csv
import io
import json
import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import ai
from .db import SessionLocal
from .fiscal import add_months, fy_label, fy_months, fy_of, month_end, month_start
from .models import (
    EXPENSE, INCOME, AiReport, Asset, Budget, Category, Planned, PlanStatus, Recurring, RunStatus, Transaction,
)
from .recurring import FREQUENCIES, next_occurrence
from .services import ZERO, category_map, forecast, rollup

log = logging.getLogger("budget")

HISTORY_MONTHS = 24
FORECAST_MONTHS = 6


def _n(v) -> str:
    """Amounts as plain numbers without thousands separators: easiest for a model to read."""
    v = Decimal(v or 0)
    if not v:
        return "0"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}"


def _row(*values) -> str:
    """One CSV line; names and notes with commas are quoted so columns stay aligned."""
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(["" if v is None else v for v in values])
    return out.getvalue()


def _pct(v: Decimal) -> str:
    return f"{Decimal(v).normalize():f}"


def forecast_window(today: date) -> list[date]:
    """The next full months, starting with next month."""
    first = add_months(month_start(today), 1)
    return [add_months(first, i) for i in range(FORECAST_MONTHS)]


def _monthly_table(rows: dict[date, dict[int, Decimal]], months: list[date], cats: dict[int, Category],
                   kind: str) -> list[str]:
    tops = sorted({c for m in months for c in rows.get(m, {}) if cats[c].kind == kind},
                  key=lambda c: cats[c].name)
    if not tops:
        return ["(none)"]
    # Months before the first entry are unknown, not zero: leave them out.
    first = min(m for m in months if any(c in tops for c in rows.get(m, {})))
    out = [_row("month", *(cats[c].name for c in tops), "total")]
    if first > months[0]:
        out.insert(0, f"(no entries before {first:%Y-%m})")
    for m in (m for m in months if m >= first):
        vals = [rows.get(m, {}).get(c, ZERO) for c in tops]
        out.append(_row(f"{m:%Y-%m}", *(_n(v) for v in vals), _n(sum(vals, ZERO))))
    return out


def snapshot(db: Session, today: date, for_chat: bool = False) -> str:
    cats = category_map(db)
    current = month_start(today)
    start = add_months(current, -HISTORY_MONTHS)
    months = [add_months(start, i) for i in range(HISTORY_MONTHS + 1)]  # ... up to this month
    regular, one_offs = defaultdict(lambda: defaultdict(Decimal)), []
    entries = list(db.scalars(select(Transaction).where(Transaction.date >= start, Transaction.date <= today)
                              .options(selectinload(Transaction.category), selectinload(Transaction.tags))
                              .order_by(Transaction.date)))
    for t in entries:
        if t.one_off:
            one_offs.append(t)
        else:
            regular[month_start(t.date)][cats[t.category_id].top_id] += t.amount

    fy = fy_of(today)
    lines = [
        f"Today is {today:%A %d %B %Y}. All amounts are Canadian dollars (CAD).",
        f"The budget year runs {fy_months(fy)[0]:%B} to {fy_months(fy)[-1]:%B}; {fy_label(fy)} is the current one.",
        f"{current:%B %Y} is not finished yet: its numbers are only up to today "
        f"(day {today.day} of {month_end(today).day}).",
        "",
        f"## Spending per month by category, last {HISTORY_MONTHS} months (one-off purchases excluded)",
        *_monthly_table(regular, months, cats, EXPENSE),
        "",
        "## One-off purchases and income (big or unusual; not part of normal spending or income)",
        "date,category,type,amount,note",
        *([_row(t.date, cats[t.category_id].full_name, cats[t.category_id].kind, _n(t.amount), t.note)
           for t in one_offs] or ["(none)"]),
        "",
        f"## Income per month by category, last {HISTORY_MONTHS} months",
        *_monthly_table(regular, months, cats, INCOME),
        "",
        "## Recurring items (added automatically on their due dates)",
        "name,category,type,amount,frequency,next due,ends",
    ]
    recurring = list(db.scalars(select(Recurring).where(Recurring.active.is_(True)).order_by(Recurring.name)
                                .options(selectinload(Recurring.skips))))
    for r in recurring:
        nxt = next_occurrence(r, today)
        lines.append(_row(r.name, r.category.full_name, r.category.kind, _n(r.amount),
                          FREQUENCIES[r.frequency][0], nxt or "none", r.end_date))
    if not recurring:
        lines.append("(none)")

    lines += ["", "## Planned future purchases or income", "date,name,category,type,amount"]
    planned = list(db.scalars(select(Planned).where(Planned.status == PlanStatus.PLANNED).order_by(Planned.date)))
    lines += [_row(p.date, p.name, p.category.full_name, p.category.kind, _n(p.amount)) for p in planned] \
        or ["(none)"]

    lines += ["", f"## Budget for {fy_label(fy)} per category", "category,type,per month,per year extra"]
    budgets = list(db.scalars(select(Budget).where(Budget.fiscal_year == fy)))
    lines += [_row(cats[b.category_id].full_name, cats[b.category_id].kind, _n(b.monthly), _n(b.annual))
              for b in budgets if b.monthly or b.annual] or ["(no budget set)"]

    lines += ["", "## Assets and debts (latest value; debts are amounts owed)",
              "name,type,asset or debt,latest value,as of,expected change per year %,earlier values"]
    assets = list(db.scalars(select(Asset).where(Asset.archived.is_(False)).order_by(Asset.name)
                             .options(selectinload(Asset.values))))
    for a in assets:
        latest = a.values[-1] if a.values else None
        earlier = " ".join(f"{v.date:%Y-%m}:{_n(v.value)}" for v in a.values[-7:-1])
        lines.append(_row(a.name, a.type_label, "debt" if a.is_liability else "asset",
                          _n(latest.value) if latest else "unknown", latest.date if latest else "",
                          _pct(a.growth), earlier))
    if not assets:
        lines.append("(none)")

    if for_chat:
        year_ago = today - timedelta(days=365)
        by_cat, by_tag = defaultdict(Decimal), defaultdict(Decimal)
        for t in entries:
            if t.date > year_ago:
                by_cat[t.category_id] += t.amount
                for tag in t.tags:
                    by_tag[(tag.name, t.category.kind)] += t.amount
        lines += ["", "## Totals for the last 12 months by subcategory (including one-offs)",
                  "category,type,total"]
        lines += [_row(cats[c].full_name, cats[c].kind, _n(v))
                  for c, v in sorted(by_cat.items(), key=lambda kv: cats[kv[0]].full_name)] or ["(none)"]
        lines += ["", "## Totals for the last 12 months by tag", "tag,type,total"]
        lines += [_row(name, kind, _n(v)) for (name, kind), v in sorted(by_tag.items())] or ["(none)"]
        lines += ["", "## The 40 most recent entries", "date,category,type,amount,note,tags"]
        lines += [_row(t.date, t.category.full_name, t.category.kind, _n(t.amount), t.note,
                       " ".join(tag.name for tag in t.tags)) for t in entries[-40:]] or ["(none)"]
    return "\n".join(lines)


# --- reports ------------------------------------------------------------------------

_LIST = {"type": "array", "items": {"type": "string"}}

EXPENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "months": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM"},
                "categories": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}, "amount": {"type": "number"}},
                    "required": ["category", "amount"]}},
                "total": {"type": "number"}},
            "required": ["month", "categories", "total"]}},
        "observations": _LIST,
        "recommendations": _LIST,
    },
    "required": ["summary", "months", "observations", "recommendations"],
}

INCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "income_months": {"type": "array", "items": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM"}, "amount": {"type": "number"}},
            "required": ["month", "amount"]}},
        "assets": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "now": {"type": "number"},
                           "in_1_year": {"type": "number"}, "in_5_years": {"type": "number"}},
            "required": ["name", "now", "in_1_year", "in_5_years"]}},
        "net_worth": {"type": "object",
                      "properties": {"now": {"type": "number"}, "in_1_year": {"type": "number"},
                                     "in_5_years": {"type": "number"}},
                      "required": ["now", "in_1_year", "in_5_years"]},
        "observations": _LIST,
        "recommendations": _LIST,
    },
    "required": ["summary", "income_months", "assets", "net_worth", "observations", "recommendations"],
}

SYSTEM = ("You are the financial assistant inside a private household budgeting app for a Canadian "
          "household. You only know the data you are given. Be concrete, use the household's own "
          "category names, and write plainly and briefly. Never invent accounts or numbers that are "
          "not supported by the data.")


def report_prompt(kind: str, today: date) -> str:
    months = ", ".join(f"{m:%Y-%m}" for m in forecast_window(today))
    if kind == "expenses":
        return (
            f"Forecast the household's spending for each of these months: {months}.\n"
            "Work per top-level spending category, using the monthly history, seasonal patterns (the same "
            "month last year), trends, recurring items that fall in each month and planned purchases dated "
            "in that month. Do not repeat one-off purchases unless they are planned again.\n"
            "For each month list every category with its forecast amount and give the month's total, which "
            "must equal the sum of its categories.\n"
            "Then write: summary (2-3 sentences), observations (3-5 short points about trends, seasonality "
            "or unusual spending, with numbers), recommendations (2-4 practical suggestions).")
    return (
        f"Forecast the household's total income for each of these months: {months}, using the income "
        "history, recurring income (pay cheques, benefits) and planned income.\n"
        "Then project every asset and debt listed: its value now, in 1 year and in 5 years, applying its "
        "expected change per year (compounded yearly) and, for debts, any regular payments visible in the "
        "recurring items. Give net worth (assets minus debts) now, in 1 year and in 5 years.\n"
        "Then write: summary (2-3 sentences), observations (3-5 short points with numbers), "
        "recommendations (2-4 practical suggestions about saving, debt and growth).")


def run_report(report_id: int, today: date | None = None) -> None:
    """Runs in a background thread: ask the model, store its answer (or the error)."""
    today = today or date.today()
    with SessionLocal() as db:
        report = db.get(AiReport, report_id)
        url, model = ai.config(db)
        try:
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": snapshot(db, today) + "\n\n## Task\n" + report_prompt(report.kind, today)},
            ]
            schema = EXPENSE_SCHEMA if report.kind == "expenses" else INCOME_SCHEMA
            text = ai.chat(url, model, messages, schema)
            try:
                result = json.loads(text)
            except ValueError:
                raise ai.AiError("The model did not return a readable report. Try again or pick a larger model.")
            report.result = json.dumps(result)
            report.status = RunStatus.DONE
        except ai.AiError as e:
            report.status, report.error = RunStatus.FAILED, str(e)[:500]
        except Exception as e:  # never leave a report "running" forever
            log.exception("AI report %s failed", report_id)
            report.status, report.error = RunStatus.FAILED, f"Unexpected error: {e}"[:500]
        report.finished_at = datetime.now()
        db.commit()


def start_report(db: Session, kind: str) -> AiReport:
    _, model = ai.config(db)
    report = AiReport(kind=kind, model=model or "", status=RunStatus.RUNNING)
    db.add(report)
    db.commit()
    threading.Thread(target=run_report, args=(report.id,), daemon=True, name=f"ai-report-{report.id}").start()
    return report


STALE = timedelta(minutes=20)
INTERRUPTED = "Interrupted (the app restarted while the report was running)."


def fail_interrupted(db: Session) -> None:
    """At startup: no report thread survives a restart, so any run still 'running' is dead."""
    for report in db.scalars(select(AiReport).where(AiReport.status == RunStatus.RUNNING)):
        report.status, report.error, report.finished_at = RunStatus.FAILED, INTERRUPTED, datetime.now()
    db.commit()


def is_stale(report: AiReport, now: datetime | None = None) -> bool:
    """A run that never finished, e.g. because the app restarted while it was working."""
    return report.status == RunStatus.RUNNING and (now or datetime.now()) - report.created_at > STALE


# --- the app's own numbers, for comparison ---------------------------------------------

def app_expense_forecast(db: Session, today: date) -> dict[str, dict[str, Decimal]]:
    """{YYYY-MM: {top category name: amount}} from the app's recurring + average forecast."""
    cats = category_map(db)
    months = forecast_window(today)
    fc = forecast(db, today, months)
    out = {}
    for m in months:
        tops = rollup({c: cell.total for c, cell in fc.cells[m].items() if cats[c].kind == EXPENSE}, cats)
        out[f"{m:%Y-%m}"] = {cats[c].name: v for c, v in tops.items() if v}
    return out


def app_income_forecast(db: Session, today: date) -> dict[str, Decimal]:
    cats = category_map(db)
    months = forecast_window(today)
    fc = forecast(db, today, months)
    return {f"{m:%Y-%m}": sum((cell.total for c, cell in fc.cells[m].items() if cats[c].kind == INCOME), ZERO)
            for m in months}


def app_asset_projection(db: Session) -> list[dict]:
    """Latest value grown at the asset's rate, compounded yearly."""
    rows = []
    for a in db.scalars(select(Asset).where(Asset.archived.is_(False)).order_by(Asset.name)
                        .options(selectinload(Asset.values))):
        if not a.values:
            continue
        now = a.values[-1].value
        factor = 1 + a.growth / 100
        rows.append({"name": a.name, "liability": a.is_liability, "rate": a.growth, "now": now,
                     "in_1_year": now * factor, "in_5_years": now * factor ** 5})
    return rows

