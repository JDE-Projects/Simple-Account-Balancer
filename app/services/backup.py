"""Backup helpers."""

import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import uuid

import webview

from app import config, db, paths, prefs, utils
from app.services import restore

# Regular backups: balancer_YYYYMMDD_HHMMSS_ffffff.db
# Pre-restore safety backups: balancer_prerestore_YYYYMMDD_HHMMSS_ffffff.db
# The _ffffff microseconds keep two backups in the same second apart. Names
# without it (balancer_YYYYMMDD_HHMMSS.db) still match, and still sort in time
# order, since "." sorts before "_". The optional "prerestore_" is not its own
# group, since both variants share the same trailing date/time. Listing,
# pruning, and restore accept only names passing _is_backup_filename, so other
# files in a shared backup folder are never shown or deleted. ASCII digits
# only, and \Z rather than $ so a trailing newline can't slip through.
BACKUP_FILENAME_RE = re.compile(
    r"^balancer_(?:prerestore_)?([0-9]{8})_([0-9]{6})(?:_([0-9]{6}))?\.db\Z"
)
BACKUP_CANDIDATE_FILENAME_RE = re.compile(
    r"^\.balancer_backup_candidate_[0-9a-f]{32}\.db(?:-journal)?\Z", re.ASCII
)


def _is_backup_filename(name: str) -> bool:
    """True only for an exact BACKUP_FILENAME_RE name whose date and time are
    real, so a name like balancer_00000000_000000.db is never listed,
    restored, or pruned as the oldest backup."""
    m = BACKUP_FILENAME_RE.match(name)
    if not m:
        return False
    try:
        datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return True

# ---------------------------------------------------------------------------
# Startup: writable-location check + rolling backups
# ---------------------------------------------------------------------------




def effective_backup_dir() -> tuple:
    """Where backups are read from and written to right now: the custom
    backup_folder pref if one is set (replacing the default, not adding to
    it), else the default backups/ folder next to the app. Returns
    (path, is_custom). Module-level, no Api state, so main() can call it
    before any window exists."""
    custom = prefs.load_prefs().get("backup_folder")
    if custom:
        return custom, True
    return os.path.join(paths.app_dir(), config.BACKUP_DIRNAME), False


def _clamp_backup_keep(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return config.BACKUP_KEEP
    return max(config.BACKUP_KEEP_MIN, min(config.BACKUP_KEEP_MAX, n))


def _list_backup_files(backups_dir: str, prerestore: bool) -> list:
    """Exact backup filenames (_is_backup_filename) sorted oldest first, so
    pruning never touches look-alike files. Regular backups exclude prerestore
    ones; the fixed-width timestamp makes lexical order match chronological
    order within each pool."""
    try:
        names = os.listdir(backups_dir)
    except Exception:
        return []
    return sorted(
        n for n in names
        if _is_backup_filename(n) and n.startswith("balancer_prerestore_") == prerestore
    )


def _prune_pool(backups_dir: str, keep: int, prerestore: bool) -> list:
    """Delete the oldest backups in one pool beyond `keep`. Returns the
    filenames that couldn't be deleted, so the caller can say so: old copies
    of financial data piling up unseen is the failure this guards against."""
    files = _list_backup_files(backups_dir, prerestore=prerestore)
    failed = []
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(backups_dir, old))
        except Exception:
            failed.append(old)
    return failed


def _prune_backups(backups_dir: str, keep: int) -> list:
    return _prune_pool(backups_dir, keep, prerestore=False)


def _prune_prerestore_backups(backups_dir: str, keep: int = config.PRERESTORE_KEEP) -> list:
    return _prune_pool(backups_dir, keep, prerestore=True)


def _new_backup_path(backups_dir: str, prefix: str) -> str:
    """A path for a new backup named <prefix>YYYYMMDD_HHMMSS_ffffff.db that
    doesn't exist yet, so a backup never overwrites an earlier one."""
    while True:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = os.path.join(backups_dir, f"{prefix}{stamp}.db")
        if not os.path.exists(dest):
            return dest


def _remove_stale_backup_candidates(backups_dir: str) -> list:
    """Remove candidate files left by an interrupted backup. Each file is
    matched on its own, so a journal whose database is already gone is
    still retried. Returns "name: error type" for each file that couldn't
    be removed; the backup goes ahead either way."""
    try:
        names = os.listdir(backups_dir)
    except Exception as e:
        return [f"backup folder listing: {type(e).__name__}"]
    failures = []
    for name in names:
        if not BACKUP_CANDIDATE_FILENAME_RE.fullmatch(name):
            continue
        try:
            os.remove(os.path.join(backups_dir, name))
        except FileNotFoundError:
            pass
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}")
    return failures


