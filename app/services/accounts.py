"""Account and configuration operations."""

import datetime

from app import prefs, utils
from app.services import backup

def _get_account(api, account_id=None):
    cur = api._conn.cursor()
    if account_id is None:
        return cur.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
    return cur.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def _current_balance_cents(api, account) -> int:
    cur = api._conn.cursor()
    total = cur.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0]
    return account["starting_balance_cents"] + total


def _today_balance_cents(api, account) -> int:
    """Balance counting only transactions dated today or earlier, unlike
        current_balance_cents which counts the full register including any
        future-dated rows."""
    cur = api._conn.cursor()
    today = datetime.date.today().isoformat()
    total = cur.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id=? AND date<=?",
        (account["id"], today),
    ).fetchone()[0]
    return account["starting_balance_cents"] + total


def _account_payload(api, account):
    tx_count = api._conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?", (account["id"],)
    ).fetchone()[0]
    return {
        "id": account["id"],
        "name": account["name"],
        "starting_balance_cents": account["starting_balance_cents"],
        "starting_date": account["starting_date"],
        "current_balance_cents": api._current_balance_cents(account),
        "today_balance_cents": api._today_balance_cents(account),
        "transaction_count": tx_count,
        "starting_balance_prev_cents": account["starting_balance_prev_cents"],
        "starting_balance_changed_at": account["starting_balance_changed_at"],
    }


def get_config(api):
    """Initial payload the UI loads on startup."""
    try:
        cur = api._conn.cursor()
        account_rows = cur.execute(
            "SELECT id, name FROM accounts ORDER BY name COLLATE NOCASE"
        ).fetchall()
        accounts = [{"id": r["id"], "name": r["name"]} for r in account_rows]
        prefs_data = prefs.load_prefs()
        active_id = prefs_data.get("active_account_id")
        account = None
        if accounts:
            if active_id is not None:
                account = api._get_account(active_id)
            if account is None:
                account = api._get_account(accounts[0]["id"])
        backup_dir, backup_is_custom = backup.effective_backup_dir()
        return {
            "ok": True,
            "version": api._version,
            "theme": api._load_theme(),
            "has_account": account is not None,
            "account": api._account_payload(account) if account is not None else None,
            "accounts": accounts,
            "backup_folder": backup_dir,
            "backup_folder_is_custom": backup_is_custom,
            "backup_keep": backup._clamp_backup_keep(prefs_data.get("backup_keep")),
            "backup_notice": api.backup_notice,
            "autopay_notice": api.autopay_notice,
            "autopay_notice_is_error": api.autopay_notice_is_error,
        }
    except Exception as e:
        api.log(f"get_config failed: {e}")
        return {"ok": False, "error": "Couldn't load the app's configuration."}


def create_account(api, name, starting_balance, starting_date):
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
        cur = api._conn.cursor()
        cur.execute(
            "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name_s, cents, date_s, now),
        )
        new_id = cur.lastrowid
        api._conn.commit()
        prefs_data = prefs.load_prefs()
        prefs_data["active_account_id"] = new_id
        if not prefs.save_prefs(prefs_data):
            api.log("Could not save active account pref")
        api.log(f"Account {new_id} created, starting as of {date_s}")
        return api.get_config()
    except Exception as e:
        api.log(f"create_account failed: {e}")
        return {"ok": False, "error": "Couldn't create the account."}


def set_active_account(api, account_id):
    """Switch which account the UI shows and operates on."""
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "That account no longer exists."}
        prefs_data = prefs.load_prefs()
        prefs_data["active_account_id"] = account["id"]
        if not prefs.save_prefs(prefs_data):
            api.log("Could not save active account pref")
        api.log(f"Active account set to {account['id']}")
        return api.get_config()
    except Exception as e:
        api.log(f"set_active_account failed: {e}")
        return {"ok": False, "error": "Couldn't switch accounts."}


def delete_account(api, account_id):
    """Delete an account and its transactions and autopays. Refuses to
        delete the only account. If the deleted account was active, the
        active pref moves to the first remaining account."""
    try:
        cur = api._conn.cursor()
        account = api._get_account(account_id)
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
        api._conn.commit()
        prefs_data = prefs.load_prefs()
        if prefs_data.get("active_account_id") == account["id"]:
            remaining = cur.execute(
                "SELECT id FROM accounts ORDER BY name COLLATE NOCASE LIMIT 1"
            ).fetchone()
            prefs_data["active_account_id"] = remaining["id"] if remaining else None
            if not prefs.save_prefs(prefs_data):
                api.log("Could not save active account pref")
        api.log(f"Account {account['id']} deleted ({tx_count} transactions removed)")
        return api.get_config()
    except Exception as e:
        api.log(f"delete_account failed: {e}")
        return {"ok": False, "error": "Couldn't delete the account."}


def update_account(api, account_id, name, starting_balance, starting_date):
    """Edit account name / starting balance / starting date. Recalcs the register."""
    try:
        account = api._get_account(account_id)
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
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Account {account['id']} updated, starting as of {date_s}")
        return api.get_config()
    except Exception as e:
        api.log(f"update_account failed: {e}")
        return {"ok": False, "error": "Couldn't update the account."}

