"""AI tab: data snapshot, reports via Ollama, chat streaming, asset growth rates."""
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app import ai, ai_data
from app.main import app
from app.migrate import add_missing_columns
from app.models import AiChat, AiMessage, AiReport, Asset, Category

import fake_ollama  # tests/ is on sys.path under pytest


@pytest.fixture(scope="module")
def ollama():
    server = fake_ollama.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def reset_fake():
    fake_ollama.State.requests.clear()
    fake_ollama.State.delay, fake_ollama.State.fail_with, fake_ollama.State.think = 0, "", False


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def connected(client, ollama):
    client.post("/ai/settings", data={"ai_url": ollama, "ai_model": "qwen3:8b"})
    return client


def cat_id(db, name):
    return db.scalar(select(Category.id).where(Category.name == name))


def seed(client, db):
    today = date.today()
    for i in range(1, 400, 15):
        client.post("/entries", data={"amount": "85.40", "category_id": cat_id(db, "Groceries"),
                                      "date": (today - timedelta(days=i)).isoformat(), "tags": "Weekly shop"})
    client.post("/entries", data={"amount": "1899", "category_id": cat_id(db, "Furniture & appliances")
                                  or cat_id(db, "Maintenance & repairs"), "date": today.isoformat(),
                                  "note": "Dishwasher", "one_off": "true"})
    client.post("/recurring", data={"name": "Pay", "amount": "3100", "category_id": cat_id(db, "Salary"),
                                    "frequency": "biweekly", "start_date": (today - timedelta(days=60)).isoformat()})
    client.post("/assets", data={"name": "TFSA", "type": "investment", "value": "20000"})
    client.post("/assets", data={"name": "Civic", "type": "vehicle", "value": "18000", "growth": "-12"})
    client.post("/assets", data={"name": "Mortgage", "type": "mortgage", "value": "300000"})


