"""AI tab: expense forecast, income & asset forecast, and chat, all on the local Ollama."""
import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai, ai_data
from ..db import SessionLocal
from ..models import AiChat, AiMessage, AiReport
from ..services import set_setting
from ..web import UserError, get_db, redirect, render

router = APIRouter()

KINDS = {"expenses": "Expense forecast", "income": "Income & assets"}
CHAT_HISTORY = 12  # earlier messages sent along with a question

CHAT_GUIDE = (
    "Answer questions about the household's budget using the data below. Show the numbers you used and "
    "add them up carefully. If the data does not answer the question, say so instead of guessing. Keep "
    "answers short; use a small table when comparing several numbers.")


def _parse(report: AiReport | None) -> dict | None:
    if not report or report.status != "done":
        return None
    try:
        return json.loads(report.result)
    except ValueError:
        return None


def _num(v) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal(0)


def _expense_view(result: dict, app_fc: dict) -> dict:
    """AI months x categories, plus checks against its own sums and the app's forecast."""
    months = [m for m in result.get("months", []) if isinstance(m, dict)]
    categories = []
    for m in months:
        for c in m.get("categories", []):
            if c.get("category") not in categories:
                categories.append(c.get("category"))
    table = {c: [] for c in categories}
    footer = {"ai_total": [], "sum": [], "app": [], "mismatch": []}
    for m in months:
        amounts = {c.get("category"): _num(c.get("amount")) for c in m.get("categories", [])}
        for c in categories:
            table[c].append(amounts.get(c))
        total, summed = _num(m.get("total")), sum(amounts.values(), Decimal(0))
        footer["ai_total"].append(total)
        footer["sum"].append(summed)
        footer["mismatch"].append(abs(total - summed) > max(Decimal(1), summed * Decimal("0.005")))
        footer["app"].append(sum(app_fc.get(m.get("month", ""), {}).values(), Decimal(0)))
    labels = [m.get("month", "?") for m in months]
    chart = {"type": "bar", "labels": labels, "datasets": [
        {"label": "AI forecast", "role": "variable", "data": footer["sum"]},
        {"label": "App forecast", "role": "budget", "type": "line", "data": footer["app"]},
    ]}
    return {"labels": labels, "rows": [{"category": c, "values": v} for c, v in table.items()],
            "footer": footer, "chart": chart}


def _income_view(result: dict, app_income: dict, app_assets: list[dict]) -> dict:
    months = [m for m in result.get("income_months", []) if isinstance(m, dict)]
    income = [{"month": m.get("month", "?"), "ai": _num(m.get("amount")),
               "app": app_income.get(m.get("month", ""))} for m in months]
    by_name = {a["name"].lower(): a for a in app_assets}
    assets = []
    for a in result.get("assets", []):
        if isinstance(a, dict):
            ref = by_name.get(str(a.get("name", "")).lower())
            assets.append({"name": a.get("name", "?"), "now": _num(a.get("now")),
                           "in_1_year": _num(a.get("in_1_year")), "in_5_years": _num(a.get("in_5_years")),
                           "ref": ref})
    nw = result.get("net_worth") or {}
    app_nw = {k: sum(((-1 if a["liability"] else 1) * a[k] for a in app_assets), Decimal(0))
              for k in ("now", "in_1_year", "in_5_years")}
    chart = {"type": "bar", "labels": [r["month"] for r in income], "datasets": [
        {"label": "AI forecast", "role": "income", "data": [r["ai"] for r in income]},
        {"label": "App forecast", "role": "budget", "type": "line", "data": [r["app"] for r in income]},
    ]}
    ai_nw = {k: _num(nw.get(k)) for k in ("now", "in_1_year", "in_5_years")}
    # Today's net worth is a plain sum of the latest values, so a big gap means the AI misread
    # the data (typically by adding debts instead of subtracting them).
    off = app_assets and abs(ai_nw["now"] - app_nw["now"]) > max(Decimal(100), abs(app_nw["now"]) * Decimal("0.02"))
    return {"income": income, "assets": assets, "chart": chart, "net_worth": ai_nw, "app_net_worth": app_nw,
            "net_worth_off": off}


@router.get("/ai")
def ai_home():
    return redirect("/ai/expenses")


@router.get("/ai/chat")
def chat_latest(db: Session = Depends(get_db)):
    latest = db.scalar(select(AiChat).order_by(AiChat.id.desc()))
    return redirect(f"/ai/chat/{latest.id}" if latest else "/ai/chat/new")


@router.get("/ai/chat/new")
def chat_new(request: Request, db: Session = Depends(get_db)):
    return _chat_page(request, db, None)


@router.get("/ai/chat/{cid}")
def chat_view(request: Request, cid: int, db: Session = Depends(get_db)):
    chat = db.get(AiChat, cid)
    if not chat:
        return redirect("/ai/chat/new")
    return _chat_page(request, db, chat)


def _chat_page(request: Request, db: Session, chat: AiChat | None):
    url, model = ai.config(db)
    chats = list(db.scalars(select(AiChat).order_by(AiChat.id.desc()).limit(30)))
    messages = [{"role": m.role, "content": m.content} for m in chat.messages] if chat else []
    return render(request, "ai_chat.html", chat=chat, chats=chats, messages=messages, model=model,
                  ai_url=url, tab="chat")


