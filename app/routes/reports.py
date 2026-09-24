from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..fiscal import (
    fiscal_month_index, fiscal_quarter, fy_label, fy_months, fy_of, fy_start, month_end, month_start,
    quarter_label,
)
from ..models import EXPENSE, INCOME, Planned, Tag
from ..services import (
    ZERO, actual_by_month, avg_window, budget_by_month, build_rows, category_map, forecast, forecast_months,
    kind_total, liquid_balance, rollup, sum_months,
)
from ..web import get_db, parse_int, render

router = APIRouter()

VIEWS = {"month": 12, "quarter": 4, "year": 1}


def _period(view: str, fy: int, n: int) -> tuple[list[date], str]:
    months = fy_months(fy)
    if view == "month":
        return [months[n - 1]], months[n - 1].strftime("%B %Y")
    if view == "quarter":
        return months[(n - 1) * 3 : n * 3], quarter_label(fy, n)
    return months, f"{fy_label(fy)} ({months[0]:%b %Y} – {months[-1]:%b %Y})"


def _step(view: str, fy: int, n: int, delta: int) -> tuple[int, int]:
    count = VIEWS[view]
    idx = fy * count + (n - 1) + delta
    return idx // count, idx % count + 1


def _projection(db: Session, today: date, months: list[date], actual_bm) -> dict[int, object] | None:
    """Actuals for closed months + forecast for the current and future months."""
    current = month_start(today)
    open_months = [m for m in months if m >= current]
    if not open_months:
        return None
    projected = sum_months(actual_bm, [m for m in months if m < current])
    fc = forecast(db, today, open_months)
    for m in open_months:
        for cid, cell in fc.cells[m].items():
            projected[cid] += cell.total
    return projected


@router.get("/reports")
def reports(request: Request, view: str = "month", fy: int | None = None, n: int | None = None,
            tag: str | None = None, db: Session = Depends(get_db)):
    today = date.today()
    current = month_start(today)
    view = view if view in VIEWS else "month"
    fy = fy if fy is not None else fy_of(today)
    if n is None:
        n = {"month": fiscal_month_index(today), "quarter": fiscal_quarter(today), "year": 1}[view] \
            if fy == fy_of(today) else 1
    n = min(max(n, 1), VIEWS[view])
    tag_id = parse_int(tag)

    months, label = _period(view, fy, n)
    cats = category_map(db)
    actual_bm = actual_by_month(db, months[0], month_end(months[-1]), tag_id)
    actual = sum_months(actual_bm, months)
    columns = {"actual": actual}
    budget_bm = {}
    projected = None
    if not tag_id:  # budgets and forecasts don't apply to a tag slice
        budget_bm = budget_by_month(db, months)
        columns["budget"] = sum_months(budget_bm, months)
        projected = _projection(db, today, months, actual_bm)
        if projected is not None:
            columns["projected"] = projected

    expense_rows, expense_totals = build_rows(db, EXPENSE, columns)
    income_rows, income_totals = build_rows(db, INCOME, columns)
    income, spent = income_totals["actual"], expense_totals["actual"]
    summary = {
        "income": income, "spent": spent, "net": income - spent,
        "savings_rate": float((income - spent) / income * 100) if income else None,
        "budget": expense_totals.get("budget"), "projected": expense_totals.get("projected"),
        "income_budget": income_totals.get("budget"),
    }

    charts = {}
    top_actual = rollup({c: v for c, v in actual.items() if cats[c].kind == EXPENSE}, cats)
    if top_actual:
        ordered = sorted(top_actual.items(), key=lambda kv: kv[1], reverse=True)
        charts["breakdown"] = {"type": "doughnut", "labels": [cats[c].name for c, _ in ordered],
                               "datasets": [{"label": "Spent", "data": [v for _, v in ordered]}]}
    if expense_rows:
        datasets = [{"label": "Actual", "role": "actual",
                     "data": [r["vals"]["actual"] for r in expense_rows]}]
        if "budget" in columns:
            datasets.insert(0, {"label": "Budget", "role": "budget",
                                "data": [r["vals"]["budget"] for r in expense_rows]})
        if "projected" in columns:
            datasets.append({"label": "Projected", "role": "forecast",
                             "data": [r["vals"]["projected"] for r in expense_rows]})
        charts["vs_budget"] = {"type": "bar", "horizontal": True,
                               "labels": [r["cat"].name for r in expense_rows], "datasets": datasets}

    trend = []
    if len(months) > 1:
        fc = forecast(db, today, [m for m in months if m >= current]) if not tag_id else None
        for m in months:
            a = actual_bm.get(m, {})
            point = {"month": m, "income": kind_total(a, cats, INCOME),
                     "spent": kind_total(a, cats, EXPENSE),
                     "budget": kind_total(budget_bm.get(m, {}), cats, EXPENSE),
                     "projected_spend": None}
            if fc and m in fc.cells:
                point["projected_spend"] = kind_total(
                    {c: cell.total for c, cell in fc.cells[m].items()}, cats, EXPENSE)
            trend.append(point)
        datasets = [
            {"label": "Income", "role": "income", "data": [p["income"] for p in trend]},
            {"label": "Spent", "role": "expense", "stack": "spend", "data": [p["spent"] for p in trend]},
        ]
        if any(p["projected_spend"] is not None for p in trend):
            datasets.append({"label": "Forecast (remaining)", "role": "forecast", "stack": "spend",
                             "data": [max(p["projected_spend"] - p["spent"], ZERO)
                                      if p["projected_spend"] is not None else None for p in trend]})
        if not tag_id:
            datasets.append({"label": "Budget", "role": "budget", "type": "line",
                             "data": [p["budget"] for p in trend]})
        charts["trend"] = {"type": "bar", "stacked": True, "labels": [p["month"].strftime("%b %y") for p in trend],
                           "datasets": datasets}

    prev, nxt = _step(view, fy, n, -1), _step(view, fy, n, 1)
    return render(
        request, "reports.html", view=view, fy=fy, n=n, label=label, months=months,
        tag_id=tag_id, tags=list(db.scalars(select(Tag).order_by(Tag.name))),
        prev=prev, next=nxt, columns=list(columns), summary=summary,
        expense_rows=expense_rows, expense_totals=expense_totals,
        income_rows=income_rows, income_totals=income_totals,
        charts=charts, trend=trend, is_open=projected is not None,
    )


