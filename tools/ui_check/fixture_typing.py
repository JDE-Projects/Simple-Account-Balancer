"""ui_drive fixture for the typing scenario. Builds one account and one
synthetic transaction in the copied app folder. Writes only inside
UI_DRIVE_APP_DIR."""
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
    """Create one transaction and return the facts the scenario needs."""
    db_path = os.path.join(app_dir, config.DB_FILENAME)
    conn = db.open_db(db_path)
    api = Api("ui-check")
    api.set_conn(conn)
    api.set_db_path(db_path)
    today = datetime.date.today().isoformat()
    account = api.create_account("Ledger sprout", "100.00", today)
    account_id = account["account"]["id"]
    payee = "Sample payee"
    notes = "Sample note"
    result = api.add_transaction(account_id, today, payee, "", notes, "12.34", "withdraw")
    if not result.get("ok"):
        raise RuntimeError(f"could not add fixture transaction: {result}")
    transaction = conn.execute(
        "SELECT id FROM transactions WHERE account_id=?", (account_id,)
    ).fetchone()
    if transaction is None:
        raise RuntimeError("fixture transaction was not created")
    return conn, {
        "account_id": account_id,
        "transaction_id": transaction["id"],
        "payee": payee,
        "notes": notes,
    }


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
