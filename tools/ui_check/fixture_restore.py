"""ui_drive fixture for the restore scenario. Builds a live database with one
account and two transactions in the copied app folder, then fills its
backups folder with two real backups (one regular, one pre-restore) and
look-alike files a shared backup folder might hold, including script-shaped
names. The app adds its own launch backup on top of these. Every look-alike is a valid copy of a backup, so it would preview if
the app ever listed it. Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import shutil
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

import simple_account_balancer as sab  # noqa: E402

REAL = ["balancer_20260101_090000.db", "balancer_prerestore_20260102_090000.db"]
LOOK_ALIKES = [
    "balancer_old.db",
    "balancer_x');window.__pwned=1;('.db",
    "balancer_20260103_090000');window.__pwned=1;('.db",
]

db_path = os.path.join(app_dir, sab.DB_FILENAME)
backups_dir = os.path.join(app_dir, sab.BACKUP_DIRNAME)
os.makedirs(backups_dir, exist_ok=True)

conn = sab.open_db(db_path)
api = sab.Api()
api.set_conn(conn)
api.set_db_path(db_path)
cfg = api.create_account("Checking", "500.00", "2026-01-15")
account_id = cfg["account"]["id"]
api.add_transaction(account_id, "2026-01-16", "Grocery store", "", "", "40.25", "withdraw")

# Backups hold the one-transaction state; the live db then gets a second
# transaction so a preview has a difference to show.
conn.commit()
for name in REAL + LOOK_ALIKES:
    shutil.copy2(db_path, os.path.join(backups_dir, name))
api.add_transaction(account_id, "2026-01-17", "Paycheck", "", "", "125.50", "deposit")
conn.commit()
conn.close()

print(json.dumps({"real": REAL, "look_alikes": LOOK_ALIKES}), flush=True)
while True:
    time.sleep(60)