@router.post("/ai/chat/send")
def chat_send(message: str = Form(""), chat_id: str = Form(""), db: Session = Depends(get_db)):
    text = message.strip()
    if not text:
        return JSONResponse({"error": "Please type a question."}, status_code=400)
    url, model = ai.config(db)
    chat = db.get(AiChat, int(chat_id)) if chat_id.isdigit() else None
    if not chat:
        chat = AiChat(title=text[:120])
        db.add(chat)
        db.flush()
    history = [{"role": m.role, "content": m.content} for m in chat.messages][-CHAT_HISTORY:]
    db.add(AiMessage(chat_id=chat.id, role="user", content=text[:8000]))
    db.commit()
    chat_ref = chat.id
    system = f"{ai_data.SYSTEM}\n\n{CHAT_GUIDE}\n\n{ai_data.snapshot(db, date.today(), for_chat=True)}"
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]

    def events():
        answer, failed = [], None
        yield json.dumps({"t": "chat", "id": chat_ref}) + "\n"
        try:
            for kind, chunk in ai.chat_stream(url, model, messages):
                if kind == "text":
                    answer.append(chunk)
                    yield json.dumps({"t": "text", "c": chunk}) + "\n"
                else:
                    yield json.dumps({"t": "thinking"}) + "\n"
        except ai.AiError as e:
            failed = str(e)
            yield json.dumps({"t": "error", "c": failed}) + "\n"
        finally:
            content = "".join(answer).strip()
            if content or failed:
                with SessionLocal() as s:  # the request's session is closed by now
                    s.add(AiMessage(chat_id=chat_ref, role="assistant", model=model,
                                    content=content or f"_(No answer: {failed})_"))
                    s.commit()
        yield json.dumps({"t": "done"}) + "\n"

    # X-Accel-Buffering keeps any proxy from holding the stream back.
    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/ai/chat/{cid}/delete")
def chat_delete(cid: int, db: Session = Depends(get_db)):
    chat = db.get(AiChat, cid)
    if chat:
        db.delete(chat)
        db.commit()
    return redirect("/ai/chat", msg="Chat deleted.")


@router.get("/ai/reports/{rid}/status")
def report_status(rid: int, db: Session = Depends(get_db)):
    report = db.get(AiReport, rid)
    if not report:
        return JSONResponse({"status": "missing"}, status_code=404)
    return {"status": "failed" if ai_data.is_stale(report) else report.status}


@router.post("/ai/reports/{rid}/delete")
def report_delete(rid: int, db: Session = Depends(get_db)):
    report = db.get(AiReport, rid)
    kind = report.kind if report else "expenses"
    if report and report.status != "running":
        db.delete(report)
        db.commit()
    return redirect(f"/ai/{kind}", msg="Report deleted.")


@router.post("/ai/settings")
def ai_settings(ai_url: str = Form(""), ai_model: str = Form(""), db: Session = Depends(get_db)):
    url = ai_url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise UserError("The Ollama address must start with http:// or https://, e.g. http://host.docker.internal:11434")
    set_setting(db, "ai_url", url[:255])
    set_setting(db, "ai_model", ai_model.strip()[:100])
    db.commit()
    return redirect("/settings", msg="AI settings saved.")


@router.get("/ai/{kind}")
def report_page(request: Request, kind: str, report: int | None = None, db: Session = Depends(get_db)):
    if kind not in KINDS:
        return redirect("/ai/expenses")
    today = date.today()
    url, model = ai.config(db)
    runs = list(db.scalars(select(AiReport).where(AiReport.kind == kind).order_by(AiReport.id.desc()).limit(15)))
    for r in runs:
        if ai_data.is_stale(r):
            r.status, r.error = "failed", "Interrupted (the app restarted while the report was running)."
    db.commit()
    running = next((r for r in runs if r.status == "running"), None)
    shown = next((r for r in runs if r.id == report), None) if report else \
        next((r for r in runs if r.status == "done"), None)
    last_failed = runs[0] if runs and runs[0].status == "failed" and not report else None
    result = _parse(shown)
    view = None
    if result:
        view = _expense_view(result, ai_data.app_expense_forecast(db, today)) if kind == "expenses" else \
            _income_view(result, ai_data.app_income_forecast(db, today), ai_data.app_asset_projection(db))
    return render(request, f"ai_{kind}.html", tab=kind, kind=kind, title=KINDS[kind], runs=runs,
                  running=running, shown=shown, result=result, view=view, last_failed=last_failed,
                  model=model, ai_url=url, months=ai_data.forecast_window(today))


@router.post("/ai/{kind}/run")
def report_run(kind: str, db: Session = Depends(get_db)):
    if kind not in KINDS:
        return redirect("/ai/expenses")
    _, model = ai.config(db)
    if not model:
        raise UserError("Pick an AI model in Settings first.")
    busy = db.scalar(select(AiReport).where(AiReport.kind == kind, AiReport.status == "running"))
    if busy and not ai_data.is_stale(busy):
        return redirect(f"/ai/{kind}", msg="A report is already being written.")
    ai_data.start_report(db, kind)
    return redirect(f"/ai/{kind}")
