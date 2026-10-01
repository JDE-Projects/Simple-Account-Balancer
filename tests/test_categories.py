"""Category synchronization coverage for transactions and autopays."""
from test_rollback import _fresh_rows, _make_api


def _insert_autopay(conn, category):
    conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, next_post_date, pay_day, post_day, is_variable, created_at) "
        "VALUES (1, 'Rule', ?, '', -100, '2024-02-01', '2024-02-01', 1, 1, 0, '2024-01-01')",
        (category,),
    )
    conn.commit()


def _insert_transaction(conn, category):
    conn.execute(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key, created_at) "
        "VALUES (1, '2024-01-02', 'Entry', ?, '', -100, 0, 0, 1, '2024-01-01')",
        (category,),
    )
    conn.commit()


def _category_id(api, name):
    return api._conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()[0]


def test_get_categories_counts_transactions_and_case_insensitive_autopays(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    api._conn.executemany("INSERT INTO categories (name) VALUES (?)", [("Mixed",), ("Autopay Only",)])
    _insert_transaction(api._conn, "mixed")
    _insert_autopay(api._conn, "MIXED")
    _insert_autopay(api._conn, "autopay only")

    categories = {category["name"]: category for category in api.get_categories()["categories"]}

    assert categories["Mixed"]["used_count"] == 1
    assert categories["Mixed"]["autopay_count"] == 1
    assert categories["Autopay Only"]["used_count"] == 0
    assert categories["Autopay Only"]["autopay_count"] == 1


def test_rename_category_updates_transactions_and_autopays(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute("INSERT INTO categories (name) VALUES ('Old')")
    _insert_transaction(api._conn, "old")
    _insert_autopay(api._conn, "OLD")

    assert api.rename_category(_category_id(api, "Old"), "New")["ok"] is True
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("New",)]
    assert _fresh_rows(db_path, "SELECT category FROM autopays") == [("New",)]


def test_rename_category_merge_moves_transactions_and_autopays(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.executemany("INSERT INTO categories (name) VALUES (?)", [("Old",), ("Existing",)])
    _insert_transaction(api._conn, "old")
    _insert_autopay(api._conn, "OLD")

    assert api.rename_category(_category_id(api, "Old"), "existing")["ok"] is True
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name='Old'") == []
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("Existing",)]
    assert _fresh_rows(db_path, "SELECT category FROM autopays") == [("Existing",)]


def test_delete_category_reassigns_transactions_and_autopays(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.executemany("INSERT INTO categories (name) VALUES (?)", [("Old",), ("New",)])
    _insert_transaction(api._conn, "old")
    _insert_autopay(api._conn, "OLD")

    assert api.delete_category(_category_id(api, "Old"), "New")["ok"] is True
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("New",)]
    assert _fresh_rows(db_path, "SELECT category FROM autopays") == [("New",)]


def test_delete_category_keeps_transaction_label_and_clears_autopays(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute("INSERT INTO categories (name) VALUES ('Old')")
    _insert_transaction(api._conn, "old")
    _insert_autopay(api._conn, "OLD")

    assert api.delete_category(_category_id(api, "Old"))["ok"] is True
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("old",)]
    assert _fresh_rows(db_path, "SELECT category FROM autopays") == [("",)]


def test_failed_rename_rolls_back_autopay_change(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute("INSERT INTO categories (name) VALUES ('Old')")
    _insert_transaction(api._conn, "Old")
    _insert_autopay(api._conn, "Old")
    api._conn.execute(
        "CREATE TRIGGER abort_autopay_category_rename BEFORE UPDATE ON autopays "
        "WHEN OLD.category = 'Old' BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.rename_category(_category_id(api, "Old"), "New")["ok"] is False
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name IN ('Old', 'New')") == [("Old",)]
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("Old",)]
    assert _fresh_rows(db_path, "SELECT category FROM autopays") == [("Old",)]
