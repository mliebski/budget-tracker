"""Smoke tests: every page renders and the main forms work end to end."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Asset, Category, Planned, Recurring, Transaction


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def cat_id(db, name):
    return db.scalar(select(Category.id).where(Category.name == name))


def populate(client, db):
    today = date.today()
    groceries, salary = cat_id(db, "Groceries"), cat_id(db, "Salary")
    for i in range(1, 200, 9):
        d = today - timedelta(days=i)
        client.post("/entries", data={"amount": "85.40", "category_id": groceries, "date": d.isoformat(),
                                      "tags": "Weekly shop"})
    client.post("/entries", data={"amount": "1,200", "category_id": cat_id(db, "Travel"),
                                  "date": today.isoformat(), "tags": "Vacation 2026, Summer", "one_off": "true"})
    client.post("/recurring", data={"name": "Pay", "amount": "3100", "category_id": salary,
                                    "frequency": "biweekly", "start_date": (today - timedelta(days=60)).isoformat()})
    client.post("/recurring", data={"name": "Rent", "amount": "2100", "category_id": cat_id(db, "Rent / Mortgage"),
                                    "frequency": "monthly", "start_date": (today - timedelta(days=90)).isoformat()})
    client.post("/planned", data={"name": "New roof", "amount": "14000", "date": (today + timedelta(days=200)).isoformat(),
                                  "category_id": cat_id(db, "Maintenance & repairs")})
    client.post("/assets", data={"name": "Chequing", "type": "bank", "value": "8500",
                                 "value_date": (today - timedelta(days=40)).isoformat()})
    client.post("/assets", data={"name": "Mortgage", "type": "mortgage", "value": "310000"})
    from app.fiscal import fy_of
    client.post("/budgets", data={"fy": fy_of(today), f"m_{groceries}": "400",
                                  f"a_{cat_id(db, 'Home insurance')}": "1800",
                                  f"am_{cat_id(db, 'Home insurance')}": "3", f"m_{salary}": "6500"})


PAGES = ["/", "/entries", "/entries?fy=2025", "/reports", "/reports?view=quarter", "/reports?view=year",
         "/reports?view=year&fy=2024", "/forecast", "/budgets", "/budgets?fy=2030", "/recurring",
         "/recurring/new", "/planned", "/assets", "/categories", "/settings"]


def test_pages_render_empty(client):
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, page


def test_pages_render_with_data(client, db):
    populate(client, db)
    for page in PAGES + ["/reports?view=year&tag=1", "/recurring/1", "/assets/1", "/entries/1/edit"]:
        r = client.get(page)
        assert r.status_code == 200, page
    assert db.scalar(select(Recurring).where(Recurring.name == "Rent")) is not None
    # recurring items back-filled their past occurrences
    assert db.scalars(select(Transaction).where(Transaction.recurring_id.is_not(None))).first() is not None
    assert "Quick add" in client.get("/").text
    assert "New roof" in client.get("/forecast").text


def test_quick_add_and_undo(client, db):
    r = client.post("/entries", data={"amount": "12.50", "category_id": cat_id(db, "Coffee & snacks"), "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303 and "undo=" in r.headers["location"]
    entry = db.scalar(select(Transaction))
    assert str(entry.amount) == "12.50" and entry.date == date.today()
    client.post(f"/entries/{entry.id}/delete", data={"next": "/"})
    db.expire_all()
    assert db.scalar(select(Transaction)) is None


def test_validation_error_redirects_back(client, db):
    r = client.post("/entries", data={"amount": "abc", "category_id": cat_id(db, "Groceries")},
                    headers={"referer": "http://testserver/entries"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/entries?") and "error=1" in r.headers["location"]


def test_planned_record_and_asset_archive(client, db):
    populate(client, db)
    p = db.scalar(select(Planned))
    client.post(f"/planned/{p.id}/record", data={"amount": "15250", "date": date.today().isoformat()})
    db.expire_all()
    p = db.get(Planned, p.id)
    assert p.status == "done"
    t = db.scalar(select(Transaction).where(Transaction.planned_id == p.id))
    assert str(t.amount) == "15250.00" and t.one_off
    a = db.scalar(select(Asset).where(Asset.name == "Chequing"))
    client.post(f"/assets/{a.id}/archive")
    db.expire_all()
    a = db.get(Asset, a.id)
    assert a.archived and a.values[-1].value == 0


def test_delete_used_category_is_refused(client, db):
    populate(client, db)
    r = client.post(f"/categories/{cat_id(db, 'Groceries')}/delete", follow_redirects=False)
    assert "error=1" in r.headers["location"]
