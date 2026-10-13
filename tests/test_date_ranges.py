"""A "last N days" range covers exactly N calendar dates, counting today as
day one. Covers the shared helper and the register and Compare defaults."""
import datetime

import pytest

from app import config, utils
from app.api import Api
from app.db import open_db

TODAY = "2026-01-05"


def _freeze_today(monkeypatch, iso_date=TODAY):
    real_date = datetime.date

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(iso_date)

    monkeypatch.setattr(utils.datetime, "date", FrozenDate)


def _days_before(days):
    return (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days)).isoformat()


def _api_with_dates(tmp_path, dates):
    conn = open_db(str(tmp_path / "ranges.db"))
    conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Checking', 0, '2025-01-01', '2025-01-01')"
    )
    conn.executemany(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, created_at) "
        "VALUES (1, ?, ?, '', '', 100, 0, '2025-01-01')",
        [(d, d) for d in dates],
    )
    conn.commit()
    api = Api("test")
    api.set_conn(conn)
    return api


@pytest.mark.parametrize(
    "days, first_included, first_excluded",
    [
        (7, "2025-12-30", "2025-12-29"),
        (14, "2025-12-23", "2025-12-22"),
        (30, "2025-12-07", "2025-12-06"),
        (90, "2025-10-08", "2025-10-07"),
    ],
)
def test_range_start_counts_today_as_day_one(monkeypatch, days, first_included, first_excluded):
    _freeze_today(monkeypatch)
    start = utils.range_start_iso(days)
    assert start == first_included
    first = datetime.date.fromisoformat(start)
    # Today through the start date, inclusive, is exactly N dates.
    assert (datetime.date.fromisoformat(TODAY) - first).days + 1 == days
    assert (first - datetime.timedelta(days=1)).isoformat() == first_excluded


def test_register_default_range_is_last_thirty_dates(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    days = config.DEFAULT_RANGE_DAYS
    first_included = _days_before(days - 1)
    first_excluded = _days_before(days)
    api = _api_with_dates(tmp_path, [first_excluded, first_included, TODAY])
    try:
        result = api.get_transactions(1)
        assert result["ok"] is True
        assert sorted(row["date"] for row in result["rows"]) == [first_included, TODAY]
    finally:
        api._conn.close()


def test_compare_default_range_is_last_ninety_dates(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    days = config.COMPARE_RANGE_DAYS
    first_included = _days_before(days - 1)
    first_excluded = _days_before(days)
    api = _api_with_dates(tmp_path, [first_excluded, first_included, TODAY])
    try:
        result = api.get_compare_data(1)
        assert result["ok"] is True
        assert [row["date"] for row in result["rows"]] == [TODAY, first_included]
    finally:
        api._conn.close()


def test_compare_finder_default_range_is_last_ninety_dates(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    days = config.COMPARE_RANGE_DAYS
    first_included = _days_before(days - 1)
    first_excluded = _days_before(days)
    api = _api_with_dates(tmp_path, [first_excluded, first_included, TODAY])
    try:
        # Every row is $1.00, so a $2.00 search pairs any two rows in range.
        result = api.find_compare_matches(1, "2.00")
        assert result["ok"] is True
        assert [
            sorted(row["date"] for row in combo) for combo in result["combinations"]
        ] == [[first_included, TODAY]]
    finally:
        api._conn.close()
