"""Backup helpers."""

import datetime
import os
import re
import shutil
import sqlite3

from app import config, db, paths, prefs, utils

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


def _make_backup(db_path: str, backups_dir: str, keep: int = config.BACKUP_KEEP) -> tuple:
    """Copy the database into backups_dir as balancer_YYYYMMDD_HHMMSS_ffffff.db,
    keeping only the newest `keep` regular backups. Pre-restore safety
    backups are a separate pool; see _make_prerestore_backup. Returns
    (success, filenames the prune couldn't delete)."""
    try:
        os.makedirs(backups_dir, exist_ok=True)
        shutil.copy2(db_path, _new_backup_path(backups_dir, "balancer_"))
    except Exception:
        return False, []
    return True, _prune_backups(backups_dir, keep)


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


def _run_backup_with_fallback(db_path: str) -> tuple:
    """Take a backup in the effective backup folder. If a custom folder is
    configured but is missing or unwritable right now, fall back to the
    default local folder for just this backup; the pref is left alone since
    the folder (e.g. a NAS) may only be temporarily offline. Never raises.
    Returns (success, used_fallback, actual_dir, filenames the prune
    couldn't delete)."""
    default_dir = os.path.join(paths.app_dir(), config.BACKUP_DIRNAME)
    try:
        prefs_data = prefs.load_prefs()
        keep = _clamp_backup_keep(prefs_data.get("backup_keep"))
        target_dir, is_custom = effective_backup_dir()
        used_fallback = False
        if is_custom and not (os.path.isdir(target_dir) and utils._writable_check(target_dir)):
            target_dir = default_dir
            used_fallback = True
        ok, prune_failed = _make_backup(db_path, target_dir, keep)
        return ok, used_fallback, target_dir, prune_failed
    except Exception:
        return False, False, default_dir, []
