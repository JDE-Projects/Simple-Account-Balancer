"""
Simple Account Balancer, a checkbook-register style balance tracker.

JDE-Projects "Simple X Tool": Python 3 + PySide6/pywebview, single-file UI.
The register is the source of truth for "how much money do I actually have."
All money is stored and computed as integer cents; never floats.

Features: a multi-account register with add/edit/delete and a rolling
balance that recalculates for out-of-order entry, a reconcile view with a
discrepancy finder, CSV export, an autopay catalog of recurring rules that
post real uncleared transactions at launch, and SQLite storage with rolling
backups (relocatable backup folder, in-app restore with a pre-restore
safety copy) plus a schema version guard against databases written by a
newer build.
"""
import calendar
import ctypes
import ctypes.wintypes as wintypes
import datetime
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import webview

APP_VERSION = "1.7.0"
GITHUB_OWNER = "JDE-Projects"
GITHUB_REPO = "Simple-Account-Balancer"

DB_FILENAME = "simple_account_balancer.db"
BACKUP_DIRNAME = "backups"
BACKUP_KEEP = 5
BACKUP_KEEP_MIN = 1
BACKUP_KEEP_MAX = 50
PRERESTORE_KEEP = 3
SCHEMA_VERSION = 3
DEFAULT_RANGE_DAYS = 30

# Enforced window minimum, read by create_window's min_size.
MIN_WINDOW_W = 680
MIN_WINDOW_H = 650

# Regular backups: balancer_YYYYMMDD_HHMMSS.db
# Pre-restore safety backups: balancer_prerestore_YYYYMMDD_HHMMSS.db
# The optional "prerestore_" is captured as part of the match but not its own
# group, since both variants share the same trailing date/time.
BACKUP_FILENAME_RE = re.compile(r"^balancer_(?:prerestore_)?(\d{8})_(\d{6})\.db$")

SEED_CATEGORIES = [
    "Auto", "Charity", "Dining", "Entertainment", "Fees", "Gas", "Gifts",
    "Groceries", "Healthcare", "Home", "Income", "Insurance", "Personal",
    "Rent/Mortgage", "Shopping", "Subscriptions", "Transfer", "Travel",
    "Utilities",
]


def resource_path(rel: str) -> str:
    """Path to a bundled resource, working both from source and PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def app_dir() -> str:
    """Folder the app lives in: next to the .exe when frozen, else the script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Money helpers. All amounts are integer cents in Python and SQLite. Never
# floats. Display formatting ("$1,234.56") happens in the UI, not here.
# ---------------------------------------------------------------------------
def parse_amount_to_cents(raw, *, allow_negative=False, allow_zero=False):
    """Parse a user-entered amount ('1,234.56', '$50', '12', '-40') to cents.

    Returns (cents, None) on success or (None, error_message) on failure.
    """
    if raw is None:
        return None, "Amount is required."
    s = str(raw).strip()
    if not s:
        return None, "Amount is required."
    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    s = s.replace("$", "").replace(",", "").strip()
    if not s:
        return None, "Amount is required."
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None, "Enter a valid amount."
    if neg:
        if not allow_negative:
            return None, "Enter a valid amount."
        value = -value
    cents = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if not allow_negative and cents < 0:
        return None, "Amount must be greater than zero."
    if not allow_zero and cents == 0:
        return None, "Amount must be greater than zero."
    return cents, None


def parse_iso_date(raw):
    """Validate a yyyy-mm-dd date string. Returns (date_str, None) or (None, error)."""
    s = (raw or "").strip()
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return None, "Enter a valid date."
    return s, None


def advance_one_month(iso_date: str, anchor_day: int) -> str:
    """Return iso_date advanced by one calendar month, re-anchored to
    anchor_day and clamped to that month's length so a day-31 anchor still
    lands somewhere sensible in short months, e.g. Jan 31 -> Feb 28 -> Mar 31,
    with no drift back toward the 28th. Handles the December -> January
    year rollover."""
    d = datetime.date.fromisoformat(iso_date)
    year = d.year
    month = d.month + 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(anchor_day, last_day)
    return datetime.date(year, month, day).isoformat()


def cents_to_decimal_str(cents: int) -> str:
    """Format integer cents as a plain unrounded decimal string for CSV export,
    e.g. -140 -> '-1.40', 500 -> '5.00'. Never uses float, so it never drifts."""
    neg = cents < 0
    cents = abs(cents)
    dollars, rem = divmod(cents, 100)
    s = f"{dollars}.{rem:02d}"
    return f"-{s}" if neg else s


_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_filename(name: str) -> str:
    """Strip characters that Windows doesn't allow in file names."""
    cleaned = "".join(c for c in name if c not in _INVALID_FILENAME_CHARS)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Preferences: a small local file next to the app, not stored in the db.
# Module-level so main() can read it before any Api/window exists (needed for
# the relocatable backup folder at startup).
# ---------------------------------------------------------------------------
def _pref_path() -> str:
    return os.path.join(app_dir(), "simple_account_balancer.pref")


