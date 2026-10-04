"""Compare operations."""

import datetime
import itertools

from app import utils

def get_compare_data(api, account_id=None, from_date=None, to_date=None):
    """Return read-only Compare rows and the full register balance.

        Rows are newest first and default to the last 90 days. The legacy
        ``cleared`` database column deliberately remains out of this API: found
        marks are maintained by the browser for the current app session only.
        """
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        cur = api._conn.cursor()
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
            "register_balance_cents": api._current_balance_cents(account),
        }
    except Exception as e:
        api.log(f"get_compare_data failed: {e}")
        return {"ok": False, "error": "Couldn't load the compare data."}


def find_compare_matches(api, account_id=None, amount=None, from_date=None, to_date=None):
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
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        cur = api._conn.cursor()
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
        api.log(
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
        api.log(f"find_compare_matches failed: {e}")
        return {"ok": False, "error": "Couldn't search for the comparison."}
