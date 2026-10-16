from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from ..fiscal import add_months, fy_end, fy_of, fy_start, month_end, month_start
from ..models import EXPENSE, INCOME, Attachment, Category, Item, Planned, RecurringSkip, Tag, Transaction
from ..services import ZERO, category_choices, tags_from_text
from ..web import (
    UserError, get_db, money, parse_amount, parse_date, parse_int, redirect, render, safe_next,
)

router = APIRouter()


def _category(db: Session, category_id: int | None) -> Category:
    cat = db.get(Category, category_id) if category_id else None
    if not cat:
        raise UserError("Please pick a category.")
    return cat


def _parse_month(text: str | None) -> date:
    try:
        return date.fromisoformat(f"{text}-01") if text else month_start(date.today())
    except ValueError:
        return month_start(date.today())


@router.get("/entries")
def list_entries(request: Request, month: str | None = None, fy: int | None = None,
                 category: str | None = None, tag: str | None = None, q: str = "",
                 db: Session = Depends(get_db)):
    category_id, tag_id = parse_int(category), parse_int(tag)
    if fy is not None:
        start, end = fy_start(fy), fy_end(fy)
        current = None
    else:
        current = _parse_month(month)
        start, end = current, month_end(current)

    query = (select(Transaction)
             .where(Transaction.date >= start, Transaction.date <= end)
             .options(selectinload(Transaction.tags), selectinload(Transaction.category))
             .order_by(Transaction.date.desc(), Transaction.id.desc()))
    if category_id:
        ids = [category_id] + list(db.scalars(select(Category.id).where(Category.parent_id == category_id)))
        query = query.where(Transaction.category_id.in_(ids))
    if tag_id:
        query = query.where(Transaction.tags.any(Tag.id == tag_id))
    if q.strip():
        query = query.where(func.lower(Transaction.note).contains(q.strip().lower()))
    entries = list(db.scalars(query))
    with_files = set(db.scalars(select(Attachment.transaction_id).where(
        Attachment.transaction_id.in_([e.id for e in entries])))) if entries else set()

    by_day = defaultdict(list)
    totals = {EXPENSE: ZERO, INCOME: ZERO}
    for e in entries:
        by_day[e.date].append(e)
        totals[e.kind] += e.amount

    return render(
        request, "entries.html", entries=entries, by_day=sorted(by_day.items(), reverse=True),
        totals=totals, current=current, fy=fy, fy_default=fy_of(date.today()),
        prev_month=add_months(current, -1) if current else None,
        next_month=add_months(current, 1) if current else None,
        category_id=category_id, tag_id=tag_id, q=q, with_files=with_files,
        categories=category_choices(db), tags=list(db.scalars(select(Tag).order_by(Tag.name))),
    )


@router.post("/entries")
def create_entry(amount: str = Form(""), category_id: str = Form(""),
                 entry_date: str = Form("", alias="date"), note: str = Form(""),
                 tags: str = Form(""), one_off: bool = Form(False), next: str = Form("/"),
                 db: Session = Depends(get_db)):
    entry = new_entry(db, amount, category_id, entry_date, note, tags, one_off)
    db.commit()
    return redirect(safe_next(next), msg=f"Added {money(entry.amount)} · {entry.category.full_name}",
                    undo=entry.id)


def new_entry(db: Session, amount: str, category_id: str, entry_date: str, note: str, tags: str,
              one_off: bool) -> Transaction:
    cat = _category(db, parse_int(category_id))
    entry = Transaction(amount=parse_amount(amount), category=cat,
                        date=parse_date(entry_date, date.today()), note=note.strip()[:255],
                        one_off=one_off, tags=tags_from_text(db, tags))
    db.add(entry)
    db.flush()
    return entry


@router.get("/entries/{entry_id}/edit")
def edit_entry(request: Request, entry_id: int, next: str = "/entries", db: Session = Depends(get_db)):
    entry = db.get(Transaction, entry_id)
    if not entry:
        return redirect("/entries", msg="That entry no longer exists.")
    return render(request, "entry_edit.html", entry=entry, next=safe_next(next, "/entries"),
                  categories=category_choices(db),
                  tags=list(db.scalars(select(Tag).order_by(Tag.name))),
                  attachments=list(db.scalars(select(Attachment).where(Attachment.transaction_id == entry.id)
                                              .order_by(Attachment.id))),
                  items=list(db.scalars(select(Item).where(Item.transaction_id == entry.id))),
                  today=date.today())


@router.post("/entries/{entry_id}")
def update_entry(entry_id: int, amount: str = Form(""), category_id: str = Form(""),
                 entry_date: str = Form("", alias="date"), note: str = Form(""),
                 tags: str = Form(""), one_off: bool = Form(False), next: str = Form("/entries"),
                 db: Session = Depends(get_db)):
    entry = db.get(Transaction, entry_id)
    if not entry:
        return redirect("/entries", msg="That entry no longer exists.")
    cat = _category(db, parse_int(category_id))
    entry.amount = parse_amount(amount)
    entry.category_id = cat.id
    entry.date = parse_date(entry_date, entry.date)
    entry.note = note.strip()[:255]
    entry.one_off = one_off
    entry.tags = tags_from_text(db, tags)
    db.commit()
    return redirect(safe_next(next, "/entries"), msg="Entry updated.")


@router.post("/entries/{entry_id}/delete")
def delete_entry(entry_id: int, next: str = Form("/entries"), db: Session = Depends(get_db)):
    entry = db.get(Transaction, entry_id)
    if entry:
        if entry.recurring_id and entry.occurrence_date:
            # Remember the deletion so the recurring item doesn't recreate it.
            db.add(RecurringSkip(recurring_id=entry.recurring_id, date=entry.occurrence_date))
        planned = db.get(Planned, entry.planned_id) if entry.planned_id else None
        if planned and planned.status == "done":
            planned.status = "planned"  # back into the forecast
        # Receipts and warranty items outlive the entry: they become unfiled / standalone.
        kept = db.execute(update(Attachment).where(Attachment.transaction_id == entry.id)
                          .values(transaction_id=None)).rowcount
        db.execute(update(Item).where(Item.transaction_id == entry.id).values(transaction_id=None))
        db.delete(entry)
        db.commit()
        if kept:
            return redirect(safe_next(next, "/entries"),
                            msg=f"Entry deleted. Its {'file was' if kept == 1 else f'{kept} files were'} "
                                "kept under Receipts › Unfiled.")
    return redirect(safe_next(next, "/entries"), msg="Entry deleted.")
