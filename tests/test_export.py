"""CSV export: user-typed text can never run as a spreadsheet formula."""
import csv

import pytest

import simple_account_balancer as sab
from test_rollback import _make_api


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1", '=HYPERLINK("x")'])
def test_csv_safe_text_prefixes_formula_leaders(text):
    assert sab.csv_safe_text(text) == "'" + text


@pytest.mark.parametrize("text", ["Groceries", "1+1", " =1", "a=b", "", "'quoted", "Café"])
def test_csv_safe_text_leaves_plain_text_alone(text):
    assert sab.csv_safe_text(text) == text


def test_csv_safe_text_none_is_empty():
    assert sab.csv_safe_text(None) == ""


def _row(payee="Store", category="Food", notes="", amount_cents=-500, balance_cents=-500):
    return {
        "date": "2024-01-02",
        "payee": payee,
        "category": category,
        "notes": notes,
        "amount_cents": amount_cents,
        "balance_cents": balance_cents,
    }


def test_csv_export_rows_guards_every_user_text_field():
    rows = sab.csv_export_rows(
        "=Checking", "All history", "2024-02-01",
        [_row(payee="=cmd", category="+cat", notes="@note")],
    )
    assert rows[0] == ["'=Checking", "All history", "Exported 2024-02-01"]
    assert rows[2][1:4] == ["'=cmd", "'+cat", "'@note"]


def test_csv_export_rows_guards_range_text():
    rows = sab.csv_export_rows("Checking", "=1 to today", "2024-02-01", [])
    assert rows[0][1] == "'=1 to today"


def test_csv_export_rows_keeps_dates_and_amounts_plain():
    rows = sab.csv_export_rows(
        "Checking", "2024-01-01 to today", "2024-02-01",
        [_row(amount_cents=-500, balance_cents=-500), _row(amount_cents=1250, balance_cents=750)],
    )
    assert rows[1] == ["Date", "Payee / Description", "Category", "Notes", "Withdraw", "Deposit", "Balance"]
    assert rows[2][0] == "2024-01-02"
    assert rows[2][4:] == ["5.00", "", "-5.00"]
    assert rows[3][4:] == ["", "12.50", "7.50"]


def test_export_csv_writes_guarded_file(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute("UPDATE accounts SET name='-Checking' WHERE id=1")
    api._conn.execute(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key, created_at) "
        "VALUES (1, '2024-01-02', '=HYPERLINK(\"http://x\")', '+Food', '@x', -500, 0, 0, 1, '2024-01-01')"
    )
    api._conn.commit()
    out_path = tmp_path / "export"

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return (str(out_path),)

    api.set_window(Window())
    result = api.export_csv(1, "", "")
    assert result["ok"] is True and result["count"] == 1

    with open(result["path"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][0] == "'-Checking"
    assert rows[2] == ["2024-01-02", "'=HYPERLINK(\"http://x\")", "'+Food", "'@x", "5.00", "", "-5.00"]
