"""Restore helpers."""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile

from app import config, db, utils
from app.services import backup

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


def _validate_backup_filename(api, filename):
    """Only a bare basename matching our own naming pattern, that
        actually exists in the effective backup folder, is accepted. Guards
        against path traversal and against opening arbitrary files."""
    name = os.path.basename(str(filename or ""))
    if not name or name != filename or not backup._is_backup_filename(name):
        return None, "That doesn't look like one of this app's backup files."
    backups_dir, _ = backup.effective_backup_dir()
    full_path = os.path.join(backups_dir, name)
    if not os.path.isfile(full_path):
        return None, "That backup file no longer exists."
    return full_path, None


def _open_backup_readonly(full_path):
    """Open a backup file read-only and sanity-check it before any use.
        Never raises; returns (connection, None) or (None, error_message)."""
    error = _check_backup(full_path)
    if error:
        return None, error
    try:
        return db._readonly_connection(full_path), None
    except Exception:
        return None, "That backup file is corrupt or unreadable."


def _diff_against_live(api, backup_conn):
    """Per-account comparison of a backup against the live database.
        Accounts are matched by id (both tables are AUTOINCREMENT, so ids
        are never reused); transactions are matched by id and their editable
        contents are compared without treating an edit as a deletion."""
    account_columns = (
        "id, name, starting_balance_cents, starting_date, "
        "starting_balance_prev_cents, starting_balance_changed_at"
    )
    live_accounts = {
        row["id"]: row for row in api._conn.execute(f"SELECT {account_columns} FROM accounts")
    }
    backup_accounts = {
        row["id"]: row for row in backup_conn.execute(f"SELECT {account_columns} FROM accounts")
    }

    def transactions_by_id(conn, account_id):
        rows = conn.execute(
            "SELECT id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key "
            "FROM transactions WHERE account_id=?",
            (account_id,),
        ).fetchall()
        return {
            row["id"]: (
                row["date"], row["payee"], row["category"], row["notes"],
                row["amount_cents"], row["cleared"], row["estimated"], row["sort_key"],
            ) for row in rows
        }

    results = []
    for account_id in set(live_accounts) | set(backup_accounts):
        in_live = account_id in live_accounts
        in_backup = account_id in backup_accounts
        name = (live_accounts.get(account_id) or backup_accounts[account_id])["name"]
        if in_live and not in_backup:
            results.append({
                "id": account_id, "name": name, "kind": "only_live",
                "count": len(transactions_by_id(api._conn, account_id)),
            })
        elif in_backup and not in_live:
            results.append({
                "id": account_id, "name": name, "kind": "only_backup",
                "count": len(transactions_by_id(backup_conn, account_id)),
            })
        else:
            live_transactions = transactions_by_id(api._conn, account_id)
            backup_transactions = transactions_by_id(backup_conn, account_id)
            added = len(live_transactions.keys() - backup_transactions.keys())
            deleted = len(backup_transactions.keys() - live_transactions.keys())
            changed = sum(
                live_transactions[transaction_id] != backup_transactions[transaction_id]
                for transaction_id in live_transactions.keys() & backup_transactions.keys()
            )
            details_changed = any(
                live_accounts[account_id][column] != backup_accounts[account_id][column]
                for column in (
                    "name", "starting_balance_cents", "starting_date",
                    "starting_balance_prev_cents", "starting_balance_changed_at",
                )
            )
            if added == 0 and changed == 0 and deleted == 0 and not details_changed:
                results.append({"id": account_id, "name": name, "kind": "same"})
            else:
                results.append({
                    "id": account_id, "name": name, "kind": "diff",
                    "added": added, "changed": changed, "deleted": deleted,
                    "details_changed": details_changed,
                })
    results.sort(key=lambda r: (r["name"] or "").casefold())
    return results


def _categories_match_live(api, backup_conn):
    live_names = sorted(row["name"] for row in api._conn.execute("SELECT name FROM categories"))
    backup_names = sorted(row["name"] for row in backup_conn.execute("SELECT name FROM categories"))
    return live_names == backup_names


def _autopays_match_live(api, backup_conn):
    columns = (
        "id, account_id, payee, category, notes, amount_cents, next_pay_date, "
        "next_post_date, pay_day, post_day, is_variable"
    )
    live_rows = {
        row["id"]: tuple(row[column] for column in row.keys() if column != "id")
        for row in api._conn.execute(f"SELECT {columns} FROM autopays")
    }
    backup_rows = {
        row["id"]: tuple(row[column] for column in row.keys() if column != "id")
        for row in backup_conn.execute(f"SELECT {columns} FROM autopays")
    }
    return live_rows == backup_rows


def preview_restore(api, filename):
    """Compare a backup file against the live database without changing
        anything, so the UI can show what a restore would do."""
    try:
        full_path, err = api._validate_backup_filename(filename)
        if err:
            return {"ok": False, "error": err}
        db_dir = os.path.dirname(os.path.abspath(api._db_path))
        staged_path, err, detail = _stage_backup(full_path, db_dir)
        if err:
            api.log(f"preview_restore staging failed: {detail}")
            return {"ok": False, "error": err}
        try:
            backup_conn = db._readonly_connection(staged_path)
            try:
                accounts_diff = api._diff_against_live(backup_conn)
                categories = "same" if api._categories_match_live(backup_conn) else "differ"
                autopays = "same" if api._autopays_match_live(backup_conn) else "differ"
            finally:
                backup_conn.close()
        finally:
            cleanup_failures = db._remove_db_artifacts(staged_path)
            if cleanup_failures:
                api.log("preview_restore couldn't clean up its staged backup")
        timestamp = backup._parse_backup_timestamp(os.path.basename(full_path), full_path)
        return {
            "ok": True, "timestamp": timestamp, "accounts": accounts_diff,
            "categories": categories, "autopays": autopays,
            "fingerprint": _file_sha256(full_path),
        }
    except Exception as e:
        api.log(f"preview_restore failed: {e}")
        return {"ok": False, "error": "Couldn't read that backup file."}


