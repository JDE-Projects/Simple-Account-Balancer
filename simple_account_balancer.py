"""
Simple Account Balancer, a checkbook-register style balance tracker.

JDE-Projects "Simple X Tool": Python 3 + PySide6/pywebview, single-file UI.
The register is the source of truth for "how much money do I actually have."
All money is stored and computed as integer cents; never floats.

Phase 3 scope: one account, transaction entry with add/edit/delete, a rolling
balance that recalculates for out-of-order entry/edit/delete, SQLite storage
with rolling backups, a writable-location startup check, and the themed UI
shell with the standard header/bottom bar.
"""
import ctypes
import datetime
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import webview

APP_VERSION = "1.0.0"
GITHUB_OWNER = "JDE-Projects"
GITHUB_REPO = "Simple-Account-Balancer"

DB_FILENAME = "simple_account_balancer.db"
BACKUP_DIRNAME = "backups"
BACKUP_KEEP = 5
DEFAULT_RANGE_DAYS = 30


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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if missing) the SQLite database and ensure the schema."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


class Api:
    """Bridge exposed to the UI. Methods return JSON-able dicts; the UI awaits."""

    def __init__(self):
        self._window = None
        self._conn = None
        self._debug = False
        self._debug_path = None

    def set_window(self, w):
        self._window = w

    def set_conn(self, conn: sqlite3.Connection):
        self._conn = conn

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

    def _account_payload(self, account):
        return {
            "id": account["id"],
            "name": account["name"],
            "starting_balance_cents": account["starting_balance_cents"],
            "starting_date": account["starting_date"],
            "current_balance_cents": self._current_balance_cents(account),
        }

    def get_config(self):
        """Initial payload the UI loads on startup."""
        try:
            account = self._get_account()
            return {
                "ok": True,
                "version": APP_VERSION,
                "theme": self._load_theme(),
                "has_account": account is not None,
                "account": self._account_payload(account) if account is not None else None,
            }
        except Exception as e:
            self.log(f"get_config failed: {e}")
            return {"ok": False, "error": "Couldn't load the app's configuration."}

    def create_account(self, name, starting_balance, starting_date):
        """First-run account setup. Phase 3 supports exactly one account."""
        try:
            if self._get_account() is not None:
                return {"ok": False, "error": "An account already exists."}
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
            self._conn.commit()
            self.log(f"Account created: {name_s!r} starting {cents}c as of {date_s}")
            return self.get_config()
        except Exception as e:
            self.log(f"create_account failed: {e}")
            return {"ok": False, "error": "Couldn't create the account."}

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
            cur.execute(
                "UPDATE accounts SET name=?, starting_balance_cents=?, starting_date=? WHERE id=?",
                (name_s, cents, date_s, account["id"]),
            )
            self._conn.commit()
            self.log(f"Account {account['id']} updated: {name_s!r} starting {cents}c as of {date_s}")
            return self.get_config()
        except Exception as e:
            self.log(f"update_account failed: {e}")
            return {"ok": False, "error": "Couldn't update the account."}

    # --- transactions ---------------------------------------------------------
    def get_transactions(self, account_id=None):
        """Rows for the default (last 30 days) range, with balances computed
        over the FULL history so the first visible row's balance is correct."""
        try:
            account = self._get_account(account_id)
            if account is None:
                return {"ok": False, "error": "No account exists yet."}
            cur = self._conn.cursor()
            all_rows = cur.execute(
                "SELECT id, date, payee, category, notes, amount_cents, cleared "
                "FROM transactions WHERE account_id=? ORDER BY date ASC, id ASC",
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
                        "balance_cents": running,
                    }
                )
            current_balance_cents = running

            cutoff = (datetime.date.today() - datetime.timedelta(days=DEFAULT_RANGE_DAYS)).isoformat()
            visible = [row for row in computed if row["date"] >= cutoff]

            return {
                "ok": True,
                "rows": visible,
                "current_balance_cents": current_balance_cents,
                "range_days": DEFAULT_RANGE_DAYS,
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
            self._conn.commit()
            self.log(f"Added transaction: {date_s} {payee_s!r} {signed}c")
            return {"ok": True}
        except Exception as e:
            self.log(f"add_transaction failed: {e}")
            return {"ok": False, "error": "Couldn't add the transaction."}

    def update_transaction(self, transaction_id, date, payee, category, notes, amount, direction):
        try:
            cur = self._conn.cursor()
            row = cur.execute("SELECT id FROM transactions WHERE id=?", (transaction_id,)).fetchone()
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
            cur.execute(
                "UPDATE transactions SET date=?, payee=?, category=?, notes=?, amount_cents=? "
                "WHERE id=?",
                (date_s, payee_s, category_s, notes_s, signed, transaction_id),
            )
            self._conn.commit()
            self.log(f"Updated transaction {transaction_id}: {date_s} {payee_s!r} {signed}c")
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

    # --- theme preference (local file, not stored in the db) ----------------
    def _pref_path(self) -> str:
        return os.path.join(app_dir(), "simple_account_balancer.pref")

    def _load_theme(self) -> str:
        try:
            with open(self._pref_path(), "r", encoding="utf-8") as f:
                theme = json.load(f).get("theme")
            return theme if theme in ("dark", "light") else "dark"
        except Exception:
            return "dark"

    def get_theme(self):
        return self._load_theme()

    def save_theme(self, theme: str):
        if theme not in ("dark", "light"):
            return {"ok": False}
        try:
            with open(self._pref_path(), "w", encoding="utf-8") as f:
                json.dump({"theme": theme}, f)
            self.log(f"Theme set to {theme}")
            return {"ok": True}
        except Exception as e:
            self.log(f"Could not save theme pref: {e}")
            return {"ok": False}

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


def _make_backup(db_path: str, backups_dir: str) -> bool:
    """Copy the database into backups/, keeping only the newest BACKUP_KEEP."""
    try:
        os.makedirs(backups_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(backups_dir, f"simple_account_balancer_{stamp}.db")
        shutil.copy2(db_path, dest)
        files = sorted(
            f
            for f in os.listdir(backups_dir)
            if f.startswith("simple_account_balancer_") and f.endswith(".db")
        )
        for old in files[:-BACKUP_KEEP] if len(files) > BACKUP_KEEP else []:
            try:
                os.remove(os.path.join(backups_dir, old))
            except Exception:
                pass
        return True
    except Exception:
        return False


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


def main():
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
    conn = open_db(db_path)

    api = Api()
    api.set_conn(conn)

    if db_existed_before:
        backups_dir = os.path.join(folder, BACKUP_DIRNAME)
        if _make_backup(db_path, backups_dir):
            api.log("Startup backup created")
        else:
            api.log("Startup backup failed or skipped")

    win = webview.create_window(
        "Simple Account Balancer",
        url=resource_path("simple_account_balancer-UI.html"),
        js_api=api,
        width=1150,
        height=760,
        min_size=(950, 650),
        background_color="#0a0e14",
    )
    api.set_window(win)
    win.events.loaded += _on_window_ready
    threading.Timer(30, _close_splash).start()  # ceiling: never hang
    try:
        webview.start(gui="qt", icon=resource_path("simple_account_balancer.png"))
    except TypeError:
        webview.start(gui="qt")


if __name__ == "__main__":
    main()