def _log_cleanup_failures(api, when: str, cleanup_failed: list) -> None:
    """Log each leftover candidate file a backup couldn't remove."""
    for failure in cleanup_failed:
        api.log(f"{when} backup couldn't remove a leftover file: {failure}")


def _candidate_is_valid(candidate_path: str) -> bool:
    """True when a candidate backup has intact, supported SQLite data."""
    conn = None
    try:
        conn = db._readonly_connection(candidate_path)
        return db._database_check_error(conn, allow_newer=True) is None
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _snapshot_for_backup_copy(api):
    """Create a consistent local snapshot while the database lock is held."""
    if api._conn is None or not api._db_path:
        return {
            "ok": False,
            "error": "The database isn't available right now. Please restart the app.",
        }
    snapshot_path = None
    dst = None
    try:
        fd, snapshot_path = tempfile.mkstemp(
            prefix=".balancer_save_snapshot_",
            suffix=".db",
            dir=os.path.dirname(api._db_path),
        )
        os.close(fd)
        dst = sqlite3.connect(snapshot_path)
        api._conn.backup(dst)
        dst.close()
        return {"ok": True, "snapshot": snapshot_path}
    except Exception as e:
        if dst is not None:
            try:
                dst.close()
            except Exception:
                pass
        if snapshot_path is not None:
            db._remove_db_artifacts(snapshot_path)
        api.log(f"save_backup_copy snapshot failed: {type(e).__name__}")
        return {"ok": False, "error": "Couldn't save the backup."}


def _cleanup_warning(api, snapshot_path, candidate_path, published):
    """Remove temporary files and return a safe warning when cleanup fails."""
    failures = [("the app's data folder", db._remove_db_artifacts(snapshot_path))]
    if candidate_path is not None and not published:
        failures.append(("the folder you picked", db._remove_db_artifacts(candidate_path)))
    failed = [(folder, entries) for folder, entries in failures if entries]
    if not failed:
        return None
    api.log(f"Backup copy cleanup failed: {sum(len(entries) for _, entries in failed)}")
    folder = next(
        (folder for folder, _ in failed if folder == "the folder you picked"), failed[0][0]
    )
    return f"A temporary backup file couldn't be removed from {folder}. You can delete it yourself."


def _copy_backup_snapshot(api, snapshot_path: str, dest: str):
    """Validate and atomically publish a user-requested backup copy."""
    candidate_path = None
    published = False
    result = None
    try:
        if restore._check_backup(snapshot_path) is not None:
            result = {"ok": False, "error": "Couldn't save the backup."}
        else:
            candidate_path = os.path.join(
                os.path.dirname(dest), f".balancer_backup_candidate_{uuid.uuid4().hex}.db"
            )
            shutil.copyfile(snapshot_path, candidate_path)
            if restore._check_backup(candidate_path) is not None:
                result = {"ok": False, "error": "Couldn't save the backup."}
            else:
                os.replace(candidate_path, dest)
                published = True
                result = {"ok": True, "path": dest}
                api.log("Backup copy saved")
    except Exception as e:
        api.log(f"save_backup_copy failed: {type(e).__name__}")
        result = {"ok": False, "error": "Couldn't save the backup."}
    finally:
        warning = _cleanup_warning(api, snapshot_path, candidate_path, published)
        if warning:
            result["cleanup_warning"] = warning
    return result


def save_backup_copy(api):
    """Save a validated copy of the live database through a native dialog."""
    try:
        documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
        start_dir = documents_dir if os.path.isdir(documents_dir) else paths.app_dir()
        selected = api._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=start_dir,
            save_filename=f"SAB_Backup_{datetime.date.today():%m%d%Y}.db",
            file_types=("Backup Files (*.db)",),
        )
        if not selected:
            return {"ok": True, "cancelled": True}
        dest = selected[0] if isinstance(selected, (list, tuple)) else selected
        if not dest:
            return {"ok": True, "cancelled": True}
        if not dest.lower().endswith(".db"):
            dest += ".db"
            if os.path.exists(dest):
                return {
                    "ok": False,
                    "error": "A file with that name already exists. Choose a different name.",
                }
        if api._db_path:
            normalized_dest = os.path.normcase(os.path.abspath(dest))
            normalized_db = os.path.normcase(os.path.abspath(api._db_path))
            if normalized_dest in (normalized_db, normalized_db + "-journal"):
                return {"ok": False, "error": "Choose a different place to save the backup."}
        snapshot = api._snapshot_for_backup_copy()
        if not snapshot["ok"]:
            return snapshot
        return _copy_backup_snapshot(api, snapshot["snapshot"], dest)
    except Exception as e:
        api.log(f"save_backup_copy failed: {type(e).__name__}")
        return {"ok": False, "error": "Couldn't save the backup."}


