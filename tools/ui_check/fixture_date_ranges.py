"""ui_drive fixture for the date-ranges scenario. Builds a live database in the
copied app folder with one account and, for each "Last N Days" choice, one
transaction on the range's first included date and one on the day before it,
plus one dated today. Writes only inside UI_DRIVE_APP_DIR."""
import datetime
import json
import os
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

from app.api import Api  # noqa: E402
from app import config, db  # noqa: E402

PRESETS = (7, 14, 30, 90)


def build():
    """Create the database and edge-date transactions; return the scenario's facts."""
    db_path = os.path.join(app_dir, config.DB_FILENAME)
    conn = db.open_db(db_path)
    api = Api("ui-check")
    api.set_conn(conn)
    api.set_db_path(db_path)
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=200)).isoformat()
    cfg = api.create_account("Checking", "500.00", start)
    account_id = cfg["account"]["id"]

    # Days back from today for each planted row. N - 1 days back is the
    # first date "Last N Days" includes; N days back is the first it leaves out.
    offsets = {"Today": 0}
    for n in PRESETS:
        offsets[f"In {n}"] = n - 1
        offsets[f"Out {n}"] = n
    for payee, back in offsets.items():
        day = (today - datetime.timedelta(days=back)).isoformat()
        result = api.add_transaction(account_id, day, payee, "", "", "1.00", "withdraw")
        if not result.get("ok"):
            raise RuntimeError(f"could not add {payee}: {result}")
    conn.commit()
    rows = conn.execute(
        "SELECT id, payee FROM transactions WHERE account_id=?", (account_id,)
    ).fetchall()
    ids = {r["payee"]: r["id"] for r in rows}
    return conn, {
        "account_id": account_id,
        "today": today.isoformat(),
        "rows": [{"payee": p, "id": ids[p], "back": b} for p, b in offsets.items()],
    }


if __name__ == "__main__":
    conn, facts = build()
    conn.close()
    print(json.dumps(facts), flush=True)
    while True:
        time.sleep(60)