def wait_for(db, report_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        db.expire_all()
        r = db.get(AiReport, report_id)
        if r.status != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("report did not finish")


# --- data & settings ---------------------------------------------------------------

def test_snapshot_has_the_numbers_the_ai_needs(client, db):
    seed(client, db)
    text = ai_data.snapshot(db, date.today())
    assert "Canadian dollars" in text and "Groceries" not in text.split("## One-off")[0]  # rolled up to Food
    table = text.split("## Spending per month")[1].split("##")[0]
    assert "month,Food,total" in table and "85.40" in table
    assert "Dishwasher" in text.split("## One-off purchases")[1].split("##")[0]  # listed apart
    assert "1899" not in table                                                     # not in normal spending
    assert "Pay,Income › Salary" not in text and "Pay,Salary,income,3100,Every 2 weeks" in text
    assets = text.split("## Assets and debts")[1]
    assert 'TFSA,"Investment (RRSP, TFSA, …)",asset,20000' in assets and ",5," in assets
    assert "Civic,Vehicle,asset,18000" in assets and ",-12," in assets and "-12.00" not in assets
    assert "Mortgage,Mortgage,debt,300000" in assets
    chat = ai_data.snapshot(db, date.today(), for_chat=True)
    assert "Weekly shop,expense" in chat and "most recent entries" in chat


def test_settings_lists_models_and_saves_choice(client, db, ollama):
    page = client.get("/settings").text
    assert "Not connected" in page
    client.post("/ai/settings", data={"ai_url": ollama, "ai_model": "llama3.2:3b"})
    page = client.get("/settings").text
    assert "Working" in page and "0.12.6-fake" in page
    assert '<option value="llama3.2:3b" selected>' in page and "qwen3:8b · 8.2B" in page
    r = client.post("/ai/settings", data={"ai_url": "ollama:11434"}, headers={"referer": "http://testserver/settings"},
                    follow_redirects=False)
    assert "error=1" in r.headers["location"]


def test_asset_growth_rate(client, db):
    client.post("/assets", data={"name": "RRSP", "type": "investment", "growth": "4.5 %"})
    client.post("/assets", data={"name": "Car", "type": "vehicle"})
    rrsp, car = db.scalar(select(Asset).where(Asset.name == "RRSP")), db.scalar(select(Asset).where(Asset.name == "Car"))
    assert rrsp.growth == D("4.50") and car.growth_rate is None and car.growth == -15
    client.post(f"/assets/{car.id}", data={"name": "Car", "type": "vehicle", "growth": "-10"})
    db.expire_all()
    assert db.get(Asset, car.id).growth_rate == D("-10.00")
    assert "-10%/yr" in client.get("/assets").text
    r = client.post(f"/assets/{car.id}", data={"name": "Car", "type": "vehicle", "growth": "fast"},
                    headers={"referer": "http://testserver/assets"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]


def test_migration_adds_growth_column_to_old_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name VARCHAR(100), type VARCHAR(20), "
                "note VARCHAR(255), archived BOOLEAN)")
    con.execute("INSERT INTO assets VALUES (1, 'TFSA', 'investment', '', 0)")
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{path}")
    add_missing_columns(engine)
    add_missing_columns(engine)  # running it again changes nothing
    assert "growth_rate" in [c["name"] for c in inspect(engine).get_columns("assets")]


# --- reports -------------------------------------------------------------------------

def test_expense_report(connected, db):
    seed(connected, db)
    assert "No report yet" in connected.get("/ai/expenses").text
    connected.post("/ai/expenses/run")
    report = db.scalar(select(AiReport))
    assert 'data-ai-poll' in connected.get("/ai/expenses").text or report.status != "running"
    report = wait_for(db, report.id)
    assert report.status == "done", report.error

    req = fake_ollama.State.requests[-1]
    assert req["model"] == "qwen3:8b" and req["format"]["required"][1] == "months" and not req["stream"]
    assert req["options"]["num_ctx"] >= 8192
    assert "## Spending per month" in req["messages"][1]["content"]

    page = connected.get("/ai/expenses").text
    assert "Spending forecast by category" in page and "Transportation" in page
    assert "App forecast" in page
    assert "doesn't match the sum of its categories" in page  # the fake's last month is off by 100
    months = ai_data.forecast_window(date.today())
    assert months[0].strftime("%Y-%m") in page


def test_income_report_compares_asset_growth(connected, db):
    seed(connected, db)
    connected.post("/ai/income/run")
    report = wait_for(db, db.scalar(select(AiReport)).id)
    assert report.status == "done", report.error
    page = connected.get("/ai/income").text
    assert "Income forecast" in page and "TFSA" in page and "Civic" in page
    assert "app $21,000" in page                 # TFSA 20,000 at +5%
    assert "app $15,840" in page                 # Civic 18,000 at -12%
    assert "-12%/yr" in page and "debt" in page
    assert "probably misread a debt as an asset" in page  # the fake adds the mortgage to net worth


def test_report_failure_is_shown(connected, db):
    fake_ollama.State.fail_with = "model requires more system memory (9.1 GiB) than is available (4.0 GiB)"
    connected.post("/ai/expenses/run")
    report = wait_for(db, db.scalar(select(AiReport)).id)
    assert report.status == "failed" and "more system memory" in report.error
    assert "The last run failed" in connected.get("/ai/expenses").text


def test_report_needs_a_model(client, db):
    r = client.post("/ai/expenses/run", headers={"referer": "http://testserver/ai/expenses"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    assert "No AI model chosen yet" in client.get("/ai/expenses").text


def test_interrupted_report_is_marked_failed(client, db):
    db.add(AiReport(kind="income", model="qwen3:8b", status="running", created_at=datetime.now() - timedelta(hours=1)))
    db.commit()
    assert "Interrupted" in client.get("/ai/income").text


# --- chat ----------------------------------------------------------------------------

def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_chat_streams_and_keeps_history(connected, db):
    seed(connected, db)
    fake_ollama.State.think = True
    r = connected.post("/ai/chat/send", data={"message": "Where did my money go?"})
    ev = events(r)
    assert ev[0]["t"] == "chat" and ev[-1]["t"] == "done"
    assert any(e["t"] == "thinking" for e in ev)
    text = "".join(e["c"] for e in ev if e["t"] == "text")
    assert "**Housing**" in text and "| Food | $1,830 |" in text
    chat = db.scalar(select(AiChat))
    assert chat.title == "Where did my money go?"
    db.expire_all()
    assert [m.role for m in db.get(AiChat, chat.id).messages] == ["user", "assistant"]

    system = fake_ollama.State.requests[-1]["messages"][0]["content"]
    assert "most recent entries" in system and "Weekly shop" in system

    connected.post("/ai/chat/send", data={"message": "And last year?", "chat_id": str(chat.id)})
    sent = fake_ollama.State.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    page = connected.get(f"/ai/chat/{chat.id}").text
    assert "And last year?" in page and "data-markdown" in page

    connected.post(f"/ai/chat/{chat.id}/delete")
    db.expire_all()
    assert db.scalar(select(AiChat)) is None and db.scalar(select(AiMessage)) is None


def test_chat_error_is_reported_and_saved(connected, db):
    fake_ollama.State.fail_with = "model 'qwen3:8b' not found"
    ev = events(connected.post("/ai/chat/send", data={"message": "Hi"}))
    assert any(e["t"] == "error" and "not found" in e["c"] for e in ev)
    assert "No answer" in db.scalar(select(AiMessage).where(AiMessage.role == "assistant")).content


def test_chat_without_ollama(client, db):
    client.post("/ai/settings", data={"ai_url": "http://127.0.0.1:9", "ai_model": "qwen3:8b"})
    ev = events(client.post("/ai/chat/send", data={"message": "Hi"}))
    assert any(e["t"] == "error" and "not reachable" in e["c"] for e in ev)


def test_think_tags_and_context_size():
    assert ai.THINK.sub("", "<think>sum it up</think>{\"a\": 1}") == "{\"a\": 1}"
    assert ai.context_size([{"content": "x" * 100}]) == 8192
    assert ai.context_size([{"content": "x" * 60000}]) == 32768


def test_pages_render(client):
    for page in ("/ai/expenses", "/ai/income", "/ai/chat/new", "/settings"):
        assert client.get(page).status_code == 200, page
    assert client.get("/ai", follow_redirects=False).headers["location"] == "/ai/expenses"
