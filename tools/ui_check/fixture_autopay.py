"""ui_drive fixture for the autopay scenario. Builds a live database in the
copied app folder with one account and one autopay whose register date is
today and whose pay date is three days out, so the app posts it at launch.
Writes only inside UI_DRIVE_APP_DIR."""
import datetime
import json
import os
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

import simple_account_balancer as sab  # noqa: E402


def build():
    """Create the database and the due autopay; return the scenario's facts."""
    today = datetime.date.today()
    pay = today + datetime.timedelta(days=3)
    db_path = os.path.join(app_dir, sab.DB_FILENAME)
    conn = sab.open_db(db_path)
    api = sab.Api()
    api.set_conn(conn)
    api.set_db_path(db_path)
    cfg = api.create_account("Checking", "500.00", "2026-01-01")
    account_id = cfg["account"]["id"]
    conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, "
        "next_post_date, pay_day, post_day, is_variable, created_at) "
        "VALUES (?, 'Rent', '', '', -120000, ?, ?, ?, ?, 0, ?)",
        (account_id, pay.isoformat(), today.isoformat(), pay.day, today.day,
         datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return conn, {"account_id": account_id, "today": today.isoformat(), "pay_date": pay.isoformat()}


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
