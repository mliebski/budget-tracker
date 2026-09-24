from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.fiscal import fiscal_quarter, fy_label, fy_months, fy_of
from app.models import Budget, Category, Planned, Recurring, RecurringSkip, Transaction
from app.recurring import materialize, occurrences
from app.services import budget_for_month, forecast, forecast_months, variable_average
from app.web import UserError, parse_amount


def cat(db, name):
    return db.scalar(select(Category).where(Category.name == name))


def test_fiscal_year_october_start():
    assert fy_of(date(2026, 9, 24)) == 2025
    assert fy_of(date(2026, 10, 1)) == 2026
    assert fy_label(2025) == "FY 2025–26"
    assert fy_months(2025)[0] == date(2025, 10, 1)
    assert fy_months(2025)[-1] == date(2026, 9, 1)
    assert fiscal_quarter(date(2025, 11, 5)) == 1
    assert fiscal_quarter(date(2026, 1, 5)) == 2
    assert fiscal_quarter(date(2026, 9, 30)) == 4


def test_monthly_occurrences_keep_end_of_month():
    got = occurrences(date(2026, 1, 31), "monthly", None, date(2026, 1, 1), date(2026, 4, 30))
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_biweekly_and_end_date():
    got = occurrences(date(2026, 1, 2), "biweekly", date(2026, 2, 20), date(2026, 1, 20), date(2026, 12, 31))
    assert got == [date(2026, 1, 30), date(2026, 2, 13)]


def test_annual_occurrence():
    got = occurrences(date(2024, 3, 15), "annual", None, date(2026, 1, 1), date(2027, 12, 31))
    assert got == [date(2026, 3, 15), date(2027, 3, 15)]


@pytest.mark.parametrize("text,expected", [("12.5", D("12.50")), ("$1,234", D("1234.00")), ("10 + 2.25", D("12.25"))])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "-5"])
def test_parse_amount_rejects(text):
    with pytest.raises(UserError):
        parse_amount(text)


def test_materialize_respects_skips(db):
    rent = cat(db, "Rent / Mortgage")
    r = Recurring(name="Rent", amount=D("2000"), category_id=rent.id, frequency="monthly",
                  start_date=date(2026, 7, 1), active=True)
    db.add(r)
    db.commit()
    assert materialize(db, date(2026, 9, 24)) == 3
    assert materialize(db, date(2026, 9, 24)) == 0  # idempotent
    db.add(RecurringSkip(recurring_id=r.id, date=date(2026, 10, 1)))
    db.commit()
    assert materialize(db, date(2026, 10, 2)) == 0
    assert len(db.scalars(select(Transaction)).all()) == 3


def test_budget_for_month():
    b = Budget(fiscal_year=2025, category_id=1, monthly=D("100"), annual=D("1200"), annual_month=None)
    assert budget_for_month(b, date(2026, 1, 1)) == D("200")
    b.annual_month = 3
    assert budget_for_month(b, date(2026, 3, 1)) == D("1300")
    assert budget_for_month(b, date(2026, 4, 1)) == D("100")


def test_variable_average_excludes_recurring_and_one_offs(db):
    groceries = cat(db, "Groceries")
    for month in range(3, 9):  # Mar..Aug 2026, six full months
        db.add(Transaction(date=date(2026, month, 10), amount=D("600"), category_id=groceries.id))
    db.add(Transaction(date=date(2026, 5, 3), amount=D("5000"), category_id=groceries.id, one_off=True))
    db.commit()
    avg = variable_average(db, date(2026, 9, 1), 6)
    assert avg[groceries.id] == D("600")
    # Shorter history: only the months that exist are averaged
    avg3 = variable_average(db, date(2026, 6, 1), 6)
    assert avg3[groceries.id] == D("600")


def test_forecast_combines_sources(db):
    groceries, rent, car = cat(db, "Groceries"), cat(db, "Rent / Mortgage"), cat(db, "Car payment")
    for month in range(3, 9):
        db.add(Transaction(date=date(2026, month, 10), amount=D("600"), category_id=groceries.id))
    db.add(Transaction(date=date(2026, 9, 5), amount=D("250"), category_id=groceries.id))
    db.add(Recurring(name="Rent", amount=D("2000"), category_id=rent.id, frequency="monthly",
                     start_date=date(2026, 10, 1), active=True))
    db.add(Planned(name="New car", amount=D("30000"), date=date(2026, 11, 15), category_id=car.id))
    db.commit()

    today = date(2026, 9, 20)  # 10 of 30 days left
    months = forecast_months(today)
    assert months[0] == date(2026, 9, 1) and months[-1] == date(2027, 9, 1)

    fc = forecast(db, today, months)
    sep = fc.cells[date(2026, 9, 1)][groceries.id]
    assert sep.actual == D("250") and sep.variable == D("200")
    nov = fc.cells[date(2026, 11, 1)]
    assert nov[rent.id].recurring == D("2000")
    assert nov[car.id].planned == D("30000")
    assert nov[groceries.id].variable == D("600")
