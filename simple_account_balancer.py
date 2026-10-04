"""
Simple Account Balancer, a checkbook-register style balance tracker.

JDE-Projects "Simple X Tool": Python 3 + PySide6/pywebview, single-file UI.
The register is the source of truth for "how much money do I actually have."
All money is stored and computed as integer cents; never floats.

Features: a multi-account register with add/edit/delete and a rolling
balance that recalculates for out-of-order entry, an on-demand Compare view
with a discrepancy finder, CSV export, an autopay catalog of recurring rules
that posts real transactions at launch, and SQLite storage with rolling
backups (relocatable backup folder, in-app restore with a pre-restore
safety copy) plus a schema version guard against databases written by a
newer build.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import shlex
import sys

from app import config, db, paths, platform_win, utils
from app.api import Api
from app.services import autopays, backup, restore

import webview

APP_VERSION = "1.9.2"

def _is_remote_debugging_switch(token):
    # Chromium on Windows accepts "--", "-" or "/" before a switch name and
    # ignores its case, so every spelling is matched.
    for prefix in ("--", "-", "/"):
        if token.startswith(prefix):
            return token[len(prefix):].lower().startswith("remote-debugging-")
    return False


def _drop_remote_debugging(tokens):
    """Return tokens without remote-debugging switches, including a value
    given as the following token (--remote-debugging-port 9222)."""
    kept = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if _is_remote_debugging_switch(token):
            if "=" not in token and index < len(tokens) and not tokens[index].startswith(("-", "/")):
                index += 1
        else:
            kept.append(token)
    return kept


def strip_remote_debugging(environ, argv, frozen):
    """Remove Qt remote-debugging controls from frozen application launches,
    so the built app never opens Qt's remote-control port. Source runs are
    left alone: the real-window smoke check depends on that port."""
    if not frozen:
        return

    environ.pop("QTWEBENGINE_REMOTE_DEBUGGING", None)

    flags = environ.get("QTWEBENGINE_CHROMIUM_FLAGS")
    if flags is not None:
        try:
            lexer = shlex.shlex(flags, posix=False)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quote: split on spaces rather than fail at startup.
            tokens = flags.split()
        kept = _drop_remote_debugging(tokens)
        if kept:
            environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(kept)
        else:
            del environ["QTWEBENGINE_CHROMIUM_FLAGS"]

    argv[1:] = _drop_remote_debugging(argv[1:])


_mutex_handle = None   # module-level: must live for the process lifetime


def _acquire_single_instance(mutex_name: str) -> bool:
    # Name convention: "JDE_Simple{Thing}Tool_SingleInstance"
    # Session-local (no "Global\" prefix): each Windows session (e.g. RDP,
    # fast user switching) gets its own instance instead of colliding across users.
    global _mutex_handle
    try:
        # use_last_error=True: ctypes.windll's GetLastError() can be clobbered
        # by ctypes-internal calls, so read the error via ctypes.get_last_error() instead.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True   # fail open: never block launch over a mutex error


def _focus_existing_window(app_title: str) -> bool:
    # Best-effort only: any failure here must not stop the caller from deciding what to do next.
    try:
        user32 = ctypes.windll.user32
        found = {"hwnd": None}

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_proc(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            # Exact match only: a prefix match could hit an unrelated window
            # (e.g. a browser tab starting with the app name). A miss falls
            # through to a normal launch anyway.
            if buf.value == app_title:
                found["hwnd"] = hwnd
                return False   # stop enumerating, match found
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_proc), 0)

        hwnd = found["hwnd"]
        if not hwnd:
            return False

        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main():
    # Stops Qt's shader cache from leaving an empty
    # %LOCALAPPDATA%\<exe name>\cache\qtpipelinecache-* folder behind that
    # the uninstaller never removes. Must be set before the window is created.
    os.environ.setdefault("QT_DISABLE_SHADER_DISK_CACHE", "1")
    strip_remote_debugging(os.environ, sys.argv, getattr(sys, "frozen", False))

    # Use the Windows certificate store for TLS instead of the bundled CA list,
    # so antivirus/network filters that inject their own root cert (common on
    # managed laptops) don't break the GitHub update check. Runs before the
    # Api object exists, so there's no logger yet to record a fallback; if
    # truststore is missing or fails, urllib silently keeps using its default
    # bundled CA list instead.
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass

    if not _acquire_single_instance("JDE_SimpleAccountBalancer_SingleInstance"):
        if _focus_existing_window("Simple Account Balancer"):
            sys.exit(0)
        # window not found (startup race): fall through and launch normally,
        # a click on the icon must never silently do nothing

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "JDEProjects.SimpleAccountBalancer"
            )
        except Exception:
            pass

    folder = paths.app_dir()
    if not utils._writable_check(folder):
        platform_win._show_write_error(folder)
        sys.exit(1)

    db_path = os.path.join(folder, config.DB_FILENAME)
    api = Api(APP_VERSION)
    api.set_db_path(db_path)
    api._debug_log.prune()
    removed_restore_count, stale_restore_failures = restore._remove_stale_restore_files(folder)
    if removed_restore_count:
        api.log(f"Launch cleanup removed {removed_restore_count} stale restore file(s) in the app folder")
    if stale_restore_failures:
        api.log(
            f"Launch cleanup couldn't delete {len(stale_restore_failures)} stale restore file(s): "
            f"{', '.join(stale_restore_failures)}"
        )
        cleanup_msg = "A temporary restore file from an earlier session couldn't be deleted."
        api.backup_notice = f"{api.backup_notice} {cleanup_msg}" if api.backup_notice else cleanup_msg
    db_existed_before = os.path.exists(db_path)

    # Launch backup runs BEFORE open_db, so every launch snapshot is a
    # pre-migration copy: open_db's schema migrations must never run first
    # and land in the backup we'd use to recover from them.
    if db_existed_before:
        ok, used_fallback, actual_dir, prune_failed = backup._run_backup_with_fallback(db_path)
        folder_kind = "the fallback folder" if used_fallback else "the backup folder"
        if ok:
            api.log(f"Launch backup created in {folder_kind}")
        else:
            api.log(f"Launch backup failed, tried {folder_kind}")
        if used_fallback:
            api.log("Backup folder unreachable at launch, used the fallback folder")
        # Failure wins over the fallback notice: a fallback that then failed
        # to write must not report that the backup was saved.
        if not ok:
            backup_msg = f"Today's backup couldn't be saved. Tried to write it to {actual_dir}."
            api.backup_notice = f"{api.backup_notice} {backup_msg}" if api.backup_notice else backup_msg
        elif used_fallback:
            backup_msg = (
                f"Backup folder wasn't reachable. Today's backup was saved to {actual_dir} instead."
            )
            api.backup_notice = f"{api.backup_notice} {backup_msg}" if api.backup_notice else backup_msg
        if ok and prune_failed:
            api.log(f"Launch prune couldn't delete {len(prune_failed)} file(s) in {folder_kind}")
            prune_msg = utils._prune_failed_message(len(prune_failed))
            api.backup_notice = f"{api.backup_notice} {prune_msg}" if api.backup_notice else prune_msg

    try:
        conn = db.open_db(db_path)
    except db.NewerSchemaError:
        platform_win._show_newer_schema_error()
        sys.exit(1)

    api.set_conn(conn)

    # post_due_autopays already commits/rolls back internally; this guard is
    # belt and suspenders so a posting failure can never block launch.
    try:
        api.post_due_autopays()
    except Exception as e:
        api.log(f"post_due_autopays call failed: {e}")
        api.autopay_notice = autopays._AUTOPAY_POST_FAILED_MESSAGE
        api.autopay_notice_is_error = True

    win = webview.create_window(
        "Simple Account Balancer",
        url=paths.resource_path("simple_account_balancer-UI.html"),
        js_api=api,
        width=1150,
        height=760,
        min_size=(config.MIN_WINDOW_W, config.MIN_WINDOW_H),
        background_color="#0a0e14",
    )
    api.set_window(win)
    win.events.shown += lambda: platform_win._restore_geometry(win)

    def _on_window_closing():
        platform_win._save_geometry(win)
        return True

    win.events.closing += _on_window_closing
    try:
        webview.start(gui="qt", icon=paths.resource_path("simple_account_balancer.png"))
    except TypeError:
        webview.start(gui="qt")

    # The window is gone, so debug log warnings from here on must not try to
    # reach it.
    api.set_window(None)

    # api._conn may no longer be the connection opened above (restore_backup
    # swaps in a new one mid-session), so close through the Api, not a stale
    # local variable.
    api.close_conn()

    # Exit backup runs unconditionally when the db file exists. Guarded so a
    # backup failure here can never block shutdown.
    try:
        if os.path.exists(db_path):
            ok, used_fallback, actual_dir, prune_failed = backup._run_backup_with_fallback(db_path)
            folder_kind = "the fallback folder" if used_fallback else "the backup folder"
            if ok:
                api.log(f"Exit backup created in {folder_kind}")
            else:
                api.log(f"Exit backup failed, tried {folder_kind}")
            # Failure wins over the fallback notice: a fallback that then
            # failed to write must not report that the backup was saved.
            if not ok:
                platform_win._show_backup_failed_notice(actual_dir)
            elif used_fallback:
                platform_win._show_backup_fallback_notice(actual_dir)
            if ok and prune_failed:
                api.log(f"Exit prune couldn't delete {len(prune_failed)} file(s) in {folder_kind}")
                platform_win._show_prune_failed_notice(len(prune_failed), actual_dir)
    except Exception:
        pass


if __name__ == "__main__":
    main()
