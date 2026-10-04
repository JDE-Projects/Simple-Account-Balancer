"""Debug log operations."""

import json
import threading

def set_debug(api, on: bool):
    """Turn the debug log on or off. "enabled" is the real state after
        the call, so the toggle can flip back if the log file couldn't be
        created; the reason arrives through onDebugLogWarning."""
    ok = api._debug_log.set_enabled(on)
    return {"ok": ok, "enabled": api._debug_log.is_enabled()}


def log(api, msg: str):
    # Privacy rule for every call site: users share this log for bug
    # reports, so lines may carry ids, dates, and counts, never file
    # paths, payees, amounts, balances, or user-entered names. Every line
    # also passes through redact_log_text, which catches paths and quoted
    # values inside error messages. Size limits, old-log pruning, and
    # write failures are handled by app/debug_log.py.
    api._debug_log.log(msg)


def _on_debug_log_warning(api, message: str):
    """Show a debug log problem (failed write, old log that couldn't be
        deleted) in the window. Before the window exists, at launch, it joins
        the backup notice shown on startup. Once the window is up it is
        pushed to the page from a separate thread, so a warning raised while
        the window's own thread is busy can never stall it."""
    window = api._window
    if window is None:
        api.backup_notice = f"{api.backup_notice} {message}" if api.backup_notice else message
        return
    script = (
        "window.onDebugLogWarning && window.onDebugLogWarning("
        f"{json.dumps(message)}, {json.dumps(api._debug_log.is_enabled())})"
    )
    def _push():
        try:
            window.evaluate_js(script)
        except Exception:
            pass
    threading.Thread(target=_push, daemon=True).start()