def _make_backup(
    db_path: str,
    backups_dir: str,
    keep: int = config.BACKUP_KEEP,
    source_conn: sqlite3.Connection | None = None,
) -> tuple:
    """Stage and validate a regular database backup before publishing it,
    then keep only the newest `keep` regular backups. Pre-restore safety
    backups are a separate pool; see _make_prerestore_backup. Returns
    (success, filenames the prune couldn't delete, leftover candidate files
    that couldn't be removed)."""
    candidate_path = None
    cleanup_failed = []
    src = source_conn
    owns_src = source_conn is None
    dst = None
    try:
        os.makedirs(backups_dir, exist_ok=True)
        cleanup_failed = _remove_stale_backup_candidates(backups_dir)
        candidate_path = os.path.join(
            backups_dir, f".balancer_backup_candidate_{uuid.uuid4().hex}.db"
        )
        if src is None:
            src = db._readwrite_connection(db_path, timeout=5.0)
        dst = sqlite3.connect(candidate_path)
        src.backup(dst)
        dst.close()
        dst = None
    except Exception:
        if dst is not None:
            try:
                dst.close()
            except Exception:
                pass
        if owns_src and src is not None:
            try:
                src.close()
            except Exception:
                pass
        if candidate_path is not None:
            db._remove_db_artifacts(candidate_path)
        return False, [], cleanup_failed
    if owns_src and src is not None:
        try:
            src.close()
        except Exception:
            db._remove_db_artifacts(candidate_path)
            return False, [], cleanup_failed
    if not _candidate_is_valid(candidate_path):
        db._remove_db_artifacts(candidate_path)
        return False, [], cleanup_failed
    while True:
        try:
            os.rename(candidate_path, _new_backup_path(backups_dir, "balancer_"))
            break
        except FileExistsError:
            continue
        except Exception:
            db._remove_db_artifacts(candidate_path)
            return False, [], cleanup_failed
    return True, _prune_backups(backups_dir, keep), cleanup_failed

def _make_prerestore_backup(
    db_path: str, backups_dir: str, source_conn: sqlite3.Connection | None = None
) -> tuple:
    """Snapshot the current live database before a restore overwrites it,
    as balancer_prerestore_YYYYMMDD_HHMMSS_ffffff.db. Kept to the newest
    PRERESTORE_KEEP, a pool separate from (and never counted against) the
    regular backup_keep limit. Returns (the new file's full path or None on
    failure, filenames the prune couldn't delete)."""
    src = source_conn
    owns_src = source_conn is None
    dst = None
    dest = None
    try:
        os.makedirs(backups_dir, exist_ok=True)
        dest = _new_backup_path(backups_dir, "balancer_prerestore_")
        if src is None:
            src = db._readonly_connection(db_path)  # never creates a missing file
        dst = sqlite3.connect(dest)
        src.backup(dst)
        dst.close()
        dst = None
        if owns_src:
            src.close()
            src = None
    except Exception:
        if dst is not None:
            try:
                dst.close()
            except Exception:
                pass
        if src is not None and owns_src:
            try:
                src.close()
            except Exception:
                pass
        if dest is not None:
            db._remove_db_artifacts(dest)
        return None, []
    return dest, _prune_prerestore_backups(backups_dir)




def _parse_backup_timestamp(filename: str, full_path: str) -> str:
    """The timestamp encoded in a balancer_* filename, falling back to the
    file's mtime if the name doesn't parse. Always returns an ISO string to
    the second; list_backups breaks same-second ties by filename."""
    m = BACKUP_FILENAME_RE.match(filename)
    if m:
        try:
            dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(full_path)).isoformat(timespec="seconds")
    except Exception:
        return datetime.datetime.min.isoformat(timespec="seconds")


