import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.exc import OperationalError

from .db import Base, SessionLocal, engine
from .migrate import add_missing_columns
from .routes import (
    ai,
    assets, budgets, categories, dashboard, entries, planned, receipts, recurring, reports, settings,
    warranties,
)
from .seed import seed_categories
from .web import BASE_DIR, RevalidatingStaticFiles, UserError, back_url, redirect

log = logging.getLogger("budget")


def init_db(retries: int = 30) -> None:
    """Create tables, waiting for the database container to accept connections."""
    for attempt in range(retries):
        try:
            Base.metadata.create_all(engine)
            add_missing_columns(engine)
            break
        except OperationalError:
            if attempt == retries - 1:
                raise
            log.warning("Database not ready, retrying in 2s…")
            time.sleep(2)
    with SessionLocal() as db:
        seed_categories(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Budget", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", RevalidatingStaticFiles(directory=BASE_DIR / "static"), name="static")

for module in (dashboard, entries, receipts, warranties, reports, budgets, recurring, planned, assets,
               categories, settings, ai):
    app.include_router(module.router)


@app.exception_handler(UserError)
async def user_error(request: Request, exc: UserError):
    return redirect(back_url(request), msg=str(exc), error=1)


@app.get("/health")
def health():
    return {"ok": True}
