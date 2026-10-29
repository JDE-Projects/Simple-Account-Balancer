"""ui_drive fixture for first-run restore. Builds one backup with two
accounts and a category in the backups folder. No live database is created,
so the app launches on its first-run card. Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import shutil
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

from app.api import Api  # noqa: E402
from app import config, db  # noqa: E402


FILENAME = "balancer_20260101_090000.db"
TEMP_FILENAME = "fixture_first_run_restore.db"


def build():
    """Create the backup without creating a live database in the copied app."""
    db_path = os.path.join(app_dir, TEMP_FILENAME)
    backup_dir = os.path.join(app_dir, config.BACKUP_DIRNAME)
    backup_path = os.path.join(backup_dir, FILENAME)
    os.makedirs(backup_dir, exist_ok=True)

    conn = db.open_db(db_path)
    api = Api("ui-check")
    api.set_conn(conn)
    api.set_db_path(db_path)
    checking = api.create_account("Checking", "500.00", "2026-01-01")
    checking_id = checking["account"]["id"]
    api.add_transaction(checking_id, "2026-01-02", "Groceries", "", "", "40.00", "withdraw")
    api.add_transaction(checking_id, "2026-01-03", "Paycheck", "", "", "100.00", "deposit")
    savings = api.create_account('<b>Savings</b> & "Co"', "100.00", "2026-01-01")
    api.add_transaction(savings["account"]["id"], "2026-01-04", "Interest", "", "", "1.00", "deposit")
    api.add_category("Restore Check")
    conn.commit()
    conn.close()
    shutil.move(db_path, backup_path)


if __name__ == "__main__":
    build()
    print(json.dumps({"filename": FILENAME}), flush=True)
    while True:
        time.sleep(60)
