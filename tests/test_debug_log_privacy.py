"""First-line privacy checks for debug-log call sites."""

import sqlite3

from app import db, utils
from app.api import Api
from app.debug_log import DebugLog
from app.services import restore


MARKER = "Zq7Marker"


def _make_api(tmp_path):
    api = Api("test")
    api._debug_log = DebugLog(str(tmp_path), "test", redact=lambda text: text)
    api.set_db_path(str(tmp_path / "live.db"))
    api.set_conn(db.open_db(api._db_path))
    assert api.set_debug(True) == {"ok": True, "enabled": True}
    return api


def _log_text(tmp_path):
    return "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("Debug_Log_*.txt"))


def test_describe_error_sqlite_keeps_code_drops_message():
    try:
        sqlite3.connect(":memory:").execute("SELEC MARKERWORD")
    except sqlite3.Error as error:
        description = utils.describe_error(error)

    assert description.startswith("OperationalError")
    assert "MARKERWORD" not in description
    assert "SELEC" not in description


def test_describe_error_oserror_drops_path():
    error = PermissionError(13, "Permission denied", r"C:\Users\Me\MARKERWORD budget.db")

    assert utils.describe_error(error) == "PermissionError: Permission denied"


def test_describe_error_other_errors_give_type_only():
    assert utils.describe_error(ValueError("could not convert string to float: 'MARKERWORD'")) == "ValueError"


def test_log_is_not_exposed_to_the_page():
    assert not hasattr(Api, "log")
    assert callable(Api._log)


def test_typed_values_never_reach_log_on_success(tmp_path):
    api = _make_api(tmp_path)
    try:
        assert api.create_account(f"{MARKER} account", "10.00", "2024-01-01")["ok"] is True
        account_id = api._conn.execute("SELECT id FROM accounts").fetchone()[0]
        assert api.update_account(account_id, f"{MARKER} account updated", "12.00", "2024-01-02")["ok"] is True

        assert api.add_transaction(
            account_id, "2024-01-03", f"{MARKER} payee", f"{MARKER} category", f"{MARKER} notes", "1.00", "withdraw",
        )["ok"] is True
        transaction_id = api._conn.execute("SELECT id FROM transactions").fetchone()[0]
        assert api.update_transaction(
            transaction_id, "2024-01-04", f"{MARKER} payee updated", f"{MARKER} category updated", f"{MARKER} notes updated", "2.00", "deposit",
        )["ok"] is True

        assert api.add_category(f"{MARKER} extra category")["ok"] is True
        category_id = api._conn.execute("SELECT id FROM categories WHERE name=?", (f"{MARKER} extra category",)).fetchone()[0]
        assert api.rename_category(category_id, f"{MARKER} renamed category")["ok"] is True

        assert api.add_autopay(
            account_id, f"{MARKER} autopay payee", f"{MARKER} autopay category", f"{MARKER} autopay notes",
            "3.00", "withdraw", "2024-02-01", "2024-02-02",
        )["ok"] is True
        autopay_id = api._conn.execute("SELECT id FROM autopays").fetchone()[0]
        assert api.update_autopay(
            autopay_id, f"{MARKER} autopay payee updated", f"{MARKER} autopay category updated", f"{MARKER} autopay notes updated",
            "4.00", "deposit", "2024-02-03", "2024-02-04",
        )["ok"] is True
        merge_target = api._conn.execute("SELECT name FROM categories WHERE id<>? LIMIT 1", (category_id,)).fetchone()[0]
        assert api.delete_category(category_id, merge_target)["ok"] is True

        text = _log_text(tmp_path)
        assert MARKER not in text
        assert "Added transaction dated" in text
    finally:
        api.close_conn()


def test_typed_values_never_reach_log_on_failure(tmp_path):
    class FailingConnection:
        # Every database call raises an error whose text carries the marker,
        # so each call reaches its except branch with typed text in hand.
        in_transaction = False

        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise sqlite3.OperationalError(f"near '{MARKER}': forced failure")
            return fail

    api = _make_api(tmp_path)
    connection = api._conn
    api._conn = FailingConnection()
    calls = [
        (api.add_transaction, (1, "2024-01-01", f"{MARKER} payee", f"{MARKER} category", f"{MARKER} notes", "1.00", "withdraw"), "add_transaction failed"),
        (api.update_transaction, (1, "2024-01-01", f"{MARKER} payee", f"{MARKER} category", f"{MARKER} notes", "1.00", "withdraw"), "update_transaction failed"),
        (api.create_account, (f"{MARKER} account", "1.00", "2024-01-01"), "create_account failed"),
        (api.update_account, (1, f"{MARKER} account", "1.00", "2024-01-01"), "update_account failed"),
        (api.add_category, (f"{MARKER} category",), "add_category failed"),
        (api.rename_category, (1, f"{MARKER} category",), "rename_category failed"),
        (api.add_autopay, (1, f"{MARKER} payee", f"{MARKER} category", f"{MARKER} notes", "1.00", "withdraw", "2024-01-01", "2024-01-02"), "add_autopay failed"),
        (api.update_autopay, (1, f"{MARKER} payee", f"{MARKER} category", f"{MARKER} notes", "1.00", "withdraw", "2024-01-01", "2024-01-02"), "update_autopay failed"),
    ]
    for method, args, _line in calls:
        assert method(*args)["ok"] is False

    text = _log_text(tmp_path)
    assert MARKER not in text
    for _method, _args, line in calls:
        assert f"{line}: OperationalError" in text
    connection.close()


def test_launch_restore_skip_lines_have_no_paths(tmp_path, monkeypatch):
    api = _make_api(tmp_path)
    unreadable = tmp_path / f"{MARKER} unreadable folder"
    backup_folder = tmp_path / f"{MARKER} backup folder"
    backup_folder.mkdir()
    (backup_folder / "balancer_20240101_000000.db").write_bytes(b"not a database")
    original_listdir = restore.os.listdir

    def listdir(path):
        if str(path) == str(unreadable):
            raise PermissionError(13, "Permission denied", str(unreadable))
        return original_listdir(path)

    monkeypatch.setattr(restore.os, "listdir", listdir)
    try:
        assert restore._find_launch_restore_backup(api, [str(unreadable), str(backup_folder)]) == (None, None, 0)
        text = _log_text(tmp_path)
        assert MARKER not in text
        assert "Launch restore skipped an unreadable backup folder" in text
        assert "Launch restore skipped unusable backup balancer_20240101_000000.db" in text
    finally:
        api.close_conn()
