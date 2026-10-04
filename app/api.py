"""pywebview bridge."""

import datetime
import itertools
import json
import os
import sqlite3
import threading
import urllib.request
from functools import wraps

import webview

from app import config, db, paths, prefs, utils
from app.debug_log import DebugLog
from app.services import autopays, backup, export, restore, updates

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
        cur = self._conn.cursor()
        if account_id is None:
            return cur.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
        return cur.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def _current_balance_cents(self, account) -> int:
        cur = self._conn.cursor()
        total = cur.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id=?",
            (account["id"],),
        ).fetchone()[0]
        return account["starting_balance_cents"] + total

    def _today_balance_cents(self, account) -> int:
        """Balance counting only transactions dated today or earlier, unlike
        current_balance_cents which counts the full register including any
        future-dated rows."""
        cur = self._conn.cursor()
        today = datetime.date.today().isoformat()
        total = cur.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id=? AND date<=?",
            (account["id"], today),
        ).fetchone()[0]
        return account["starting_balance_cents"] + total

    def _account_payload(self, account):
        tx_count = self._conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE account_id=?", (account["id"],)
        ).fetchone()[0]
        return {
            "id": account["id"],
            "name": account["name"],
            "starting_balance_cents": account["starting_balance_cents"],
            "starting_date": account["starting_date"],
            "current_balance_cents": self._current_balance_cents(account),
            "today_balance_cents": self._today_balance_cents(account),
            "transaction_count": tx_count,
            "starting_balance_prev_cents": account["starting_balance_prev_cents"],
            "starting_balance_changed_at": account["starting_balance_changed_at"],
        }

    @_database_call
    def get_config(self):
        """Initial payload the UI loads on startup."""
        try:
            cur = self._conn.cursor()
            account_rows = cur.execute(
                "SELECT id, name FROM accounts ORDER BY name COLLATE NOCASE"
            ).fetchall()
            accounts = [{"id": r["id"], "name": r["name"]} for r in account_rows]

            prefs_data = prefs.load_prefs()
            active_id = prefs_data.get("active_account_id")
            account = None
            if accounts:
                if active_id is not None:
                    account = self._get_account(active_id)
                if account is None:
                    account = self._get_account(accounts[0]["id"])

            backup_dir, backup_is_custom = backup.effective_backup_dir()

            return {
                "ok": True,
                "version": self._version,
                "theme": self._load_theme(),
                "has_account": account is not None,
                "account": self._account_payload(account) if account is not None else None,
                "accounts": accounts,
                "backup_folder": backup_dir,
                "backup_folder_is_custom": backup_is_custom,
                "backup_keep": backup._clamp_backup_keep(prefs_data.get("backup_keep")),
                "backup_notice": self.backup_notice,
                "autopay_notice": self.autopay_notice,
                "autopay_notice_is_error": self.autopay_notice_is_error,
            }
        except Exception as e:
            self.log(f"get_config failed: {e}")
            return {"ok": False, "error": "Couldn't load the app's configuration."}

    @_database_call
    def create_account(self, name, starting_balance, starting_date):
        """Create an account. Used both for first-run setup and for 'Add
        account' once other accounts already exist. The new account becomes
        the active one."""
        try:
            name_s = (name or "").strip() or "Checking"
            cents, err = utils.parse_amount_to_cents(starting_balance, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            date_s, err = utils.parse_iso_date(starting_date)
            if err:
                return {"ok": False, "error": err}
            now = datetime.datetime.now().isoformat(timespec="seconds")
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name_s, cents, date_s, now),
            )
            new_id = cur.lastrowid
            self._conn.commit()
            prefs_data = prefs.load_prefs()
            prefs_data["active_account_id"] = new_id
            if not prefs.save_prefs(prefs_data):
                self.log("Could not save active account pref")
            self.log(f"Account {new_id} created, starting as of {date_s}")
            return self.get_config()
        except Exception as e:
            self.log(f"create_account failed: {e}")
            return {"ok": False, "error": "Couldn't create the account."}

    @_database_call
    def set_active_account(self, account_id):
        """Switch which account the UI shows and operates on."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "That account no longer exists."}
            prefs_data = prefs.load_prefs()
            prefs_data["active_account_id"] = account["id"]
            if not prefs.save_prefs(prefs_data):
                self.log("Could not save active account pref")
            self.log(f"Active account set to {account['id']}")
            return self.get_config()
        except Exception as e:
            self.log(f"set_active_account failed: {e}")
            return {"ok": False, "error": "Couldn't switch accounts."}

    @_database_call
    def delete_account(self, account_id):
        """Delete an account and its transactions and autopays. Refuses to
        delete the only account. If the deleted account was active, the
        active pref moves to the first remaining account."""
        try:
            cur = self._conn.cursor()
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "That account no longer exists."}
            total_accounts = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if total_accounts <= 1:
                return {"ok": False, "error": "Can't delete the only account."}
            tx_count = cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id=?", (account["id"],)
            ).fetchone()[0]
            cur.execute("DELETE FROM transactions WHERE account_id=?", (account["id"],))
            cur.execute("DELETE FROM autopays WHERE account_id=?", (account["id"],))
            cur.execute("DELETE FROM accounts WHERE id=?", (account["id"],))
            self._conn.commit()
            prefs_data = prefs.load_prefs()
            if prefs_data.get("active_account_id") == account["id"]:
                remaining = cur.execute(
                    "SELECT id FROM accounts ORDER BY name COLLATE NOCASE LIMIT 1"
                ).fetchone()
                prefs_data["active_account_id"] = remaining["id"] if remaining else None
                if not prefs.save_prefs(prefs_data):
                    self.log("Could not save active account pref")
            self.log(f"Account {account['id']} deleted ({tx_count} transactions removed)")
            return self.get_config()
        except Exception as e:
            self.log(f"delete_account failed: {e}")
            return {"ok": False, "error": "Couldn't delete the account."}

    @_database_call
    def update_account(self, account_id, name, starting_balance, starting_date):
        """Edit account name / starting balance / starting date. Recalcs the register."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "That account no longer exists."}
            name_s = (name or "").strip()
            if not name_s:
                return {"ok": False, "error": "Account name is required."}
            cents, err = utils.parse_amount_to_cents(starting_balance, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            date_s, err = utils.parse_iso_date(starting_date)
            if err:
                return {"ok": False, "error": err}
            cur = self._conn.cursor()
            if cents != account["starting_balance_cents"]:
                # Remember the last starting-balance change so account settings
                # can show a "last changed ... from X to Y" note.
                now = datetime.datetime.now().isoformat(timespec="seconds")
                cur.execute(
                    "UPDATE accounts SET name=?, starting_balance_cents=?, starting_date=?, "
                    "starting_balance_prev_cents=?, starting_balance_changed_at=? WHERE id=?",
                    (name_s, cents, date_s, account["starting_balance_cents"], now, account["id"]),
                )
            else:
                cur.execute(
                    "UPDATE accounts SET name=?, starting_balance_cents=?, starting_date=? WHERE id=?",
                    (name_s, cents, date_s, account["id"]),
                )
            self._conn.commit()
            self.log(f"Account {account['id']} updated, starting as of {date_s}")
            return self.get_config()
        except Exception as e:
            self.log(f"update_account failed: {e}")
            return {"ok": False, "error": "Couldn't update the account."}

    # --- categories -------------------------------------------------------------
    @_database_call
    def get_categories(self):
        """Name-sorted category list with usage counts (case-insensitive)."""
        try:
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT c.id, c.name, "
                "(SELECT COUNT(*) FROM transactions t WHERE t.category = c.name COLLATE NOCASE) AS used_count, "
                "(SELECT COUNT(*) FROM autopays a WHERE a.category = c.name COLLATE NOCASE) AS autopay_count "
                "FROM categories c ORDER BY c.name COLLATE NOCASE"
            ).fetchall()
            categories = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "used_count": r["used_count"],
                    "autopay_count": r["autopay_count"],
                }
                for r in rows
            ]
            return {"ok": True, "categories": categories}
        except Exception as e:
            self.log(f"get_categories failed: {e}")
            return {"ok": False, "error": "Couldn't load the categories."}

    @_database_call
    def add_category(self, name):
        try:
            name_s = (name or "").strip()
            if not name_s:
                return {"ok": False, "error": "Category name is required."}
            cur = self._conn.cursor()
            cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name_s,))
            self._conn.commit()
            self.log("Category added")
            return self.get_categories()
        except Exception as e:
            self.log(f"add_category failed: {e}")
            return {"ok": False, "error": "Couldn't add the category."}

    @_database_call
    def rename_category(self, category_id, new_name):
        """Rename a category and carry the change over to past transactions and autopays.
        If the new name collides with another existing category, the two are
        merged: transactions and autopays move to the existing category and this row goes away."""
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, name FROM categories WHERE id=?", (category_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That category no longer exists."}
            new_name_s = (new_name or "").strip()
            if not new_name_s:
                return {"ok": False, "error": "Category name is required."}
            old_name = row["name"]
            merge_target = cur.execute(
                "SELECT id, name FROM categories WHERE name=? COLLATE NOCASE AND id<>?",
                (new_name_s, category_id),
            ).fetchone()
            if merge_target is not None:
                cur.execute(
                    "UPDATE transactions SET category=? WHERE category=? COLLATE NOCASE",
                    (merge_target["name"], old_name),
                )
                cur.execute(
                    "UPDATE autopays SET category=? WHERE category=? COLLATE NOCASE",
                    (merge_target["name"], old_name),
                )
                cur.execute("DELETE FROM categories WHERE id=?", (category_id,))
                self._conn.commit()
                self.log(f"Category {category_id} merged into category {merge_target['id']}")
                return self.get_categories()
            cur.execute("UPDATE categories SET name=? WHERE id=?", (new_name_s, category_id))
            cur.execute(
                "UPDATE transactions SET category=? WHERE category=? COLLATE NOCASE",
                (new_name_s, old_name),
            )
            cur.execute(
                "UPDATE autopays SET category=? WHERE category=? COLLATE NOCASE",
                (new_name_s, old_name),
            )
            self._conn.commit()
            self.log(f"Category {category_id} renamed")
            return self.get_categories()
        except Exception as e:
            self.log(f"rename_category failed: {e}")
            return {"ok": False, "error": "Couldn't rename the category."}

    @_database_call
    def delete_category(self, category_id, reassign_to=None):
        """Delete a category. With reassign_to, transactions and autopays move
        to that category. Otherwise past transactions keep the label and
        autopays become uncategorized."""
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, name FROM categories WHERE id=?", (category_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That category no longer exists."}
            old_name = row["name"]
            reassign_s = (reassign_to or "").strip()
            if reassign_s:
                cur.execute(
                    "UPDATE transactions SET category=? WHERE category=? COLLATE NOCASE",
                    (reassign_s, old_name),
                )
                cur.execute(
                    "UPDATE autopays SET category=? WHERE category=? COLLATE NOCASE",
                    (reassign_s, old_name),
                )
            else:
                cur.execute(
                    "UPDATE autopays SET category='' WHERE category=? COLLATE NOCASE",
                    (old_name,),
                )
            cur.execute("DELETE FROM categories WHERE id=?", (category_id,))
            self._conn.commit()
            detail = " (transactions reassigned)" if reassign_s else ""
            self.log(f"Category {category_id} deleted{detail}")
            return self.get_categories()
        except Exception as e:
            self.log(f"delete_category failed: {e}")
            return {"ok": False, "error": "Couldn't delete the category."}

    # --- transactions ---------------------------------------------------------
    @_database_call
    def get_payees(self, account_id=None):
        """Distinct payees for the account, most-recent-first, each carrying
        the category from its most recent transaction (max date, then max id)."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT payee, category FROM transactions WHERE account_id=? "
                "ORDER BY date DESC, sort_key DESC, id DESC",
                (account["id"],),
            ).fetchall()
            seen = set()
            payees = []
            for r in rows:
                key = r["payee"].strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                payees.append({"payee": r["payee"], "last_category": r["category"]})
            return {"ok": True, "payees": payees}
        except Exception as e:
            self.log(f"get_payees failed: {e}")
            return {"ok": False, "error": "Couldn't load the payees."}

    def _rows_with_balance(self, account) -> list:
        """Full-history transaction rows, oldest first, each carrying the
        true rolling register balance. Shared by get_transactions (which
        filters to a visible range) and export_csv (which does the same)."""
        cur = self._conn.cursor()
        all_rows = cur.execute(
            "SELECT id, date, payee, category, notes, amount_cents, cleared, estimated "
            "FROM transactions WHERE account_id=? ORDER BY date ASC, sort_key ASC, id ASC",
            (account["id"],),
        ).fetchall()
        running = account["starting_balance_cents"]
        computed = []
        for r in all_rows:
            running += r["amount_cents"]
            computed.append(
                {
                    "id": r["id"],
                    "date": r["date"],
                    "payee": r["payee"],
                    "category": r["category"],
                    "notes": r["notes"],
                    "amount_cents": r["amount_cents"],
                    "cleared": bool(r["cleared"]),
                    "estimated": bool(r["estimated"]),
                    "balance_cents": running,
                }
            )
        return computed

    @_database_call
    def get_transactions(self, account_id=None, from_date=None, to_date=None, search=""):
        """Rows for the given date range and search text, with balances computed
        over the FULL history so the first visible row's balance is correct.
        The balance column always reflects the true register, never a filtered
        sum. With no from_date, falls back to the last 30 days."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            computed = self._rows_with_balance(account)
            current_balance_cents = computed[-1]["balance_cents"] if computed else account["starting_balance_cents"]

            from_s = (from_date or "").strip()
            if not from_s:
                from_s = (datetime.date.today() - datetime.timedelta(days=config.DEFAULT_RANGE_DAYS)).isoformat()
            to_s = (to_date or "").strip()

            visible = [row for row in computed if row["date"] >= from_s]
            if to_s:
                visible = [row for row in visible if row["date"] <= to_s]

            search_s = (search or "").strip().lower()
            if search_s:
                visible = [
                    row
                    for row in visible
                    if search_s in (row["payee"] or "").lower()
                    or search_s in (row["category"] or "").lower()
                    or search_s in (row["notes"] or "").lower()
                ]

            today_s = datetime.date.today().isoformat()
            today_balance_cents = account["starting_balance_cents"] + sum(
                row["amount_cents"] for row in computed if row["date"] <= today_s
            )
            # Derived, never stored: how many estimated postings have come due
            # so far, across the full account history (not just the visible
            # range), so the notice can never drift from the register itself.
            estimated_due_count = sum(
                1 for row in computed if row["estimated"] and row["date"] <= today_s
            )

            return {
                "ok": True,
                "rows": visible,
                "current_balance_cents": current_balance_cents,
                "today_balance_cents": today_balance_cents,
                "transaction_count": len(computed),
                "estimated_due_count": estimated_due_count,
            }
        except Exception as e:
            self.log(f"get_transactions failed: {e}")
            return {"ok": False, "error": "Couldn't load the transactions."}

    @_database_call
    def add_transaction(self, account_id, date, payee, category, notes, amount, direction):
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            date_s, err = utils.parse_iso_date(date)
            if err:
                return {"ok": False, "error": err}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            signed = -cents if direction == "withdraw" else cents
            category_s = (category or "").strip()
            notes_s = (notes or "").strip()
            now = datetime.datetime.now().isoformat(timespec="seconds")
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO transactions "
                "(account_id, date, payee, category, notes, amount_cents, cleared, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (account["id"], date_s, payee_s, category_s, notes_s, signed, now),
            )
            cur.execute(
                "UPDATE transactions SET sort_key=? WHERE id=?",
                (cur.lastrowid, cur.lastrowid),
            )
            if category_s:
                cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_s,))
            self._conn.commit()
            self.log(f"Added transaction dated {date_s}")
            return {"ok": True}
        except Exception as e:
            self.log(f"add_transaction failed: {e}")
            return {"ok": False, "error": "Couldn't add the transaction."}

    @_database_call
    def update_transaction(self, transaction_id, date, payee, category, notes, amount, direction):
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, date FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That transaction no longer exists."}
            date_s, err = utils.parse_iso_date(date)
            if err:
                return {"ok": False, "error": err}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            signed = -cents if direction == "withdraw" else cents
            category_s = (category or "").strip()
            notes_s = (notes or "").strip()
            # A date change moves the transaction to a different day, so its
            # sort_key is reset to its id, landing it by the old insertion-order
            # rule in the target day rather than carrying a stale position.
            # Saving the full editor counts as reviewing the posting, so any
            # estimated flag from a variable autopay is cleared here. Editing
            # without changing the amount still clears it, otherwise a user who
            # agrees with the estimate would have no way to dismiss the flag.
            if date_s != row["date"]:
                cur.execute(
                    "UPDATE transactions SET date=?, payee=?, category=?, notes=?, amount_cents=?, "
                    "estimated=0, sort_key=id WHERE id=?",
                    (date_s, payee_s, category_s, notes_s, signed, transaction_id),
                )
            else:
                cur.execute(
                    "UPDATE transactions SET date=?, payee=?, category=?, notes=?, amount_cents=?, "
                    "estimated=0 WHERE id=?",
                    (date_s, payee_s, category_s, notes_s, signed, transaction_id),
                )
            if category_s:
                cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_s,))
            self._conn.commit()
            self.log(f"Updated transaction {transaction_id}, dated {date_s}")
            return {"ok": True}
        except Exception as e:
            self.log(f"update_transaction failed: {e}")
            return {"ok": False, "error": "Couldn't update the transaction."}

    @_database_call
    def delete_transaction(self, transaction_id):
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That transaction no longer exists."}
            cur.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
            self._conn.commit()
            self.log(f"Deleted transaction {transaction_id}")
            return {"ok": True}
        except Exception as e:
            self.log(f"delete_transaction failed: {e}")
            return {"ok": False, "error": "Couldn't delete the transaction."}

    @_database_call
    def reorder_transactions(self, account_id, date, ordered_ids):
        """Set the within-day display order for one date's transactions. The
        caller supplies the full set of that day's ids in the new order;
        sort_key values 1..n never collide across days since date sorts
        first, so this never needs to touch any other day's rows."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            date_s, err = utils.parse_iso_date(date)
            if err:
                return {"ok": False, "error": err}
            cur = self._conn.cursor()
            day_rows = cur.execute(
                "SELECT id FROM transactions WHERE account_id=? AND date=? "
                "ORDER BY sort_key ASC, id ASC",
                (account["id"], date_s),
            ).fetchall()
            day_ids = {r["id"] for r in day_rows}
            ordered = list(ordered_ids or [])
            if len(ordered) != len(set(ordered)) or set(ordered) != day_ids:
                return {
                    "ok": False,
                    "error": "That day's transactions changed. Close and reopen the reorder window.",
                }
            for position, transaction_id in enumerate(ordered, start=1):
                cur.execute(
                    "UPDATE transactions SET sort_key=? WHERE id=?",
                    (position, transaction_id),
                )
            self._conn.commit()
            self.log(f"Reordered {len(ordered)} transaction(s) on {date_s}")
            return {"ok": True}
        except Exception as e:
            self.log(f"reorder_transactions failed: {e}")
            return {"ok": False, "error": "Couldn't reorder the transactions."}

    @_database_call
    def confirm_estimated_amount(self, transaction_id, amount):
        """Correct an estimated autopay posting's amount. Only the amount and
        the estimated flag change: the row's existing sign (withdraw stays
        negative, deposit stays positive) is preserved, and cleared status,
        the rule, and every other field are left untouched."""
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That transaction no longer exists."}
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            signed = -cents if row["amount_cents"] < 0 else cents
            cur.execute(
                "UPDATE transactions SET amount_cents=?, estimated=0 WHERE id=?",
                (signed, transaction_id),
            )
            self._conn.commit()
            self.log(f"Confirmed estimated amount for transaction {transaction_id}")
            return {"ok": True}
        except Exception as e:
            self.log(f"confirm_estimated_amount failed: {e}")
            return {"ok": False, "error": "Couldn't confirm the amount."}

    # --- autopays ---------------------------------------------------------------
    @_database_call
    def get_autopays(self, account_id):
        """Autopay rules for the account, ordered by their post-day anchor
        then payee, so the list reads roughly in the order rules land in
        the register each month."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT id, payee, category, notes, amount_cents, next_post_date, "
                "next_pay_date, post_day, pay_day, is_variable FROM autopays WHERE account_id=? "
                "ORDER BY post_day, payee COLLATE NOCASE",
                (account["id"],),
            ).fetchall()
            autopays = [
                {
                    "id": r["id"],
                    "payee": r["payee"],
                    "category": r["category"],
                    "notes": r["notes"],
                    "amount_cents": r["amount_cents"],
                    "next_post_date": r["next_post_date"],
                    "next_pay_date": r["next_pay_date"],
                    "post_day": r["post_day"],
                    "pay_day": r["pay_day"],
                    "is_variable": bool(r["is_variable"]),
                }
                for r in rows
            ]
            return {"ok": True, "autopays": autopays}
        except Exception as e:
            self.log(f"get_autopays failed: {e}")
            return {"ok": False, "error": "Couldn't load the autopays."}

    @_database_call
    def add_autopay(self, account_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
        """Create a recurring autopay rule. post_date and pay_date are the
        first occurrence; their day numbers become the hidden post_day and
        pay_day anchors used to advance the rule each month. is_variable
        marks a rule whose amount changes month to month (cell phone, car
        insurance): postings from it arrive flagged as an estimate to confirm."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            post_s, err = utils.parse_iso_date(post_date)
            if err:
                return {"ok": False, "error": err}
            pay_s, err = utils.parse_iso_date(pay_date)
            if err:
                return {"ok": False, "error": err}
            if post_s > pay_s:
                return {"ok": False, "error": "The register date must be on or before the pay date."}
            signed = -cents if direction == "withdraw" else cents
            category_s = (category or "").strip()
            notes_s = (notes or "").strip()
            is_variable_i = 1 if is_variable else 0
            post_day = datetime.date.fromisoformat(post_s).day
            pay_day = datetime.date.fromisoformat(pay_s).day
            now = datetime.datetime.now().isoformat(timespec="seconds")
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO autopays "
                "(account_id, payee, category, notes, amount_cents, next_pay_date, "
                "next_post_date, pay_day, post_day, is_variable, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account["id"], payee_s, category_s, notes_s, signed, pay_s, post_s, pay_day, post_day, is_variable_i, now),
            )
            if category_s:
                cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_s,))
            self._conn.commit()
            self.log(f"Added autopay, next post {post_s}, next pay {pay_s}")
            posted = 0
            post_failed = False
            try:
                post_result = self.post_due_autopays()
                if post_result is None:
                    post_failed = True
                else:
                    posted = post_result
            except Exception as e:
                self.log(f"post_due_autopays call failed: {e}")
                post_failed = True
            # This posting pass is triggered from the UI, not launch, so don't
            # leave a stale launch notice for the next startup to pick up.
            self.autopay_notice = None
            self.autopay_notice_is_error = False
            result = self.get_autopays(account["id"])
            if result.get("ok"):
                result["posted"] = posted
                if post_failed:
                    result["post_failed"] = True
                    result["post_error"] = autopays._AUTOPAY_SAVED_POST_FAILED_MESSAGE
            return result
        except Exception as e:
            self.log(f"add_autopay failed: {e}")
            return {"ok": False, "error": "Couldn't add the autopay."}

    @_database_call
    def update_autopay(self, autopay_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
        """Edit an autopay rule. Re-anchoring post_day/pay_day from the newly
        chosen dates is the point of editing them, so both are recomputed.
        Toggling is_variable only affects postings this rule makes from now
        on; transactions it already posted keep whatever estimated flag they
        posted with."""
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, account_id FROM autopays WHERE id=?", (autopay_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That autopay no longer exists."}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            post_s, err = utils.parse_iso_date(post_date)
            if err:
                return {"ok": False, "error": err}
            pay_s, err = utils.parse_iso_date(pay_date)
            if err:
                return {"ok": False, "error": err}
            if post_s > pay_s:
                return {"ok": False, "error": "The register date must be on or before the pay date."}
            signed = -cents if direction == "withdraw" else cents
            category_s = (category or "").strip()
            notes_s = (notes or "").strip()
            is_variable_i = 1 if is_variable else 0
            post_day = datetime.date.fromisoformat(post_s).day
            pay_day = datetime.date.fromisoformat(pay_s).day
            cur.execute(
                "UPDATE autopays SET payee=?, category=?, notes=?, amount_cents=?, "
                "next_pay_date=?, next_post_date=?, pay_day=?, post_day=?, is_variable=? WHERE id=?",
                (payee_s, category_s, notes_s, signed, pay_s, post_s, pay_day, post_day, is_variable_i, autopay_id),
            )
            if category_s:
                cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_s,))
            self._conn.commit()
            self.log(f"Updated autopay {autopay_id}, next post {post_s}, next pay {pay_s}")
            posted = 0
            post_failed = False
            try:
                post_result = self.post_due_autopays()
                if post_result is None:
                    post_failed = True
                else:
                    posted = post_result
            except Exception as e:
                self.log(f"post_due_autopays call failed: {e}")
                post_failed = True
            # This posting pass is triggered from the UI, not launch, so don't
            # leave a stale launch notice for the next startup to pick up.
            self.autopay_notice = None
            self.autopay_notice_is_error = False
            result = self.get_autopays(row["account_id"])
            if result.get("ok"):
                result["posted"] = posted
                if post_failed:
                    result["post_failed"] = True
                    result["post_error"] = autopays._AUTOPAY_SAVED_POST_FAILED_MESSAGE
            return result
        except Exception as e:
            self.log(f"update_autopay failed: {e}")
            return {"ok": False, "error": "Couldn't update the autopay."}

    @_database_call
    def delete_autopay(self, autopay_id):
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, account_id FROM autopays WHERE id=?", (autopay_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That autopay no longer exists."}
            cur.execute("DELETE FROM autopays WHERE id=?", (autopay_id,))
            self._conn.commit()
            self.log(f"Deleted autopay {autopay_id}")
            return self.get_autopays(row["account_id"])
        except Exception as e:
            self.log(f"delete_autopay failed: {e}")
            return {"ok": False, "error": "Couldn't delete the autopay."}

    @_database_call
    def post_due_autopays(self):
        """Called from main() at launch, not from the UI. Posts a real
        uncleared transaction for every autopay rule whose next_post_date has
        arrived, then advances that rule's dates. All inserts and date
        advances for every rule commit together in one transaction at the
        end, so a crash partway through can never leave a posted transaction
        whose rule didn't also advance (that would double-post next launch).
        Returns the number of transactions posted; doesn't return an {"ok"}
        dict since it isn't a UI-facing bridge method."""
        today = datetime.date.today().isoformat()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        posted_count = 0
        try:
            cur = self._conn.cursor()
            rules = cur.execute(
                "SELECT id, account_id, payee, category, notes, amount_cents, "
                "next_pay_date, next_post_date, pay_day, post_day, is_variable FROM autopays"
            ).fetchall()
            for rule in rules:
                next_pay_date = rule["next_pay_date"]
                next_post_date = rule["next_post_date"]
                iterations = 0
                while next_post_date <= today:
                    iterations += 1
                    if iterations > 120:
                        self.log(
                            f"post_due_autopays: autopay {rule['id']} "
                            f"hit the 120-iteration safety cap; stopping this rule for now."
                        )
                        break
                    cur.execute(
                        "INSERT INTO transactions "
                        "(account_id, date, payee, category, notes, amount_cents, cleared, estimated, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (
                            rule["account_id"], next_pay_date, rule["payee"], rule["category"],
                            rule["notes"], rule["amount_cents"], 1 if rule["is_variable"] else 0, now,
                        ),
                    )
                    cur.execute(
                        "UPDATE transactions SET sort_key=? WHERE id=?",
                        (cur.lastrowid, cur.lastrowid),
                    )
                    posted_count += 1
                    next_pay_date = utils.advance_one_month(next_pay_date, rule["pay_day"])
                    next_post_date = utils.advance_one_month(next_post_date, rule["post_day"])
                cur.execute(
                    "UPDATE autopays SET next_pay_date=?, next_post_date=? WHERE id=?",
                    (next_pay_date, next_post_date, rule["id"]),
                )
            self._conn.commit()
            if posted_count == 1:
                self.autopay_notice = "Added 1 autopay to the register."
            elif posted_count > 1:
                self.autopay_notice = f"Added {posted_count} autopays to the register."
            self.autopay_notice_is_error = False
            self.log(f"post_due_autopays: posted {posted_count} transaction(s)")
            return posted_count
        except Exception as e:
            self._conn.rollback()
            self.log(f"post_due_autopays failed: {e}")
            self.autopay_notice = autopays._AUTOPAY_POST_FAILED_MESSAGE
            self.autopay_notice_is_error = True
            return None

    # --- compare ---------------------------------------------------------------
    @_database_call
    def get_compare_data(self, account_id=None, from_date=None, to_date=None):
        """Return read-only Compare rows and the full register balance.

        Rows are newest first and default to the last 90 days. The legacy
        ``cleared`` database column deliberately remains out of this API: found
        marks are maintained by the browser for the current app session only.
        """
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            cur = self._conn.cursor()
            from_s = (from_date or "").strip()
            if not from_s:
                from_s = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
            to_s = (to_date or "").strip()

            query = (
                "SELECT id, date, payee, category, notes, amount_cents "
                "FROM transactions WHERE account_id=? AND date>=?"
            )
            params = [account["id"], from_s]
            if to_s:
                query += " AND date<=?"
                params.append(to_s)
            query += " ORDER BY date DESC, sort_key DESC, id DESC"
            rows = cur.execute(query, params).fetchall()
            payload_rows = [
                {
                    "id": r["id"],
                    "date": r["date"],
                    "payee": r["payee"],
                    "category": r["category"],
                    "notes": r["notes"],
                    "amount_cents": r["amount_cents"],
                }
                for r in rows
            ]
            return {
                "ok": True,
                "rows": payload_rows,
                "register_balance_cents": self._current_balance_cents(account),
            }
        except Exception as e:
            self.log(f"get_compare_data failed: {e}")
            return {"ok": False, "error": "Couldn't load the compare data."}

    @_database_call
    def find_compare_matches(self, account_id=None, amount=None, from_date=None, to_date=None):
        """Look for likely causes of a Compare difference: a single transaction
        that matches it exactly, one that matches half of it (a wrong withdraw
        or deposit direction), a divisible-by-9 hint (a classic transposed-digit
        typo), and combinations of the visible-range transactions that add up
        to it. Found marks are browser-only, so all range rows are considered.
        """
        try:
            cents, err = utils.parse_amount_to_cents(amount, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            diff_cents = abs(cents)
            if diff_cents == 0:
                return {"ok": False, "error": "Enter the amount you are off by."}

            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}

            cur = self._conn.cursor()

            from_s = (from_date or "").strip()
            if not from_s:
                from_s = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
            to_s = (to_date or "").strip()

            def row_dict(r):
                return {
                    "id": r["id"],
                    "date": r["date"],
                    "payee": r["payee"],
                    "amount_cents": r["amount_cents"],
                }

            # 1. exact: full history, any transaction whose absolute amount matches.
            exact_rows = cur.execute(
                "SELECT id, date, payee, amount_cents FROM transactions "
                "WHERE account_id=? AND ABS(amount_cents)=? ORDER BY date ASC, sort_key ASC, id ASC",
                (account["id"], diff_cents),
            ).fetchall()
            exact = [row_dict(r) for r in exact_rows]

            # 2. half: only meaningful when the difference splits evenly into cents.
            half = []
            if diff_cents % 2 == 0:
                half_rows = cur.execute(
                    "SELECT id, date, payee, amount_cents FROM transactions "
                    "WHERE account_id=? AND ABS(amount_cents)=? ORDER BY date ASC, sort_key ASC, id ASC",
                    (account["id"], diff_cents // 2),
                ).fetchall()
                half = [row_dict(r) for r in half_rows]

            # 3. transposition hint: a swapped-digits typo always produces a
            # difference divisible by 9.
            transposition_hint = diff_cents % 9 == 0

            # 4. combinations: always searched, scoped to the visible date range,
            # same as the Compare table itself, not the full history. A
            # coincidental exact or half match should never hide a combination
            # the user actually needed.
            combinations = []
            combinations_skipped = False
            query = "SELECT id, date, payee, amount_cents FROM transactions WHERE account_id=? AND date>=?"
            params = [account["id"], from_s]
            if to_s:
                query += " AND date<=?"
                params.append(to_s)
            query += " ORDER BY date ASC, sort_key ASC, id ASC"
            range_rows = cur.execute(query, params).fetchall()

            if len(range_rows) > 300:
                combinations_skipped = True
            else:
                range_list = [row_dict(r) for r in range_rows]
                targets = (diff_cents, -diff_cents)
                allow_triples = len(range_list) <= 100
                seen_id_sets = set()

                def search_combos(rows, cap_remaining):
                    found_here = []
                    for pair in itertools.combinations(rows, 2):
                        ids = frozenset(r["id"] for r in pair)
                        if ids in seen_id_sets:
                            continue
                        if sum(r["amount_cents"] for r in pair) in targets:
                            found_here.append(list(pair))
                            seen_id_sets.add(ids)
                            if len(found_here) >= cap_remaining:
                                return found_here
                    if allow_triples:
                        for triple in itertools.combinations(rows, 3):
                            ids = frozenset(r["id"] for r in triple)
                            if ids in seen_id_sets:
                                continue
                            if sum(r["amount_cents"] for r in triple) in targets:
                                found_here.append(list(triple))
                                seen_id_sets.add(ids)
                                if len(found_here) >= cap_remaining:
                                    return found_here
                    return found_here

                combinations = search_combos(range_list, 10)

            self.log(
                f"find_compare_matches: exact={len(exact)} half={len(half)} transposition_hint={transposition_hint} "
                f"combinations={len(combinations)} combinations_skipped={combinations_skipped}"
            )
            return {
                "ok": True,
                "exact": exact,
                "half": half,
                "transposition_hint": transposition_hint,
                "combinations": combinations,
                "combinations_skipped": combinations_skipped,
            }
        except Exception as e:
            self.log(f"find_compare_matches failed: {e}")
            return {"ok": False, "error": "Couldn't search for the comparison."}

    # --- CSV export -------------------------------------------------------------
    @_database_call
    def _export_csv_snapshot(self, account_id, from_date, to_date, range_label):
        account = self._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        computed = self._rows_with_balance(account)

        from_s = (from_date or "").strip()
        to_s = (to_date or "").strip()
        all_history = not from_s and not to_s

        rows = computed
        if from_s:
            rows = [r for r in rows if r["date"] >= from_s]
        if to_s:
            rows = [r for r in rows if r["date"] <= to_s]

        expected_tokens = (
            "AllHistory",
            "Last7Days",
            "Last14Days",
            "Last30Days",
            "Last90Days",
            "CustomRange",
        )
        token = (range_label or "").strip()
        if token not in expected_tokens:
            token = "AllHistory" if all_history else "CustomRange"
        return {
            "ok": True,
            "account_name": account["name"],
            "rows": rows,
            "from_date": from_s,
            "to_date": to_s,
            "all_history": all_history,
            "default_name": utils.sanitize_filename(
                f"{account['name']}_Export_{token}_{datetime.date.today().strftime('%m%d%Y')}.csv"
            ),
        }

    def export_csv(self, account_id, from_date, to_date, range_label=""):
        """Export the register to a CSV file via a native save dialog. An
        empty from_date and to_date together mean all history (no default
        range applied here; that default is only for the register views).
        range_label is the preset token the UI picked (e.g. AllHistory,
        Last30Days, CustomRange); it only affects the default filename."""
        try:
            snapshot = self._export_csv_snapshot(account_id, from_date, to_date, range_label)
            if not snapshot["ok"]:
                return snapshot
            account_name = snapshot["account_name"]
            rows = snapshot["rows"]
            from_s = snapshot["from_date"]
            to_s = snapshot["to_date"]
            all_history = snapshot["all_history"]

            documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
            start_dir = documents_dir if os.path.isdir(documents_dir) else paths.app_dir()

            result = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=start_dir,
                save_filename=snapshot["default_name"],
                file_types=("CSV Files (*.csv)",),
            )
            if not result:
                return {"ok": True, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            if not path:
                return {"ok": True, "cancelled": True}
            if not path.lower().endswith(".csv"):
                path += ".csv"

            import csv

            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                range_text = "All history" if all_history else f"{from_s or 'start'} to {to_s or 'today'}"
                csv.writer(f).writerows(
                    export.csv_export_rows(account_name, range_text, datetime.date.today().isoformat(), rows)
                )

            self.log(f"Exported {len(rows)} transactions to CSV")
            return {"ok": True, "path": path, "count": len(rows)}
        except Exception as e:
            self.log(f"export_csv failed: {e}")
            return {"ok": False, "error": "Couldn't export the CSV file."}

    # --- preferences (local file, not stored in the db) ----------------------
    def _load_theme(self) -> str:
        theme = prefs.load_prefs().get("theme")
        return theme if theme in ("dark", "light") else "dark"

    def save_theme(self, theme: str):
        if theme not in ("dark", "light"):
            return {"ok": False}
        prefs_data = prefs.load_prefs()
        prefs_data["theme"] = theme
        if prefs.save_prefs(prefs_data):
            self.log(f"Theme set to {theme}")
            return {"ok": True}
        self.log("Could not save theme pref")
        return {
            "ok": False,
            "error": "Theme won't be remembered: couldn't save settings next to the app.",
        }

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
        """Open a link in the system browser, never by navigating the app window.
        Only the JDE-Projects website is allowed (see _is_allowed_url)."""
        import webbrowser

        if not updates._is_allowed_url(url):
            self.log("open_url refused an address outside the allowed site")
            return {"ok": False, "error": "That link isn't allowed."}
        webbrowser.open(url)
        return {"ok": True}

    def check_update(self):
        """Compare the latest published release to APP_VERSION. Quiet in the UI on
        failure (see _update_error_reason), but always logged when debug is on."""
        result = {"current": self._version, "version": None, "update": False, "offline": False}
        try:
            url = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            latest = (data.get("tag_name") or "").lstrip("v")
            result["version"] = latest
            if latest and self._is_newer(latest, self._version):
                result["update"] = True
            self.log(f"check_update: found v{latest}, current v{self._version}")
        except Exception as e:
            result["offline"] = True  # offline / private repo / rate-limited: stay quiet
            result["reason"] = updates._update_error_reason(e)
            self.log(f"check_update failed: {type(e).__name__}: {e}")
        return result

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        def parts(v):
            out = []
            for p in v.split("."):
                try:
                    out.append(int(p))
                except ValueError:
                    out.append(0)
            return out

        return parts(latest) > parts(current)

    # --- debug log --------------------------------------------------------------
    def set_debug(self, on: bool):
        """Turn the debug log on or off. "enabled" is the real state after
        the call, so the toggle can flip back if the log file couldn't be
        created; the reason arrives through onDebugLogWarning."""
        ok = self._debug_log.set_enabled(on)
        return {"ok": ok, "enabled": self._debug_log.is_enabled()}

    def log(self, msg: str):
        # Privacy rule for every call site: users share this log for bug
        # reports, so lines may carry ids, dates, and counts, never file
        # paths, payees, amounts, balances, or user-entered names. Every line
        # also passes through redact_log_text, which catches paths and quoted
        # values inside error messages. Size limits, old-log pruning, and
        # write failures are handled by app/debug_log.py.
        self._debug_log.log(msg)

    def _on_debug_log_warning(self, message: str):
        """Show a debug log problem (failed write, old log that couldn't be
        deleted) in the window. Before the window exists, at launch, it joins
        the backup notice shown on startup. Once the window is up it is
        pushed to the page from a separate thread, so a warning raised while
        the window's own thread is busy can never stall it."""
        window = self._window
        if window is None:
            self.backup_notice = f"{self.backup_notice} {message}" if self.backup_notice else message
            return
        script = (
            "window.onDebugLogWarning && window.onDebugLogWarning("
            f"{json.dumps(message)}, {json.dumps(self._debug_log.is_enabled())})"
        )

        def _push():
            try:
                window.evaluate_js(script)
            except Exception:
                pass

        threading.Thread(target=_push, daemon=True).start()


