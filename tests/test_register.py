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


def _persisted_transactions(api, account_id=None):
    query = "SELECT * FROM transactions"
    params = ()
    if account_id is not None:
        query += " WHERE account_id=?"
        params = (account_id,)
    query += " ORDER BY id"
    return [dict(row) for row in api._conn.execute(query, params)]


def _expected_transaction_state(rows, transaction_id, **changes):
    expected = [row.copy() for row in rows]
    for row in expected:
        if row["id"] == transaction_id:
            row.update(changes)
            return expected
    raise AssertionError(f"Missing transaction {transaction_id}")


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


def test_register_same_day_edit_keeps_position_and_rebalances_later_rows(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        before = _add(api, account_id, "2026-01-05", "Before", "5.00", "deposit")
        edited = _add(api, account_id, "2026-01-10", "Original", "2.00", "withdraw")
        same_day_later = _add(api, account_id, "2026-01-10", "Same day later", "3.00", "deposit")
        later = _add(api, account_id, "2026-01-11", "Later", "1.00", "withdraw")
        api._conn.execute("UPDATE transactions SET estimated=1 WHERE id=?", (edited,))
        api._conn.commit()
        state_before = _persisted_transactions(api)

        result = api.update_transaction(
            edited, "2026-01-10", "Updated", "Income", "Reviewed", "7.00", "deposit"
        )

        assert result == {"ok": True}
        assert _persisted_transactions(api) == _expected_transaction_state(
            state_before,
            edited,
            payee="Updated",
            category="Income",
            notes="Reviewed",
            amount_cents=700,
            estimated=0,
        )
        register = api.get_transactions(account_id, "2026-01-01", "2026-01-30")
        assert _rows_by_payee(register) == [
            (before, "Before", 10500),
            (edited, "Updated", 11200),
            (same_day_later, "Same day later", 11500),
            (later, "Later", 11400),
        ]
        assert register["current_balance_cents"] == 11400
        assert register["today_balance_cents"] == 11400
    finally:
        api._conn.close()


def test_register_date_move_resets_position_and_rebalances_from_earlier_day(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        before = _add(api, account_id, "2026-01-05", "Before", "1.00", "withdraw")
        moved = _add(api, account_id, "2026-01-10", "Move me", "2.00", "deposit")
        old_day = _add(api, account_id, "2026-01-10", "Old day", "3.00", "withdraw")
        new_day_first = _add(api, account_id, "2026-01-12", "New day first", "4.00", "deposit")
        new_day_second = _add(api, account_id, "2026-01-12", "New day second", "5.00", "withdraw")
        later = _add(api, account_id, "2026-01-20", "Later", "6.00", "deposit")
        api._conn.execute("UPDATE transactions SET estimated=1 WHERE id=?", (moved,))
        api._conn.commit()
        state_before = _persisted_transactions(api)

        result = api.update_transaction(
            moved, "2026-01-12", "Moved", "Transfer", "Moved day", "2.00", "deposit"
        )

        assert result == {"ok": True}
        assert _persisted_transactions(api) == _expected_transaction_state(
            state_before,
            moved,
            date="2026-01-12",
            payee="Moved",
            category="Transfer",
            notes="Moved day",
            estimated=0,
            sort_key=moved,
        )
        register = api.get_transactions(account_id, "2026-01-01", "2026-01-30")
        assert _rows_by_payee(register) == [
            (before, "Before", 9900),
            (old_day, "Old day", 9600),
            (moved, "Moved", 9800),
            (new_day_first, "New day first", 10200),
            (new_day_second, "New day second", 9700),
            (later, "Later", 10300),
        ]
        assert register["current_balance_cents"] == 10300
        assert register["today_balance_cents"] == 10300
    finally:
        api._conn.close()


def test_register_delete_removes_row_and_updates_current_and_today_balances(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        before = _add(api, account_id, "2026-01-05", "Before", "5.00", "deposit")
        deleted = _add(api, account_id, "2026-01-10", "Delete me", "2.00", "withdraw")
        same_day_later = _add(api, account_id, "2026-01-10", "Same day later", "3.00", "deposit")
        future = _add(api, account_id, "2026-01-31", "Future", "4.00", "deposit")
        state_before = _persisted_transactions(api)

        result = api.delete_transaction(deleted)

        assert result == {"ok": True}
        assert _persisted_transactions(api) == [
            row for row in state_before if row["id"] != deleted
        ]
        register = api.get_transactions(account_id, "2026-01-01", "2026-02-01")
        assert _rows_by_payee(register) == [
            (before, "Before", 10500),
            (same_day_later, "Same day later", 10800),
            (future, "Future", 11200),
        ]
        assert register["current_balance_cents"] == 11200
        assert register["today_balance_cents"] == 10800
    finally:
        api._conn.close()


def test_register_reorder_updates_only_that_day_and_new_rows_follow_it(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_id = _account(api)
        other_day = _add(api, account_id, "2026-01-09", "Other day", "1.00", "withdraw")
        first = _add(api, account_id, "2026-01-10", "First", "2.00", "deposit")
        second = _add(api, account_id, "2026-01-10", "Second", "3.00", "withdraw")
        third = _add(api, account_id, "2026-01-10", "Third", "5.00", "deposit")
        after = _add(api, account_id, "2026-01-11", "After", "0.50", "withdraw")
        state_before = _persisted_transactions(api)

        result = api.reorder_transactions(account_id, "2026-01-10", [third, first, second])

        assert result == {"ok": True}
        expected = _expected_transaction_state(state_before, third, sort_key=1)
        expected = _expected_transaction_state(expected, first, sort_key=2)
        expected = _expected_transaction_state(expected, second, sort_key=3)
        assert _persisted_transactions(api) == expected
        register = api.get_transactions(account_id, "2026-01-01", "2026-01-30")
        assert _rows_by_payee(register) == [
            (other_day, "Other day", 9900),
            (third, "Third", 10400),
            (first, "First", 10600),
            (second, "Second", 10300),
            (after, "After", 10250),
        ]

        added = _add(api, account_id, "2026-01-10", "Added later", "1.00", "withdraw")

        assert _persisted_transactions(api)[-1]["sort_key"] == added
        register = api.get_transactions(account_id, "2026-01-01", "2026-01-30")
        assert _rows_by_payee(register) == [
            (other_day, "Other day", 9900),
            (third, "Third", 10400),
            (first, "First", 10600),
            (second, "Second", 10300),
            (added, "Added later", 10200),
            (after, "After", 10150),
        ]
    finally:
        api._conn.close()


def test_register_edits_and_reorders_are_isolated_between_accounts(tmp_path, monkeypatch):
    _freeze_today(monkeypatch)
    api = _api(tmp_path, monkeypatch)
    try:
        account_a = _account(api, "Account A")
        account_b = _account(api, "Account B", "500.00")
        first_a = _add(api, account_a, "2026-01-10", "A first", "1.00", "deposit")
        second_a = _add(api, account_a, "2026-01-10", "A second", "2.00", "withdraw")
        deleted_a = _add(api, account_a, "2026-01-11", "A delete", "3.00", "deposit")
        first_b = _add(api, account_b, "2026-01-10", "B first", "4.00", "deposit")
        _add(api, account_b, "2026-01-10", "B second", "5.00", "withdraw")
        b_state = _persisted_transactions(api, account_b)
        b_register = api.get_transactions(account_b, "2026-01-01", "2026-01-30")
        state_before = _persisted_transactions(api)

        assert api.update_transaction(
            first_a, "2026-01-10", "A updated", "Income", "Edited", "1.50", "deposit"
        ) == {"ok": True}
        assert _persisted_transactions(api) == _expected_transaction_state(
            state_before,
            first_a,
            payee="A updated",
            category="Income",
            notes="Edited",
            amount_cents=150,
        )
        assert _persisted_transactions(api, account_b) == b_state
        assert api.get_transactions(account_b, "2026-01-01", "2026-01-30") == b_register

        state_before = _persisted_transactions(api)
        assert api.delete_transaction(deleted_a) == {"ok": True}
        assert _persisted_transactions(api) == [
            row for row in state_before if row["id"] != deleted_a
        ]
        assert _persisted_transactions(api, account_b) == b_state
        assert api.get_transactions(account_b, "2026-01-01", "2026-01-30") == b_register

        state_before = _persisted_transactions(api)
        refused = api.reorder_transactions(account_a, "2026-01-10", [first_a, first_b])
        assert refused["ok"] is False
        assert _persisted_transactions(api) == state_before
        assert _persisted_transactions(api, account_b) == b_state
        assert api.get_transactions(account_b, "2026-01-01", "2026-01-30") == b_register

        state_before = _persisted_transactions(api)
        assert api.reorder_transactions(account_a, "2026-01-10", [second_a, first_a]) == {"ok": True}
        expected = _expected_transaction_state(state_before, second_a, sort_key=1)
        expected = _expected_transaction_state(expected, first_a, sort_key=2)
        assert _persisted_transactions(api) == expected
        assert _persisted_transactions(api, account_b) == b_state
        assert api.get_transactions(account_b, "2026-01-01", "2026-01-30") == b_register
        register_a = api.get_transactions(account_a, "2026-01-01", "2026-01-30")
        assert _rows_by_payee(register_a) == [
            (second_a, "A second", 9800),
            (first_a, "A updated", 9950),
        ]
    finally:
        api._conn.close()
