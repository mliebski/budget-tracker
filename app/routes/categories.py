from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    EXPENSE, INCOME, KINDS, Budget, Category, Planned, Recurring, Tag, Transaction, recurring_tags, transaction_tags,
)
from ..services import all_tags, category_tree
from ..web import UserError, get_db, parse_int, redirect, render

router = APIRouter()


def _usage(db: Session) -> dict[int, int]:
    return dict(db.execute(select(Transaction.category_id, func.count()).group_by(Transaction.category_id)).all())


def _parent(db: Session, parent_id: str, kind: str, self_id: int | None = None) -> Category | None:
    pid = parse_int(parent_id)
    if not pid:
        return None
    parent = db.get(Category, pid)
    if not parent or parent.parent_id is not None or parent.id == self_id:
        raise UserError("A subcategory can only sit under a main category.")
    if parent.kind != kind:
        raise UserError("The main category must be of the same type (expense/income).")
    return parent


@router.get("/categories")
def categories_page(request: Request, db: Session = Depends(get_db)):
    tag_counts = dict(db.execute(select(transaction_tags.c.tag_id, func.count())
                                 .group_by(transaction_tags.c.tag_id)).all())
    return render(request, "categories.html",
                  trees={k: category_tree(db, k, include_archived=True) for k in (EXPENSE, INCOME)},
                  usage=_usage(db), tags=all_tags(db),
                  tag_counts=tag_counts)


@router.post("/categories")
def category_create(name: str = Form(""), kind: str = Form(EXPENSE), parent_id: str = Form(""),
                    db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise UserError("Unknown category type.")
    if not name.strip():
        raise UserError("Please enter a category name.")
    parent = _parent(db, parent_id, kind)
    db.add(Category(name=name.strip()[:80], kind=kind, parent_id=parent.id if parent else None))
    db.commit()
    return redirect("/categories", msg=f"Added “{name.strip()}”.")


@router.post("/categories/{cid}")
def category_update(cid: int, name: str = Form(""), parent_id: str = Form(""), db: Session = Depends(get_db)):
    cat = db.get(Category, cid)
    if not cat:
        return redirect("/categories")
    if not name.strip():
        raise UserError("Please enter a category name.")
    parent = _parent(db, parent_id, cat.kind, cid)
    if parent and cat.children:
        raise UserError(f"“{cat.name}” has subcategories, so it has to stay a main category.")
    cat.name = name.strip()[:80]
    cat.parent_id = parent.id if parent else None
    db.commit()
    return redirect("/categories", msg="Category saved.")


@router.post("/categories/{cid}/archive")
def category_archive(cid: int, db: Session = Depends(get_db)):
    cat = db.get(Category, cid)
    if cat:
        cat.archived = not cat.archived
        for child in cat.children:
            child.archived = cat.archived
        db.commit()
        return redirect("/categories", msg=f"“{cat.name}” {'hidden' if cat.archived else 'restored'}.")
    return redirect("/categories")


@router.post("/categories/{cid}/delete")
def category_delete(cid: int, db: Session = Depends(get_db)):
    cat = db.get(Category, cid)
    if not cat:
        return redirect("/categories")
    in_use = (cat.children
              or db.scalar(select(Transaction.id).where(Transaction.category_id == cid).limit(1))
              or db.scalar(select(Recurring.id).where(Recurring.category_id == cid).limit(1))
              or db.scalar(select(Planned.id).where(Planned.category_id == cid).limit(1)))
    if in_use:
        raise UserError(f"“{cat.name}” is in use. Hide it instead to keep your history intact.")
    for b in db.scalars(select(Budget).where(Budget.category_id == cid)):
        db.delete(b)
    db.delete(cat)
    db.commit()
    return redirect("/categories", msg=f"Deleted “{cat.name}”.")


@router.post("/tags/{tid}")
def tag_rename(tid: int, name: str = Form(""), db: Session = Depends(get_db)):
    tag = db.get(Tag, tid)
    name = " ".join(name.split())[:50]
    if not tag:
        return redirect("/categories#tags", msg="That tag no longer exists.")
    if not name:
        raise UserError("Please enter a tag name.")
    clash = db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower(), Tag.id != tid))
    if clash:
        raise UserError(f"A tag called “{clash.name}” already exists.")
    tag.name = name
    db.commit()
    return redirect("/categories#tags", msg="Tag renamed.")


@router.post("/tags/{tid}/delete")
def tag_delete(tid: int, db: Session = Depends(get_db)):
    tag = db.get(Tag, tid)
    if tag:
        db.execute(transaction_tags.delete().where(transaction_tags.c.tag_id == tid))
        db.execute(recurring_tags.delete().where(recurring_tags.c.tag_id == tid))
        db.delete(tag)
        db.commit()
    return redirect("/categories#tags", msg="Tag removed from all entries.")
