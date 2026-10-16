"""Receipts, OCR parsing, attachments and warranties."""
import io
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app import files, ocr
from app.main import app
from app.models import Attachment, Category, Item, Planned, Transaction
from app.receipts import amounts_in, find_date, month_number, parse

TODAY = date(2026, 10, 16)

COSTCO = """COSTCO WHOLESALE
Ottawa #545
  E 1234567 KS ORGANIC EGGS   8.99
  E 2345 BANANAS             1.99
  SUBTOTAL                  10.98
  HST 13%                    0.00
**** TOTAL                  10.98
  VISA                      10.98
CHANGE                       0.00
10/14/2026 18:42
TOTAL NUMBER OF ITEMS SOLD = 2
"""

METRO = """METRO
Bienvenue chez Metro
LAIT 2%           5,49
PAIN              3,99
SOUS-TOTAL        9,48
TPS               0,00
TOTAL            9,48
INTERAC          9,48
Date: 12 oct. 2026 14:03
"""

RESTAURANT = """The Keg Steakhouse
Tel 613-555-0199
Oct 3, 2026  7:45 PM
Subtotal     84.00
HST          10.92
Total        94.92
Tip          15.00
Grand Total  109.92
"""

BEST_BUY = """BEST BUY
Store #932
SAMSUNG FRIDGE RF28    2,199.99
SUB-TOTAL              2,229.99
HST                      289.90
TOTAL                  2,519.89
MASTERCARD             2,519.89
Returns until 2026-11-30
15/09/2026
"""


# --- parsing ------------------------------------------------------------------------

@pytest.mark.parametrize("text, store, amount, day", [
    (COSTCO, "Costco Wholesale", "10.98", date(2026, 10, 14)),
    (METRO, "Metro", "9.48", date(2026, 10, 12)),
    (RESTAURANT, "The Keg Steakhouse", "109.92", date(2026, 10, 3)),
    (BEST_BUY, "Best Buy", "2519.89", date(2026, 9, 15)),  # the future return date is ignored
    ("  ~~ SHOPPERS DRUG MART ~~\n T0TAL\n   23.47\nDEBIT 23.47\n 09-30-26\n",
     "Shoppers Drug Mart", "23.47", date(2026, 9, 30)),
])
def test_parse_receipts(text, store, amount, day):
    info = parse(text, today=TODAY)
    assert (info.store, info.amount, info.date) == (store, D(amount), day)


def test_known_store_beats_first_line():
    text = "Welcome!\nSUPERSTORE #1234\nREAL CANADIAN SUPERSTORE\nTOTAL 5.00"
    assert parse(text, known_stores=["Real Canadian Superstore"], today=TODAY).store == "Real Canadian Superstore"


def test_amount_formats():
    assert amounts_in("TOTAL $1,234.56") == [D("1234.56")]
    assert amounts_in("TOTAL 1 234,56 $") == [D("1234.56")]
    assert amounts_in("2 x 3.50  7.00") == [D("3.50"), D("7.00")]
    assert amounts_in("Item 4006381333931") == []


def test_dates():
    assert month_number("Févr.") == 2 and month_number("juil") == 7 and month_number("jui") is None
    assert month_number("market") is None
    # 03/04/2026 is ambiguous: the more recent reading that isn't in the future wins
    assert find_date("03/04/2026", today=TODAY) == date(2026, 4, 3)
    assert find_date("le 1er octobre 2026", today=TODAY) == date(2026, 10, 1)
    assert find_date("2026-12-24", today=TODAY) is None
    assert find_date("Tel 613-555-0199", today=TODAY) is None


# --- web flows ---------------------------------------------------------------------------

@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ocr_text(monkeypatch):
    """Pretend the OCR container is running and returns the given text."""
    state = {"text": COSTCO}
    monkeypatch.setattr(ocr, "OCR_URL", "http://ocr.test")
    monkeypatch.setattr(ocr, "read_text", lambda name, ctype: state["text"])
    return state


def jpeg(size=(600, 900), exif=True) -> bytes:
    img = Image.new("RGB", size, "white")
    out = io.BytesIO()
    kwargs = {}
    if exif:
        e = Image.Exif()
        e[0x0112] = 6  # orientation: rotate 90°
        e[0x8825] = {1: "N", 2: (45.0, 25.0, 0.0)}  # GPS
        kwargs["exif"] = e
    img.save(out, "JPEG", **kwargs)
    return out.getvalue()


def cat_id(db, name):
    return db.scalar(select(Category.id).where(Category.name == name))


def scan(client, data=None, name="receipt.jpg"):
    return client.post("/receipts/scan", files=[("file", ("", b"", "application/octet-stream")),
                                                ("file", (name, data or jpeg(), "image/jpeg"))],
                       follow_redirects=False)