def load_prefs() -> dict:
    """Load the full prefs dict. Tolerant of a missing or corrupt file."""
    try:
        with open(_pref_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(prefs: dict) -> bool:
    try:
        with open(_pref_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        return True
    except Exception:
        return False


# Save and restore the ABSOLUTE window frame rectangle via Win32, found by
# the window title. GetWindowRect (save) and SetWindowPos (restore) share
# one frame-based, physical-pixel coordinate space, so the rect round-trips
# exactly at any DPI or monitor layout. Do NOT pass x/y into create_window
# and do NOT use window.move: pywebview's Qt backend applies those pre-show
# and relative to the primary screen, so the window lands on the wrong
# monitor, drifts down by the title-bar height each launch, and slides
# sideways at non-100% scaling.
def _win32():
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return u


def _save_geometry(win) -> None:
    """Save the absolute frame rect (physical px) via Win32. Wired to
    `closing`. Guarded end-to-end so a failure here can never interfere with
    closing the app."""
    try:
        u = _win32()
        hwnd = u.FindWindowW(None, win.title)
        if not hwnd:
            return
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return
        x, y, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        # A minimized window reports a position around -32000; don't save
        # that as if it were the user's chosen spot.
        if x <= -30000 or y <= -30000:
            return
        if w <= 0 or h <= 0:
            return
        prefs = load_prefs()
        prefs["window"] = {"x": x, "y": y, "width": w, "height": h}
        save_prefs(prefs)
    except Exception:
        pass


def _restore_geometry(win) -> None:
    """Restore the saved frame rect via Win32. Wired to `shown` (after the OS
    window exists). Validated against the monitors currently connected before
    applying; never raises."""
    try:
        geo = load_prefs().get("window")
        if not isinstance(geo, dict):
            return
        x, y, w, h = geo.get("x"), geo.get("y"), geo.get("width"), geo.get("height")
        for v in (x, y, w, h):
            if not isinstance(v, int) or isinstance(v, bool):
                return
        if w <= 0 or h <= 0:
            return
        # Confirm a point inside the title bar area is still on a connected
        # monitor; MonitorFromPoint returns NULL if it isn't (for example the
        # saved monitor has been unplugged since the last launch).
        point = wintypes.POINT(x + 100, y + 30)
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        MONITOR_DEFAULTTONULL = 0
        if not user32.MonitorFromPoint(point, MONITOR_DEFAULTTONULL):
            return
        u = _win32()
        hwnd = u.FindWindowW(None, win.title)
        if not hwnd:
            return
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        u.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class NewerSchemaError(Exception):
    """Raised by open_db when the database's PRAGMA user_version is higher
    than this build's SCHEMA_VERSION. The database is never touched in this
    case; the caller should tell the user to update the app."""


def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if missing) the SQLite database and ensure the schema.
    Refuses to touch a database stamped with a schema newer than this build
    understands; see NewerSchemaError."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    existing_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if existing_version > SCHEMA_VERSION:
        conn.close()
        raise NewerSchemaError(
            f"Database schema {existing_version} is newer than this app supports ({SCHEMA_VERSION})."
        )

    # Check before creating so we only seed categories the first time this
    # table shows up (fresh db or an upgraded Phase 3 db); later runs must
    # never re-add categories the user deliberately deleted.
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone()
    categories_is_new = existing is None

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            starting_balance_cents INTEGER NOT NULL,
            starting_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            date TEXT NOT NULL,
            payee TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER NOT NULL,
            cleared INTEGER NOT NULL DEFAULT 0,
            estimated INTEGER NOT NULL DEFAULT 0,
            sort_key INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS autopays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            payee TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER NOT NULL,
            next_pay_date TEXT NOT NULL,
            next_post_date TEXT NOT NULL,
            pay_day INTEGER NOT NULL,
            post_day INTEGER NOT NULL,
            is_variable INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    if categories_is_new:
        conn.executemany(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)",
            [(c,) for c in SEED_CATEGORIES],
        )

    # Migration for databases created before the starting-balance change note:
    # remember the previous amount and when it was last edited.
    account_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "starting_balance_prev_cents" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN starting_balance_prev_cents INTEGER")
        conn.execute("ALTER TABLE accounts ADD COLUMN starting_balance_changed_at TEXT")

    # Migration for variable autopays: transactions posted from a rule marked
    # "variable" arrive flagged as an estimate the user later confirms.
    transaction_cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "estimated" not in transaction_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0")

    # Migration for day-scoped reordering: sort_key breaks ties within a day
    # for transactions that share a date. Backfilled from id so existing rows
    # keep their current insertion-order position until the user reorders them.
    if "sort_key" not in transaction_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN sort_key INTEGER")
    conn.execute("UPDATE transactions SET sort_key = id WHERE sort_key IS NULL")

    autopay_cols = {r["name"] for r in conn.execute("PRAGMA table_info(autopays)")}
    if "is_variable" not in autopay_cols:
        conn.execute("ALTER TABLE autopays ADD COLUMN is_variable INTEGER NOT NULL DEFAULT 0")

    # Standing rule: migrations in this function must stay additive-only (new
    # tables/columns guarded by an existence check, never a destructive
    # rewrite), so any older backup file can always be opened and upgraded
    # in place by restore_backup.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


