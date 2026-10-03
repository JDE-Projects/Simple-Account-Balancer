"""Rollback and serialization coverage for Api database calls."""
import sqlite3
import threading

from app import db, paths
import simple_account_balancer as sab


def _make_api(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(sab, "effective_backup_dir", lambda: (str(backups), False))
    # Keep pref writes from account changes inside tmp_path, not the repo.
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    db_path = str(tmp_path / "live.db")
    api = sab.Api()
    api.set_conn(db.open_db(db_path))
    api.set_db_path(db_path)
    api._conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Checking', 0, '2024-01-01', '2024-01-01')"
    )
    api._conn.commit()
    return api, db_path, backups


def _fresh_rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _insert_transaction(conn, account_id=1, payee="Original"):
    conn.execute(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key, created_at) "
        "VALUES (?, '2024-01-02', ?, 'Old', '', -100, 0, 1, 1, '2024-01-01')",
        (account_id, payee),
    )
    conn.commit()


def test_account_failure_rolls_back_partial_delete_and_later_write_succeeds(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Savings', 0, '2024-01-01', '2024-01-01')"
    )
    _insert_transaction(api._conn, 2)
    api._conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, next_post_date, pay_day, post_day, is_variable, created_at) "
        "VALUES (2, 'Rent', '', '', -100, '2024-02-01', '2024-02-01', 1, 1, 0, '2024-01-01')"
    )
    api._conn.execute(
        "CREATE TRIGGER abort_autopay_delete BEFORE DELETE ON autopays "
        "WHEN OLD.account_id = 2 BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.delete_account(2)["ok"] is False
    assert api.add_category("After failure")["ok"] is True
    assert _fresh_rows(db_path, "SELECT name FROM accounts ORDER BY id") == [("Checking",), ("Savings",)]
    assert _fresh_rows(db_path, "SELECT account_id FROM transactions") == [(2,)]
    assert _fresh_rows(db_path, "SELECT account_id FROM autopays") == [(2,)]
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name='After failure'") == [("After failure",)]


def test_category_failure_rolls_back_rename(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute("INSERT INTO categories (name) VALUES ('Old')")
    _insert_transaction(api._conn)
    api._conn.execute(
        "CREATE TRIGGER abort_category_rename BEFORE UPDATE ON transactions "
        "WHEN OLD.category = 'Old' BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.rename_category(20, "New")["ok"] is False
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name IN ('Old', 'New')") == [("Old",)]
    assert _fresh_rows(db_path, "SELECT category FROM transactions") == [("Old",)]


def test_transaction_failure_rolls_back_insert(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute(
        "CREATE TRIGGER abort_transaction_sort BEFORE UPDATE ON transactions "
        "WHEN NEW.sort_key = NEW.id BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.add_transaction(1, "2024-01-02", "Failure", "", "", "1", "deposit")["ok"] is False
    assert _fresh_rows(db_path, "SELECT payee FROM transactions") == []


def test_reorder_failure_rolls_back_earlier_positions(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    _insert_transaction(api._conn, payee="One")
    _insert_transaction(api._conn, payee="Two")
    api._conn.execute(
        "CREATE TRIGGER abort_second_reorder BEFORE UPDATE ON transactions "
        "WHEN OLD.id = 2 BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.reorder_transactions(1, "2024-01-02", [2, 1])["ok"] is False
    assert _fresh_rows(db_path, "SELECT id, sort_key FROM transactions ORDER BY id") == [(1, 1), (2, 1)]


def test_estimate_failure_leaves_transaction_unchanged(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    _insert_transaction(api._conn)
    api._conn.execute(
        "CREATE TRIGGER abort_estimate BEFORE UPDATE ON transactions "
        "WHEN OLD.id = 1 BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.confirm_estimated_amount(1, "2")["ok"] is False
    assert _fresh_rows(db_path, "SELECT amount_cents, estimated FROM transactions") == [(-100, 1)]


def test_autopay_failure_rolls_back_rule_insert(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute(
        "CREATE TRIGGER abort_autopay_category BEFORE INSERT ON categories "
        "WHEN NEW.name = 'Injected' BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    result = api.add_autopay(
        1, "Failure", "Injected", "", "1", "withdraw", "2999-01-01", "2999-01-01"
    )

    assert result["ok"] is False
    assert _fresh_rows(db_path, "SELECT payee FROM autopays") == []
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name='Injected'") == []


def test_outermost_guard_rolls_back_early_return_after_a_write(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)

    def write_then_reject(self):
        self._conn.execute("INSERT INTO categories (name) VALUES ('Uncommitted')")
        return {"ok": False, "error": "Validation rejected this write."}

    result = sab._database_call(write_then_reject)(api)

    assert result == {"ok": False, "error": "Validation rejected this write."}
    assert _fresh_rows(db_path, "SELECT name FROM categories WHERE name='Uncommitted'") == []


def test_nested_get_config_does_not_rollback_successful_create(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)

    result = api.create_account("Saved", "12.34", "2024-01-01")

    assert result["ok"] is True
    assert _fresh_rows(db_path, "SELECT name, starting_balance_cents FROM accounts WHERE name='Saved'") == [("Saved", 1234)]


class _RollbackFailingConnection:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    def rollback(self):
        raise sqlite3.OperationalError("rollback failed")

    def close(self):
        self.closed = True
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_rollback_failure_closes_connection_and_blocks_later_calls(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    api._conn.execute(
        "CREATE TRIGGER abort_transaction_sort BEFORE UPDATE ON transactions "
        "WHEN NEW.sort_key = NEW.id BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()
    broken = _RollbackFailingConnection(api._conn)
    api._conn = broken

    assert api.add_transaction(1, "2024-01-02", "Failure", "", "", "1", "deposit")["ok"] is False
    assert broken.closed is True
    assert api._conn is None
    assert api.get_config() == {"ok": False, "error": sab._DATABASE_FAILURE_MESSAGE}
    assert _fresh_rows(db_path, "SELECT payee FROM transactions") == []


def _blocking_write(api, entered, release, name):
    def write(self):
        self._conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        self._conn.commit()
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    return sab._database_call(write)(api)


def test_database_calls_serialize_writes_restore_and_close(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = backups / "balancer_20240101_000000.db"
    destination = sqlite3.connect(backup_path)
    try:
        api._conn.backup(destination)
    finally:
        destination.close()

    for index, action in enumerate((
        lambda: api.add_category("Second writer"),
        lambda: api.restore_backup(backup_path.name),
        api.close_conn,
    )):
        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        errors = []

        def run(callable_, errors=errors, done=second_done):
            try:
                callable_()
            except BaseException as e:
                errors.append(e)
            finally:
                done.set()

        def write_call(entered=entered, release=release, index=index):
            return _blocking_write(api, entered, release, f"Blocked writer {index}")

        writer = threading.Thread(
            target=run,
            args=(write_call,),
        )
        writer.start()
        assert entered.wait(3)
        second = threading.Thread(target=run, args=(action,))
        second.start()
        assert not second_done.wait(0.2)
        release.set()
        writer.join(3)
        second.join(3)
        assert not writer.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert _fresh_rows(db_path, "PRAGMA integrity_check") == [("ok",)]
        if api._conn is None:
            api.set_conn(db.open_db(db_path))


def test_export_releases_database_lock_before_opening_dialog(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    completed = threading.Event()

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            thread = threading.Thread(target=lambda: (api.get_config(), completed.set()))
            thread.start()
            assert completed.wait(3)
            thread.join(3)

    api.set_window(Window())

    assert api.export_csv(1, "", "") == {"ok": True, "cancelled": True}


def test_every_decorated_bridge_method_keeps_its_happy_path_return_shape(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return None

    api.set_window(Window())
    assert api.set_conn(api._conn) is None
    assert api.get_config()["ok"] is True
    assert api.create_account("Savings", "0", "2024-01-01")["ok"] is True
    assert api.set_active_account(1)["ok"] is True
    assert api.update_account(2, "Savings", "1", "2024-01-01")["ok"] is True
    assert api.get_categories()["ok"] is True
    assert api.add_category("Temporary")["ok"] is True
    category_id = api._conn.execute("SELECT id FROM categories WHERE name='Temporary'").fetchone()[0]
    assert api.rename_category(category_id, "Renamed")["ok"] is True
    assert api.delete_category(category_id)["ok"] is True
    assert api.get_payees(1)["ok"] is True
    assert api.get_transactions(1, "2024-01-01")["ok"] is True
    assert api.add_transaction(1, "2024-01-02", "One", "", "", "1", "deposit")["ok"] is True
    transaction_id = api._conn.execute("SELECT id FROM transactions WHERE payee='One'").fetchone()[0]
    assert api.update_transaction(transaction_id, "2024-01-02", "One", "", "", "2", "deposit")["ok"] is True
    assert api.delete_transaction(transaction_id)["ok"] is True
    assert api.add_transaction(1, "2024-01-02", "One", "", "", "1", "deposit")["ok"] is True
    assert api.add_transaction(1, "2024-01-02", "Two", "", "", "1", "deposit")["ok"] is True
    transaction_ids = [row[0] for row in api._conn.execute("SELECT id FROM transactions ORDER BY id")]
    assert api.reorder_transactions(1, "2024-01-02", transaction_ids[::-1])["ok"] is True
    api._conn.execute("UPDATE transactions SET estimated=1 WHERE id=?", (transaction_ids[0],))
    api._conn.commit()
    assert api.confirm_estimated_amount(transaction_ids[0], "3")["ok"] is True
    assert api.get_autopays(1)["ok"] is True
    assert api.add_autopay(1, "Rule", "", "", "1", "withdraw", "2999-01-01", "2999-01-01")["ok"] is True
    autopay_id = api._conn.execute("SELECT id FROM autopays WHERE payee='Rule'").fetchone()[0]
    assert api.update_autopay(autopay_id, "Rule", "", "", "2", "withdraw", "2999-01-01", "2999-01-01")["ok"] is True
    assert api.delete_autopay(autopay_id)["ok"] is True
    assert isinstance(api.post_due_autopays(), int)
    assert api.get_compare_data(1, "2024-01-01")["ok"] is True
    assert api.find_compare_matches(1, "1", "2024-01-01")["ok"] is True
    assert api.export_csv(1, "", "") == {"ok": True, "cancelled": True}
    backup = backups / "balancer_20240101_000000.db"
    destination = sqlite3.connect(backup)
    try:
        api._conn.backup(destination)
    finally:
        destination.close()
    assert api.preview_restore(backup.name)["ok"] is True
    assert api.restore_backup(backup.name)["ok"] is True
    assert api.close_conn() is True


# Bridge methods that never reach the database. Every other public Api method
# must carry @_database_call so it runs under the lock.
_NO_DATABASE_METHODS = {
    "set_window", "set_db_path", "export_csv", "save_theme",
    "choose_backup_folder", "reset_backup_folder", "set_backup_keep",
    "list_backups", "open_url", "check_update", "set_debug", "log",
}


def test_every_database_method_holds_the_lock():
    public = [
        name for name, value in vars(sab.Api).items()
        if callable(value) and not name.startswith("_")
    ]
    unlocked = [
        name for name in public
        if name not in _NO_DATABASE_METHODS and not hasattr(getattr(sab.Api, name), "__wrapped__")
    ]
    assert unlocked == []
    assert hasattr(sab.Api._export_csv_snapshot, "__wrapped__")
