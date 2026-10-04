"""Autopay messages and operations."""

import datetime

from app import utils

_AUTOPAY_POST_FAILED_MESSAGE = (
    "Autopays couldn't be added to the register today. Nothing was posted, and "
    "the app will try again next launch."
)
_AUTOPAY_SAVED_POST_FAILED_MESSAGE = (
    "The autopay was saved, but payments already due couldn't be added. "
    "The app will try again next launch."
)

def get_autopays(api, account_id):
    """Autopay rules for the account, ordered by their post-day anchor
        then payee, so the list reads roughly in the order rules land in
        the register each month."""
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        cur = api._conn.cursor()
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
        api.log(f"get_autopays failed: {e}")
        return {"ok": False, "error": "Couldn't load the autopays."}


def add_autopay(api, account_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
    """Create a recurring autopay rule. post_date and pay_date are the
        first occurrence; their day numbers become the hidden post_day and
        pay_day anchors used to advance the rule each month. is_variable
        marks a rule whose amount changes month to month (cell phone, car
        insurance): postings from it arrive flagged as an estimate to confirm."""
    try:
        account = api._get_account(account_id)
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
        cur = api._conn.cursor()
        cur.execute(
            "INSERT INTO autopays "
            "(account_id, payee, category, notes, amount_cents, next_pay_date, "
            "next_post_date, pay_day, post_day, is_variable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account["id"], payee_s, category_s, notes_s, signed, pay_s, post_s, pay_day, post_day, is_variable_i, now),
        )
        if category_s:
            cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_s,))
        api._conn.commit()
        api.log(f"Added autopay, next post {post_s}, next pay {pay_s}")
        posted = 0
        post_failed = False
        try:
            post_result = api.post_due_autopays()
            if post_result is None:
                post_failed = True
            else:
                posted = post_result
        except Exception as e:
            api.log(f"post_due_autopays call failed: {e}")
            post_failed = True
        # This posting pass is triggered from the UI, not launch, so don't
        # leave a stale launch notice for the next startup to pick up.
        api._set_autopay_notice(None)
        result = api.get_autopays(account["id"])
        if result.get("ok"):
            result["posted"] = posted
            if post_failed:
                result["post_failed"] = True
                result["post_error"] = _AUTOPAY_SAVED_POST_FAILED_MESSAGE
        return result
    except Exception as e:
        api.log(f"add_autopay failed: {e}")
        return {"ok": False, "error": "Couldn't add the autopay."}


def update_autopay(api, autopay_id, payee, category, notes, amount, direction, post_date, pay_date, is_variable=0):
    """Edit an autopay rule. Re-anchoring post_day/pay_day from the newly
        chosen dates is the point of editing them, so both are recomputed.
        Toggling is_variable only affects postings this rule makes from now
        on; transactions it already posted keep whatever estimated flag they
        posted with."""
    try:
        cur = api._conn.cursor()
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
        api._conn.commit()
        api.log(f"Updated autopay {autopay_id}, next post {post_s}, next pay {pay_s}")
        posted = 0
        post_failed = False
        try:
            post_result = api.post_due_autopays()
            if post_result is None:
                post_failed = True
            else:
                posted = post_result
        except Exception as e:
            api.log(f"post_due_autopays call failed: {e}")
            post_failed = True
        # This posting pass is triggered from the UI, not launch, so don't
        # leave a stale launch notice for the next startup to pick up.
        api._set_autopay_notice(None)
        result = api.get_autopays(row["account_id"])
        if result.get("ok"):
            result["posted"] = posted
            if post_failed:
                result["post_failed"] = True
                result["post_error"] = _AUTOPAY_SAVED_POST_FAILED_MESSAGE
        return result
    except Exception as e:
        api.log(f"update_autopay failed: {e}")
        return {"ok": False, "error": "Couldn't update the autopay."}


def delete_autopay(api, autopay_id):
    try:
        cur = api._conn.cursor()
        row = cur.execute("SELECT id, account_id FROM autopays WHERE id=?", (autopay_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "That autopay no longer exists."}
        cur.execute("DELETE FROM autopays WHERE id=?", (autopay_id,))
        api._conn.commit()
        api.log(f"Deleted autopay {autopay_id}")
        return api.get_autopays(row["account_id"])
    except Exception as e:
        api.log(f"delete_autopay failed: {e}")
        return {"ok": False, "error": "Couldn't delete the autopay."}


def post_due_autopays(api):
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
        cur = api._conn.cursor()
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
                    api.log(
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
        api._conn.commit()
        if posted_count == 1:
            api._autopay_notice = "Added 1 autopay to the register."
        elif posted_count > 1:
            api._autopay_notice = f"Added {posted_count} autopays to the register."
        api._autopay_notice_is_error = False
        api.log(f"post_due_autopays: posted {posted_count} transaction(s)")
        return posted_count
    except Exception as e:
        api._conn.rollback()
        api.log(f"post_due_autopays failed: {e}")
        api._set_autopay_notice(_AUTOPAY_POST_FAILED_MESSAGE, is_error=True)
        return None

