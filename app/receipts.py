"""Reading store, date and total from OCR text, and guessing the category.

OCR of crumpled receipts is never perfect, so these are suggestions that the
user confirms. The heuristics cover English and French Canadian receipts.
"""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
import datetime as dt
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Attachment, Transaction


@dataclass
class ReceiptInfo:
    store: str = ""
    amount: Decimal | None = None
    date: dt.date | None = None


def _plain(text: str) -> str:
    """Lower case without accents: 'Total à payer' -> 'total a payer'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


# --- amounts --------------------------------------------------------------------

# 1,234.56 / 1234.56 (English) or 1 234,56 / 1234,56 (French)
MONEY = re.compile(r"(?<![\d.,])(?:"
                   r"(\d{1,3}(?:,\d{3})+|\d+)\s?\.\s?(\d{2})"    # English
                   r"|(\d{1,3}(?: \d{3})+|\d+)\s?,\s?(\d{2})"    # French
                   r")(?![\d])")
TOTAL = r"t\s?[o0]\s?t\s?a\s?[l1i]"
STRONG = re.compile(rf"grand {TOTAL}|{TOTAL} (due|a payer|ttc)|amount due|balance due|montant (du|total)"
                    r"|net a payer|a payer")
WEAK = re.compile(rf"{TOTAL}|montant|amount|balance")
NOT_TOTAL = re.compile(r"sub|sous|s-total|tax|taxe|tps|tvq|gst|hst|pst|qst|saving|econom|rabais|discount"
                       r"|item|article|nombre|point|tip|pourboire|change|monnaie|remis")
PAYMENT = re.compile(r"visa|master ?card|\bmc\b|debit|interac|amex|american express|cash|comptant|credit")


def amounts_in(line: str) -> list[Decimal]:
    out = []
    for m in MONEY.finditer(line):
        whole, cents = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
        out.append(Decimal(f"{whole.replace(',', '').replace(' ', '')}.{cents}"))
    return out


def find_total(text: str) -> Decimal | None:
    lines = [_plain(line) for line in text.splitlines()]

    def line_amount(i: int) -> Decimal | None:
        found = amounts_in(lines[i])
        if not found and i + 1 < len(lines) and len(lines[i + 1].strip()) < 16:
            found = amounts_in(lines[i + 1])  # amount printed on the next line
        return found[-1] if found else None

    for pattern in (STRONG, WEAK, PAYMENT):
        found = [a for i, line in enumerate(lines)
                 if pattern.search(line) and not NOT_TOTAL.search(line)
                 and (a := line_amount(i)) is not None and a > 0]
        if found:
            return max(found)
    every = [a for line in lines for a in amounts_in(line)]
    return max(every) if every else None


# --- dates ----------------------------------------------------------------------

_MONTH_NAMES = ("january february march april may june july august september october november december "
                "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre").split()


def month_number(token: str) -> int | None:
    """'Oct', 'oct.', 'Févr', 'juil' -> month number; ambiguous or unknown -> None."""
    token = _plain(token).rstrip(".")
    if len(token) < 3:
        return None
    months = {_MONTH_NAMES.index(n) % 12 + 1 for n in _MONTH_NAMES if n.startswith(token)}
    return months.pop() if len(months) == 1 else None


NUMERIC_YMD = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")
DAY_MONTH = re.compile(r"(?<!\d)(\d{1,2})(?:er)?[\s-]+([a-zA-ZÀ-ÿ]{3,9}\.?)[\s,-]+(\d{4}|\d{2})(?!\d)")
MONTH_DAY = re.compile(r"\b([a-zA-ZÀ-ÿ]{3,9}\.?)[\s-]+(\d{1,2})(?:st|nd|rd|th)?[\s,-]+(\d{4}|\d{2})(?!\d)")


def _year(y: str) -> int:
    return int(y) + 2000 if len(y) == 2 else int(y)


def _make(y: int, m: int | None, d: int) -> date | None:
    try:
        return date(y, m, d) if m else None
    except ValueError:
        return None


def find_date(text: str, today: date | None = None) -> date | None:
    """First plausible purchase date (not in the future, not older than 3 years)."""
    today = today or date.today()
    lo, hi = today - timedelta(days=3 * 366), today + timedelta(days=1)
    found = []  # (position in text, [candidate dates])
    for m in NUMERIC_YMD.finditer(text):
        found.append((m.start(), [_make(int(m[1]), int(m[2]), int(m[3]))]))
    for m in NUMERIC.finditer(text):
        a, b, y = int(m[1]), int(m[2]), m[3]
        options = [_make(_year(y), a, b), _make(_year(y), b, a)]  # MM/DD/YYYY and DD/MM/YYYY
        if len(y) == 2:
            options.append(_make(2000 + a, b, int(y)))                # YY/MM/DD
        found.append((m.start(), options))
    for m in DAY_MONTH.finditer(text):
        found.append((m.start(), [_make(_year(m[3]), month_number(m[2]), int(m[1]))]))
    for m in MONTH_DAY.finditer(text):
        found.append((m.start(), [_make(_year(m[3]), month_number(m[1]), int(m[2]))]))
    for _, options in sorted(found, key=lambda f: f[0]):
        plausible = [d for d in options if d and lo <= d <= hi]
        if plausible:
            return max(plausible)  # the most recent reading of an ambiguous date
    return None


# --- store ----------------------------------------------------------------------

NOISE = re.compile(r"welcome|bienvenue|receipt|recu|facture|invoice|store ?#|magasin|succursale|\btel\b|phone"
                   r"|www|http|@|date|cashier|caissier|transaction|merci|thank")


def _tidy(name: str) -> str:
    name = re.sub(r"\s+", " ", re.sub(r"^[^\wÀ-ÿ]+|[^\wÀ-ÿ.!&')]+$", "", name)).strip()
    letters = [c for c in name if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        name = " ".join(w.capitalize() if len(w) > 2 or i == 0 else w.lower()
                        for i, w in enumerate(name.split()))
    return name[:100]


def find_store(text: str, known: list[str] = ()) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    head = _plain(" ".join(lines[:12]))
    head = re.sub(r"\s+", " ", head)
    matches = [k for k in known if len(k) >= 3 and _plain(k) in head]
    if matches:
        return max(matches, key=len)
    for line in lines[:6]:
        letters = sum(c.isalpha() for c in line)
        if letters >= 3 and letters / len(line.replace(" ", "")) > 0.6 and not NOISE.search(_plain(line)):
            return _tidy(line)
    return ""


def parse(text: str, known_stores: list[str] = (), today: date | None = None) -> ReceiptInfo:
    return ReceiptInfo(store=find_store(text, known_stores), amount=find_total(text),
                       date=find_date(text, today))


# --- using history ----------------------------------------------------------------

def known_stores(db: Session) -> list[str]:
    """Store names the user has confirmed before; they beat guessing from the first line."""
    return [s for s in db.scalars(select(Attachment.store).where(Attachment.store != "").distinct())]


def guess_category(db: Session, store: str) -> int | None:
    """The category most often used for entries noted with this store name."""
    if not store:
        return None
    rows = db.scalars(select(Transaction.category_id)
                      .where(func.lower(Transaction.note) == store.lower())
                      .order_by(Transaction.date.desc()).limit(20)).all()
    return Counter(rows).most_common(1)[0][0] if rows else None


def matching_entries(db: Session, amount: Decimal | None, on: date | None) -> list[Transaction]:
    """Existing entries this receipt may belong to (typed in by hand, or recurring)."""
    if not amount:
        return []
    center = on or date.today()
    q = (select(Transaction).where(Transaction.amount == amount,
                                   Transaction.date >= center - timedelta(days=10),
                                   Transaction.date <= center + timedelta(days=10))
         .order_by(Transaction.date.desc()).limit(5))
    return list(db.scalars(q))
