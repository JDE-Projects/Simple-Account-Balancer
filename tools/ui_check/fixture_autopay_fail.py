"""ui_drive fixture for the autopay-fail scenario. Builds the same database as
fixture_autopay.py, then adds a trigger that refuses every new transaction, so
the app's own autopay posting really fails, both at launch and when an
autopay is saved from the form. Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_autopay import build  # noqa: E402

conn, facts = build()
conn.execute(
    "CREATE TRIGGER ui_check_block_posting BEFORE INSERT ON transactions "
    "BEGIN SELECT RAISE(ABORT, 'ui_check: posting blocked'); END"
)
conn.commit()
conn.close()
print(json.dumps(facts), flush=True)
while True:
    time.sleep(60)