def _run_backup_with_fallback(
    db_path: str, source_conn: sqlite3.Connection | None = None
) -> tuple:
    """Take a backup in the effective backup folder. If a custom folder is
    configured but is missing or unwritable right now, fall back to the
    default local folder for just this backup; the pref is left alone since
    the folder (e.g. a NAS) may only be temporarily offline. Never raises.
    Returns (success, used_fallback, actual_dir, filenames the prune
    couldn't delete, leftover candidate files that couldn't be removed)."""
    default_dir = os.path.join(paths.app_dir(), config.BACKUP_DIRNAME)
    try:
        prefs_data = prefs.load_prefs()
        keep = _clamp_backup_keep(prefs_data.get("backup_keep"))
        target_dir, is_custom = effective_backup_dir()
        used_fallback = False
        if is_custom and not (os.path.isdir(target_dir) and utils._writable_check(target_dir)):
            target_dir = default_dir
            used_fallback = True
        ok, prune_failed, cleanup_failed = _make_backup(
            db_path, target_dir, keep, source_conn=source_conn
        )
        return ok, used_fallback, target_dir, prune_failed, cleanup_failed
    except Exception:
        return False, False, default_dir, [], []


def choose_backup_folder(api):
    """Move the backup location to a folder the user picks. Existing
        backup files are never moved; new backups just start landing there."""
    try:
        result = api._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return {"ok": True, "cancelled": True}
        folder = result[0] if isinstance(result, (list, tuple)) else result
        if not folder:
            return {"ok": True, "cancelled": True}
        if not utils._writable_check(folder):
            return {"ok": False, "error": "That folder isn't writable. Choose a different one."}
        prefs_data = prefs.load_prefs()
        prefs_data["backup_folder"] = folder
        if not prefs.save_prefs(prefs_data):
            api.log("Could not save backup folder pref")
            return {"ok": False, "error": "Couldn't save the backup folder setting."}
        api.log("Backup folder moved to a custom folder")
        return {"ok": True, "backup_folder": folder, "backup_folder_is_custom": True}
    except Exception as e:
        api.log(f"choose_backup_folder failed: {e}")
        return {"ok": False, "error": "Couldn't set the backup folder."}


def reset_backup_folder(api):
    """Reset the backup location back to the default folder next to the app."""
    try:
        prefs_data = prefs.load_prefs()
        prefs_data.pop("backup_folder", None)
        if not prefs.save_prefs(prefs_data):
            api.log("Could not save backup folder pref on reset")
            return {"ok": False, "error": "Couldn't reset the backup folder."}
        api.log("Backup folder reset to default")
        default_dir, _ = effective_backup_dir()
        return {"ok": True, "backup_folder": default_dir, "backup_folder_is_custom": False}
    except Exception as e:
        api.log(f"reset_backup_folder failed: {e}")
        return {"ok": False, "error": "Couldn't reset the backup folder."}


def set_backup_keep(api, n):
    """Set how many regular backups to keep, clamped to 1..50. Pre-restore
        safety backups are pruned separately and never count against this."""
    try:
        keep = _clamp_backup_keep(n)
        prefs_data = prefs.load_prefs()
        prefs_data["backup_keep"] = keep
        if not prefs.save_prefs(prefs_data):
            api.log("Could not save backup count pref")
            return {"ok": False, "error": "Couldn't save the backup count."}
        api.log(f"Backup keep count set to {keep}")
        return {"ok": True, "backup_keep": keep}
    except Exception as e:
        api.log(f"set_backup_keep failed: {e}")
        return {"ok": False, "error": "Couldn't save the backup count."}


def list_backups(api):
    """List backups in the effective backup folder, newest first."""
    try:
        backups_dir, _ = effective_backup_dir()
        try:
            names = os.listdir(backups_dir)
        except Exception:
            names = []
        items = []
        for name in names:
            if not _is_backup_filename(name):
                continue
            full_path = os.path.join(backups_dir, name)
            items.append({
                "filename": name,
                "timestamp": _parse_backup_timestamp(name, full_path),
                "is_prerestore": name.startswith("balancer_prerestore_"),
                "size_bytes": os.path.getsize(full_path) if os.path.isfile(full_path) else 0,
            })
        items.sort(key=lambda it: (it["timestamp"], it["filename"]), reverse=True)
        return {"ok": True, "backups": items}
    except Exception as e:
        api.log(f"list_backups failed: {e}")
        return {"ok": False, "error": "Couldn't list backups."}
