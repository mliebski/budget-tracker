from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Attachment, Category, Item, Planned, Transaction
from ..services import ZERO, category_choices
from ..web import UserError, get_db, money, parse_amount, parse_date, parse_int, redirect, render

router = APIRouter()


def _category(db: Session, category_id: str) -> Category:
    cat = db.get(Category, parse_int(category_id) or 0)
    if not cat:
        raise UserError("Please pick a category.")
    return cat


@router.get("/planned")
def planned_list(request: Request, edit: int | None = None, db: Session = Depends(get_db)):
    items = list(db.scalars(select(Planned).order_by(Planned.date)))
    open_items = [p for p in items if p.status == "planned"]
    closed = sorted((p for p in items if p.status != "planned"), key=lambda p: p.date, reverse=True)
    editing = db.get(Planned, edit) if edit else None
    file_counts = dict(db.execute(select(Attachment.planned_id, func.count()).where(Attachment.planned_id.is_not(None))
                                  .group_by(Attachment.planned_id)).all())
    return render(request, "planned.html", open_items=open_items, closed=closed[:30],
                  total=sum((p.amount for p in open_items if p.category.kind == "expense"), ZERO),
                  editing=editing, file_counts=file_counts,
                  attachments=list(db.scalars(select(Attachment).where(Attachment.planned_id == editing.id)
                                              .order_by(Attachment.id))) if editing else [],
                  items=list(db.scalars(select(Item).where(Item.planned_id == editing.id))) if editing else [],
                  categories=category_choices(db), today=date.today())


@router.post("/planned")
def planned_save(pid: str = Form(""), name: str = Form(""), amount: str = Form(""),
                 planned_date: str = Form("", alias="date"), category_id: str = Form(""),
                 note: str = Form(""), db: Session = Depends(get_db)):
    cat = _category(db, category_id)
    if not name.strip():
        raise UserError("Please give the planned item a name.")
    p = db.get(Planned, parse_int(pid)) if pid else None
    if not p:
        p = Planned(status="planned")
    p.name = name.strip()[:100]
    p.amount = parse_amount(amount)
    p.date = parse_date(planned_date)
    p.category_id = cat.id
    p.note = note.strip()[:255]
    db.add(p)
    db.commit()
    return redirect("/planned", msg=f"Saved “{p.name}” ({money(p.amount)}, {p.date:%b %Y}).")


@router.post("/planned/{pid}/record")
def planned_record(pid: int, amount: str = Form(""), paid_date: str = Form("", alias="date"),
                   db: Session = Depends(get_db)):
    """Turn a planned item into an actual entry once it has been paid/received."""
    p = db.get(Planned, pid)
    if not p or p.status != "planned":
        return redirect("/planned")
    entry = Transaction(amount=parse_amount(amount) if amount else p.amount,
                        date=parse_date(paid_date, date.today()), category_id=p.category_id,
                        note=p.name, one_off=True, planned_id=p.id)
    p.status = "done"
    db.add(entry)
    db.flush()
    # Quotes, invoices and warranty items move along to the real entry.
    db.execute(update(Attachment).where(Attachment.planned_id == p.id).values(transaction_id=entry.id))
    db.execute(update(Item).where(Item.planned_id == p.id).values(transaction_id=entry.id))
    db.commit()
    return redirect("/planned", msg=f"Recorded {money(entry.amount)} for “{p.name}”.")


@router.post("/planned/{pid}/cancel")
def planned_cancel(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planned, pid)
    if p:
        p.status = "planned" if p.status == "cancelled" else "cancelled"
        db.commit()
    return redirect("/planned")


@router.post("/planned/{pid}/delete")
def planned_delete(pid: int, db: Session = Depends(get_db)):
    p = db.get(Planned, pid)
    if p:
        db.execute(update(Transaction).where(Transaction.planned_id == pid).values(planned_id=None))
        db.execute(update(Attachment).where(Attachment.planned_id == pid).values(planned_id=None))
        db.execute(update(Item).where(Item.planned_id == pid).values(planned_id=None))
        db.delete(p)
        db.commit()
    return redirect("/planned", msg="Planned item deleted. Any recorded entry and files were kept.")