class Api:
    """Bridge exposed to the UI. Methods return JSON-able dicts; the UI awaits."""

    def __init__(self):
        self._window = None
        self._conn = None
        self._db_path = None
        self._debug = False
        self._debug_path = None
        self.backup_notice = None
        self.autopay_notice = None

    def set_window(self, w):
        self._window = w

    def set_conn(self, conn: sqlite3.Connection):
        self._conn = conn

    def set_db_path(self, path: str):
        self._db_path = path

    def close_conn(self):
        """Close whichever connection is currently live. restore_backup can
        swap in a new connection mid-session, so callers (main() at exit)
        should always go through this rather than holding a stale local."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

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

    def get_config(self):
        """Initial payload the UI loads on startup."""
        try:
            cur = self._conn.cursor()
            account_rows = cur.execute(
                "SELECT id, name FROM accounts ORDER BY name COLLATE NOCASE"
            ).fetchall()
            accounts = [{"id": r["id"], "name": r["name"]} for r in account_rows]

            prefs = load_prefs()
            active_id = prefs.get("active_account_id")
            account = None
            if accounts:
                if active_id is not None:
                    account = self._get_account(active_id)
                if account is None:
                    account = self._get_account(accounts[0]["id"])

            backup_dir, backup_is_custom = effective_backup_dir()

            return {
                "ok": True,
                "version": APP_VERSION,
                "theme": self._load_theme(),
                "has_account": account is not None,
                "account": self._account_payload(account) if account is not None else None,
                "accounts": accounts,
                "backup_folder": backup_dir,
                "backup_folder_is_custom": backup_is_custom,
                "backup_keep": _clamp_backup_keep(prefs.get("backup_keep")),
                "backup_notice": self.backup_notice,
                "autopay_notice": self.autopay_notice,
            }
        except Exception as e:
            self.log(f"get_config failed: {e}")
            return {"ok": False, "error": "Couldn't load the app's configuration."}

    def create_account(self, name, starting_balance, starting_date):
        """Create an account. Used both for first-run setup and for 'Add
        account' once other accounts already exist. The new account becomes
        the active one."""
        try:
            name_s = (name or "").strip() or "Checking"
            cents, err = parse_amount_to_cents(starting_balance, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            date_s, err = parse_iso_date(starting_date)
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
            prefs = load_prefs()
            prefs["active_account_id"] = new_id
            save_prefs(prefs)
            self.log(f"Account {new_id} created, starting as of {date_s}")
            return self.get_config()
        except Exception as e:
            self.log(f"create_account failed: {e}")
            return {"ok": False, "error": "Couldn't create the account."}

    def set_active_account(self, account_id):
        """Switch which account the UI shows and operates on."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "That account no longer exists."}
            prefs = load_prefs()
            prefs["active_account_id"] = account["id"]
            save_prefs(prefs)
            self.log(f"Active account set to {account['id']}")
            return self.get_config()
        except Exception as e:
            self.log(f"set_active_account failed: {e}")
            return {"ok": False, "error": "Couldn't switch accounts."}

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
            prefs = load_prefs()
            if prefs.get("active_account_id") == account["id"]:
                remaining = cur.execute(
                    "SELECT id FROM accounts ORDER BY name COLLATE NOCASE LIMIT 1"
                ).fetchone()
                prefs["active_account_id"] = remaining["id"] if remaining else None
                save_prefs(prefs)
            self.log(f"Account {account['id']} deleted ({tx_count} transactions removed)")
            return self.get_config()
        except Exception as e:
            self.log(f"delete_account failed: {e}")
            return {"ok": False, "error": "Couldn't delete the account."}

    def update_account(self, account_id, name, starting_balance, starting_date):
        """Edit account name / starting balance / starting date. Recalcs the register."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "That account no longer exists."}
            name_s = (name or "").strip()
            if not name_s:
                return {"ok": False, "error": "Account name is required."}
            cents, err = parse_amount_to_cents(starting_balance, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            date_s, err = parse_iso_date(starting_date)
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
    def get_categories(self):
        """Name-sorted category list with usage counts (case-insensitive)."""
        try:
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT c.id, c.name, "
                "(SELECT COUNT(*) FROM transactions t WHERE t.category = c.name COLLATE NOCASE) AS used_count "
                "FROM categories c ORDER BY c.name COLLATE NOCASE"
            ).fetchall()
            categories = [
                {"id": r["id"], "name": r["name"], "used_count": r["used_count"]} for r in rows
            ]
            return {"ok": True, "categories": categories}
        except Exception as e:
            self.log(f"get_categories failed: {e}")
            return {"ok": False, "error": "Couldn't load the categories."}

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

    def rename_category(self, category_id, new_name):
        """Rename a category and carry the change over to past transactions.
        If the new name collides with another existing category, the two are
        merged: transactions move to the existing category and this row goes away."""
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
                cur.execute("DELETE FROM categories WHERE id=?", (category_id,))
                self._conn.commit()
                self.log(f"Category {category_id} merged into category {merge_target['id']}")
                return self.get_categories()
            cur.execute("UPDATE categories SET name=? WHERE id=?", (new_name_s, category_id))
            cur.execute(
                "UPDATE transactions SET category=? WHERE category=? COLLATE NOCASE",
                (new_name_s, old_name),
            )
            self._conn.commit()
            self.log(f"Category {category_id} renamed")
            return self.get_categories()
        except Exception as e:
            self.log(f"rename_category failed: {e}")
            return {"ok": False, "error": "Couldn't rename the category."}

    def delete_category(self, category_id, reassign_to=None):
        """Delete a category. Past transactions keep the old label unless
        reassign_to names another category to move them to."""
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
            cur.execute("DELETE FROM categories WHERE id=?", (category_id,))
            self._conn.commit()
            detail = " (transactions reassigned)" if reassign_s else ""
            self.log(f"Category {category_id} deleted{detail}")
            return self.get_categories()
        except Exception as e:
            self.log(f"delete_category failed: {e}")
            return {"ok": False, "error": "Couldn't delete the category."}

    # --- transactions ---------------------------------------------------------
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
                from_s = (datetime.date.today() - datetime.timedelta(days=DEFAULT_RANGE_DAYS)).isoformat()
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

    def add_transaction(self, account_id, date, payee, category, notes, amount, direction):
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            date_s, err = parse_iso_date(date)
            if err:
                return {"ok": False, "error": err}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
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

    def update_transaction(self, transaction_id, date, payee, category, notes, amount, direction):
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id, date FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That transaction no longer exists."}
            date_s, err = parse_iso_date(date)
            if err:
                return {"ok": False, "error": err}
            payee_s = (payee or "").strip()
            if not payee_s:
                return {"ok": False, "error": "Payee / description is required."}
            if direction not in ("withdraw", "deposit"):
                return {"ok": False, "error": "Choose withdraw or deposit."}
            cents, err = parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            signed = -cents if direction == "withdraw" else cents
            category_s = (category or "").strip()
            notes_s = (notes or "").strip()
            # A date change moves the transaction to a different day, so its
            # sort_key is reset to its id, landing it by the old insertion-order
            # rule in the target day rather than carrying a stale position.
            if date_s != row["date"]:
                cur.execute(
                    "UPDATE transactions SET date=?, payee=?, category=?, notes=?, amount_cents=?, "
                    "sort_key=id WHERE id=?",
                    (date_s, payee_s, category_s, notes_s, signed, transaction_id),
                )
            else:
                cur.execute(
                    "UPDATE transactions SET date=?, payee=?, category=?, notes=?, amount_cents=? "
                    "WHERE id=?",
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

    def reorder_transactions(self, account_id, date, ordered_ids):
        """Set the within-day display order for one date's transactions. The
        caller supplies the full set of that day's ids in the new order;
        sort_key values 1..n never collide across days since date sorts
        first, so this never needs to touch any other day's rows."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            date_s, err = parse_iso_date(date)
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
            cents, err = parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
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
            cents, err = parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            post_s, err = parse_iso_date(post_date)
            if err:
                return {"ok": False, "error": err}
            pay_s, err = parse_iso_date(pay_date)
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
            try:
                posted = self.post_due_autopays()
            except Exception as e:
                self.log(f"post_due_autopays call failed: {e}")
            # This posting pass is triggered from the UI, not launch, so don't
            # leave a stale launch notice for the next startup to pick up.
            self.autopay_notice = None
            result = self.get_autopays(account["id"])
            if result.get("ok"):
                result["posted"] = posted
            return result
        except Exception as e:
            self.log(f"add_autopay failed: {e}")
            return {"ok": False, "error": "Couldn't add the autopay."}

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
            cents, err = parse_amount_to_cents(amount, allow_negative=False, allow_zero=False)
            if err:
                return {"ok": False, "error": err}
            post_s, err = parse_iso_date(post_date)
            if err:
                return {"ok": False, "error": err}
            pay_s, err = parse_iso_date(pay_date)
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
            try:
                posted = self.post_due_autopays()
            except Exception as e:
                self.log(f"post_due_autopays call failed: {e}")
            # This posting pass is triggered from the UI, not launch, so don't
            # leave a stale launch notice for the next startup to pick up.
            self.autopay_notice = None
            result = self.get_autopays(row["account_id"])
            if result.get("ok"):
                result["posted"] = posted
            return result
        except Exception as e:
            self.log(f"update_autopay failed: {e}")
            return {"ok": False, "error": "Couldn't update the autopay."}

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
                    next_pay_date = advance_one_month(next_pay_date, rule["pay_day"])
                    next_post_date = advance_one_month(next_post_date, rule["post_day"])
                cur.execute(
                    "UPDATE autopays SET next_pay_date=?, next_post_date=? WHERE id=?",
                    (next_pay_date, next_post_date, rule["id"]),
                )
            self._conn.commit()
            if posted_count == 1:
                self.autopay_notice = "Added 1 autopay to the register."
            elif posted_count > 1:
                self.autopay_notice = f"Added {posted_count} autopays to the register."
            self.log(f"post_due_autopays: posted {posted_count} transaction(s)")
            return posted_count
        except Exception as e:
            self._conn.rollback()
            self.log(f"post_due_autopays failed: {e}")
            return 0

    # --- reconcile --------------------------------------------------------------
    def _reconcile_summary(self, account) -> dict:
        """Cleared vs. register totals for the reconcile view, in integer cents.
        Cleared total is full history (a bank statement balance is cumulative,
        not scoped to whatever date range the view happens to be showing)."""
        cur = self._conn.cursor()
        cleared_total = cur.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
            "WHERE account_id=? AND cleared=1",
            (account["id"],),
        ).fetchone()[0]
        register_balance_cents = self._current_balance_cents(account)
        cleared_balance_cents = account["starting_balance_cents"] + cleared_total
        return {
            "register_balance_cents": register_balance_cents,
            "cleared_balance_cents": cleared_balance_cents,
            "difference_cents": register_balance_cents - cleared_balance_cents,
        }

    def set_transaction_cleared(self, transaction_id, cleared):
        """Toggle a transaction's cleared flag. Never affects any balance;
        it only changes which side of the reconcile summary it counts on."""
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": "That transaction no longer exists."}
            cur.execute(
                "UPDATE transactions SET cleared=? WHERE id=?",
                (1 if cleared else 0, transaction_id),
            )
            self._conn.commit()
            self.log(f"Transaction {transaction_id} cleared set to {bool(cleared)}")
            account = self._get_account(row["account_id"])
            return {"ok": True, "summary": self._reconcile_summary(account)}
        except Exception as e:
            self.log(f"set_transaction_cleared failed: {e}")
            return {"ok": False, "error": "Couldn't update the cleared status."}

    def get_reconcile_data(self, account_id=None, from_date=None, to_date=None):
        """Rows and summary totals for the reconcile view. Rows are scoped to
        the given date range like the register; the summary totals are always
        computed over the full history. With no from_date, falls back to the
        last 30 days."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            cur = self._conn.cursor()
            from_s = (from_date or "").strip()
            if not from_s:
                from_s = (datetime.date.today() - datetime.timedelta(days=DEFAULT_RANGE_DAYS)).isoformat()
            to_s = (to_date or "").strip()

            query = (
                "SELECT id, date, payee, category, notes, amount_cents, cleared "
                "FROM transactions WHERE account_id=? AND date>=?"
            )
            params = [account["id"], from_s]
            if to_s:
                query += " AND date<=?"
                params.append(to_s)
            query += " ORDER BY date ASC, sort_key ASC, id ASC"
            rows = cur.execute(query, params).fetchall()
            payload_rows = [
                {
                    "id": r["id"],
                    "date": r["date"],
                    "payee": r["payee"],
                    "category": r["category"],
                    "notes": r["notes"],
                    "amount_cents": r["amount_cents"],
                    "cleared": bool(r["cleared"]),
                }
                for r in rows
            ]
            return {
                "ok": True,
                "rows": payload_rows,
                "summary": self._reconcile_summary(account),
            }
        except Exception as e:
            self.log(f"get_reconcile_data failed: {e}")
            return {"ok": False, "error": "Couldn't load the reconcile data."}

    def find_discrepancy(self, account_id=None, amount=None, from_date=None, to_date=None):
        """Look for likely causes of a reconcile difference: a single transaction
        that matches it exactly, one that matches half of it (a wrong withdraw
        or deposit direction), a divisible-by-9 hint (a classic transposed-digit
        typo), and combinations of the visible-range transactions that add up
        to it (unchecked transactions searched first, since they are the prime
        suspects for a reconcile difference)."""
        try:
            cents, err = parse_amount_to_cents(amount, allow_negative=True, allow_zero=True)
            if err:
                return {"ok": False, "error": err}
            diff_cents = abs(cents)
            if diff_cents == 0:
                return {"ok": False, "error": "Enter the amount you are off by."}

            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}

            cur = self._conn.cursor()

            # 0. unchecked_match: the register balance and the cleared balance
            # are both full-history numbers, so their difference always equals
            # the full-history unchecked total. If that total matches the
            # entered amount, everything still unchecked explains it.
            unchecked_row = cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_cents), 0) AS total "
                "FROM transactions WHERE account_id=? AND cleared=0",
                (account["id"],),
            ).fetchone()
            unchecked_match = None
            if unchecked_row["cnt"] > 0 and abs(unchecked_row["total"]) == diff_cents:
                unchecked_match = {"count": unchecked_row["cnt"], "total_cents": unchecked_row["total"]}

            from_s = (from_date or "").strip()
            if not from_s:
                from_s = (datetime.date.today() - datetime.timedelta(days=DEFAULT_RANGE_DAYS)).isoformat()
            to_s = (to_date or "").strip()

            def row_dict(r):
                return {
                    "id": r["id"],
                    "date": r["date"],
                    "payee": r["payee"],
                    "amount_cents": r["amount_cents"],
                    "cleared": bool(r["cleared"]),
                }

            # 1. exact: full history, any transaction whose absolute amount matches.
            exact_rows = cur.execute(
                "SELECT id, date, payee, amount_cents, cleared FROM transactions "
                "WHERE account_id=? AND ABS(amount_cents)=? ORDER BY date ASC, sort_key ASC, id ASC",
                (account["id"], diff_cents),
            ).fetchall()
            exact = [row_dict(r) for r in exact_rows]

            # 2. half: only meaningful when the difference splits evenly into cents.
            half = []
            if diff_cents % 2 == 0:
                half_rows = cur.execute(
                    "SELECT id, date, payee, amount_cents, cleared FROM transactions "
                    "WHERE account_id=? AND ABS(amount_cents)=? ORDER BY date ASC, sort_key ASC, id ASC",
                    (account["id"], diff_cents // 2),
                ).fetchall()
                half = [row_dict(r) for r in half_rows]

            # 3. transposition hint: a swapped-digits typo always produces a
            # difference divisible by 9.
            transposition_hint = diff_cents % 9 == 0

            # 4. combinations: always searched, scoped to the visible date range,
            # same as the reconcile table itself, not the full history. A
            # coincidental exact or half match should never hide a combination
            # the user actually needed.
            combinations = []
            combinations_skipped = False
            query = "SELECT id, date, payee, amount_cents, cleared FROM transactions WHERE account_id=? AND date>=?"
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
                unchecked_list = [r for r in range_list if not r["cleared"]]
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

                # Pass 1: unchecked transactions only, the prime suspects.
                found = search_combos(unchecked_list, 10)
                # Pass 2: all range rows, skipping id sets already found in pass 1.
                if len(found) < 10:
                    found.extend(search_combos(range_list, 10 - len(found)))
                combinations = found[:10]

            self.log(
                f"find_discrepancy: unchecked_match={unchecked_match is not None} "
                f"exact={len(exact)} half={len(half)} transposition_hint={transposition_hint} "
                f"combinations={len(combinations)} combinations_skipped={combinations_skipped}"
            )
            return {
                "ok": True,
                "unchecked_match": unchecked_match,
                "exact": exact,
                "half": half,
                "transposition_hint": transposition_hint,
                "combinations": combinations,
                "combinations_skipped": combinations_skipped,
            }
        except Exception as e:
            self.log(f"find_discrepancy failed: {e}")
            return {"ok": False, "error": "Couldn't search for the discrepancy."}

    # --- CSV export -------------------------------------------------------------
    def export_csv(self, account_id, from_date, to_date, range_label=""):
        """Export the register to a CSV file via a native save dialog. An
        empty from_date and to_date together mean all history (no default
        range applied here; that default is only for the register views).
        range_label is the preset token the UI picked (e.g. AllHistory,
        Last30Days, CustomRange); it only affects the default filename."""
        try:
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
            default_name = sanitize_filename(
                f"{account['name']}_Export_{token}_{datetime.date.today().strftime('%m%d%Y')}.csv"
            )

            documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
            start_dir = documents_dir if os.path.isdir(documents_dir) else app_dir()

            result = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=start_dir,
                save_filename=default_name,
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
                writer = csv.writer(f)
                range_text = "All history" if all_history else f"{from_s or 'start'} to {to_s or 'today'}"
                writer.writerow(
                    [account["name"], range_text, f"Exported {datetime.date.today().isoformat()}"]
                )
                writer.writerow(
                    ["Date", "Payee / Description", "Category", "Notes", "Withdraw", "Deposit", "Balance"]
                )
                for r in rows:
                    withdraw = cents_to_decimal_str(-r["amount_cents"]) if r["amount_cents"] < 0 else ""
                    deposit = cents_to_decimal_str(r["amount_cents"]) if r["amount_cents"] > 0 else ""
                    balance = cents_to_decimal_str(r["balance_cents"])
                    writer.writerow([r["date"], r["payee"], r["category"], r["notes"], withdraw, deposit, balance])

            self.log(f"Exported {len(rows)} transactions to {path}")
            return {"ok": True, "path": path, "count": len(rows)}
        except Exception as e:
            self.log(f"export_csv failed: {e}")
            return {"ok": False, "error": "Couldn't export the CSV file."}

    # --- preferences (local file, not stored in the db) ----------------------
    def _load_theme(self) -> str:
        theme = load_prefs().get("theme")
        return theme if theme in ("dark", "light") else "dark"

    def get_theme(self):
        return self._load_theme()

    def save_theme(self, theme: str):
        if theme not in ("dark", "light"):
            return {"ok": False}
        prefs = load_prefs()
        prefs["theme"] = theme
        if save_prefs(prefs):
            self.log(f"Theme set to {theme}")
            return {"ok": True}
        self.log("Could not save theme pref")
        return {"ok": False}

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
            if not _writable_check(folder):
                return {"ok": False, "error": "That folder isn't writable. Choose a different one."}
            prefs = load_prefs()
            prefs["backup_folder"] = folder
            save_prefs(prefs)
            self.log(f"Backup folder moved to {folder}")
            return {"ok": True, "backup_folder": folder, "backup_folder_is_custom": True}
        except Exception as e:
            self.log(f"choose_backup_folder failed: {e}")
            return {"ok": False, "error": "Couldn't set the backup folder."}

    def reset_backup_folder(self):
        """Reset the backup location back to the default folder next to the app."""
        try:
            prefs = load_prefs()
            prefs.pop("backup_folder", None)
            save_prefs(prefs)
            self.log("Backup folder reset to default")
            default_dir, _ = effective_backup_dir()
            return {"ok": True, "backup_folder": default_dir, "backup_folder_is_custom": False}
        except Exception as e:
            self.log(f"reset_backup_folder failed: {e}")
            return {"ok": False, "error": "Couldn't reset the backup folder."}

    def set_backup_keep(self, n):
        """Set how many regular backups to keep, clamped to 1..50. Pre-restore
        safety backups are pruned separately and never count against this."""
        try:
            keep = _clamp_backup_keep(n)
            prefs = load_prefs()
            prefs["backup_keep"] = keep
            save_prefs(prefs)
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
        if not name or name != filename or not BACKUP_FILENAME_RE.match(name):
            return None, "That doesn't look like one of this app's backup files."
        backups_dir, _ = effective_backup_dir()
        full_path = os.path.join(backups_dir, name)
        if not os.path.isfile(full_path):
            return None, "That backup file no longer exists."
        return full_path, None

    @staticmethod
    def _open_backup_readonly(full_path):
        """Open a backup file read-only and sanity-check it before any use.
        Never raises; returns (connection, None) or (None, error_message)."""
        try:
            uri = "file:" + urllib.parse.quote(full_path.replace("\\", "/")) + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                conn.close()
                return None, "That backup was made by a newer version of Simple Account Balancer. Update the app to restore it."
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"accounts", "transactions"}.issubset(tables):
                conn.close()
                return None, "That file doesn't look like a Simple Account Balancer backup."
            return conn, None
        except Exception:
            return None, "That backup file is corrupt or unreadable."

    def _diff_against_live(self, backup_conn):
        """Per-account comparison of a backup against the live database.
        Accounts are matched by id (both tables are AUTOINCREMENT, so ids
        are never reused); transactions are compared by full row content."""
        live_accounts = {r["id"]: r["name"] for r in self._conn.execute("SELECT id, name FROM accounts")}
        backup_accounts = {r["id"]: r["name"] for r in backup_conn.execute("SELECT id, name FROM accounts")}

        def tx_set(conn, account_id):
            rows = conn.execute(
                "SELECT id, date, payee, category, notes, amount_cents, cleared "
                "FROM transactions WHERE account_id=?",
                (account_id,),
            ).fetchall()
            return {
                (r["id"], r["date"], r["payee"], r["category"], r["notes"], r["amount_cents"], bool(r["cleared"]))
                for r in rows
            }

        results = []
        for account_id in set(live_accounts) | set(backup_accounts):
            in_live = account_id in live_accounts
            in_backup = account_id in backup_accounts
            name = live_accounts.get(account_id, backup_accounts.get(account_id))
            if in_live and not in_backup:
                results.append({
                    "id": account_id, "name": name, "kind": "only_live",
                    "count": len(tx_set(self._conn, account_id)),
                })
            elif in_backup and not in_live:
                results.append({
                    "id": account_id, "name": name, "kind": "only_backup",
                    "count": len(tx_set(backup_conn, account_id)),
                })
            else:
                live_set = tx_set(self._conn, account_id)
                backup_set = tx_set(backup_conn, account_id)
                added_or_changed = len(live_set - backup_set)
                deleted = len(backup_set - live_set)
                if added_or_changed == 0 and deleted == 0:
                    results.append({"id": account_id, "name": name, "kind": "same"})
                else:
                    results.append({
                        "id": account_id, "name": name, "kind": "diff",
                        "added_or_changed": added_or_changed, "deleted": deleted,
                    })
        results.sort(key=lambda r: (r["name"] or "").casefold())
        return results

    def list_backups(self):
        """List backups in the effective backup folder, newest first."""
        try:
            backups_dir, _ = effective_backup_dir()
            try:
                names = os.listdir(backups_dir)
            except Exception:
                names = []
            items = []
            for name in names:
                if not (name.startswith("balancer_") and name.endswith(".db")):
                    continue
                full_path = os.path.join(backups_dir, name)
                items.append({
                    "filename": name,
                    "timestamp": _parse_backup_timestamp(name, full_path),
                    "is_prerestore": name.startswith("balancer_prerestore_"),
                    "size_bytes": os.path.getsize(full_path) if os.path.isfile(full_path) else 0,
                })
            items.sort(key=lambda it: it["timestamp"], reverse=True)
            return {"ok": True, "backups": items}
        except Exception as e:
            self.log(f"list_backups failed: {e}")
            return {"ok": False, "error": "Couldn't list backups."}

    def preview_restore(self, filename):
        """Compare a backup file against the live database without changing
        anything, so the UI can show what a restore would do."""
        try:
            full_path, err = self._validate_backup_filename(filename)
            if err:
                return {"ok": False, "error": err}
            backup_conn, err = self._open_backup_readonly(full_path)
            if err:
                return {"ok": False, "error": err}
            try:
                accounts_diff = self._diff_against_live(backup_conn)
            finally:
                backup_conn.close()
            timestamp = _parse_backup_timestamp(os.path.basename(full_path), full_path)
            return {"ok": True, "timestamp": timestamp, "accounts": accounts_diff}
        except Exception as e:
            self.log(f"preview_restore failed: {e}")
            return {"ok": False, "error": "Couldn't read that backup file."}

    def restore_backup(self, filename):
        """Restore the live database from a backup file. Takes a pre-restore
        safety backup of the current live data first, so this can be undone."""
        try:
            full_path, err = self._validate_backup_filename(filename)
            if err:
                return {"ok": False, "error": err}
            # Validate the backup is actually usable before touching anything live.
            backup_conn, err = self._open_backup_readonly(full_path)
            if err:
                return {"ok": False, "error": err}
            backup_conn.close()

            db_path = self._db_path
            backups_dir, _ = effective_backup_dir()
            prerestore_path = _make_prerestore_backup(db_path, backups_dir)
            if prerestore_path is None:
                return {"ok": False, "error": "Couldn't take a safety backup, so the restore was cancelled."}

            self.close_conn()
            try:
                shutil.copy2(full_path, db_path)
            except Exception as e:
                self.log(f"restore_backup copy failed: {e}")
                try:
                    self._conn = open_db(db_path)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": "Couldn't copy the backup into place. A safety backup of "
                             "your previous data was taken before this, so nothing is lost.",
                }

            new_conn = open_db(db_path)  # upgrades an older-schema backup automatically
            self.set_conn(new_conn)
            self.log(
                f"Restored from backup {os.path.basename(full_path)} "
                f"(safety backup: {os.path.basename(prerestore_path)})"
            )
            return self.get_config()
        except Exception as e:
            self.log(f"restore_backup failed: {e}")
            return {"ok": False, "error": "Couldn't restore that backup."}

    # --- misc bridge helpers --------------------------------------------------
    def open_url(self, url: str):
        """Open a link in the system browser, never by navigating the app window."""
        import webbrowser

        webbrowser.open(url)
        return {"ok": True}

    def check_update(self):
        """Compare the latest published release to APP_VERSION. Silent on failure."""
        result = {"current": APP_VERSION, "version": None, "update": False, "offline": False}
        try:
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.load(r)
            latest = (data.get("tag_name") or "").lstrip("v")
            result["version"] = latest
            if latest and self._is_newer(latest, APP_VERSION):
                result["update"] = True
        except Exception:
            result["offline"] = True  # offline / private repo / rate-limited: stay quiet
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
        self._debug = bool(on)
        if self._debug and not self._debug_path:
            stamp = datetime.datetime.now().strftime("%m%d%Y_%H%M%S")
            self._debug_path = os.path.join(app_dir(), f"Debug_Log_{stamp}.txt")
            self.log("Debug log started")
        return {"ok": True}

    def log(self, msg: str):
        # Privacy rule for every call site: users share this log for bug
        # reports, so lines may carry ids, dates, counts, and paths, never
        # payees, amounts, balances, or user-entered names.
        if not self._debug or not self._debug_path:
            return
        try:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self._debug_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Startup: writable-location check + rolling backups
# ---------------------------------------------------------------------------
def _writable_check(folder: str) -> bool:
    """Try creating and deleting a temp file next to the exe."""
    try:
        test_path = os.path.join(folder, f".wtest_{os.getpid()}.tmp")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(test_path)
        return True
    except Exception:
        return False


def _show_write_error(folder: str):
    msg = (
        "Simple Account Balancer keeps its data in a file next to the app, "
        f"but this folder isn't writable:\n\n{folder}\n\n"
        "This often happens when the app is placed in Program Files. Move it "
        "to a writable folder (like your Desktop or Documents) and try again."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def effective_backup_dir() -> tuple:
    """Where backups are read from and written to right now: the custom
    backup_folder pref if one is set (replacing the default, not adding to
    it), else the default backups/ folder next to the app. Returns
    (path, is_custom). Module-level, no Api state, so main() can call it
    before any window exists."""
    custom = load_prefs().get("backup_folder")
    if custom:
        return custom, True
    return os.path.join(app_dir(), BACKUP_DIRNAME), False


def _clamp_backup_keep(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return BACKUP_KEEP
    return max(BACKUP_KEEP_MIN, min(BACKUP_KEEP_MAX, n))


def _list_backup_files(backups_dir: str, prerestore: bool) -> list:
    """Filenames sorted oldest first. Regular backups exclude prerestore
    ones even though both start with 'balancer_', since the fixed-width
    timestamp suffix makes lexical order match chronological order either way."""
    try:
        names = os.listdir(backups_dir)
    except Exception:
        return []
    if prerestore:
        return sorted(n for n in names if n.startswith("balancer_prerestore_") and n.endswith(".db"))
    return sorted(
        n for n in names
        if n.startswith("balancer_") and n.endswith(".db") and not n.startswith("balancer_prerestore_")
    )


def _prune_backups(backups_dir: str, keep: int):
    files = _list_backup_files(backups_dir, prerestore=False)
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(backups_dir, old))
        except Exception:
            pass


def _prune_prerestore_backups(backups_dir: str, keep: int = PRERESTORE_KEEP):
    files = _list_backup_files(backups_dir, prerestore=True)
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(backups_dir, old))
        except Exception:
            pass


def _make_backup(db_path: str, backups_dir: str, keep: int = BACKUP_KEEP) -> bool:
    """Copy the database into backups_dir as balancer_YYYYMMDD_HHMMSS.db,
    keeping only the newest `keep` regular backups. Pre-restore safety
    backups are a separate pool; see _make_prerestore_backup."""
    try:
        os.makedirs(backups_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(backups_dir, f"balancer_{stamp}.db")
        shutil.copy2(db_path, dest)
        _prune_backups(backups_dir, keep)
        return True
    except Exception:
        return False


def _make_prerestore_backup(db_path: str, backups_dir: str):
    """Copy the current live database aside before a restore overwrites it,
    as balancer_prerestore_YYYYMMDD_HHMMSS.db. Returns the new file's full
    path, or None on failure. Kept to the newest PRERESTORE_KEEP, a pool
    separate from (and never counted against) the regular backup_keep limit."""
    try:
        os.makedirs(backups_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(backups_dir, f"balancer_prerestore_{stamp}.db")
        shutil.copy2(db_path, dest)
        _prune_prerestore_backups(backups_dir)
        return dest
    except Exception:
        return None


def _parse_backup_timestamp(filename: str, full_path: str) -> str:
    """The timestamp encoded in a balancer_* filename, falling back to the
    file's mtime if the name doesn't parse. Always returns an ISO string."""
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
    Returns (success, used_fallback, actual_dir)."""
    default_dir = os.path.join(app_dir(), BACKUP_DIRNAME)
    try:
        prefs = load_prefs()
        keep = _clamp_backup_keep(prefs.get("backup_keep"))
        target_dir, is_custom = effective_backup_dir()
        used_fallback = False
        if is_custom and not (os.path.isdir(target_dir) and _writable_check(target_dir)):
            target_dir = default_dir
            used_fallback = True
        ok = _make_backup(db_path, target_dir, keep)
        return ok, used_fallback, target_dir
    except Exception:
        return False, False, default_dir


