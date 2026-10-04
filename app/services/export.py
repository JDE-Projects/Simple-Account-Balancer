"""CSV export helpers."""

from app import utils

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
