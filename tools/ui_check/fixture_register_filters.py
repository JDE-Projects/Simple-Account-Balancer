"""ui_drive fixture for the register-filters scenario. Builds a live database
in the copied app folder with register rows inside and outside the default
range. Writes only inside UI_DRIVE_APP_DIR."""
import datetime
import json
import os
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

from app.api import Api  # noqa: E402
from app import config, db  # noqa: E402


def build():
    """Create a mixed register and return row ids, signs, and true balances."""
    db_path = os.path.join(app_dir, config.DB_FILENAME)
    conn = db.open_db(db_path)
    api = Api("ui-check")
    api.set_conn(conn)
    api.set_db_path(db_path)
    today = datetime.date.today()
    cfg = api.create_account("Checking", "100.00", (today - datetime.timedelta(days=90)).isoformat())
    account_id = cfg["account"]["id"]
    seeds = [
        ("Old anchor", 40, "5.00", "withdraw"),
        ("Shared market", 10, "10.00", "withdraw"),
        ("Shared paycheck", 5, "40.00", "deposit"),
        ("Coffee", 2, "3.00", "withdraw"),
        ("Refund", 1, "2.00", "deposit"),
    ]
    for payee, days_back, amount, direction in seeds:
        date = (today - datetime.timedelta(days=days_back)).isoformat()
        result = api.add_transaction(account_id, date, payee, "", "", amount, direction)
        if not result.get("ok"):
            raise RuntimeError(f"could not add {payee}: {result}")
    conn.commit()
    rows = conn.execute(
        "SELECT id, payee, date, amount_cents FROM transactions WHERE account_id=? "
        "ORDER BY date ASC, sort_key ASC, id ASC",
        (account_id,),
    ).fetchall()
    balance = 10000
    facts_rows = []
    for row in rows:
        balance += row["amount_cents"]
        facts_rows.append({
            "id": row["id"],
            "payee": row["payee"],
            "date": row["date"],
            "sign": "withdraw" if row["amount_cents"] < 0 else "deposit",
            "balance_cents": balance,
        })
    return conn, {
        "account_id": account_id,
        "today": today.isoformat(),
        "default_from": (today - datetime.timedelta(days=29)).isoformat(),
        "narrow_from": (today - datetime.timedelta(days=10)).isoformat(),
        "narrow_to": (today - datetime.timedelta(days=5)).isoformat(),
        "deposit_only_day": (today - datetime.timedelta(days=1)).isoformat(),
        "empty_from": (today + datetime.timedelta(days=20)).isoformat(),
        "empty_to": (today + datetime.timedelta(days=21)).isoformat(),
        "rows": facts_rows,
    }


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
