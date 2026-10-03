"""Local application preferences."""

import json
import os

from app import paths


# ---------------------------------------------------------------------------
# Preferences: a small local file next to the app, not stored in the db.
# Module-level so main() can read it before any Api/window exists (needed for
# the relocatable backup folder at startup).
# ---------------------------------------------------------------------------
def _pref_path() -> str:
    return os.path.join(paths.app_dir(), "simple_account_balancer.pref")


def load_prefs() -> dict:
    """Load the full prefs dict. Tolerant of a missing or corrupt file."""
    try:
        with open(_pref_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(prefs: dict) -> bool:
    try:
        with open(_pref_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        return True
    except Exception:
        return False
