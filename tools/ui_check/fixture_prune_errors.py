"""ui_drive fixture for the prune-errors scenario. Builds a live database in
the copied app folder with "Keep last" set to 1, then plants old backups and
holds some of them open, which makes Windows refuse to delete them, so the
app's own pruning really fails:

- an old regular backup, held for HOLD_S seconds: the launch prune fails on
  it, then it is released so the closing backup's prune succeeds (a failed
  closing prune shows a Windows message box the driver can't dismiss);
- an old pre-restore backup, held for the whole run: the prune after the
  pre-restore safety copy fails on it during a restore.

A valid pre-restore backup is the restore target, and two more old
pre-restore files make the pool overflow. Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import shutil
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)

import simple_account_balancer as sab  # noqa: E402

HOLD_S = 20
TARGET = "balancer_prerestore_20260102_090000.db"
OLD_REGULAR = "balancer_20200101_000000.db"
OLD_PRERESTORE = [
    "balancer_prerestore_20200101_000000.db",
    "balancer_prerestore_20200102_000000.db",
    "balancer_prerestore_20200103_000000.db",
]

db_path = os.path.join(app_dir, sab.DB_FILENAME)
backups_dir = os.path.join(app_dir, sab.BACKUP_DIRNAME)
os.makedirs(backups_dir, exist_ok=True)

conn = sab.open_db(db_path)
api = sab.Api()
api.set_conn(conn)
api.set_db_path(db_path)
cfg = api.create_account("Checking", "500.00", "2026-01-15")
api.add_transaction(cfg["account"]["id"], "2026-01-16", "Grocery store", "", "", "40.25", "withdraw")
conn.commit()
conn.close()

prefs = sab.load_prefs()
prefs["backup_keep"] = 1
if not sab.save_prefs(prefs):
    raise SystemExit("couldn't save prefs")

for name in [TARGET, OLD_REGULAR, *OLD_PRERESTORE]:
    shutil.copy2(db_path, os.path.join(backups_dir, name))

held_regular = open(os.path.join(backups_dir, OLD_REGULAR), "rb")
held_prerestore = open(os.path.join(backups_dir, OLD_PRERESTORE[0]), "rb")
release_at_ms = int((time.time() + HOLD_S) * 1000)

print(json.dumps({"target": TARGET, "release_at_ms": release_at_ms}), flush=True)
time.sleep(HOLD_S)
held_regular.close()
while True:
    time.sleep(60)
