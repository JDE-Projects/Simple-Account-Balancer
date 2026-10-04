"""CSV export helpers and operations."""

import datetime
import os

import webview

from app import paths, utils

# Excel and other spreadsheets treat a cell starting with one of these as a
# formula (tab and carriage return can hide one).
_CSV_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_text(value) -> str:
    """Make user-typed text safe for a CSV cell: text that a spreadsheet would
    run as a formula gets a leading apostrophe so it shows as plain text."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_FORMULA_LEADERS):
        return "'" + text
    return text


def csv_export_rows(account_name, range_text, export_date, rows) -> list:
    """Build every row of the register CSV export. Text that comes from the
    user or the page (account name, date range, transaction date, payee,
    category, notes) is passed through csv_safe_text. A real date never starts
    with a formula character, so it is unchanged; a date from a hand-edited
    backup can't run as a formula. Amounts stay plain values so spreadsheets
    read them as numbers, including negative balances."""
    out = [
        [csv_safe_text(account_name), csv_safe_text(range_text), f"Exported {export_date}"],
        ["Date", "Payee / Description", "Category", "Notes", "Withdraw", "Deposit", "Balance"],
    ]
    for r in rows:
        withdraw = utils.cents_to_decimal_str(-r["amount_cents"]) if r["amount_cents"] < 0 else ""
        deposit = utils.cents_to_decimal_str(r["amount_cents"]) if r["amount_cents"] > 0 else ""
        balance = utils.cents_to_decimal_str(r["balance_cents"])
        out.append([
            csv_safe_text(r["date"]),
            csv_safe_text(r["payee"]),
            csv_safe_text(r["category"]),
            csv_safe_text(r["notes"]),
            withdraw,
            deposit,
            balance,
        ])
    return out

def _export_csv_snapshot(api, account_id, from_date, to_date, range_label):
    account = api._get_account(account_id)
    if account is None:
        return {"ok": False, "error": "No account exists yet."}
    computed = api._rows_with_balance(account)
    from_s = (from_date or "").strip()
    to_s = (to_date or "").strip()
    all_history = not from_s and not to_s
    rows = computed
    if from_s:
        rows = [r for r in rows if r["date"] >= from_s]
    if to_s:
        rows = [r for r in rows if r["date"] <= to_s]
    expected_tokens = (
        "AllHistory",
        "Last7Days",
        "Last14Days",
        "Last30Days",
        "Last90Days",
        "CustomRange",
    )
    token = (range_label or "").strip()
    if token not in expected_tokens:
        token = "AllHistory" if all_history else "CustomRange"
    return {
        "ok": True,
        "account_name": account["name"],
        "rows": rows,
        "from_date": from_s,
        "to_date": to_s,
        "all_history": all_history,
        "default_name": utils.sanitize_filename(
            f"{account['name']}_Export_{token}_{datetime.date.today().strftime('%m%d%Y')}.csv"
        ),
    }


def export_csv(api, account_id, from_date, to_date, range_label=""):
    """Export the register to a CSV file via a native save dialog. An
        empty from_date and to_date together mean all history (no default
        range applied here; that default is only for the register views).
        range_label is the preset token the UI picked (e.g. AllHistory,
        Last30Days, CustomRange); it only affects the default filename."""
    try:
        snapshot = api._export_csv_snapshot(account_id, from_date, to_date, range_label)
        if not snapshot["ok"]:
            return snapshot
        account_name = snapshot["account_name"]
        rows = snapshot["rows"]
        from_s = snapshot["from_date"]
        to_s = snapshot["to_date"]
        all_history = snapshot["all_history"]
        documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
        start_dir = documents_dir if os.path.isdir(documents_dir) else paths.app_dir()
        result = api._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=start_dir,
            save_filename=snapshot["default_name"],
            file_types=("CSV Files (*.csv)",),
        )
        if not result:
            return {"ok": True, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        if not path:
            return {"ok": True, "cancelled": True}
        if not path.lower().endswith(".csv"):
            path += ".csv"
        import csv
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            range_text = "All history" if all_history else f"{from_s or 'start'} to {to_s or 'today'}"
            csv.writer(f).writerows(
                csv_export_rows(account_name, range_text, datetime.date.today().isoformat(), rows)
            )
        api.log(f"Exported {len(rows)} transactions to CSV")
        return {"ok": True, "path": path, "count": len(rows)}
    except Exception as e:
        api.log(f"export_csv failed: {e}")
        return {"ok": False, "error": "Couldn't export the CSV file."}

