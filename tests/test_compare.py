"""Focused coverage for the read-only Compare API."""
import datetime

from simple_account_balancer import Api, open_db


def _api_with_transactions(tmp_path, transactions, starting_balance_cents=10_000):
    conn = open_db(str(tmp_path / "compare.db"))
    conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Checking', ?, '2026-01-01', '2026-01-01')",
        (starting_balance_cents,),
    )
    conn.executemany(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, created_at) "
        "VALUES (1, ?, ?, '', '', ?, ?, '2026-01-01')",
        transactions,
    )
    conn.commit()
    api = Api()
    api.set_conn(conn)
    return api


def test_compare_data_scopes_range_orders_newest_first_and_returns_full_balance(tmp_path):
    api = _api_with_transactions(
        tmp_path,
        [
            ("2026-05-01", "Outside", 2_500, 1),
            ("2026-06-01", "Older", -1_000, 0),
            ("2026-06-15", "Newer", 500, 1),
        ],
    )
    try:
        result = api.get_compare_data(1, "2026-06-01", "2026-06-30")
        assert result["ok"] is True
        assert [row["payee"] for row in result["rows"]] == ["Newer", "Older"]
        assert result["register_balance_cents"] == 12_000
        assert all("cleared" not in row for row in result["rows"])
    finally:
        api._conn.close()


def test_compare_default_range_is_last_ninety_days(tmp_path):
    today = datetime.date.today()
    api = _api_with_transactions(
        tmp_path,
        [
            ((today - datetime.timedelta(days=91)).isoformat(), "Old", 100, 0),
            ((today - datetime.timedelta(days=90)).isoformat(), "Boundary", 200, 1),
        ],
    )
    try:
        result = api.get_compare_data(1)
        assert [row["payee"] for row in result["rows"]] == ["Boundary"]
    finally:
        api._conn.close()


def test_compare_finder_ignores_legacy_cleared_state(tmp_path):
    api = _api_with_transactions(tmp_path, [("2026-06-01", "Already cleared", -1_250, 1)])
    try:
        result = api.find_compare_matches(1, "12.50", "2026-06-01", "2026-06-30")
        assert result["ok"] is True
        assert [row["payee"] for row in result["exact"]] == ["Already cleared"]
        assert "cleared" not in result["exact"][0]
    finally:
        api._conn.close()


def test_compare_finder_combinations_stay_in_selected_range(tmp_path):
    api = _api_with_transactions(
        tmp_path,
        [
            ("2026-05-01", "Outside one", 4_000, 0),
            ("2026-05-02", "Outside two", 6_000, 1),
            ("2026-06-01", "Inside one", 3_000, 1),
            ("2026-06-02", "Inside two", 7_000, 0),
        ],
    )
    try:
        result = api.find_compare_matches(1, "100.00", "2026-06-01", "2026-06-30")
        assert result["ok"] is True
        assert result["combinations"]
        assert {
            row["payee"] for row in result["combinations"][0]
        } == {"Inside one", "Inside two"}
    finally:
        api._conn.close()


def test_compare_access_never_mutates_legacy_cleared_state(tmp_path):
    api = _api_with_transactions(
        tmp_path,
        [("2026-06-01", "Cleared", -500, 1), ("2026-06-02", "Open", 500, 0)],
    )
    try:
        before = list(api._conn.execute("SELECT id, cleared FROM transactions ORDER BY id"))
        api.get_compare_data(1, "2026-06-01", "2026-06-30")
        api.find_compare_matches(1, "5.00", "2026-06-01", "2026-06-30")
        after = list(api._conn.execute("SELECT id, cleared FROM transactions ORDER BY id"))
        assert after == before
    finally:
        api._conn.close()