def test_scan_confirm_with_warranty(client, db, ocr_text):
    r = scan(client)
    assert r.status_code == 303 and r.headers["location"].endswith("/confirm")
    att = db.scalar(select(Attachment))
    assert (att.store, att.amount, att.doc_type, att.filed) == ("Costco Wholesale", D("10.98"), "receipt", False)

    page = client.get(r.headers["location"]).text
    assert 'value="10.98"' in page and "Costco Wholesale" in page

    stored = Image.open(files.path_of(att.stored_name))
    assert stored.size == (900, 600)  # turned upright
    assert not stored.getexif()        # location and other metadata removed

    r = client.post(f"/receipts/{att.id}/confirm", follow_redirects=False, data={
        "amount": "10.98", "category_id": cat_id(db, "Groceries"), "date": "2026-10-14",
        "note": "Costco", "tags": "", "item_name": "Blender", "warranty_end": "2028-10-14",
        "serial_number": "SN-123"})
    assert r.status_code == 303 and "undo=" in r.headers["location"]
    db.expire_all()
    t = db.scalar(select(Transaction))
    att = db.get(Attachment, att.id)
    item = db.scalar(select(Item))
    assert att.transaction_id == t.id and att.store == "Costco"
    assert (item.name, item.serial_number, item.transaction_id, item.purchase_date) == \
        ("Blender", "SN-123", t.id, date(2026, 10, 14))
    assert "📎" in client.get("/entries?month=2026-10").text

    # The next receipt from the same store suggests the same category
    ocr_text["text"] = "COSTCO\nTOTAL 55.10\n"
    r = scan(client)
    page = client.get(r.headers["location"]).text
    assert f'value="{cat_id(db, "Groceries")}" data-kind="expense" selected' in page


def test_scan_offers_existing_entry(client, db, ocr_text):
    client.post("/entries", data={"amount": "10.98", "category_id": cat_id(db, "Groceries"),
                                  "date": (date.today() - timedelta(days=1)).isoformat(), "note": "eggs"})
    ocr_text["text"] = "SOME SHOP\nTOTAL 10.98\n"
    r = scan(client)
    att = db.scalar(select(Attachment))
    assert "Already entered?" in client.get(r.headers["location"]).text
    t = db.scalar(select(Transaction))
    client.post(f"/receipts/{att.id}/attach", data={"transaction_id": t.id})
    db.expire_all()
    assert db.get(Attachment, att.id).transaction_id == t.id
    assert db.scalar(select(Transaction).where(Transaction.id != t.id)) is None


def test_ocr_unavailable_still_saves_photo(client, db):
    r = scan(client)  # OCR_URL not set in tests
    att = db.scalar(select(Attachment))
    assert att.ocr_status == "none" and att.amount is None
    assert "switched off" in client.get(r.headers["location"]).text


def test_ocr_failure_can_be_retried(client, db, monkeypatch, ocr_text):
    def broken(name, ctype):
        raise ocr.OcrError("not reachable")
    monkeypatch.setattr(ocr, "read_text", broken)
    scan(client)
    att = db.scalar(select(Attachment))
    assert att.ocr_status == "failed"
    assert "Read again" in client.get("/receipts").text
    monkeypatch.setattr(ocr, "read_text", lambda name, ctype: METRO)
    client.post(f"/attachments/{att.id}/ocr", data={"next": "/receipts"})
    db.expire_all()
    att = db.get(Attachment, att.id)
    assert (att.ocr_status, att.amount) == ("done", D("9.48"))


