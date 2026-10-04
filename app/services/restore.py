"""Restore helpers."""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile

from app import config, db

RESTORE_TEMP_FILENAME_RE = re.compile(
    r"^\.balancer_restore_(?:stage|rollback)_[A-Za-z0-9_]+\.db(?:-journal)?\Z"
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------








def _check_backup(path: str) -> str | None:
    """Return a plain-English reason when a backup is unsafe to restore."""
    conn = None
    try:
        conn = db._readonly_connection(path)
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        if len(integrity) != 1 or integrity[0][0] != "ok":
            return "That backup file is corrupt or unreadable."
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > config.SCHEMA_VERSION:
            return (
                "That backup was made by a newer version of Simple Account Balancer. "
                "Update the app to restore it."
            )
        schema_error = db._schema_contract_error(conn, version)
        if schema_error:
            return schema_error
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return "That backup has broken links between its records."
        return None
    except Exception:
        return "That backup file is corrupt or unreadable."
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass




def _remove_stale_restore_files(db_dir: str) -> tuple:
    """Remove restore staging files left by an interrupted earlier run.
    Returns (how many were removed, names that couldn't be deleted)."""
    removed = 0
    failures = []
    try:
        names = os.listdir(db_dir)
    except Exception as e:
        return 0, [f"{db_dir}: {e}"]
    for name in names:
        if not RESTORE_TEMP_FILENAME_RE.fullmatch(name):
            continue
        path = os.path.join(db_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except Exception:
            failures.append(name)
    return removed, failures


def _file_sha256(path: str) -> str:
    """Return the SHA-256 digest of a file without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_backup(full_path: str, db_dir: str) -> tuple:
    """Copy, upgrade, and validate a backup before it can replace live data.
    Returns (staged path, None, None) or (None, plain-English error for the
    user, technical detail for the debug log)."""
    error = _check_backup(full_path)
    if error:
        return None, error, error
    staged_path = None
    try:
        fd, staged_path = tempfile.mkstemp(
            prefix=".balancer_restore_stage_", suffix=".db", dir=db_dir
        )
        os.close(fd)
        shutil.copy2(full_path, staged_path)
        conn = db.open_db(staged_path)
        conn.close()
        error = _check_backup(staged_path)
        if error:
            raise RuntimeError(error)
        conn = db._readonly_connection(staged_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != config.SCHEMA_VERSION:
                raise RuntimeError("The staged backup did not reach the current schema.")
        finally:
            conn.close()
        return staged_path, None, None
    except Exception as e:
        cleanup_failures = db._remove_db_artifacts(staged_path) if staged_path else []
        detail = f"{type(e).__name__}: {e}"
        if cleanup_failures:
            detail += " (couldn't clean up the failed staged backup)"
        return None, "Couldn't prepare that backup for restore. Nothing was changed.", detail


def _make_rollback_copy(conn: sqlite3.Connection, db_dir: str) -> tuple:
    """Create a local SQLite snapshot used only while a restore is in progress."""
    rollback_path = None
    dest = None
    try:
        fd, rollback_path = tempfile.mkstemp(
            prefix=".balancer_restore_rollback_", suffix=".db", dir=db_dir
        )
        os.close(fd)
        dest = sqlite3.connect(rollback_path)
        conn.backup(dest)
        dest.close()
        return rollback_path, None
    except Exception as e:
        if dest is not None:
            try:
                dest.close()
            except Exception:
                pass
        if rollback_path:
            db._remove_db_artifacts(rollback_path)
        return None, str(e) or "Couldn't prepare the local rollback copy."
