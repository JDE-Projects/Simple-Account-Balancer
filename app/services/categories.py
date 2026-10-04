"""Category and payee operations."""

def get_categories(api):
    """Name-sorted category list with usage counts (case-insensitive)."""
    try:
        cur = api._conn.cursor()
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
        api.log(f"get_categories failed: {e}")
        return {"ok": False, "error": "Couldn't load the categories."}


def add_category(api, name):
    try:
        name_s = (name or "").strip()
        if not name_s:
            return {"ok": False, "error": "Category name is required."}
        cur = api._conn.cursor()
        cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name_s,))
        api._conn.commit()
        api.log("Category added")
        return api.get_categories()
    except Exception as e:
        api.log(f"add_category failed: {e}")
        return {"ok": False, "error": "Couldn't add the category."}


def rename_category(api, category_id, new_name):
    """Rename a category and carry the change over to past transactions and autopays.
        If the new name collides with another existing category, the two are
        merged: transactions and autopays move to the existing category and this row goes away."""
    try:
        cur = api._conn.cursor()
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
            api._conn.commit()
            api.log(f"Category {category_id} merged into category {merge_target['id']}")
            return api.get_categories()
        cur.execute("UPDATE categories SET name=? WHERE id=?", (new_name_s, category_id))
        cur.execute(
            "UPDATE transactions SET category=? WHERE category=? COLLATE NOCASE",
            (new_name_s, old_name),
        )
        cur.execute(
            "UPDATE autopays SET category=? WHERE category=? COLLATE NOCASE",
            (new_name_s, old_name),
        )
        api._conn.commit()
        api.log(f"Category {category_id} renamed")
        return api.get_categories()
    except Exception as e:
        api.log(f"rename_category failed: {e}")
        return {"ok": False, "error": "Couldn't rename the category."}


def delete_category(api, category_id, reassign_to=None):
    """Delete a category. With reassign_to, transactions and autopays move
        to that category. Otherwise past transactions keep the label and
        autopays become uncategorized."""
    try:
        cur = api._conn.cursor()
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
        api._conn.commit()
        detail = " (transactions reassigned)" if reassign_s else ""
        api.log(f"Category {category_id} deleted{detail}")
        return api.get_categories()
    except Exception as e:
        api.log(f"delete_category failed: {e}")
        return {"ok": False, "error": "Couldn't delete the category."}


def get_payees(api, account_id=None):
    """Distinct payees for the account, most-recent-first, each carrying
        the category from its most recent transaction (max date, then max id)."""
    try:
        account = api._get_account(account_id)
        if account is None:
            return {"ok": False, "error": "No account exists yet."}
        cur = api._conn.cursor()
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
        api.log(f"get_payees failed: {e}")
        return {"ok": False, "error": "Couldn't load the payees."}

