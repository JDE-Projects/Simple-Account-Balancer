"""Transaction operations."""

import datetime

from app import config, utils

def _rows_with_balance(api, account) -> list:
    """Full-history transaction rows, oldest first, each carrying the
        true rolling register balance. Shared by get_transactions (which
        filters to a visible range) and export_csv (which does the same)."""
    cur = api._conn.cursor()
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


def get_transactions(api, account_id=None, from_date=None, to_date=None, search=""):
    """Rows for the given date range and search text, with balances computed
        over the FULL history so the first visible row's balance is correct.
        The balance column always reflects the true register, never a filtered
        sum. With no from_date, falls back to the last 30 days."""
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        computed = api._rows_with_balance(account)
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
        api.log(f"get_transactions failed: {e}")
        return {"ok": False, "error": "Couldn't load the transactions."}


def add_transaction(api, account_id, date, payee, category, notes, amount, direction):
    try:
        account = api._get_account(account_id)
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
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Added transaction dated {date_s}")
        return {"ok": True}
    except Exception as e:
        api.log(f"add_transaction failed: {e}")
        return {"ok": False, "error": "Couldn't add the transaction."}


def update_transaction(api, transaction_id, date, payee, category, notes, amount, direction):
    try:
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Updated transaction {transaction_id}, dated {date_s}")
        return {"ok": True}
    except Exception as e:
        api.log(f"update_transaction failed: {e}")
        return {"ok": False, "error": "Couldn't update the transaction."}


def delete_transaction(api, transaction_id):
    try:
        cur = api._conn.cursor()
        row = cur.execute("SELECT id FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "That transaction no longer exists."}
        cur.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
        api._conn.commit()
        api.log(f"Deleted transaction {transaction_id}")
        return {"ok": True}
    except Exception as e:
        api.log(f"delete_transaction failed: {e}")
        return {"ok": False, "error": "Couldn't delete the transaction."}


def reorder_transactions(api, account_id, date, ordered_ids):
    """Set the within-day display order for one date's transactions. The
        caller supplies the full set of that day's ids in the new order;
        sort_key values 1..n never collide across days since date sorts
        first, so this never needs to touch any other day's rows."""
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        date_s, err = utils.parse_iso_date(date)
        if err:
            return {"ok": False, "error": err}
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Reordered {len(ordered)} transaction(s) on {date_s}")
        return {"ok": True}
    except Exception as e:
        api.log(f"reorder_transactions failed: {e}")
        return {"ok": False, "error": "Couldn't reorder the transactions."}


def confirm_estimated_amount(api, transaction_id, amount):
    """Correct an estimated autopay posting's amount. Only the amount and
        the estimated flag change: the row's existing sign (withdraw stays
        negative, deposit stays positive) is preserved, and cleared status,
        the rule, and every other field are left untouched."""
    try:
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Confirmed estimated amount for transaction {transaction_id}")
        return {"ok": True}
    except Exception as e:
        api.log(f"confirm_estimated_amount failed: {e}")
        return {"ok": False, "error": "Couldn't confirm the amount."}

