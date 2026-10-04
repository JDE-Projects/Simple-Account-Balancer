"""pywebview bridge."""

import os
import sqlite3
import threading
from functools import wraps

import webview

from app import db, paths, prefs, utils
from app.debug_log import DebugLog
from app.services import (
    accounts, autopays, backup, categories, compare, debug, export, restore, settings,
    transactions, updates,
)

_DATABASE_FAILURE_MESSAGE = "Something went wrong saving your data. Please restart the app."
def _database_call(method):
    """Serialize Api database access and discard abandoned write transactions."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._db_lock:
            if self._database_failed:
                self.log(f"{method.__name__} refused after database rollback failure")
                if method.__name__ == "close_conn":
                    return False
                if method.__name__ == "post_due_autopays":
                    return None
                return {"ok": False, "error": _DATABASE_FAILURE_MESSAGE}

            self._database_call_depth += 1
            try:
                return method(self, *args, **kwargs)
            finally:
                self._database_call_depth -= 1
                if self._database_call_depth == 0:
                    try:
                        if self._conn is not None and self._conn.in_transaction:
                            self._conn.rollback()
                            self.log(f"{method.__name__} left uncommitted work; rolled back")
                    except Exception as e:
                        self.log(f"{method.__name__} could not roll back database work: {e}")
                        try:
                            if self._conn is not None:
                                self._conn.close()
                        except Exception as close_error:
                            self.log(f"{method.__name__} could not close the database: {close_error}")
                        self._conn = None
                        self._database_failed = True

    return wrapped


class Api:
    """Bridge exposed to the UI. Methods return JSON-able dicts; the UI awaits."""

    def __init__(self, version):
        self._version = version
        self._window = None
        self._conn = None
        self._db_lock = threading.RLock()
        self._database_call_depth = 0
        self._database_failed = False
        self._db_path = None
        self._debug_log = DebugLog(
            paths.app_dir(), "Simple Account Balancer",
            redact=utils.redact_log_text, on_warning=self._on_debug_log_warning,
        )
        self.backup_notice = None
        self.autopay_notice = None
        self.autopay_notice_is_error = False

    def set_window(self, w):
        self._window = w

    @_database_call
    def set_conn(self, conn: sqlite3.Connection):
        self._conn = conn

    def set_db_path(self, path: str):
        self._db_path = path

    @_database_call
    def close_conn(self):
        """Close whichever connection is currently live. restore_backup can
        swap in a new connection mid-session, so callers (main() at exit)
        should always go through this rather than holding a stale local."""
        try:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            return True
        except Exception as e:
            self.log(f"close_conn failed: {e}")
            return False

    # --- account + config ---------------------------------------------------
    def _get_account(self, account_id=None):
        return accounts._get_account(self, account_id)

    def _current_balance_cents(self, account) -> int:
        return accounts._current_balance_cents(self, account)

    def _today_balance_cents(self, account) -> int:
        return accounts._today_balance_cents(self, account)

    def _account_payload(self, account):
        return accounts._account_payload(self, account)

    @_database_call
    def get_config(self):
        return accounts.get_config(self)

    @_database_call
    def create_account(self, name, starting_balance, starting_date):
        return accounts.create_account(self, name, starting_balance, starting_date)

    @_database_call
    def set_active_account(self, account_id):
        return accounts.set_active_account(self, account_id)

    @_database_call
    def delete_account(self, account_id):
        return accounts.delete_account(self, account_id)

    @_database_call
    def update_account(self, account_id, name, starting_balance, starting_date):
        return accounts.update_account(self, account_id, name, starting_balance, starting_date)

    # --- categories -------------------------------------------------------------
    @_database_call
    def get_categories(self):
        return categories.get_categories(self)

    @_database_call
    def add_category(self, name):
        return categories.add_category(self, name)

    @_database_call
    def rename_category(self, category_id, new_name):
        return categories.rename_category(self, category_id, new_name)

    @_database_call
    def delete_category(self, category_id, reassign_to=None):
        return categories.delete_category(self, category_id, reassign_to)

    # --- transactions ---------------------------------------------------------
    @_database_call
    def get_payees(self, account_id=None):
        return categories.get_payees(self, account_id)

    def _rows_with_balance(self, account) -> list:
        return transactions._rows_with_balance(self, account)

    @_database_call
    def get_transactions(self, account_id=None, from_date=None, to_date=None, search=""):
        return transactions.get_transactions(self, account_id, from_date, to_date, search)

    @_database_call
    def add_transaction(self, account_id, date, payee, category, notes, amount, direction):
        return transactions.add_transaction(self, account_id, date, payee, category, notes, amount, direction)

    @_database_call
    def update_transaction(self, transaction_id, date, payee, category, notes, amount, direction):
        return transactions.update_transaction(self, transaction_id, date, payee, category, notes, amount, direction)

    @_database_call
    def delete_transaction(self, transaction_id):
        return transactions.delete_transaction(self, transaction_id)

    @_database_call
    def reorder_transactions(self, account_id, date, ordered_ids):
        return transactions.reorder_transactions(self, account_id, date, ordered_ids)

    @_database_call
    def confirm_estimated_amount(self, transaction_id, amount):
        return transactions.confirm_estimated_amount(self, transaction_id, amount)

    # --- autopays ---------------------------------------------------------------
    @_database_call
    def get_autopays(self, account_id):
        return autopays.get_autopays(self, account_id)

    @_database_call
    def add_autopay(self, account_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
        return autopays.add_autopay(self, account_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable)

    @_database_call
    def update_autopay(self, autopay_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
        return autopays.update_autopay(self, autopay_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable)

    @_database_call
    def delete_autopay(self, autopay_id):
        return autopays.delete_autopay(self, autopay_id)

    @_database_call
    def post_due_autopays(self):
        return autopays.post_due_autopays(self)

    # --- compare ---------------------------------------------------------------
    @_database_call
    def get_compare_data(self, account_id=None, from_date=None, to_date=None):
        return compare.get_compare_data(self, account_id, from_date, to_date)

    @_database_call
    def find_compare_matches(self, account_id=None, amount=None, from_date=None, to_date=None):
        return compare.find_compare_matches(self, account_id, amount, from_date, to_date)

    # --- CSV export -------------------------------------------------------------
    @_database_call
    def _export_csv_snapshot(self, account_id, from_date, to_date, range_label):
        return export._export_csv_snapshot(self, account_id, from_date, to_date, range_label)

    def export_csv(self, account_id, from_date, to_date, range_label=""):
        return export.export_csv(self, account_id, from_date, to_date, range_label)

    # --- preferences (local file, not stored in the db) ----------------------
    def _load_theme(self) -> str:
        return settings._load_theme(self)

    def save_theme(self, theme: str):
        return settings.save_theme(self, theme)

    def choose_backup_folder(self):
        """Move the backup location to a folder the user picks. Existing
        backup files are never moved; new backups just start landing there."""
        try:
            result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
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
                self.log("Could not save backup folder pref")
                return {"ok": False, "error": "Couldn't save the backup folder setting."}
            self.log("Backup folder moved to a custom folder")
            return {"ok": True, "backup_folder": folder, "backup_folder_is_custom": True}
        except Exception as e:
            self.log(f"choose_backup_folder failed: {e}")
            return {"ok": False, "error": "Couldn't set the backup folder."}

    def reset_backup_folder(self):
        """Reset the backup location back to the default folder next to the app."""
        try:
            prefs_data = prefs.load_prefs()
            prefs_data.pop("backup_folder", None)
            if not prefs.save_prefs(prefs_data):
                self.log("Could not save backup folder pref on reset")
                return {"ok": False, "error": "Couldn't reset the backup folder."}
            self.log("Backup folder reset to default")
            default_dir, _ = backup.effective_backup_dir()
            return {"ok": True, "backup_folder": default_dir, "backup_folder_is_custom": False}
        except Exception as e:
            self.log(f"reset_backup_folder failed: {e}")
            return {"ok": False, "error": "Couldn't reset the backup folder."}

    def set_backup_keep(self, n):
        """Set how many regular backups to keep, clamped to 1..50. Pre-restore
        safety backups are pruned separately and never count against this."""
        try:
            keep = backup._clamp_backup_keep(n)
            prefs_data = prefs.load_prefs()
            prefs_data["backup_keep"] = keep
            if not prefs.save_prefs(prefs_data):
                self.log("Could not save backup count pref")
                return {"ok": False, "error": "Couldn't save the backup count."}
            self.log(f"Backup keep count set to {keep}")
            return {"ok": True, "backup_keep": keep}
        except Exception as e:
            self.log(f"set_backup_keep failed: {e}")
            return {"ok": False, "error": "Couldn't save the backup count."}

    # --- backup restore ---------------------------------------------------------
    def _validate_backup_filename(self, filename):
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

    @staticmethod
    def _open_backup_readonly(full_path):
        """Open a backup file read-only and sanity-check it before any use.
        Never raises; returns (connection, None) or (None, error_message)."""
        error = restore._check_backup(full_path)
        if error:
            return None, error
        try:
            return db._readonly_connection(full_path), None
        except Exception:
            return None, "That backup file is corrupt or unreadable."

    def _diff_against_live(self, backup_conn):
        """Per-account comparison of a backup against the live database.
        Accounts are matched by id (both tables are AUTOINCREMENT, so ids
        are never reused); transactions are matched by id and their editable
        contents are compared without treating an edit as a deletion."""
        account_columns = (
            "id, name, starting_balance_cents, starting_date, "
            "starting_balance_prev_cents, starting_balance_changed_at"
        )
        live_accounts = {
            row["id"]: row for row in self._conn.execute(f"SELECT {account_columns} FROM accounts")
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
                    "count": len(transactions_by_id(self._conn, account_id)),
                })
            elif in_backup and not in_live:
                results.append({
                    "id": account_id, "name": name, "kind": "only_backup",
                    "count": len(transactions_by_id(backup_conn, account_id)),
                })
            else:
                live_transactions = transactions_by_id(self._conn, account_id)
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

    def _categories_match_live(self, backup_conn):
        live_names = sorted(row["name"] for row in self._conn.execute("SELECT name FROM categories"))
        backup_names = sorted(row["name"] for row in backup_conn.execute("SELECT name FROM categories"))
        return live_names == backup_names

    def _autopays_match_live(self, backup_conn):
        columns = (
            "id, account_id, payee, category, notes, amount_cents, next_pay_date, "
            "next_post_date, pay_day, post_day, is_variable"
        )
        live_rows = {
            row["id"]: tuple(row[column] for column in row.keys() if column != "id")
            for row in self._conn.execute(f"SELECT {columns} FROM autopays")
        }
        backup_rows = {
            row["id"]: tuple(row[column] for column in row.keys() if column != "id")
            for row in backup_conn.execute(f"SELECT {columns} FROM autopays")
        }
        return live_rows == backup_rows

    def list_backups(self):
        """List backups in the effective backup folder, newest first."""
        try:
            backups_dir, _ = backup.effective_backup_dir()
            try:
                names = os.listdir(backups_dir)
            except Exception:
                names = []
            items = []
            for name in names:
                if not backup._is_backup_filename(name):
                    continue
                full_path = os.path.join(backups_dir, name)
                items.append({
                    "filename": name,
                    "timestamp": backup._parse_backup_timestamp(name, full_path),
                    "is_prerestore": name.startswith("balancer_prerestore_"),
                    "size_bytes": os.path.getsize(full_path) if os.path.isfile(full_path) else 0,
                })
            items.sort(key=lambda it: (it["timestamp"], it["filename"]), reverse=True)
            return {"ok": True, "backups": items}
        except Exception as e:
            self.log(f"list_backups failed: {e}")
            return {"ok": False, "error": "Couldn't list backups."}

    @_database_call
    def preview_restore(self, filename):
        """Compare a backup file against the live database without changing
        anything, so the UI can show what a restore would do."""
        try:
            full_path, err = self._validate_backup_filename(filename)
            if err:
                return {"ok": False, "error": err}
            db_dir = os.path.dirname(os.path.abspath(self._db_path))
            staged_path, err, detail = restore._stage_backup(full_path, db_dir)
            if err:
                self.log(f"preview_restore staging failed: {detail}")
                return {"ok": False, "error": err}
            try:
                backup_conn = db._readonly_connection(staged_path)
                try:
                    accounts_diff = self._diff_against_live(backup_conn)
                    categories = "same" if self._categories_match_live(backup_conn) else "differ"
                    autopays = "same" if self._autopays_match_live(backup_conn) else "differ"
                finally:
                    backup_conn.close()
            finally:
                cleanup_failures = db._remove_db_artifacts(staged_path)
                if cleanup_failures:
                    self.log("preview_restore couldn't clean up its staged backup")
            timestamp = backup._parse_backup_timestamp(os.path.basename(full_path), full_path)
            return {
                "ok": True, "timestamp": timestamp, "accounts": accounts_diff,
                "categories": categories, "autopays": autopays,
                "fingerprint": restore._file_sha256(full_path),
            }
        except Exception as e:
            self.log(f"preview_restore failed: {e}")
            return {"ok": False, "error": "Couldn't read that backup file."}

    @_database_call
    def restore_backup(self, filename, fingerprint=None):
        """Restore the live database from a backup file. Takes a pre-restore
        safety backup of the current live data first, so this can be undone."""
        try:
            full_path, err = self._validate_backup_filename(filename)
            if err:
                return {"ok": False, "error": err}
            if fingerprint is not None and restore._file_sha256(full_path) != fingerprint:
                message = "That backup file changed since you previewed it. Open it again to see what it would change."
                self.log("restore_backup refused because the backup changed since preview")
                return {"ok": False, "error": message}
            db_path = self._db_path
            backups_dir, _ = backup.effective_backup_dir()
            db_dir = os.path.dirname(os.path.abspath(db_path))
            staged_path, err, detail = restore._stage_backup(full_path, db_dir)
            if err:
                self.log(f"restore_backup staging failed: {detail}")
                return {"ok": False, "error": err}

            prerestore_path, prune_failed = backup._make_prerestore_backup(
                db_path, backups_dir, self._conn
            )
            if prerestore_path is None:
                cleanup_failures = db._remove_db_artifacts(staged_path)
                self.log("restore_backup cancelled because its safety backup failed")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up its staged backup")
                return {"ok": False, "error": "Couldn't take a safety backup, so the restore was cancelled."}

            rollback_path, err = restore._make_rollback_copy(self._conn, db_dir)
            if err:
                cleanup_failures = db._remove_db_artifacts(staged_path)
                self.log(f"restore_backup rollback snapshot failed: {err}")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up its staged backup")
                return {"ok": False, "error": "Couldn't prepare a local rollback copy, so the restore was cancelled."}

            if not self.close_conn():
                cleanup_failures = db._remove_db_artifacts(staged_path)
                cleanup_failures += db._remove_db_artifacts(rollback_path)
                self.log("restore_backup cancelled because the live database did not close")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up temporary restore files")
                return {"ok": False, "error": "Couldn't close the live database, so the restore was cancelled."}

            if os.path.exists(db_path + "-journal"):
                try:
                    self.set_conn(db.open_db(db_path))
                except Exception as e:
                    self._conn = None
                    self.log(f"restore_backup couldn't reopen the live database after finding a journal: {e}")
                cleanup_failures = db._remove_db_artifacts(staged_path)
                cleanup_failures += db._remove_db_artifacts(rollback_path)
                self.log("restore_backup refused to replace a database with a journal beside it")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up temporary restore files")
                return {"ok": False, "error": "The live database has an unfinished journal, so the restore was cancelled."}

            try:
                os.replace(staged_path, db_path)
            except Exception as e:
                try:
                    self.set_conn(db.open_db(db_path))
                except Exception as reopen_error:
                    self._conn = None
                    self.log(f"restore_backup couldn't reopen unchanged live database: {reopen_error}")
                cleanup_failures = db._remove_db_artifacts(staged_path)
                cleanup_failures += db._remove_db_artifacts(rollback_path)
                self.log(f"restore_backup replace failed; live database was not changed: {e}")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up temporary restore files")
                return {
                    "ok": False,
                    "error": "Couldn't put the backup into place. Nothing was changed.",
                }

            try:
                self.set_conn(db.open_db(db_path))
            except Exception as e:
                self._conn = None
                try:
                    os.replace(rollback_path, db_path)
                except Exception as rollback_error:
                    cleanup_failures = db._remove_db_artifacts(staged_path)
                    cleanup_failures += db._remove_db_artifacts(rollback_path)
                    self.log(
                        "restore_backup failed after replacement and couldn't put back "
                        f"the local rollback copy: {rollback_error}"
                    )
                    if cleanup_failures:
                        self.log("restore_backup couldn't clean up temporary restore files")
                    return {
                        "ok": False,
                        "error": (
                            "The restore failed and the app couldn't put your data back. "
                            f"Recover it manually from this safety backup: {prerestore_path}"
                        ),
                    }
                try:
                    self.set_conn(db.open_db(db_path))
                except Exception as reopen_error:
                    self._conn = None
                    self.log(f"restore_backup put data back but couldn't reopen it: {reopen_error}")
                cleanup_failures = db._remove_db_artifacts(staged_path)
                cleanup_failures += db._remove_db_artifacts(rollback_path)
                self.log(f"restore_backup failed after replacement; live data was put back: {e}")
                if cleanup_failures:
                    self.log("restore_backup couldn't clean up temporary restore files")
                return {"ok": False, "error": "The restore failed and your data was put back."}

            rollback_cleanup_failed = bool(db._remove_db_artifacts(rollback_path))
            if rollback_cleanup_failed:
                self.log("restore_backup completed but couldn't clean up its local rollback copy")
            self.log(
                f"Restored from backup {os.path.basename(full_path)} "
                f"(safety backup: {os.path.basename(prerestore_path)})"
            )
            result = self.get_config()
            warnings = []
            if prune_failed:
                self.log(f"Pre-restore prune couldn't delete {len(prune_failed)} file(s) in the backup folder")
                warnings.append(utils._prune_failed_message(len(prune_failed)))
            if rollback_cleanup_failed:
                warnings.append("Restored, but a temporary copy in the data folder couldn't be deleted.")
            if warnings:
                result["warning"] = " ".join(warnings)
            return result
        except Exception as e:
            self.log(f"restore_backup failed: {e}")
            return {"ok": False, "error": "Couldn't restore that backup."}

    # --- misc bridge helpers --------------------------------------------------
    def open_url(self, url: str):
        return updates.open_url(self, url)

    def check_update(self):
        return updates.check_update(self)

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        return updates._is_newer(latest, current)

    # --- debug log --------------------------------------------------------------
    def set_debug(self, on: bool):
        return debug.set_debug(self, on)

    def log(self, msg: str):
        return debug.log(self, msg)

    def _on_debug_log_warning(self, message: str):
        return debug._on_debug_log_warning(self, message)


