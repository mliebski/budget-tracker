from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.fiscal import fiscal_quarter, fy_label, fy_months, fy_of
from app.models import Budget, Category, Planned, Recurring, RecurringSkip, Transaction
from app.recurring import materialize, occurrences
from app.services import budget_for_month, forecast, forecast_months, variable_average
from app.web import UserError, parse_amount, parse_fy, safe_next


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


@pytest.mark.parametrize("url", ["https://evil.com", "//evil.com", "/\\evil.com", "\\\\evil.com",
                                 "/\\/evil.com", "evil.com", ""])
def test_safe_next_rejects_offsite(url):
    assert safe_next(url, "/home") == "/home"


@pytest.mark.parametrize("url", ["/", "/entries?month=2026-10", "/categories#tags"])
def test_safe_next_keeps_local_paths(url):
    assert safe_next(url) == url


@pytest.mark.parametrize("text", ["NaN", "-nan", "Infinity", "inf", "1e999999", "10000000000", "5 + 9999999999.99"])
def test_parse_amount_rejects_non_finite_and_too_large(text):
    with pytest.raises(UserError):
        parse_amount(text)


def test_parse_amount_accepts_the_largest_storable_amount():
    assert parse_amount("9,999,999,999.99") == D("9999999999.99")


@pytest.mark.parametrize("value, fy", [("2026", 2026), (2025, 2025), ("", None), (None, None), ("abc", None),
                                       ("0", None), ("10000", None)])
def test_parse_fy(value, fy):
    assert parse_fy(value) == fy


def test_variable_average_skips_a_partly_recorded_first_month(db):
    groceries = cat(db, "Groceries")
    db.add(Transaction(date=date(2026, 5, 25), amount=D("100"), category_id=groceries.id))  # started late in May
    for month in (6, 7, 8):
        db.add(Transaction(date=date(2026, month, 10), amount=D("600"), category_id=groceries.id))
    db.commit()
    assert variable_average(db, date(2026, 9, 1), 6)[groceries.id] == D("600")  # Jun-Aug, not (100+1800)/4
    # While the partial month is all there is, it is still used
    assert variable_average(db, date(2026, 6, 1), 6)[groceries.id] == D("100")


def test_materialize_uses_skips_and_existing_entries(db):
    rent = cat(db, "Rent / Mortgage")
    r = Recurring(name="Rent", amount=D("2000"), category_id=rent.id, frequency="monthly",
                  start_date=date(2026, 1, 1), active=True)
    db.add(r)
    db.commit()
    db.add(RecurringSkip(recurring_id=r.id, date=date(2026, 2, 1)))
    db.add(Transaction(date=date(2026, 3, 1), amount=D("2000"), category_id=rent.id, recurring_id=r.id,
                       occurrence_date=date(2026, 3, 1)))
    db.commit()
    assert materialize(db, date(2026, 4, 15)) == 2  # Jan and Apr; Feb skipped, Mar exists
    assert materialize(db, date(2026, 4, 15)) == 0


def test_net_worth_series_matches_month_by_month(db):
    from app.models import Asset, AssetValue
    from app.services import net_worth, net_worth_series
    bank = Asset(name="Bank", type="bank")
    car = Asset(name="Car", type="vehicle")
    loan = Asset(name="Loan", type="loan")
    db.add_all([bank, car, loan])
    db.flush()
    for asset, day, value in [(bank, date(2025, 11, 3), 1000), (bank, date(2026, 1, 20), 2500),
                              (bank, date(2026, 1, 28), 2400), (car, date(2025, 12, 1), 20000),
                              (loan, date(2026, 2, 14), 8000), (bank, date(2026, 4, 2), 3100)]:
        db.add(AssetValue(asset_id=asset.id, date=day, value=D(value)))
    db.commit()
    today = date(2026, 4, 10)
    series = net_worth_series(db, today)
    assets = [bank, car, loan]
    from app.fiscal import month_end
    assert [p["month"] for p in series] == [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1),
                                            date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]
    for p in series:
        expected = net_worth(assets, min(month_end(p["month"]), today))
        assert {k: p[k] for k in expected} == expected
    assert series[-1]["net"] == D(3100 + 20000 - 8000)


def test_category_list_is_cached_per_request_until_a_write(db):
    from app.services import all_categories
    db.info["cache"] = True
    first = all_categories(db)
    assert all_categories(db) is first
    db.add(Category(name="Aardvarks", kind="expense"))
    db.flush()
    assert all_categories(db)[0].name == "Aardvarks"


def test_sqlite_enforces_foreign_keys(db):
    from sqlalchemy import text
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