def test_rejects_other_files(client, db):
    r = client.post("/receipts/scan", files={"file": ("notes.txt", b"hello", "text/plain")},
                    headers={"referer": "http://testserver/"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 50
    r = client.post("/receipts/scan", files={"file": ("a.heic", heic, "image/heic")}, follow_redirects=False)
    assert "HEIC" in r.headers["location"] or "error=1" in r.headers["location"]
    assert db.scalar(select(Attachment)) is None


def test_entry_files_survive_entry_delete(client, db):
    client.post("/entries", data={"amount": "1299", "category_id": cat_id(db, "Groceries"), "note": "Dishwasher"})
    t = db.scalar(select(Transaction))
    pdf = b"%PDF-1.4\n% invoice\n"
    client.post("/attachments", data={"transaction_id": t.id, "doc_type": "invoice", "next": "/"},
                files=[("files", ("invoice.pdf", pdf, "application/pdf")), ("files", ("photo.jpg", jpeg(), "image/jpeg"))])
    atts = list(db.scalars(select(Attachment)))
    assert len(atts) == 2 and all(a.transaction_id == t.id for a in atts)
    assert "invoice.pdf" in client.get(f"/entries/{t.id}/edit").text
    pdf_att = next(a for a in atts if a.is_pdf)
    r = client.get(f"/files/{pdf_att.id}")
    assert r.status_code == 200 and r.content == pdf and r.headers["content-type"] == "application/pdf"

    r = client.post(f"/entries/{t.id}/delete", data={"next": "/entries"}, follow_redirects=False)
    assert "Unfiled" in r.headers["location"]
    db.expire_all()
    assert all(a.transaction_id is None for a in db.scalars(select(Attachment)))
    assert "File it" in client.get("/receipts").text

    path = files.path_of(pdf_att.stored_name)
    client.post(f"/attachments/{pdf_att.id}/delete", data={"next": "/receipts"})
    assert not path.exists()


def test_planned_files_move_to_entry(client, db):
    client.post("/planned", data={"name": "New roof", "amount": "14000", "date": "2027-05-01",
                                  "category_id": cat_id(db, "Maintenance & repairs")})
    p = db.scalar(select(Planned))
    client.post("/attachments", data={"planned_id": p.id, "doc_type": "invoice"},
                files={"files": ("quote.pdf", b"%PDF-1.4 quote", "application/pdf")})
    client.post("/warranties", data={"name": "Roof", "warranty_end": "2047-05-01", "planned_id": p.id})
    assert "quote.pdf" in client.get(f"/planned?edit={p.id}").text
    client.post(f"/planned/{p.id}/record", data={"amount": "15200", "date": "2026-10-10"})
    db.expire_all()
    t = db.scalar(select(Transaction).where(Transaction.planned_id == p.id))
    assert db.scalar(select(Attachment)).transaction_id == t.id
    assert db.scalar(select(Item)).transaction_id == t.id


def test_search(client, db, ocr_text):
    for text, note, amount in ((COSTCO, "Costco", "10.98"), (BEST_BUY, "Best Buy", "2519.89")):
        ocr_text["text"] = text
        r = scan(client)
        att_id = int(r.headers["location"].split("/")[2])
        client.post(f"/receipts/{att_id}/confirm", data={"amount": amount, "category_id": cat_id(db, "Groceries"),
                                                         "date": date.today().isoformat(), "note": note})

    def found(**params):
        page = client.get("/receipts", params=params).text
        return [n for n in ("Costco", "Best Buy") if f"<strong>{n}</strong>" in page]

    assert found(q="costco") == ["Costco"]
    assert found(q="samsung fridge") == ["Best Buy"]   # OCR text
    assert found(amount="2519.89") == ["Best Buy"]
    assert found(amount="<100") == ["Costco"]
    assert found(amount="5-20") == ["Costco"]
    assert found(amount=">100", q="best") == ["Best Buy"]
    r = client.get("/receipts", params={"amount": "lots"}, headers={"referer": "http://testserver/receipts"},
                   follow_redirects=False)
    assert "error=1" in r.headers["location"]


def test_warranty_reminders(client, db):
    today = date.today()
    for name, end in (("Dishwasher", today + timedelta(days=20)), ("TV", today + timedelta(days=400)),
                      ("Old phone", today - timedelta(days=5))):
        client.post("/warranties", data={"name": name, "warranty_end": end.isoformat(),
                                         "purchase_date": (today - timedelta(days=700)).isoformat()})
    client.post("/warranties", data={"name": "Bike", "serial_number": "WTU123"})
    home = client.get("/").text
    assert "Warranties ending soon" in home and "Dishwasher" in home and "TV" not in home
    page = client.get("/warranties").text
    assert page.index("Dishwasher") < page.index("TV") < page.index("Bike") < page.index("Old phone")

    client.post("/settings", data={"avg_months": "6", "warranty_days": "0"})
    assert "Warranties ending soon" not in client.get("/").text

    r = client.post("/warranties", data={"name": "X", "purchase_date": "2026-05-01", "warranty_end": "2026-01-01"},
                    headers={"referer": "http://testserver/warranties/new"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]


# --- the OCR container ---------------------------------------------------------------------

def test_ocr_server_falls_back_to_tesseract_for_scanned_pdfs(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1] / "ocr"))
    import server
    calls = []

    def fake_run(*args):
        calls.append(args[0])
        if args[0] == "pdftotext":
            return "  \n"  # no text layer
        if args[0] == "pdftoppm":
            (tmp_path / "page-1.png").write_bytes(b"png")
            return ""
        return "TOTAL 12.00\n"

    monkeypatch.setattr(server, "run", fake_run)
    assert server.read_pdf(tmp_path / "in.pdf", tmp_path) == "TOTAL 12.00\n"
    assert calls == ["pdftotext", "pdftoppm", "tesseract"]


def test_warranty_on_entry_marks_it_one_off(client, db):
    client.post("/entries", data={"amount": "1299", "category_id": cat_id(db, "Groceries"), "note": "Dishwasher"})
    t = db.scalar(select(Transaction))
    assert not t.one_off
    r = client.post("/warranties", data={"name": "Dishwasher", "warranty_end": "2028-10-11",
                                         "transaction_id": t.id}, follow_redirects=False)
    assert "one-off" in r.headers["location"].replace("%20", " ")
    db.expire_all()
    assert db.get(Transaction, t.id).one_off
