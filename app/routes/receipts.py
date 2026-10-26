"""Receipts & documents: scanning, OCR, attaching files and searching them."""
import logging
import threading
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import files, ocr, receipts
from ..db import SessionLocal
from ..models import DOC_TYPES, Attachment, Item, OcrStatus, Planned, Transaction
from ..services import all_tags, category_choices, recent_categories
from ..web import (
    UserError, get_db, money, parse_amount, parse_int, parse_optional_date, redirect, render, safe_next,
)
from .entries import new_entry
from .warranties import item_from_form

log = logging.getLogger("budget")
router = APIRouter()


def _read(upload: UploadFile) -> bytes:
    return upload.file.read(files.MAX_UPLOAD_BYTES + 1)


def run_ocr(db: Session, att: Attachment) -> None:
    """Read the text of an attachment and what it says (store, total, date)."""
    try:
        att.ocr_text = ocr.read_text(att.stored_name, att.content_type)[:60000]
        att.ocr_status = OcrStatus.DONE
    except ocr.OcrError as e:
        log.warning("OCR of attachment %s failed: %s", att.id, e)
        att.ocr_status = OcrStatus.FAILED if ocr.enabled() else OcrStatus.NONE
        return
    info = receipts.parse(att.ocr_text, receipts.known_stores(db))
    att.store = att.store or info.store
    att.amount = info.amount
    att.doc_date = info.date


def add_attachment(db: Session, upload: UploadFile, doc_type: str, read_now: bool = True, **owner) -> Attachment:
    try:
        stored = files.store(_read(upload))
    except files.UploadError as e:
        raise UserError(str(e))
    att = Attachment(filename=(upload.filename or "upload")[-255:], stored_name=stored.stored_name,
                     content_type=stored.content_type, size=stored.size, has_thumb=stored.has_thumb,
                     doc_type=doc_type if doc_type in DOC_TYPES else "other", **owner)
    db.add(att)
    db.flush()
    if read_now:
        run_ocr(db, att)
    return att


def ocr_in_background(ids: list[int]) -> None:
    """Read uploaded files after the response has gone out; OCR can take minutes per file."""
    def work():
        with SessionLocal() as db:
            for aid in ids:
                try:
                    att = db.get(Attachment, aid)
                    if att:
                        run_ocr(db, att)
                        db.commit()
                except Exception:  # one bad file must not stop the others
                    log.exception("Background OCR of attachment %s failed", aid)
                    db.rollback()

    threading.Thread(target=work, daemon=True, name="ocr-uploads").start()


def _attachment(db: Session, aid: int) -> Attachment:
    att = db.get(Attachment, aid)
    if not att:
        raise UserError("That file no longer exists.")
    return att


# --- serving files ------------------------------------------------------------------