@router.get("/forecast")
def forecast_page(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    current = month_start(today)
    cats = category_map(db)
    months = forecast_months(today)
    fc = forecast(db, today, months)
    budgets = budget_by_month(db, months)

    rows = []
    balance = start_balance = liquid_balance(db, today)
    cumulative = ZERO
    for m in months:
        parts = defaultdict(lambda: ZERO)
        income = remaining_net = ZERO
        for cid, cell in fc.cells[m].items():
            if cats[cid].kind == INCOME:
                income += cell.total
                remaining_net += cell.total - cell.actual
            else:
                parts["actual"] += cell.actual
                parts["recurring"] += cell.recurring
                parts["variable"] += cell.variable
                parts["planned"] += cell.planned
                remaining_net -= cell.total - cell.actual
        spend = sum(parts.values(), ZERO)
        cumulative += remaining_net
        if balance is not None:
            balance += remaining_net
        rows.append({"month": m, "income": income, "spend": spend, "parts": dict(parts),
                     "budget": kind_total(budgets[m], cats, EXPENSE), "net": income - spend,
                     "cumulative": cumulative, "balance": balance, "current": m == current})

    # Projected fiscal-year totals vs budget, per category
    this_fy, next_fy = fy_of(today), fy_of(today) + 1
    past = [m for m in fy_months(this_fy) if m < current]
    actual_past = sum_months(actual_by_month(db, fy_start(this_fy), today), past)
    fy_budgets = budget_by_month(db, fy_months(this_fy) + fy_months(next_fy))
    columns = {"this_budget": sum_months(fy_budgets, fy_months(this_fy)),
               "this_projected": defaultdict(lambda: ZERO, actual_past),
               "next_budget": sum_months(fy_budgets, fy_months(next_fy)),
               "next_projected": defaultdict(lambda: ZERO)}
    for m in months:
        key = "this_projected" if fy_of(m) == this_fy else "next_projected"
        for cid, cell in fc.cells[m].items():
            columns[key][cid] += cell.total
    expense_rows, expense_totals = build_rows(db, EXPENSE, columns)
    income_rows, income_totals = build_rows(db, INCOME, columns)

    # Month x top-level expense category matrix
    matrix_rows = []
    by_month_top = {m: rollup({c: cell.total for c, cell in fc.cells[m].items()}, cats) for m in months}
    tops = sorted({c for m in months for c in by_month_top[m] if cats[c].kind == EXPENSE},
                  key=lambda c: cats[c].name)
    for c in tops:
        matrix_rows.append({"cat": cats[c], "vals": [by_month_top[m].get(c, ZERO) for m in months]})

    window = avg_window(db)
    charts = {"monthly": {
        "type": "bar", "stacked": True, "labels": [r["month"].strftime("%b %y") for r in rows],
        "datasets": [
            {"label": "Spent so far", "role": "actual", "stack": "spend",
             "data": [r["parts"].get("actual", ZERO) for r in rows]},
            {"label": "Recurring", "role": "recurring", "stack": "spend",
             "data": [r["parts"].get("recurring", ZERO) for r in rows]},
            {"label": f"Typical spending ({window}-mo avg)", "role": "variable", "stack": "spend",
             "data": [r["parts"].get("variable", ZERO) for r in rows]},
            {"label": "Planned purchases", "role": "planned", "stack": "spend",
             "data": [r["parts"].get("planned", ZERO) for r in rows]},
            {"label": "Income", "role": "income", "type": "line", "data": [r["income"] for r in rows]},
            {"label": "Budget", "role": "budget", "type": "line", "data": [r["budget"] for r in rows]},
        ]}}
    if start_balance is not None:
        charts["balance"] = {"type": "line", "labels": [r["month"].strftime("%b %y") for r in rows],
                             "datasets": [{"label": "Projected bank & cash", "role": "net",
                                           "data": [r["balance"] for r in rows]}]}
    else:
        charts["balance"] = {"type": "line", "labels": [r["month"].strftime("%b %y") for r in rows],
                             "datasets": [{"label": "Cumulative savings", "role": "net",
                                           "data": [r["cumulative"] for r in rows]}]}

    planned = list(db.scalars(select(Planned).where(Planned.status == "planned").order_by(Planned.date)))
    return render(
        request, "forecast.html", rows=rows, months=months, charts=charts, planned=planned,
        start_balance=start_balance, this_fy=this_fy, next_fy=next_fy,
        expense_rows=expense_rows, expense_totals=expense_totals,
        income_rows=income_rows, income_totals=income_totals,
        matrix_rows=matrix_rows, window=window, today=today,
    )
