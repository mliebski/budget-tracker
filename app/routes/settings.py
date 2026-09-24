import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import engine
from ..fiscal import FY_START_MONTH
from ..services import avg_window, set_setting
from ..web import UserError, get_db, redirect, render

router = APIRouter()

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))


def _backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    files = sorted(BACKUP_DIR.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size,
             "when": datetime.fromtimestamp(p.stat().st_mtime)} for p in files[:10]]


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "settings.html", window=avg_window(db), backups=_backups(),
                  backup_dir=str(BACKUP_DIR), backup_enabled=BACKUP_DIR.is_dir(),
                  database=engine.url.get_backend_name(), fy_start_month=FY_START_MONTH)


@router.post("/settings")
def settings_save(avg_months: int = Form(6), db: Session = Depends(get_db)):
    if not 1 <= avg_months <= 24:
        raise UserError("The averaging window must be between 1 and 24 months.")
    set_setting(db, "avg_months", str(avg_months))
    db.commit()
    return redirect("/settings", msg="Settings saved.")
