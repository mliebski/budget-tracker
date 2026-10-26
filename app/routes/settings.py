import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import ai, files, ocr
from ..db import engine
from ..fiscal import FY_START_MONTH
from ..services import avg_window, set_setting, warranty_days
from ..web import UserError, get_db, redirect, render

router = APIRouter()

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))


def _backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    files = sorted(BACKUP_DIR.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size,
             "when": datetime.fromtimestamp(p.stat().st_mtime)} for p in files[:10]]


def _whole_number(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    ai_url, ai_model = ai.config(db)
    return render(request, "settings.html", window=avg_window(db), warranty_days=warranty_days(db),
                  ocr_status=ocr.status(), storage=files.usage(),
                  ai_url=ai_url, ai_model=ai_model, ai_status=ai.status(ai_url), ai_default_url=ai.DEFAULT_URL,
                  files_backed_up=(BACKUP_DIR / "files").is_dir(), backups=_backups(),
                  backup_dir=str(BACKUP_DIR), backup_enabled=BACKUP_DIR.is_dir(),
                  database=engine.url.get_backend_name(), fy_start_month=FY_START_MONTH)


@router.post("/settings")
def settings_save(avg_months: str = Form("6"), warranty_days: str = Form("60"), db: Session = Depends(get_db)):
    # Taken as text so a typo gets a message on the page, not FastAPI's raw 422 error.
    avg_months, warranty_days = _whole_number(avg_months), _whole_number(warranty_days)
    if avg_months is None or not 1 <= avg_months <= 24:
        raise UserError("The averaging window must be between 1 and 24 months.")
    if warranty_days is None or not 0 <= warranty_days <= 365:
        raise UserError("The warranty reminder must be between 0 and 365 days.")
    set_setting(db, "avg_months", str(avg_months))
    set_setting(db, "warranty_days", str(warranty_days))
    db.commit()
    return redirect("/settings", msg="Settings saved.")
