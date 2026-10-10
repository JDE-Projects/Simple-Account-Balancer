"""Every service that takes a typed amount must refuse malformed or oversized
input with the shared error text, and must write nothing when it does.
"""
import pytest

import app.api as sab
from app import db
from app.services import backup

INVALID = "Enter a valid amount, such as 12, 12.34, or $1,234.56."
INVALID_NEG_OK = "Enter a valid amount, such as 12, 12.34, $1,234.56, or -40."
TOO_LARGE = "Amount is too large. The most it can be is $999,999,999,999.99."

# (bad input, expected error when negatives are not allowed, expected when they are)
BAD_AMOUNTS = [
    ("1e3", INVALID, INVALID_NEG_OK),
    ("--5", INVALID, INVALID_NEG_OK),
    ("1,2,3", INVALID, INVALID_NEG_OK),
    ("1000000000000", TOO_LARGE, TOO_LARGE),
]
BAD_STRICT = [(raw, strict) for raw, strict, _ in BAD_AMOUNTS]
BAD_NEG_OK = [(raw, neg_ok) for raw, _, neg_ok in BAD_AMOUNTS]


def _make_api(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(backup, "effective_backup_dir", lambda: (str(backups), False))
    db_path = str(tmp_path / "live.db")
    api = sab.Api("test")
    api.set_conn(db.open_db(db_path))
    api.set_db_path(db_path)
    api._conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Checking', 5000, '2024-01-01', '2024-01-01')"
    )
    api._conn.execute(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, estimated, created_at) "
        "VALUES (1, '2024-01-05', 'Store', '', '', -1234, 1, '2024-01-01')"
    )
    api._conn.execute(
        "INSERT INTO autopays (account_id, payee, category, notes, amount_cents, "
        "next_pay_date, next_post_date, pay_day, post_day, is_variable, created_at) "
        "VALUES (1, 'Rent', 'Bills', '', -9900, '2099-01-10', '2099-01-10', 10, 10, 0, '2024-01-01')"
    )
    api._conn.commit()
    return api


def _snapshot(api):
    """Every table's full contents, so any write at all shows up as a change."""
    return {
        table: [tuple(r) for r in api._conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        for table in ("accounts", "transactions", "autopays")
    }


@pytest.mark.parametrize("raw, message", BAD_NEG_OK)
def test_create_account_rejects_bad_balance(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.create_account("Savings", raw, "2024-01-01")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_NEG_OK)
def test_update_account_rejects_bad_balance(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.update_account(1, "Checking", raw, "2024-01-01")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_STRICT)
def test_add_autopay_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.add_autopay(1, "Gym", "Bills", "", raw, "withdraw", "2099-02-01", "2099-02-01")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_STRICT)
def test_update_autopay_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.update_autopay(1, "Rent", "Bills", "", raw, "withdraw", "2099-01-10", "2099-01-10")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_STRICT)
def test_add_transaction_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.add_transaction(1, "2024-02-01", "Coffee", "", "", raw, "withdraw")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_STRICT)
def test_update_transaction_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.update_transaction(1, "2024-01-05", "Store", "", "", raw, "withdraw")
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_STRICT)
def test_confirm_estimated_amount_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    before = _snapshot(api)
    result = api.confirm_estimated_amount(1, raw)
    assert result["ok"] is False
    assert result["error"] == message
    assert _snapshot(api) == before


@pytest.mark.parametrize("raw, message", BAD_NEG_OK)
def test_find_compare_matches_rejects_bad_amount(tmp_path, monkeypatch, raw, message):
    api = _make_api(tmp_path, monkeypatch)
    result = api.find_compare_matches(1, raw, "2024-01-01", "2024-12-31")
    assert result["ok"] is False
    assert result["error"] == message
