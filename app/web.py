"""Shared web helpers: templates, form parsing, redirects, DB session dependency."""
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from . import fiscal
from .db import SessionLocal
from .models import ASSET_TYPES, DOC_TYPES, KINDS
from .recurring import FREQUENCIES, ensure_up_to_date

BASE_DIR = Path(__file__).parent

NAV = [
    ("/", "Home"), ("/entries", "Entries"), ("/receipts", "Receipts"), ("/reports", "Reports"),
    ("/forecast", "Forecast"), ("/budgets", "Budgets"), ("/recurring", "Recurring"), ("/planned", "Planned"),
    ("/assets", "Assets"), ("/categories", "Categories"), ("/settings", "Settings"),
]


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers re-check on every load (cheap 304s thanks to the ETag).

    Without this, a phone that used the app before an update keeps running the old
    script for days (heuristic caching), and new buttons silently do nothing.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def static_url(path: str) -> str:
    """/static/app.js?v=<mtime>: a new URL after every change, so no stale copy is reused."""
    try:
        version = int((BASE_DIR / "static" / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


class UserError(Exception):
    """A validation problem to show to the user on the page they came from."""


def get_db():
    db = SessionLocal()
    try:
        ensure_up_to_date(db)
        yield db
    finally:
        db.close()


# --- formatting -----------------------------------------------------------------

def money(value, cents: bool = True) -> str:
    if isinstance(value, Undefined):  # a typo in a template must not silently show $0
        raise TypeError(f"money filter got an undefined value: {value._undefined_name!r}")
    v = Decimal(value or 0)
    text = f"{abs(v):,.2f}" if cents else f"{abs(v):,.0f}"
    return ("−$" if v < 0 else "$") + text


def to_float(value) -> float:
    return round(float(value or 0), 2)


def month_label(d: date, fmt: str = "%b %Y") -> str:
    return d.strftime(fmt)


def percent(part, whole) -> float | None:
    return float(part) / float(whole) * 100 if whole else None


def bar(actual, budget=None, projected=None) -> dict:
    """Geometry (in %) and state for a spent-vs-budget progress bar."""
    a, b, p = float(actual or 0), float(budget or 0), float(projected or 0)
    scale = max(a, b, p) or 1
    if not b:
        state = "none"
    elif a > b + 0.005:
        state = "over"
    elif p > b + 0.005:
        state = "warn"
    else:
        state = "ok"
    return {"fill": a / scale * 100, "proj": max(p - a, 0) / scale * 100,
            "mark": b / scale * 100 if b else None, "state": state}


def chart_json(spec: dict) -> str:
    # Embedded in <script type="application/json">, so "<" must not appear raw.
    return json.dumps(spec, default=to_float).replace("<", "\\u003c")


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters.update(money=money, month=month_label)
templates.env.globals.update(
    NAV=NAV, KINDS=KINDS, DOC_TYPES=DOC_TYPES, static_url=static_url, FREQUENCIES=FREQUENCIES, ASSET_TYPES=ASSET_TYPES,
    fy_label=fiscal.fy_label, percent=percent, chart_json=chart_json, bar=bar,
    month_name=lambda m: date(2000, m, 1).strftime("%B"),
    fy_month_numbers=fiscal.fy_month_numbers(),
)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


FLASH_PARAMS = ("msg", "error", "undo")


def redirect(url: str, msg: str | None = None, **params) -> RedirectResponse:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in FLASH_PARAMS]
    query += [(k, v) for k, v in {"msg": msg, **params}.items() if v is not None]
    url = parts.path + (f"?{urlencode(query)}" if query else "")
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow local paths as redirect targets."""
    if not url:
        return default
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path.startswith("/") or url.startswith("//"):
        return default
    return url


def back_url(request: Request, default: str = "/") -> str:
    ref = request.headers.get("referer")
    if ref:
        parts = urlsplit(ref)
        if parts.netloc == request.url.netloc:
            path = parts.path + (f"?{parts.query}" if parts.query else "")
            return safe_next(path, default)
    return default


# --- parsing --------------------------------------------------------------------

def parse_amount(text: str | None, allow_zero: bool = False, allow_negative: bool = False) -> Decimal:
    """Accepts '12.50', '$1,234', ' 12 + 3.40 ' (sums are handy for split receipts)."""
    cleaned = (text or "").replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned:
        if allow_zero:
            return Decimal("0")
        raise UserError("Please enter an amount.")
    try:
        parts = cleaned.replace("-", "+-").split("+")
        total = sum((Decimal(p) for p in parts if p), Decimal("0"))
    except InvalidOperation:
        raise UserError(f"“{text}” is not a valid amount.")
    total = total.quantize(Decimal("0.01"))
    if total < 0 and not allow_negative:
        raise UserError("Amounts must be positive — pick an income or expense category instead.")
    if total == 0 and not allow_zero:
        raise UserError("Amount must be more than zero.")
    return total


def parse_date(text: str | None, default: date | None = None) -> date:
    if not text:
        if default:
            return default
        raise UserError("Please enter a date.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UserError(f"“{text}” is not a valid date.")


def parse_optional_date(text: str | None) -> date | None:
    return parse_date(text) if text else None


def parse_int(text: str | None) -> int | None:
    try:
        return int(text) if text not in (None, "") else None
    except ValueError:
        raise UserError("Invalid selection.")
