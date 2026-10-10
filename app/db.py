"""SQLite database schema and connection helpers."""

from dataclasses import dataclass
import os
import sqlite3
import urllib.parse

from app import config


SEED_CATEGORIES = [
    "Auto", "Charity", "Dining", "Entertainment", "Fees", "Gas", "Gifts",
    "Groceries", "Healthcare", "Home", "Income", "Insurance", "Personal",
    "Rent/Mortgage", "Shopping", "Subscriptions", "Transfer", "Travel",
    "Utilities",
]

# The minimum schema each historical user_version promised. Later additions
# are optional for an older version, but must be complete if present.
_SCHEMA_BASE_COLUMNS = {
    "accounts": {
        "id", "name", "starting_balance_cents", "starting_date", "created_at",
    },
    "transactions": {
        "id", "account_id", "date", "payee", "category", "notes",
        "amount_cents", "cleared", "created_at",
    },
    "categories": {"id", "name"},
    "autopays": {
        "id", "account_id", "payee", "category", "notes", "amount_cents",
        "next_pay_date", "next_post_date", "pay_day", "post_day", "created_at",
    },
}
_SCHEMA_REQUIRED_COLUMNS = {
    0: {
        "accounts": _SCHEMA_BASE_COLUMNS["accounts"],
        "transactions": _SCHEMA_BASE_COLUMNS["transactions"],
    },
    1: {
        "accounts": _SCHEMA_BASE_COLUMNS["accounts"]
        | {"starting_balance_prev_cents", "starting_balance_changed_at"},
        "transactions": _SCHEMA_BASE_COLUMNS["transactions"],
        "categories": _SCHEMA_BASE_COLUMNS["categories"],
    },
    2: {
        "accounts": _SCHEMA_BASE_COLUMNS["accounts"]
        | {"starting_balance_prev_cents", "starting_balance_changed_at"},
        "transactions": _SCHEMA_BASE_COLUMNS["transactions"] | {"estimated"},
        "categories": _SCHEMA_BASE_COLUMNS["categories"],
        "autopays": _SCHEMA_BASE_COLUMNS["autopays"] | {"is_variable"},
    },
    3: {
        "accounts": _SCHEMA_BASE_COLUMNS["accounts"]
        | {"starting_balance_prev_cents", "starting_balance_changed_at"},
        "transactions": _SCHEMA_BASE_COLUMNS["transactions"] | {"estimated", "sort_key"},
        "categories": _SCHEMA_BASE_COLUMNS["categories"],
        "autopays": _SCHEMA_BASE_COLUMNS["autopays"] | {"is_variable"},
    },
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class NewerSchemaError(Exception):
    """Raised by open_db when the database's PRAGMA user_version is higher
    than this build's SCHEMA_VERSION. The database is never touched in this
    case; the caller should tell the user to update the app."""


@dataclass
class RecoveryResult:
    """The outcome of opening and checking an existing database."""

    status: str
    conn: sqlite3.Connection | None
    version: int | None
    detail: str


def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if missing) the SQLite database and ensure the schema.
    Refuses to touch a database stamped with a schema newer than this build
    understands; see NewerSchemaError."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level="")
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        existing_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if existing_version > config.SCHEMA_VERSION:
            raise NewerSchemaError(
                f"Database schema {existing_version} is newer than this app supports ({config.SCHEMA_VERSION})."
            )

        # Check before creating so we only seed categories the first time this
        # table shows up (a fresh database, or one upgraded from a version
        # without categories); later runs must never re-add categories the
        # user deliberately deleted.
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
        conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION}")
        conn.commit()
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def _readonly_connection(path: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(path.replace("\\", "/")) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _readwrite_connection(path: str, timeout: float = 5.0) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(path.replace("\\", "/")) + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _schema_contract_error(conn: sqlite3.Connection, version: int) -> str | None:
    required = _SCHEMA_REQUIRED_COLUMNS.get(version)
    if required is None:
        return "That backup file has an unsupported schema version."
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, columns in required.items():
        if table not in tables:
            return f"That backup is missing its required {table} table."
        actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(columns - actual)
        if missing:
            return f"That backup is missing required column {table}.{missing[0]}."
    for table, columns in _SCHEMA_BASE_COLUMNS.items():
        if table in tables:
            actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(columns - actual)
            if missing:
                return f"That backup has an incomplete {table} table (missing {missing[0]})."
    return None


def _database_check_error(conn: sqlite3.Connection, allow_newer: bool) -> str | None:
    """Return a plain-English reason when a database is unsafe to use."""
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        return "That backup file is corrupt or unreadable."
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > config.SCHEMA_VERSION:
        if allow_newer:
            return None
        return (
            "That backup was made by a newer version of Simple Account Balancer. "
            "Update the app to restore it."
        )
    schema_error = _schema_contract_error(conn, version)
    if schema_error:
        return schema_error
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return "That backup has broken links between its records."
    return None


def _holds_no_rows(conn: sqlite3.Connection) -> bool:
    """True when no table in the database has a single row, as left by a first
    launch that stopped before the schema was fully created."""
    tables = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    return all(
        conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() is None
        for name in tables
    )


def recover_and_check(path: str, timeout: float = 5.0) -> RecoveryResult:
    """Recover a rollback journal and check an existing database without
    writing schema. Status is "ok" (conn left open), "empty" (an intact
    version 0 file with no rows, safe for open_db to set up), "damaged", or
    "unavailable"."""
    conn = None
    try:
        conn = _readwrite_connection(path, timeout=timeout)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        check_error = _database_check_error(conn, allow_newer=True)
        if check_error is not None:
            integrity_ok = check_error != "That backup file is corrupt or unreadable."
            if integrity_ok and version == 0 and _holds_no_rows(conn):
                conn.close()
                return RecoveryResult("empty", None, version, "database holds no data yet")
            conn.close()
            return RecoveryResult("damaged", None, version, f"database check failed: {check_error}")
        return RecoveryResult("ok", conn, version, "database recovered and checked")
    except sqlite3.DatabaseError as error:
        code = getattr(error, "sqlite_errorcode", 0) or 0
        name = getattr(error, "sqlite_errorname", None) or type(error).__name__
        if code & 0xFF in (11, 26):
            status, detail = "damaged", f"database is corrupt or not SQLite ({name})"
        else:
            status, detail = "unavailable", f"database could not be opened ({name})"
    except Exception as error:
        status, detail = "unavailable", f"database could not be opened ({type(error).__name__})"
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return RecoveryResult(status, None, None, detail)


def _remove_db_artifacts(path: str) -> list:
    """Remove a temporary database and its rollback journal, if present."""
    failures = []
    for candidate in (path, path + "-journal"):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
        except Exception as e:
            failures.append(f"{candidate}: {e}")
    return failures
