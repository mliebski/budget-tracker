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
         "/recurring/new", "/planned", "/assets", "/categories", "/settings", "/receipts", "/warranties",
         "/warranties/new", "/receipts?q=x&amount=10-20&doc_type=receipt&date_from=2026-01-01"]


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


@pytest.mark.parametrize("url", ["/entries?fy=0", "/entries?fy=10000", "/entries?fy=abc", "/reports?fy=0&view=year",
                                 "/reports?fy=99999", "/budgets?fy=0", "/budgets?fy=abc"])
def test_bad_fiscal_year_falls_back_instead_of_crashing(client, url):
    assert client.get(url).status_code == 200


def test_budget_form_rejects_bad_input(client, db):
    from app.fiscal import fy_of
    fy = fy_of(date.today())
    food = cat_id(db, "Groceries")
    for data in ({"fy": "abc", f"m_{food}": "100"}, {f"m_{food}": "100"}, {"fy": "0", f"m_{food}": "100"},
                 {"fy": fy, f"m_{food}": "0", f"a_{food}": "500", f"am_{food}": "13"}):
        r = client.post("/budgets", data=data, follow_redirects=False)
        assert r.status_code == 303 and "error=1" in r.headers["location"]
    # Unknown category ids and odd field names are ignored, not a crash
    r = client.post("/budgets", data={"fy": fy, "m_99999": "100", "m_x": "5", f"m_{food}": "250"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    from app.models import Budget
    assert [(b.category_id, b.monthly) for b in db.scalars(select(Budget))] == [(food, 250)]
    r = client.post("/budgets/copy", data={"fy": "abc"}, follow_redirects=False)
    assert r.status_code == 303 and "error=1" in r.headers["location"]


def test_undo_button_only_for_an_entry_id(client):
    assert "/entries/42/delete" in client.get("/?msg=Added&undo=42").text
    page = client.get("/?msg=Added&undo=../../categories/5").text
    assert "categories/5/delete" not in page and "Undo" not in page


def test_tag_rename_messages(client, db):
    from app.models import Tag
    client.post("/entries", data={"amount": "5", "category_id": cat_id(db, "Groceries"), "tags": "Shop"})
    tag = db.scalar(select(Tag))
    assert "no+longer+exists" in client.post("/tags/9999", data={"name": "X"}, follow_redirects=False).headers["location"]
    r = client.post(f"/tags/{tag.id}", data={"name": "  "}, follow_redirects=False)
    assert "error=1" in r.headers["location"]


def test_tag_delete_also_clears_recurring_items(client, db):
    from app.models import Tag, recurring_tags
    client.post("/recurring", data={"name": "Gym", "amount": "40", "category_id": cat_id(db, "Fitness"),
                                    "frequency": "monthly", "tags": "Health"})
    tag = db.scalar(select(Tag))
    client.post(f"/tags/{tag.id}/delete")
    assert db.execute(select(recurring_tags)).all() == []


def test_settings_typo_gets_a_message(client):
    r = client.post("/settings", data={"avg_months": "six", "warranty_days": "60"}, follow_redirects=False)
    assert r.status_code == 303 and "error=1" in r.headers["location"]


def test_entry_search_treats_percent_literally(client, db):
    groceries = cat_id(db, "Groceries")
    client.post("/entries", data={"amount": "5", "category_id": groceries, "note": "50% off"})
    client.post("/entries", data={"amount": "6", "category_id": groceries, "note": "500 points"})
    page = client.get("/entries?q=50%25").text
    assert "50% off" in page and "500 points" not in page


def test_asset_value_returns_to_the_detail_page(client, db):
    client.post("/assets", data={"name": "Chequing", "type": "bank"})
    a = db.scalar(select(Asset))
    r = client.post(f"/assets/{a.id}/value", data={"value": "100", "next": f"/assets/{a.id}"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/assets/{a.id}?")
    r = client.post(f"/assets/{a.id}/value", data={"value": "100", "next": "https://evil.com"}, follow_redirects=False)
    assert r.headers["location"].startswith("/assets?")
