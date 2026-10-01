"""ui_drive fixture for the notices scenario. Builds the autopay scenario's
database (one autopay posted at launch, so the register opens with a teal
notice) and saves the window size as the app's minimum, so the scenario sees
the notice banner at the narrowest width the app allows.
Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import sys
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]
sys.path.insert(0, app_dir)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture_autopay  # noqa: E402
import simple_account_balancer as sab  # noqa: E402

if __name__ == "__main__":
    conn, facts = fixture_autopay.build()
    conn.close()
    prefs = {
        "active_account_id": facts["account_id"],
        "window": {"x": 100, "y": 100, "width": sab.MIN_WINDOW_W, "height": sab.MIN_WINDOW_H},
    }
    with open(os.path.join(app_dir, "simple_account_balancer.pref"), "w", encoding="utf-8") as f:
        json.dump(prefs, f)
    print(json.dumps({**facts, "min_width": sab.MIN_WINDOW_W}), flush=True)
    while True:
        time.sleep(60)
