from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..fiscal import fy_months, fy_of, fy_start, month_end, month_start
from ..models import EXPENSE, INCOME, Tag, Transaction
from ..services import (
    actual_by_month, budget_by_month, build_rows, category_choices, category_map, forecast,
    kind_total, recent_categories, sum_months, upcoming, warranties_ending,
)
from ..web import get_db, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    current, fy = month_start(today), fy_of(today)
    cats = category_map(db)
    months = fy_months(fy)
    fc = forecast(db, today, [m for m in months if m >= current])
    budgets = budget_by_month(db, months)
    actuals = actual_by_month(db, fy_start(fy), today)

    # This month, per expense category
    m_actual, m_budget = actuals.get(current, {}), budgets[current]
    m_projected = {cid: cell.total for cid, cell in fc.cells[current].items()}
    rows, totals = build_rows(db, EXPENSE, {"actual": m_actual, "budget": m_budget,
                                            "projected": m_projected})
    month = {
        "rows": rows, "totals": totals,
        "income": kind_total(m_actual, cats, INCOME),
        "income_projected": kind_total(m_projected, cats, INCOME),
        "income_budget": kind_total(m_budget, cats, INCOME),
        "elapsed": today.day / month_end(today).day * 100,
    }

    # Fiscal year so far and projected to year end
    past = [m for m in months if m < current]
    projected = sum_months(actuals, past)
    for m in fc.months:
        for cid, cell in fc.cells[m].items():
            projected[cid] = projected.get(cid, 0) + cell.total
    fy_actual, fy_budget = sum_months(actuals, months), sum_months(budgets, months)
    year = {
        "fy": fy,
        "spent": kind_total(fy_actual, cats, EXPENSE),
        "earned": kind_total(fy_actual, cats, INCOME),
        "budget": kind_total(fy_budget, cats, EXPENSE),
        "budget_to_date": kind_total(sum_months(budgets, past + [current]), cats, EXPENSE),
        "projected_spend": kind_total(projected, cats, EXPENSE),
        "projected_income": kind_total(projected, cats, INCOME),
    }

    recent = list(db.scalars(select(Transaction)
                             .options(selectinload(Transaction.category), selectinload(Transaction.tags))
                             .order_by(Transaction.id.desc()).limit(8)))
    return render(
        request, "dashboard.html", today=today, month=month, year=year, recent=recent,
        upcoming=upcoming(db, today), warranties=warranties_ending(db, today),
        categories=category_choices(db),
        recent_categories=recent_categories(db, today),
        tags=list(db.scalars(select(Tag).order_by(Tag.name))),
    )
