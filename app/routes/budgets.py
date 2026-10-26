from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..fiscal import fy_end, fy_label, fy_months, fy_of, fy_start, month_start
from ..models import EXPENSE, INCOME, Budget, Category
from ..services import (
    ZERO, actual_by_month, avg_window, category_tree, sum_months, variable_average,
)
from ..web import UserError, get_db, parse_amount, parse_fy, parse_int, redirect, render

router = APIRouter()


def _annual_total(b: Budget | None) -> Decimal:
    return (b.monthly * 12 + b.annual) if b else ZERO


@router.get("/budgets")
def budgets_page(request: Request, fy: str | None = None, db: Session = Depends(get_db)):
    today = date.today()
    fy = parse_fy(fy) or fy_of(today)
    budgets = {b.category_id: b for b in db.scalars(select(Budget).where(Budget.fiscal_year == fy))}
    last_year = sum_months(actual_by_month(db, fy_start(fy - 1), fy_end(fy - 1)), fy_months(fy - 1))
    averages = variable_average(db, month_start(today), avg_window(db))

    sections = []
    for kind in (EXPENSE, INCOME):
        groups, kind_total = [], ZERO
        for top, subs in category_tree(db, kind):
            lines = [{"cat": c, "budget": budgets.get(c.id), "annual_total": _annual_total(budgets.get(c.id)),
                      "last_year": last_year.get(c.id, ZERO), "average": averages.get(c.id, ZERO)}
                     for c in [top] + subs]
            total = sum((line["annual_total"] for line in lines), ZERO)
            kind_total += total
            groups.append({"top": top, "lines": lines, "total": total,
                           "last_year": sum((line["last_year"] for line in lines), ZERO)})
        sections.append({"kind": kind, "groups": groups, "total": kind_total})

    has_previous = db.scalar(select(Budget.id).where(Budget.fiscal_year == fy - 1).limit(1)) is not None
    return render(request, "budgets.html", fy=fy, sections=sections, has_previous=has_previous,
                  window=avg_window(db), is_empty=not budgets)


async def _form(request: Request):
    """The posted form; its fields are named per category (m_<id>, a_<id>, am_<id>)."""
    return await request.form()


@router.post("/budgets")
def save_budgets(form=Depends(_form), db: Session = Depends(get_db)):
    # A plain def, so the database work runs in the threadpool instead of blocking the event loop.
    fy = parse_fy(form.get("fy"))
    if fy is None:
        raise UserError("Unknown budget year.")
    existing = {b.category_id: b for b in db.scalars(select(Budget).where(Budget.fiscal_year == fy))}
    known = set(db.scalars(select(Category.id)))
    ids = {int(k[2:]) for k in form if k.startswith("m_") and k[2:].isdigit()} & known
    for cid in ids:
        monthly = parse_amount(form.get(f"m_{cid}"), allow_zero=True)
        annual = parse_amount(form.get(f"a_{cid}"), allow_zero=True)
        annual_month = parse_int(form.get(f"am_{cid}"))
        if annual_month is not None and not 1 <= annual_month <= 12:
            raise UserError("Please pick a month for the yearly amount.")
        b = existing.get(cid)
        if not monthly and not annual:
            if b:
                db.delete(b)
            continue
        if not b:
            b = Budget(fiscal_year=fy, category_id=cid)
            db.add(b)
        b.monthly, b.annual, b.annual_month = monthly, annual, annual_month
    db.commit()
    return redirect(f"/budgets?fy={fy}", msg=f"Budget for {fy_label(fy)} saved.")


@router.post("/budgets/copy")
def copy_budgets(fy: str = Form(""), db: Session = Depends(get_db)):
    fy = parse_fy(fy)
    if fy is None:
        raise UserError("Unknown budget year.")
    source = list(db.scalars(select(Budget).where(Budget.fiscal_year == fy - 1)))
    for b in db.scalars(select(Budget).where(Budget.fiscal_year == fy)):
        db.delete(b)
    db.flush()
    for b in source:
        db.add(Budget(fiscal_year=fy, category_id=b.category_id, monthly=b.monthly,
                      annual=b.annual, annual_month=b.annual_month))
    db.commit()
    return redirect(f"/budgets?fy={fy}", msg=f"Copied {len(source)} budget lines from {fy_label(fy - 1)}.")
