from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ASSET_TYPES, Asset, AssetValue
from ..services import net_worth, net_worth_series
from ..web import UserError, get_db, money, parse_amount, parse_date, redirect, render

router = APIRouter()


def _set_value(db: Session, asset: Asset, value: str, on: str) -> AssetValue:
    d = parse_date(on, date.today())
    amount = parse_amount(value, allow_zero=True, allow_negative=True)
    row = db.scalar(select(AssetValue).where(AssetValue.asset_id == asset.id, AssetValue.date == d))
    if row:
        row.value = amount
    else:
        row = AssetValue(asset_id=asset.id, date=d, value=amount)
        db.add(row)
    return row


def _growth(text: str) -> Decimal | None:
    """'5', '-15', '4.5 %' -> percent per year; empty -> use the type's default."""
    cleaned = text.replace("%", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise UserError(f"“{text}” is not a percentage. Use e.g. 5 or -15.")
    if not -100 <= value <= 100:
        raise UserError("The expected change must be between -100% and +100% per year.")
    return value


def _validate(name: str, type_: str) -> None:
    if not name.strip():
        raise UserError("Please give it a name.")
    if type_ not in ASSET_TYPES:
        raise UserError("Please pick a type.")


@router.get("/assets")
def assets_page(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    assets = list(db.scalars(select(Asset).order_by(Asset.name)))
    groups = {"assets": [], "liabilities": []}
    archived = []
    for a in assets:
        latest = a.values[-1] if a.values else None
        previous = a.values[-2] if len(a.values) > 1 else None
        row = {"a": a, "latest": latest,
               "change": latest.value - previous.value if latest and previous else None}
        if a.archived:
            archived.append(row)
        else:
            groups["liabilities" if a.is_liability else "assets"].append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: (list(ASSET_TYPES).index(r["a"].type), r["a"].name))

    series = net_worth_series(db, today)
    chart = None
    if series:
        chart = {"type": "line", "labels": [p["month"].strftime("%b %y") for p in series], "datasets": [
            {"label": "Net worth", "role": "net", "data": [p["net"] for p in series]},
            {"label": "Assets", "role": "income", "data": [p["assets"] for p in series]},
            {"label": "Liabilities", "role": "expense", "data": [p["liabilities"] for p in series]},
        ]}
    return render(request, "assets.html", groups=groups, archived=archived, today=today,
                  totals=net_worth([a for a in assets if not a.archived], today), networth_chart=chart)


@router.post("/assets")
def asset_create(name: str = Form(""), type: str = Form(""), note: str = Form(""),
                 value: str = Form(""), value_date: str = Form(""), growth: str = Form(""),
                 db: Session = Depends(get_db)):
    _validate(name, type)
    asset = Asset(name=name.strip()[:100], type=type, note=note.strip()[:255], growth_rate=_growth(growth))
    db.add(asset)
    db.flush()
    if value.strip():
        _set_value(db, asset, value, value_date)
    db.commit()
    return redirect("/assets", msg=f"Added “{asset.name}”.")


@router.post("/assets/{aid}/value")
def asset_value(aid: int, value: str = Form(""), value_date: str = Form(""), next: str = Form("/assets"),
                db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if not asset:
        return redirect("/assets")
    row = _set_value(db, asset, value, value_date)
    db.commit()
    target = f"/assets/{aid}" if next == "detail" else "/assets"
    return redirect(target, msg=f"{asset.name}: {money(row.value)} on {row.date:%d %b %Y}.")


@router.get("/assets/{aid}")
def asset_detail(request: Request, aid: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if not asset:
        return redirect("/assets")
    chart = None
    if asset.values:
        chart = {"type": "line", "labels": [v.date.strftime("%d %b %y") for v in asset.values],
                 "datasets": [{"label": asset.name, "role": "expense" if asset.is_liability else "net",
                               "data": [v.value for v in asset.values]}]}
    return render(request, "asset_detail.html", asset=asset, history_chart=chart, today=date.today(),
                  history=list(reversed(asset.values)))


@router.post("/assets/{aid}")
def asset_update(aid: int, name: str = Form(""), type: str = Form(""), note: str = Form(""),
                 growth: str = Form(""), db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if not asset:
        return redirect("/assets")
    _validate(name, type)
    asset.name, asset.type, asset.note = name.strip()[:100], type, note.strip()[:255]
    asset.growth_rate = _growth(growth)
    db.commit()
    return redirect(f"/assets/{aid}", msg="Saved.")


@router.post("/assets/{aid}/archive")
def asset_archive(aid: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if asset:
        asset.archived = not asset.archived
        if asset.archived and asset.values and asset.values[-1].value != 0:
            # Sold/closed: drop out of net worth from today, keep the history.
            _set_value(db, asset, "0", date.today().isoformat())
        db.commit()
        return redirect("/assets", msg=f"“{asset.name}” {'archived' if asset.archived else 'restored'}.")
    return redirect("/assets")


@router.post("/assets/{aid}/delete")
def asset_delete(aid: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, aid)
    if asset:
        db.delete(asset)
        db.commit()
    return redirect("/assets", msg="Deleted, including its value history.")


@router.post("/asset-values/{vid}/delete")
def asset_value_delete(vid: int, db: Session = Depends(get_db)):
    row = db.get(AssetValue, vid)
    if not row:
        return redirect("/assets")
    aid = row.asset_id
    db.delete(row)
    db.commit()
    return redirect(f"/assets/{aid}", msg="Value removed.")