def restore_backup(api, filename, fingerprint=None):
    """Restore the live database from a backup file. Takes a pre-restore
        safety backup of the current live data first, so this can be undone."""
    try:
        full_path, err = api._validate_backup_filename(filename)
        if err:
            return {"ok": False, "error": err}
        if fingerprint is not None and _file_sha256(full_path) != fingerprint:
            message = "That backup file changed since you previewed it. Open it again to see what it would change."
            api.log("restore_backup refused because the backup changed since preview")
            return {"ok": False, "error": message}
        db_path = api._db_path
        backups_dir, _ = backup.effective_backup_dir()
        db_dir = os.path.dirname(os.path.abspath(db_path))
        staged_path, err, detail = _stage_backup(full_path, db_dir)
        if err:
            api.log(f"restore_backup staging failed: {detail}")
            return {"ok": False, "error": err}

        prerestore_path, prune_failed = backup._make_prerestore_backup(
            db_path, backups_dir, api._conn
        )
        if prerestore_path is None:
            cleanup_failures = db._remove_db_artifacts(staged_path)
            api.log("restore_backup cancelled because its safety backup failed")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up its staged backup")
            return {"ok": False, "error": "Couldn't take a safety backup, so the restore was cancelled."}

        rollback_path, err = _make_rollback_copy(api._conn, db_dir)
        if err:
            cleanup_failures = db._remove_db_artifacts(staged_path)
            api.log(f"restore_backup rollback snapshot failed: {err}")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up its staged backup")
            return {"ok": False, "error": "Couldn't prepare a local rollback copy, so the restore was cancelled."}

        if not api.close_conn():
            cleanup_failures = db._remove_db_artifacts(staged_path)
            cleanup_failures += db._remove_db_artifacts(rollback_path)
            api.log("restore_backup cancelled because the live database did not close")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up temporary restore files")
            return {"ok": False, "error": "Couldn't close the live database, so the restore was cancelled."}

        if os.path.exists(db_path + "-journal"):
            try:
                api.set_conn(db.open_db(db_path))
            except Exception as e:
                api._conn = None
                api.log(f"restore_backup couldn't reopen the live database after finding a journal: {e}")
            cleanup_failures = db._remove_db_artifacts(staged_path)
            cleanup_failures += db._remove_db_artifacts(rollback_path)
            api.log("restore_backup refused to replace a database with a journal beside it")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up temporary restore files")
            return {"ok": False, "error": "The live database has an unfinished journal, so the restore was cancelled."}

        try:
            os.replace(staged_path, db_path)
        except Exception as e:
            try:
                api.set_conn(db.open_db(db_path))
            except Exception as reopen_error:
                api._conn = None
                api.log(f"restore_backup couldn't reopen unchanged live database: {reopen_error}")
            cleanup_failures = db._remove_db_artifacts(staged_path)
            cleanup_failures += db._remove_db_artifacts(rollback_path)
            api.log(f"restore_backup replace failed; live database was not changed: {e}")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up temporary restore files")
            return {
                "ok": False,
                "error": "Couldn't put the backup into place. Nothing was changed.",
            }

        try:
            api.set_conn(db.open_db(db_path))
        except Exception as e:
            api._conn = None
            try:
                os.replace(rollback_path, db_path)
            except Exception as rollback_error:
                cleanup_failures = db._remove_db_artifacts(staged_path)
                cleanup_failures += db._remove_db_artifacts(rollback_path)
                api.log(
                    "restore_backup failed after replacement and couldn't put back "
                    f"the local rollback copy: {rollback_error}"
                )
                if cleanup_failures:
                    api.log("restore_backup couldn't clean up temporary restore files")
                return {
                    "ok": False,
                    "error": (
                        "The restore failed and the app couldn't put your data back. "
                        f"Recover it manually from this safety backup: {prerestore_path}"
                    ),
                }
            try:
                api.set_conn(db.open_db(db_path))
            except Exception as reopen_error:
                api._conn = None
                api.log(f"restore_backup put data back but couldn't reopen it: {reopen_error}")
            cleanup_failures = db._remove_db_artifacts(staged_path)
            cleanup_failures += db._remove_db_artifacts(rollback_path)
            api.log(f"restore_backup failed after replacement; live data was put back: {e}")
            if cleanup_failures:
                api.log("restore_backup couldn't clean up temporary restore files")
            return {"ok": False, "error": "The restore failed and your data was put back."}

        rollback_cleanup_failed = bool(db._remove_db_artifacts(rollback_path))
        if rollback_cleanup_failed:
            api.log("restore_backup completed but couldn't clean up its local rollback copy")
        api.log(
            f"Restored from backup {os.path.basename(full_path)} "
            f"(safety backup: {os.path.basename(prerestore_path)})"
        )
        result = api.get_config()
        warnings = []
        if prune_failed:
            api.log(f"Pre-restore prune couldn't delete {len(prune_failed)} file(s) in the backup folder")
            warnings.append(utils._prune_failed_message(len(prune_failed)))
        if rollback_cleanup_failed:
            warnings.append("Restored, but a temporary copy in the data folder couldn't be deleted.")
        if warnings:
            result["warning"] = " ".join(warnings)
        return result
    except Exception as e:
        api.log(f"restore_backup failed: {e}")
        return {"ok": False, "error": "Couldn't restore that backup."}
