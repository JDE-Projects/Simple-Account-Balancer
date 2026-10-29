"""pywebview bridge."""

import sqlite3
import threading
import secrets
from functools import wraps

from app import db, paths, utils
from app.debug_log import DebugLog
from app.services.debug import log as _write_debug_log
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
                self._log(f"{method.__name__} refused after database rollback failure")
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
                            self._log(f"{method.__name__} left uncommitted work; rolled back")
                    except Exception as e:
                        self._log(f"{method.__name__} could not roll back database work: {utils.describe_error(e)}")
                        try:
                            if self._conn is not None:
                                self._conn.close()
                        except Exception as close_error:
                            self._log(f"{method.__name__} could not close the database: {utils.describe_error(close_error)}")
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
        self._restore_import = None
        self._debug_log = DebugLog(
            paths.app_dir(), "Simple Account Balancer",
            redact=utils.redact_log_text, on_warning=self._on_debug_log_warning,
        )
        self._backup_notice = None
        self._autopay_notice = None
        self._autopay_notice_is_error = False

    def set_window(self, w):
        self._window = w

    @_database_call
    def set_conn(self, conn: sqlite3.Connection):
        self._conn = conn

    def set_db_path(self, path: str):
        self._db_path = path

    # Underscore-prefixed so pywebview does not expose them to the page.
    def _add_backup_notice(self, message: str):
        """Join a message onto the notice shown on startup."""
        self._backup_notice = f"{self._backup_notice} {message}" if self._backup_notice else message

    def _set_autopay_notice(self, message, is_error: bool = False):
        self._autopay_notice = message
        self._autopay_notice_is_error = is_error

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
            self._log(f"close_conn failed: {utils.describe_error(e)}")
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

    @_database_call
    def _snapshot_for_backup_copy(self):
        return backup._snapshot_for_backup_copy(self)

    def save_backup_copy(self):
        return backup.save_backup_copy(self)

    # --- preferences (local file, not stored in the db) ----------------------
    def _load_theme(self) -> str:
        return settings._load_theme(self)

    def save_theme(self, theme: str):
        return settings.save_theme(self, theme)

    def choose_backup_folder(self):
        return backup.choose_backup_folder(self)

    def reset_backup_folder(self):
        return backup.reset_backup_folder(self)

    def set_backup_keep(self, n):
        return backup.set_backup_keep(self, n)

    # --- backup restore ---------------------------------------------------------
    def _validate_backup_filename(self, filename):
        return restore._validate_backup_filename(self, filename)

    @staticmethod
    def _open_backup_readonly(full_path):
        return restore._open_backup_readonly(full_path)

    def _diff_against_live(self, backup_conn):
        return restore._diff_against_live(self, backup_conn)

    def _categories_match_live(self, backup_conn):
        return restore._categories_match_live(self, backup_conn)

    def _autopays_match_live(self, backup_conn):
        return restore._autopays_match_live(self, backup_conn)

    def list_backups(self):
        return backup.list_backups(self)

    @_database_call
    def preview_restore(self, filename):
        return restore.preview_restore(self, filename)

    @_database_call
    def restore_backup(self, filename, fingerprint=None):
        return restore.restore_backup(self, filename, fingerprint)

    def choose_restore_file(self):
        return restore.choose_restore_file(self)

    def _clear_restore_import(self):
        """Forget the held import after trying to delete its local copy."""
        held = self._restore_import
        self._restore_import = None
        if held is None:
            return []
        return db._remove_db_artifacts(held["path"])

    @_database_call
    def _publish_restore_import(self, held_path, source_name, timestamp):
        """Publish and preview a copied restore file while holding the database lock."""
        cleanup_failures = self._clear_restore_import()
        if cleanup_failures:
            self._log("restore import couldn't discard an earlier temporary copy")
        held = {
            "token": secrets.token_hex(16),
            "path": held_path,
            "source_name": source_name,
            "timestamp": timestamp,
            "fingerprint": None,
        }
        self._restore_import = held
        preview = restore._preview_restore_path(self, held_path, timestamp)
        if not preview["ok"]:
            cleanup_failures = self._clear_restore_import()
            if cleanup_failures:
                self._log("restore import couldn't clean up a failed temporary copy")
            return preview
        held["fingerprint"] = preview["fingerprint"]
        preview["token"] = held["token"]
        preview["source_name"] = source_name
        return preview

    @_database_call
    def restore_from_file(self, token, fingerprint):
        held = self._restore_import
        if (
            held is None
            or token != held["token"]
            or not isinstance(fingerprint, str)
            or not fingerprint
            or fingerprint != held["fingerprint"]
        ):
            return {"ok": False, "error": "That file selection expired. Choose the file again."}
        try:
            result = restore._restore_from_path(self, held["path"], fingerprint)
        finally:
            cleanup_failures = self._clear_restore_import()
        if cleanup_failures:
            self._log("restore import couldn't clean up its temporary copy")
            message = "A temporary copy in the data folder couldn't be deleted."
            if result["ok"]:
                result["warning"] = " ".join(filter(None, (result.get("warning"), message)))
            else:
                result["error"] = f"{result['error']} {message}"
        if not result["ok"]:
            result["selection_cleared"] = True
        return result

    @_database_call
    def discard_restore_file(self, token):
        held = self._restore_import
        if held is None or token != held["token"]:
            return {"ok": True}
        cleanup_failures = self._clear_restore_import()
        if cleanup_failures:
            self._log("restore import couldn't discard its temporary copy")
            return {
                "ok": True,
                "warning": "A temporary copy in the data folder couldn't be deleted.",
            }
        return {"ok": True}

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

    def _log(self, msg: str):
        return _write_debug_log(self, msg)

    def _on_debug_log_warning(self, message: str):
        return debug._on_debug_log_warning(self, message)


