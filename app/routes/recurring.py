from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..models import EXPENSE, Recurring, Transaction
from ..recurring import FREQUENCIES, ensure_up_to_date, monthly_equivalent, next_occurrence, skip_past
from ..services import ZERO, all_tags, category_choices, tags_from_text
from ..web import (
    UserError, get_db, parse_amount, parse_category, parse_date, parse_optional_date, redirect, render,
)

router = APIRouter()


def _apply_form(db: Session, r: Recurring, name, amount, category_id, frequency, start_date, end_date, tags):
    cat = parse_category(db, category_id)
    if frequency not in FREQUENCIES:
        raise UserError("Please pick how often it repeats.")
    r.name = name.strip()[:100] or cat.name
    r.amount = parse_amount(amount)
    r.category_id = cat.id
    r.frequency = frequency
    r.start_date = parse_date(start_date, date.today())
    r.end_date = parse_optional_date(end_date)
    if r.end_date and r.end_date < r.start_date:
        raise UserError("The end date is before the start date.")
    r.tags = tags_from_text(db, tags)


@router.get("/recurring")
def recurring_list(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    items = list(db.scalars(select(Recurring).order_by(Recurring.active.desc(), Recurring.name)
                            .options(selectinload(Recurring.category), selectinload(Recurring.skips))))
    rows = []
    totals = {"expense": ZERO, "income": ZERO}
    for r in items:
        per_month = monthly_equivalent(r.amount, r.frequency)
        ended = r.end_date is not None and r.end_date < today
        if r.active and not ended:
            totals[r.category.kind] += per_month
        rows.append({"r": r, "per_month": per_month, "ended": ended,
                     "next": next_occurrence(r, today) if r.active else None})
    return render(request, "recurring.html", rows=rows, totals=totals)


@router.get("/recurring/new")
def recurring_new(request: Request, db: Session = Depends(get_db)):
    r = Recurring(name="", amount=None, frequency="monthly", start_date=date.today(), active=True)
    return render(request, "recurring_form.html", r=r, kind=EXPENSE, categories=category_choices(db),
                  tags=all_tags(db))


@router.get("/recurring/{rid}")
def recurring_edit(request: Request, rid: int, db: Session = Depends(get_db)):
    r = db.get(Recurring, rid)
    if not r:
        return redirect("/recurring", msg="That recurring item no longer exists.")
    recent = list(db.scalars(select(Transaction).where(Transaction.recurring_id == rid)
                             .order_by(Transaction.date.desc()).limit(12)))
    return render(request, "recurring_form.html", r=r, kind=r.category.kind, recent=recent,
                  categories=category_choices(db), tags=all_tags(db))


@router.post("/recurring")
def recurring_create(name: str = Form(""), amount: str = Form(""), category_id: str = Form(""),
                     frequency: str = Form("monthly"), start_date: str = Form(""),
                     end_date: str = Form(""), tags: str = Form(""), db: Session = Depends(get_db)):
    r = Recurring(active=True)
    _apply_form(db, r, name, amount, category_id, frequency, start_date, end_date, tags)
    db.add(r)
    db.commit()
    ensure_up_to_date(db, force=True)
    return redirect("/recurring", msg=f"Added recurring item “{r.name}”.")


@router.post("/recurring/{rid}")
def recurring_update(rid: int, name: str = Form(""), amount: str = Form(""), category_id: str = Form(""),
                     frequency: str = Form("monthly"), start_date: str = Form(""),
                     end_date: str = Form(""), tags: str = Form(""), db: Session = Depends(get_db)):
    r = db.get(Recurring, rid)
    if not r:
        return redirect("/recurring", msg="That recurring item no longer exists.")
    schedule = (r.frequency, r.start_date, r.end_date)
    _apply_form(db, r, name, amount, category_id, frequency, start_date, end_date, tags)
    if (r.frequency, r.start_date, r.end_date) != schedule:
        # A new schedule applies from today on; don't back-fill past dates.
        skip_past(db, r, date.today())
    db.commit()
    ensure_up_to_date(db, force=True)
    return redirect("/recurring", msg=f"Saved “{r.name}”. Past entries were left unchanged.")


@router.post("/recurring/{rid}/toggle")
def recurring_toggle(rid: int, db: Session = Depends(get_db)):
    r = db.get(Recurring, rid)
    if r:
        if not r.active:
            skip_past(db, r, date.today())  # nothing is back-filled for the paused period
        r.active = not r.active
        db.commit()
        ensure_up_to_date(db, force=True)
        return redirect("/recurring", msg=f"“{r.name}” {'resumed' if r.active else 'paused'}.")
    return redirect("/recurring")


@router.post("/recurring/{rid}/delete")
def recurring_delete(rid: int, db: Session = Depends(get_db)):
    r = db.get(Recurring, rid)
    if r:
        # Keep the entries already created, but treat them as one-offs so these
        # fixed costs don't leak into the "typical spending" average.
        db.execute(update(Transaction).where(Transaction.recurring_id == rid)
                   .values(recurring_id=None, occurrence_date=None, one_off=True))
        db.delete(r)
        db.commit()
    return redirect("/recurring", msg="Recurring item deleted. Past entries were kept.")