@router.get("/files/{aid}")
def file_view(aid: int, db: Session = Depends(get_db)):
    att = db.get(Attachment, aid)
    if not att or not files.path_of(att.stored_name).is_file():
        return Response("File not found", status_code=404)
    return FileResponse(files.path_of(att.stored_name), media_type=att.content_type,
                        filename=att.filename, content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/files/{aid}/thumb")
def file_thumb(aid: int, db: Session = Depends(get_db)):
    att = db.get(Attachment, aid)
    path = files.path_of(files.thumb_name(att.stored_name)) if att and att.has_thumb else None
    if not path or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# --- scan a receipt into a new entry ------------------------------------------------------

@router.post("/receipts/scan")
def scan(uploads: list[UploadFile | str] = File([], alias="file"), db: Session = Depends(get_db)):
    # The form has two inputs (camera / file picker); use whichever was filled in.
    chosen = next((u for u in uploads if not isinstance(u, str) and u.filename), None)
    if not chosen:
        raise UserError("Please choose a photo or PDF of the receipt.")
    att = add_attachment(db, chosen, "receipt")
    db.commit()
    return redirect(f"/receipts/{att.id}/confirm")


@router.get("/receipts/{aid}/confirm")
def confirm_page(request: Request, aid: int, db: Session = Depends(get_db)):
    att = db.get(Attachment, aid)
    if not att:
        return redirect("/receipts", msg="That receipt no longer exists.")
    if att.transaction_id:
        return redirect(f"/entries/{att.transaction_id}/edit", msg="This receipt is already filed.")
    category_id = receipts.guess_category(db, att.store)
    cats = category_choices(db)
    kind = next((c["kind"] for c in cats if c["id"] == category_id), "expense")
    entry = {"kind": kind, "amount": att.amount or "", "category_id": category_id, "note": att.store,
             "date": att.doc_date or date.today(), "tags": [], "one_off": False}
    return render(request, "receipt_confirm.html", att=att, entry=entry, categories=cats,
                  recent_categories=recent_categories(db, date.today()),
                  tags=all_tags(db),
                  matches=receipts.matching_entries(db, att.amount, att.doc_date),
                  ocr_enabled=ocr.enabled(), today=date.today())


@router.post("/receipts/{aid}/confirm")
def confirm_save(aid: int, amount: str = Form(""), category_id: str = Form(""),
                 entry_date: str = Form("", alias="date"), note: str = Form(""), tags: str = Form(""),
                 one_off: bool = Form(False), item_name: str = Form(""), warranty_end: str = Form(""),
                 serial_number: str = Form(""), db: Session = Depends(get_db)):
    att = _attachment(db, aid)
    if att.transaction_id:  # sent twice (double tap, or Back and submit again)
        return redirect(f"/entries/{att.transaction_id}/edit", msg="This receipt is already filed.")
    entry = new_entry(db, amount, category_id, entry_date, note, tags, one_off)
    att.transaction_id = entry.id
    att.store = note.strip()[:100] or att.store  # the confirmed name teaches future scans
    msg = f"Added {money(entry.amount)} · {entry.category.full_name} with its receipt"
    if warranty_end or serial_number.strip():
        item = item_from_form(db, None, name=item_name or note or entry.category.name, store=note,
                              serial_number=serial_number, purchase_date=entry.date.isoformat(),
                              price="", warranty_end=warranty_end, note="")
        item.transaction_id = entry.id
        msg += " and warranty"
    db.commit()
    return redirect("/", msg=msg + ".", undo=entry.id)


@router.post("/receipts/{aid}/attach")
def attach_to_entry(aid: int, transaction_id: str = Form(""), db: Session = Depends(get_db)):
    att = _attachment(db, aid)
    entry = db.get(Transaction, parse_int(transaction_id) or 0)
    if not entry:
        raise UserError("That entry no longer exists.")
    att.transaction_id = entry.id
    if entry.note:
        att.store = entry.note[:100]
    db.commit()
    return redirect(f"/entries/{entry.id}/edit", msg="Receipt attached to the entry.")


# --- attaching files to entries, planned items and warranty items ----------------------------

@router.post("/attachments")
def upload(uploads: list[UploadFile | str] = File([], alias="files"), doc_type: str = Form("receipt"),
           transaction_id: str = Form(""), planned_id: str = Form(""), item_id: str = Form(""),
           next: str = Form("/receipts"), db: Session = Depends(get_db)):
    owner = {}
    for key, model, raw in (("transaction_id", Transaction, transaction_id), ("planned_id", Planned, planned_id),
                            ("item_id", Item, item_id)):
        if parse_int(raw):
            if not db.get(model, parse_int(raw)):
                raise UserError("That item no longer exists.")
            owner[key] = parse_int(raw)
    uploads = [u for u in uploads if not isinstance(u, str) and u.filename]
    if not uploads:
        raise UserError("Please choose one or more photos or PDFs.")
    added = []
    try:
        for u in uploads:
            added.append(add_attachment(db, u, doc_type, read_now=False, **owner))
    except UserError:
        # One bad file cancels the upload: remove the files already written, or they'd
        # stay on disk without a database row.
        written = [att.stored_name for att in added]
        db.rollback()
        for name in written:
            files.remove(name)
        raise
    db.commit()
    if ocr.enabled():
        ocr_in_background([att.id for att in added])
    n = len(uploads)
    return redirect(safe_next(next, "/receipts"), msg=f"{n} file{'s' if n > 1 else ''} added.")


@router.post("/attachments/{aid}/delete")
def delete_attachment(aid: int, next: str = Form("/receipts"), db: Session = Depends(get_db)):
    att = db.get(Attachment, aid)
    if att:
        stored = att.stored_name
        db.delete(att)
        db.commit()
        files.remove(stored)
    return redirect(safe_next(next, "/receipts"), msg="File deleted.")


@router.post("/attachments/{aid}/ocr")
def reread(aid: int, next: str = Form("/receipts"), db: Session = Depends(get_db)):
    att = _attachment(db, aid)
    run_ocr(db, att)
    db.commit()
    ok = att.ocr_status == OcrStatus.DONE
    return redirect(safe_next(next, "/receipts"), msg="Text read again." if ok else "OCR is not available.",
                    error=None if ok else 1)


# --- the receipts page: unfiled scans and search --------------------------------------------

def _amount_range(text: str):
    """'45.67' -> exact; '40-50' -> range; '>100' / '<20' -> open range."""
    text = (text or "").replace("$", "").replace(" ", "")
    if not text:
        return None
    try:
        if text[0] in "<>":
            v = parse_amount(text[1:], allow_zero=True)
            return (None, v) if text[0] == "<" else (v, None)
        if "-" in text.strip("-"):
            lo, hi = text.split("-", 1)
            lo, hi = parse_amount(lo, allow_zero=True), parse_amount(hi, allow_zero=True)
            return (min(lo, hi), max(lo, hi))
        v = parse_amount(text)
        return (v, v)
    except UserError:
        raise UserError(f"“{text}” is not an amount. Try 45.67, 40-50, >100 or <20.")


@router.get("/receipts")
def receipts_page(request: Request, q: str = "", amount: str = "", doc_type: str = "",
                  date_from: str = "", date_to: str = "", db: Session = Depends(get_db)):
    query = (select(Attachment)
             .outerjoin(Transaction, Attachment.transaction_id == Transaction.id)
             .outerjoin(Planned, Attachment.planned_id == Planned.id)
             .outerjoin(Item, Attachment.item_id == Item.id)
             .options(selectinload(Attachment.transaction).selectinload(Transaction.category),
                      selectinload(Attachment.planned), selectinload(Attachment.item))
             .order_by(Attachment.id.desc()))
    searching = bool(q.strip() or amount.strip() or doc_type or date_from or date_to)
    for word in q.lower().split():
        like = f"%{word}%"
        query = query.where(or_(func.lower(Attachment.store).like(like), func.lower(Attachment.ocr_text).like(like),
                                func.lower(Attachment.filename).like(like), func.lower(Transaction.note).like(like),
                                func.lower(Planned.name).like(like), func.lower(Item.name).like(like),
                                func.lower(Item.serial_number).like(like)))
    rng = _amount_range(amount)
    if rng:
        value = func.coalesce(Transaction.amount, Planned.amount, Attachment.amount, Item.price)
        if rng[0] is not None:
            query = query.where(value >= rng[0])
        if rng[1] is not None:
            query = query.where(value <= rng[1])
    if doc_type in DOC_TYPES:
        query = query.where(Attachment.doc_type == doc_type)
    shown_date = func.coalesce(Transaction.date, Attachment.doc_date, func.date(Attachment.uploaded_at))
    if d := parse_optional_date(date_from):
        query = query.where(shown_date >= d)
    if d := parse_optional_date(date_to):
        query = query.where(shown_date <= d)

    limit = 60
    results = list(db.scalars(query.limit(limit + 1)))
    unfiled = [] if searching else list(db.scalars(
        select(Attachment).where(Attachment.transaction_id.is_(None), Attachment.planned_id.is_(None),
                                 Attachment.item_id.is_(None)).order_by(Attachment.id.desc())))
    unfiled_ids = {a.id for a in unfiled}
    return render(request, "receipts.html", results=[a for a in results[:limit] if a.id not in unfiled_ids],
                  more=len(results) > limit, unfiled=unfiled, searching=searching, q=q, amount=amount,
                  doc_type=doc_type, date_from=date_from, date_to=date_to, DOC_TYPES=DOC_TYPES,
                  ocr_enabled=ocr.enabled())
