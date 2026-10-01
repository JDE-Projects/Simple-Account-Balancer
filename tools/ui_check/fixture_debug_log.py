"""ui_drive fixture for the debug-log scenario. Plants five old debug logs and
one file that only looks like a log, in the copied app folder, then holds
some logs open, which makes Windows refuse to delete them:

- the oldest planted log, held for the whole run: the launch prune fails on
  it before the window exists, so its warning joins the startup notice;
- a still older log, created and held after LATE_S seconds: the prune when
  the debug toggle turns on fails on it while the window is up, so its
  warning is pushed to the page.

Writes only inside UI_DRIVE_APP_DIR."""
import json
import os
import time

app_dir = os.environ["UI_DRIVE_APP_DIR"]

LATE_S = 8
OLD_LOGS = [f"Debug_Log_0{d}012020_000000.txt" for d in range(1, 6)]
HELD_AT_LAUNCH = OLD_LOGS[0]
HELD_LATE = "Debug_Log_12312019_000000.txt"
NOT_A_LOG = "Debug_Log_notes.txt"

for name in [*OLD_LOGS, NOT_A_LOG]:
    with open(os.path.join(app_dir, name), "w", encoding="utf-8") as f:
        f.write("planted by the debug-log fixture\n")

held = [open(os.path.join(app_dir, HELD_AT_LAUNCH), "rb")]
late_ready_ms = int((time.time() + LATE_S) * 1000)

print(json.dumps({
    "held_at_launch": HELD_AT_LAUNCH,
    "held_late": HELD_LATE,
    "late_ready_ms": late_ready_ms,
}), flush=True)

time.sleep(LATE_S)
late_path = os.path.join(app_dir, HELD_LATE)
with open(late_path, "w", encoding="utf-8") as f:
    f.write("planted by the debug-log fixture\n")
held.append(open(late_path, "rb"))
while True:
    time.sleep(60)
