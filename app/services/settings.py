"""Settings operations."""

from app import prefs

def _load_theme(api) -> str:
    theme = prefs.load_prefs().get("theme")
    return theme if theme in ("dark", "light") else "dark"


def save_theme(api, theme: str):
    if theme not in ("dark", "light"):
        return {"ok": False}
    prefs_data = prefs.load_prefs()
    prefs_data["theme"] = theme
    if prefs.save_prefs(prefs_data):
        api.log(f"Theme set to {theme}")
        return {"ok": True}
    api.log("Could not save theme pref")
    return {
        "ok": False,
        "error": "Theme won't be remembered: couldn't save settings next to the app.",
    }

