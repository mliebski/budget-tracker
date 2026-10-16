"""Warranties & serial numbers: things you own, their warranty end and documents."""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..models import Attachment, Item, Planned, Transaction
from ..services import warranty_days
from ..web import UserError, get_db, parse_amount, parse_int, parse_optional_date, redirect, render

router = APIRouter()


def item_from_form(db: Session, item: Item | None, *, name: str, store: str, serial_number: str,
                   purchase_date: str, price: str, warranty_end: str, note: str) -> Item:
    if not name.strip():
        raise UserError("Please give the item a name, e.g. “Dishwasher”.")
    item = item or Item()
    item.name = name.strip()[:100]
    item.store = store.strip()[:100]
    item.serial_number = serial_number.strip()[:100]
    item.purchase_date = parse_optional_date(purchase_date)
    item.price = parse_amount(price) if price.strip() else None
    item.warranty_end = parse_optional_date(warranty_end)
    item.note = note.strip()[:255]
    if item.purchase_date and item.warranty_end and item.warranty_end < item.purchase_date:
        raise UserError("The warranty can't end before the purchase date.")
    db.add(item)
    db.flush()
    return item


@router.get("/warranties")
def warranties_page(request: Request, db: Session = Depends(get_db)):
    today, days = date.today(), warranty_days(db)
    items = list(db.scalars(select(Item).options(selectinload(Item.attachments), selectinload(Item.transaction))
                            .order_by(Item.warranty_end, Item.name)))
    groups = {"soon": [], "active": [], "none": [], "expired": []}
    for i in items:
        left = i.days_left(today)
        if left is None:
            groups["none"].append(i)
        elif left < 0:
            groups["expired"].append(i)
        elif left <= days:
            groups["soon"].append(i)
        else:
            groups["active"].append(i)
    groups["expired"].reverse()  # most recently expired first
    return render(request, "warranties.html", groups=groups, today=today, days=days)


@router.get("/warranties/new")
def warranty_new(request: Request, entry: int | None = None, planned: int | None = None,
                 db: Session = Depends(get_db)):
    t = db.get(Transaction, entry) if entry else None
    p = db.get(Planned, planned) if planned else None
    prefill = {"name": "", "store": "", "purchase_date": date.today(), "price": None}
    if t:
        prefill.update(name=t.note or t.category.name, store=t.note, purchase_date=t.date, price=None)
    elif p:
        prefill.update(name=p.name, purchase_date=p.date)
    return render(request, "warranty_edit.html", item=None, prefill=prefill, entry=t, planned=p,
                  attachments=[], today=date.today())


@router.get("/warranties/{iid}")
def warranty_edit(request: Request, iid: int, db: Session = Depends(get_db)):
    item = db.get(Item, iid)
    if not item:
        return redirect("/warranties", msg="That item no longer exists.")
    # Documents of the item itself plus those of the purchase entry (the receipt)
    attachments = list(item.attachments)
    if item.transaction_id:
        attachments += list(db.scalars(select(Attachment).where(Attachment.transaction_id == item.transaction_id,
                                                                 Attachment.item_id.is_(None))))
    return render(request, "warranty_edit.html", item=item, prefill=None, entry=item.transaction,
                  planned=item.planned, attachments=attachments, today=date.today())


@router.post("/warranties")
def warranty_save(iid: str = Form(""), name: str = Form(""), store: str = Form(""), serial_number: str = Form(""),
                  purchase_date: str = Form(""), price: str = Form(""), warranty_end: str = Form(""),
                  note: str = Form(""), transaction_id: str = Form(""), planned_id: str = Form(""),
                  db: Session = Depends(get_db)):
    item = db.get(Item, parse_int(iid)) if parse_int(iid) else None
    is_new = item is None
    item = item_from_form(db, item, name=name, store=store, serial_number=serial_number,
                          purchase_date=purchase_date, price=price, warranty_end=warranty_end, note=note)
    if is_new:
        if parse_int(transaction_id) and db.get(Transaction, parse_int(transaction_id)):
            item.transaction_id = parse_int(transaction_id)
        if parse_int(planned_id) and db.get(Planned, parse_int(planned_id)):
            item.planned_id = parse_int(planned_id)
    db.commit()
    return redirect(f"/warranties/{item.id}", msg=f"Saved “{item.name}”."
                    + (" Add the warranty card or manual below." if is_new else ""))


@router.post("/warranties/{iid}/delete")
def warranty_delete(iid: int, db: Session = Depends(get_db)):
    item = db.get(Item, iid)
    kept = 0
    if item:
        kept = db.execute(update(Attachment).where(Attachment.item_id == iid).values(item_id=None)).rowcount
        db.delete(item)
        db.commit()
    return redirect("/warranties", msg="Item deleted." + (" Its documents were kept under Receipts › Unfiled."
                                                         if kept else ""))
