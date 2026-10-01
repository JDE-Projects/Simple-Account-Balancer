"""ui_drive fixture for the categories scenario. Builds a live database in the
copied app folder with one account, a category used only by an autopay, and a
category used by both a transaction and an autopay. Both autopays post far in
the future, so nothing posts at launch. Writes only inside UI_DRIVE_APP_DIR."""
import datetime
import json
import os
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

import simple_account_balancer as sab  # noqa: E402


def _add_autopay(conn, account_id, payee, category, now):
    conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, "
        "next_post_date, pay_day, post_day, is_variable, created_at) "
        "VALUES (?, ?, ?, '', -5000, '2099-01-15', '2099-01-15', 15, 15, 0, ?)",
        (account_id, payee, category, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def build():
    """Create the database and the categories; return the scenario's facts."""
    db_path = os.path.join(app_dir, sab.DB_FILENAME)
    conn = sab.open_db(db_path)
    api = sab.Api()
    api.set_conn(conn)
    api.set_db_path(db_path)
    cfg = api.create_account("Checking", "500.00", "2026-01-01")
    account_id = cfg["account"]["id"]
    now = datetime.datetime.now().isoformat(timespec="seconds")
    api.add_category("Zumba Club")
    api.add_category("Old Bills")
    api.add_transaction(account_id, "2026-01-05", "Power Co", "Old Bills", "", "40.00", "withdraw")
    gym_ap = _add_autopay(conn, account_id, "Gym", "Zumba Club", now)
    bills_ap = _add_autopay(conn, account_id, "Water", "Old Bills", now)
    conn.commit()
    ids = {r["name"]: r["id"] for r in api.get_categories()["categories"]}
    return conn, {
        "account_id": account_id,
        "autopay_only_id": ids["Zumba Club"],
        "shared_id": ids["Old Bills"],
        "gym_autopay_id": gym_ap,
        "bills_autopay_id": bills_ap,
    }


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