def _show_backup_fallback_notice(actual_dir: str):
    msg = (
        "The backup folder wasn't reachable, so the closing backup was saved "
        f"to {actual_dir} instead."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


def _show_newer_schema_error():
    msg = (
        "This data file was created by a newer version of Simple Account "
        "Balancer than this one.\n\n"
        "Update to the latest version of the app to open it."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


# Splash close: honor a 5s minimum so it doesn't just flash, but never
# hang past 30s. Whichever of (window ready after the floor) / (watchdog)
# fires first wins; the rest are no-ops. In source/dev runs pyi_splash is
# absent, so all of this does nothing.
_splash = {"closed": False, "start": time.monotonic()}


def _close_splash():
    if _splash["closed"]:
        return
    _splash["closed"] = True
    try:
        import pyi_splash  # only present in the frozen build

        pyi_splash.close()
    except Exception:
        pass


def _on_window_ready():
    elapsed = time.monotonic() - _splash["start"]
    if elapsed >= 5:
        _close_splash()
    else:
        threading.Timer(5 - elapsed, _close_splash).start()


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

    folder = app_dir()
    if not _writable_check(folder):
        _show_write_error(folder)
        sys.exit(1)

    db_path = os.path.join(folder, DB_FILENAME)
    db_existed_before = os.path.exists(db_path)

    api = Api()
    api.set_db_path(db_path)

    # Launch backup runs BEFORE open_db, so every launch snapshot is a
    # pre-migration copy: open_db's schema migrations must never run first
    # and land in the backup we'd use to recover from them.
    if db_existed_before:
        ok, used_fallback, actual_dir = _run_backup_with_fallback(db_path)
        if ok:
            api.log(f"Launch backup created in {actual_dir}")
        else:
            api.log("Launch backup failed or skipped")
        if used_fallback:
            api.backup_notice = (
                f"Backup folder wasn't reachable. Today's backup was saved to {actual_dir} instead."
            )
            api.log(f"Backup folder unreachable at launch, fell back to {actual_dir}")

    try:
        conn = open_db(db_path)
    except NewerSchemaError:
        _show_newer_schema_error()
        sys.exit(1)

    api.set_conn(conn)

    # post_due_autopays already commits/rolls back internally; this guard is
    # belt and suspenders so a posting failure can never block launch.
    try:
        api.post_due_autopays()
    except Exception as e:
        api.log(f"post_due_autopays call failed: {e}")

    win = webview.create_window(
        "Simple Account Balancer",
        url=resource_path("simple_account_balancer-UI.html"),
        js_api=api,
        width=1150,
        height=760,
        min_size=(MIN_WINDOW_W, MIN_WINDOW_H),
        background_color="#0a0e14",
    )
    api.set_window(win)
    win.events.shown += lambda: _restore_geometry(win)

    def _on_window_closing():
        _save_geometry(win)
        return True

    win.events.closing += _on_window_closing
    win.events.loaded += _on_window_ready
    threading.Timer(30, _close_splash).start()  # ceiling: never hang
    try:
        webview.start(gui="qt", icon=resource_path("simple_account_balancer.png"))
    except TypeError:
        webview.start(gui="qt")

    # api._conn may no longer be the connection opened above (restore_backup
    # swaps in a new one mid-session), so close through the Api, not a stale
    # local variable.
    api.close_conn()

    # Exit backup runs unconditionally when the db file exists. Guarded so a
    # backup failure here can never block shutdown.
    try:
        if os.path.exists(db_path):
            ok, used_fallback, actual_dir = _run_backup_with_fallback(db_path)
            if used_fallback:
                _show_backup_fallback_notice(actual_dir)
    except Exception:
        pass


if __name__ == "__main__":
    main()
