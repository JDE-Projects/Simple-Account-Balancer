"""Regression coverage for register transaction loading and balances."""
import datetime

from app import config, utils
from app.api import Api
from app.db import open_db
from app.services import accounts, transactions


TODAY = "2026-01-30"


def _freeze_today(monkeypatch, iso_date=TODAY):
    real_date = datetime.date

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(iso_date)

    monkeypatch.setattr(transactions.datetime, "date", FrozenDate)
    monkeypatch.setattr(utils.datetime, "date", FrozenDate)


def _api(tmp_path, monkeypatch):
    """Build an Api on a fresh database without writing preferences outside it."""
    monkeypatch.setattr(accounts.prefs, "load_prefs", lambda: {})
    monkeypatch.setattr(accounts.prefs, "save_prefs", lambda prefs: True)
    conn = open_db(str(tmp_path / "register.db"))
    api = Api("test")
    api.set_conn(conn)
    return api


def _account(api, name="Checking", balance="100.00"):
    result = api.create_account(name, balance, "2025-01-01")
    assert result["ok"] is True
    return api._conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()["id"]


def _add(api, account_id, date, payee, amount, direction, category="", notes=""):
    result = api.add_transaction(account_id, date, payee, category, notes, amount, direction)
    assert result == {"ok": True}
    return api._conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND payee=?", (account_id, payee)
    ).fetchone()["id"]


def _rows_by_payee(result):
    return [(row["id"], row["payee"], row["balance_cents"]) for row in result["rows"]]


def test_register_rolls_balances_in_date_sort_key_and_id_order(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        earlier = _add(api, account_id, "2026-01-02", "Earlier", "1.00", "withdraw")
        first = _add(api, account_id, "2026-01-03", "First added", "2.00", "deposit")
        second = _add(api, account_id, "2026-01-03", "Second added", "3.00", "withdraw")
        third = _add(api, account_id, "2026-01-03", "Third added", "4.00", "deposit")
        api._conn.executemany(
            "UPDATE transactions SET sort_key=? WHERE id=?",
            [(20, first), (10, second), (10, third)],
        )
        api._conn.commit()

        result = api.get_transactions(account_id, "2026-01-01", "2026-01-10")

        assert result["ok"] is True
        assert _rows_by_payee(result) == [
            (earlier, "Earlier", 9900),
            (second, "Second added", 9600),
            (third, "Third added", 10000),
            (first, "First added", 10200),
        ]
    finally:
        api._conn.close()


def test_register_visible_rows_keep_balances_from_before_the_range(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        _add(api, account_id, "2026-01-01", "Before range", "25.00", "withdraw")
        visible = _add(api, account_id, "2026-01-10", "Visible", "10.00", "deposit")

        result = api.get_transactions(account_id, "2026-01-10", "2026-01-10")

        assert _rows_by_payee(result) == [(visible, "Visible", 8500)]
    finally:
        api._conn.close()


def test_register_current_and_today_balances_handle_future_and_empty_accounts(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api, "Checking", "100.00")
        _add(api, account_id, "2026-01-29", "Yesterday", "10.00", "withdraw")
        _add(api, account_id, TODAY, "Today", "5.00", "deposit")
        _add(api, account_id, "2026-01-31", "Tomorrow", "20.00", "deposit")

        result = api.get_transactions(account_id, "2026-01-01")

        assert result["current_balance_cents"] == 11500
        assert result["today_balance_cents"] == 9500

        empty_account_id = _account(api, "Savings", "42.50")
        empty = api.get_transactions(empty_account_id, "2026-01-01")
        assert empty["rows"] == []
        assert empty["current_balance_cents"] == 4250
        assert empty["today_balance_cents"] == 4250
    finally:
        api._conn.close()


def test_register_date_range_edges_default_and_open_ended_end(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        _add(api, account_id, "2025-12-31", "Before default range", "1.00", "deposit")
        before = _add(api, account_id, "2026-01-09", "Before", "1.00", "deposit")
        start = _add(api, account_id, "2026-01-10", "Start", "1.00", "deposit")
        end = _add(api, account_id, "2026-01-20", "End", "1.00", "deposit")
        after = _add(api, account_id, "2026-01-21", "After", "1.00", "deposit")
        future = _add(api, account_id, "2026-02-10", "Future", "1.00", "deposit")

        bounded = api.get_transactions(account_id, "2026-01-10", "2026-01-20")
        assert [row["id"] for row in bounded["rows"]] == [start, end]
        assert {row["id"] for row in bounded["rows"]}.isdisjoint({before, after, future})

        no_end = api.get_transactions(account_id, "2026-01-20")
        assert [row["id"] for row in no_end["rows"]] == [end, after, future]

        defaulted = api.get_transactions(account_id)
        default_start = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(
            days=config.DEFAULT_RANGE_DAYS - 1
        )).isoformat()
        assert default_start == "2026-01-01"
        assert [row["id"] for row in defaulted["rows"]] == [before, start, end, after, future]
    finally:
        api._conn.close()


def test_register_search_is_trimmed_case_insensitive_and_keeps_true_balances(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        _add(api, account_id, "2026-01-01", "Hidden opening", "20.00", "withdraw")
        payee = _add(api, account_id, "2026-01-10", "Coffee Roaster", "3.00", "withdraw")
        category = _add(api, account_id, "2026-01-11", "Market", "4.00", "deposit", "Coffee")
        _add(api, account_id, "2026-01-11", "Groceries", "1.00", "deposit")
        notes = _add(api, account_id, "2026-01-12", "Transfer", "5.00", "withdraw", notes="COFFEE fund")
        _add(api, account_id, "2026-01-13", "Coffee after range", "6.00", "deposit")

        searched = api.get_transactions(account_id, "2026-01-10", "2026-01-12", "  cOfFeE  ")

        assert _rows_by_payee(searched) == [
            (payee, "Coffee Roaster", 7700),
            (category, "Market", 8100),
            (notes, "Transfer", 7700),
        ]
    finally:
        api._conn.close()


def test_register_reads_are_isolated_to_the_requested_account(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        checking_id = _account(api, "Checking", "100.00")
        checking_row = _add(api, checking_id, "2026-01-10", "Checking row", "10.00", "withdraw")
        savings_id = _account(api, "Savings", "500.00")
        _add(api, savings_id, "2026-01-10", "Savings row", "400.00", "deposit")

        result = api.get_transactions(checking_id, "2026-01-01", "2026-01-30")

        assert _rows_by_payee(result) == [(checking_row, "Checking row", 9000)]
        assert result["current_balance_cents"] == 9000
        assert result["today_balance_cents"] == 9000
    finally:
        api._conn.close()
