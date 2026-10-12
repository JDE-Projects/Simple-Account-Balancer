"""ui_drive fixture for the balance-warning scenario. Builds a live database in
the copied app folder with one account starting at $500.00 and one
transaction, so a starting-balance change asks for confirmation first.
Writes only inside UI_DRIVE_APP_DIR."""
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
    """Create the database, the account, and one transaction; return the scenario's facts."""
    db_path = os.path.join(app_dir, config.DB_FILENAME)
    conn = db.open_db(db_path)
    api = Api("ui-check")
    api.set_conn(conn)
    api.set_db_path(db_path)
    cfg = api.create_account("Checking", "500.00", "2026-01-01")
    account_id = cfg["account"]["id"]
    today = datetime.date.today().isoformat()
    result = api.add_transaction(account_id, today, "Grocery store", "", "", "40.25", "withdraw")
    if not result.get("ok"):
        raise RuntimeError(f"could not add the transaction: {result}")
    conn.commit()
    return conn, {"account_id": account_id, "starting_cents": 50000}


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
