"""Restore safety checks. Every database and backup lives under tmp_path."""
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from app import config, db
import app.api as sab
from app.services import backup, restore


BACKUP_NAME = "balancer_20240101_000000.db"


def _account_names(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT name FROM accounts ORDER BY id")]
    finally:
        conn.close()


def _insert_account(conn, name):
    conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES (?, 0, '2024-01-01', '2024-01-01T00:00:00')",
        (name,),
    )
    conn.commit()


def _make_api(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(backup, "effective_backup_dir", lambda: (str(backups), False))
    db_path = str(tmp_path / "live.db")
    api = sab.Api("test")
    api.set_conn(db.open_db(db_path))
    api.set_db_path(db_path)
    _insert_account(api._conn, "Live")
    return api, db_path, backups


def _make_empty_api(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    api._conn.execute("DELETE FROM accounts")
    api._conn.commit()
    return api, db_path, backups


def _backup_from_conn(conn, path):
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def _backup_with_account(api, backups, name="Backup"):
    backup_path = backups / BACKUP_NAME
    _backup_from_conn(api._conn, str(backup_path))
    conn = sqlite3.connect(backup_path)
    try:
        _insert_account(conn, name)
    finally:
        conn.close()
    return backup_path


def _insert_transaction(conn, account_id=1, payee="Original", estimated=0, sort_key=1):
    conn.execute(
        "INSERT INTO transactions "
        "(account_id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key, created_at) "
        "VALUES (?, '2024-01-02', ?, 'Food', 'note', 1234, 0, ?, ?, '2024-01-02T00:00:00')",
        (account_id, payee, estimated, sort_key),
    )
    conn.commit()


def _insert_autopay(conn, account_id=1, payee="Rent", is_variable=0):
    conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, next_post_date, "
        "pay_day, post_day, is_variable, created_at) "
        "VALUES (?, ?, 'Home', 'note', -100000, '2024-02-01', '2024-02-01', 1, 1, ?, '2024-01-01T00:00:00')",
        (account_id, payee, is_variable),
    )
    conn.commit()


def _preview(api):
    result = api.preview_restore(BACKUP_NAME)
    assert result["ok"] is True
    return result


def _assert_no_restore_temps(tmp_path):
    assert not [
        path for path in tmp_path.iterdir()
        if path.name.startswith(".balancer_restore_") or path.name.endswith("-journal")
    ]


class _RestoreWindow:
    def __init__(self, path):
        self.path = path

    def create_file_dialog(self, *_args, **_kwargs):
        return str(self.path)


def _choose_restore_file(api, path):
    api.set_window(_RestoreWindow(path))
    return api.choose_restore_file()


def _outside_backup(tmp_path, api, name="SAB_Backup_01012026.db"):
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    path = outside / name
    _backup_from_conn(api._conn, str(path))
    return path


def _outside_backup_with_restore_data(tmp_path, api):
    path = _outside_backup(tmp_path, api)
    conn = sqlite3.connect(path)
    try:
        _insert_account(conn, "Backup One")
        _insert_account(conn, "Backup Two")
        account_id = conn.execute("SELECT id FROM accounts WHERE name='Backup One'").fetchone()[0]
        conn.execute("INSERT INTO categories (name) VALUES ('Backup category')")
        _insert_transaction(conn, account_id=account_id, payee="Backup transaction")
        _insert_autopay(conn, account_id=account_id, payee="Backup autopay")
    finally:
        conn.close()
    return path


def _make_versioned_backup(path, version):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "starting_balance_cents INTEGER NOT NULL, starting_date TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
            "date TEXT NOT NULL, payee TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', "
            "notes TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL, "
            "cleared INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )
        if version >= 1:
            conn.execute("ALTER TABLE accounts ADD COLUMN starting_balance_prev_cents INTEGER")
            conn.execute("ALTER TABLE accounts ADD COLUMN starting_balance_changed_at TEXT")
            conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        if version >= 2:
            conn.execute("ALTER TABLE transactions ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE TABLE autopays (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
                "payee TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', "
                "amount_cents INTEGER NOT NULL, next_pay_date TEXT NOT NULL, "
                "next_post_date TEXT NOT NULL, pay_day INTEGER NOT NULL, post_day INTEGER NOT NULL, "
                "is_variable INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
            )
        conn.execute(
            "INSERT INTO accounts (id, name, starting_balance_cents, starting_date, created_at) "
            "VALUES (1, ?, 0, '2024-01-01', '2024-01-01T00:00:00')",
            (f"Version {version}",),
        )
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_restore_round_trip_uses_backup_data(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert _account_names(db_path) == ["Live", "Backup"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


@pytest.mark.parametrize("kind", ["garbage", "truncated"])
def test_restore_refuses_corrupt_backup_without_touching_live(tmp_path, monkeypatch, kind):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = backups / BACKUP_NAME
    if kind == "garbage":
        backup_path.write_bytes(b"not a sqlite database")
    else:
        _backup_from_conn(api._conn, str(backup_path))
        data = backup_path.read_bytes()
        backup_path.write_bytes(data[:len(data) // 2])

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_refuses_backup_missing_a_required_table(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = backups / BACKUP_NAME
    _backup_from_conn(api._conn, str(backup_path))
    conn = sqlite3.connect(backup_path)
    conn.execute("DROP TABLE categories")
    conn.commit()
    conn.close()

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert "categories" in result["error"]
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_refuses_backup_missing_a_required_column(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = backups / BACKUP_NAME
    conn = sqlite3.connect(backup_path)
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert "accounts" in result["error"]
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_restore_upgrades_each_older_schema_version(tmp_path, monkeypatch, version):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _make_versioned_backup(backups / BACKUP_NAME, version)

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert _account_names(db_path) == [f"Version {version}"]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == config.SCHEMA_VERSION
    finally:
        conn.close()
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_refuses_version_three_backup_missing_sort_key(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _make_versioned_backup(backups / BACKUP_NAME, 2)
    conn = sqlite3.connect(backups / BACKUP_NAME)
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert "transactions.sort_key" in result["error"]
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_refuses_foreign_key_violation(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = backups / BACKUP_NAME
    _backup_from_conn(api._conn, str(backup_path))
    conn = sqlite3.connect(backup_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO transactions (account_id, date, payee, category, notes, amount_cents, cleared, "
        "estimated, sort_key, created_at) VALUES (999, '2024-01-01', 'x', '', '', 1, 0, 0, 1, '2024-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_recovers_when_replace_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_replace = restore.os.replace

    def fail_stage_replace(src, dst):
        if os.path.basename(src).startswith(".balancer_restore_stage_"):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", fail_stage_replace)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def _fail_live_reopens(monkeypatch, db_path):
    real_open_db = db.open_db

    def fail_live_reopens(path):
        conn = real_open_db(path)
        if path == db_path:
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(db, "open_db", fail_live_reopens)


@pytest.mark.parametrize("reopen_fails", [False, True])
def test_restore_journal_cancel_says_restart_only_when_reopen_fails(tmp_path, monkeypatch, reopen_fails):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    journal = Path(db_path + "-journal")
    real_close_conn = api.close_conn

    def close_then_leave_journal():
        closed = real_close_conn()
        journal.write_bytes(b"")
        return closed

    monkeypatch.setattr(api, "close_conn", close_then_leave_journal)
    if reopen_fails:
        _fail_live_reopens(monkeypatch, db_path)
    result = api.restore_backup(BACKUP_NAME)

    expected = "The live database has an unfinished journal, so the restore was cancelled."
    if reopen_fails:
        expected += " Close the app and open it again."
    assert result["ok"] is False
    assert result["error"] == expected
    assert (api._conn is None) is reopen_fails
    real_close_conn()
    journal.unlink(missing_ok=True)
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)


@pytest.mark.parametrize("reopen_fails", [False, True])
def test_restore_replace_failure_says_restart_only_when_reopen_fails(tmp_path, monkeypatch, reopen_fails):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_replace = restore.os.replace

    def fail_stage_replace(src, dst):
        if os.path.basename(src).startswith(".balancer_restore_stage_"):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", fail_stage_replace)
    if reopen_fails:
        _fail_live_reopens(monkeypatch, db_path)
    result = api.restore_backup(BACKUP_NAME)

    expected = "Couldn't put the backup into place. Nothing was changed."
    if reopen_fails:
        expected += " Close the app and open it again."
    assert result["ok"] is False
    assert result["error"] == expected
    assert (api._conn is None) is reopen_fails
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_rolls_back_when_reopen_after_replace_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = db.open_db
    failed = False

    def fail_once_after_replacement(path):
        nonlocal failed
        conn = real_open_db(path)
        if path == db_path and not failed:
            failed = True
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(db, "open_db", fail_once_after_replacement)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert result["error"] == "The restore failed and your data was put back."
    assert _account_names(db_path) == ["Live"]
    assert api._conn is not None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_reports_restart_when_reopen_after_rollback_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = db.open_db

    def fail_live_reopens(path):
        conn = real_open_db(path)
        if path == db_path:
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(db, "open_db", fail_live_reopens)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert result["error"] == (
        "The restore failed and your data was put back. Close the app and open it again."
    )
    assert api._conn is None
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)


def test_restore_reports_manual_recovery_when_rollback_replace_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = db.open_db
    real_replace = restore.os.replace
    failed_open = False

    def fail_open_after_replacement(path):
        nonlocal failed_open
        conn = real_open_db(path)
        if path == db_path and not failed_open:
            failed_open = True
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    def fail_rollback_replace(src, dst):
        if os.path.basename(src).startswith(".balancer_restore_rollback_"):
            raise OSError("rollback replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(db, "open_db", fail_open_after_replacement)
    monkeypatch.setattr(restore.os, "replace", fail_rollback_replace)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert api._conn is None
    assert "balancer_prerestore_" in result["error"]
    assert _account_names(db_path) == ["Live", "Backup"]
    _assert_no_restore_temps(tmp_path)


def test_restore_succeeds_with_warning_when_rollback_cleanup_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_remove = db._remove_db_artifacts

    def fail_rollback_cleanup(path):
        if os.path.basename(path).startswith(".balancer_restore_rollback_"):
            return [f"{path}: locked"]
        return real_remove(path)

    monkeypatch.setattr(db, "_remove_db_artifacts", fail_rollback_cleanup)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert "temporary copy" in result["warning"]
    assert _account_names(db_path) == ["Live", "Backup"]
    api.close_conn()


def test_restore_shows_plain_error_when_staging_raises(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = db.open_db

    def fail_on_stage(path):
        if os.path.basename(path).startswith(".balancer_restore_stage_"):
            raise sqlite3.OperationalError("disk I/O error at C:\\secret\\path")
        return real_open_db(path)

    monkeypatch.setattr(db, "open_db", fail_on_stage)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert result["error"] == "Couldn't prepare that backup for restore. Nothing was changed."
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_prerestore_backup_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / "missing.db"
    path, _ = backup._make_prerestore_backup(str(missing), str(tmp_path / "backups"))

    assert path is None
    assert not missing.exists()


def test_restore_cancels_when_safety_backup_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    monkeypatch.setattr(backup, "_make_prerestore_backup", lambda *_: (None, []))

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_preview_counts_transaction_edit_once_and_additions_and_deletions(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _insert_transaction(api._conn)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    api._conn.execute("UPDATE transactions SET payee='Edited' WHERE id=1")
    _insert_transaction(api._conn, payee="Live only", sort_key=2)
    backup_conn = sqlite3.connect(backups / BACKUP_NAME)
    try:
        backup_conn.execute(
            "INSERT INTO transactions "
            "(id, account_id, date, payee, category, notes, amount_cents, cleared, estimated, sort_key, created_at) "
            "VALUES (3, 1, '2024-01-02', 'Backup only', 'Food', 'note', 1234, 0, 0, 2, '2024-01-02T00:00:00')"
        )
        backup_conn.commit()
    finally:
        backup_conn.close()

    account = _preview(api)["accounts"][0]

    assert account == {
        "id": 1, "name": "Live", "kind": "diff", "added": 1,
        "changed": 1, "deleted": 1, "details_changed": False,
    }
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("estimated", 1),
        ("sort_key", 99),
    ],
)
def test_preview_detects_each_transaction_field(tmp_path, monkeypatch, column, value):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _insert_transaction(api._conn)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    api._conn.execute(f"UPDATE transactions SET {column}=? WHERE id=1", (value,))
    api._conn.commit()

    account = _preview(api)["accounts"][0]

    assert (account["added"], account["changed"], account["deleted"]) == (0, 1, 0)
    assert account["details_changed"] is False
    api.close_conn()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("starting_balance_cents", 1),
        ("starting_date", "2024-02-01"),
        ("name", "Renamed"),
    ],
)
def test_preview_detects_each_account_detail(tmp_path, monkeypatch, column, value):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    api._conn.execute(f"UPDATE accounts SET {column}=? WHERE id=1", (value,))
    api._conn.commit()

    account = _preview(api)["accounts"][0]

    assert account["kind"] == "diff"
    assert account["details_changed"] is True
    assert (account["added"], account["changed"], account["deleted"]) == (0, 0, 0)
    api.close_conn()


@pytest.mark.parametrize("change", ["added", "removed", "case_changed"])
def test_preview_compares_categories_exactly(tmp_path, monkeypatch, change):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    if change == "added":
        api._conn.execute("INSERT INTO categories (name) VALUES ('New category')")
    elif change == "removed":
        api._conn.execute("DELETE FROM categories WHERE name='Auto'")
    else:
        api._conn.execute("UPDATE categories SET name='AUTO' WHERE name='Auto'")
    api._conn.commit()

    assert _preview(api)["categories"] == "differ"
    api.close_conn()


def test_preview_compares_autopays_and_identical_backup_is_all_same(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _insert_autopay(api._conn)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    before_live = Path(db_path).read_bytes()

    identical = _preview(api)

    assert identical["accounts"] == [{"id": 1, "name": "Live", "kind": "same"}]
    assert identical["categories"] == "same"
    assert identical["autopays"] == "same"
    assert Path(db_path).read_bytes() == before_live
    _assert_no_restore_temps(tmp_path)

    api._conn.execute("UPDATE autopays SET is_variable=1 WHERE id=1")
    api._conn.commit()
    assert _preview(api)["autopays"] == "differ"
    api.close_conn()


def test_preview_upgrades_older_backup_and_removes_staged_files(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _make_versioned_backup(backups / BACKUP_NAME, 0)
    before_live = Path(db_path).read_bytes()

    result = _preview(api)

    assert result["accounts"][0]["kind"] == "diff"
    assert Path(db_path).read_bytes() == before_live
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_accepts_matching_preview_fingerprint(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    fingerprint = _preview(api)["fingerprint"]

    result = api.restore_backup(BACKUP_NAME, fingerprint)

    assert result["ok"] is True
    assert _account_names(db_path) == ["Live", "Backup"]
    api.close_conn()


def test_restore_refuses_changed_backup_fingerprint_without_touching_live(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    fingerprint = _preview(api)["fingerprint"]
    backup_path = backups / BACKUP_NAME
    backup_path.write_bytes(backup_path.read_bytes() + b"changed")

    result = api.restore_backup(BACKUP_NAME, fingerprint)

    assert result == {
        "ok": False,
        "error": "That backup file changed since you previewed it. Open it again to see what it would change.",
    }
    assert _account_names(db_path) == ["Live"]
    api.close_conn()


def test_restore_without_fingerprint_keeps_existing_behavior(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert _account_names(db_path) == ["Live", "Backup"]
    api.close_conn()


def test_remove_stale_restore_files_only_matches_exact_restore_temp_names(tmp_path):
    removable = [
        ".balancer_restore_stage_abc_123.db",
        ".balancer_restore_stage_abc_123.db-journal",
        ".balancer_restore_rollback_abc_123.db",
        ".balancer_restore_rollback_abc_123.db-journal",
        ".balancer_restore_import_abc123.db",
        ".balancer_restore_import_abc123.db-journal",
        ".balancer_save_snapshot_abc123.db",
        ".balancer_save_snapshot_abc123.db-journal",
    ]
    untouched = [
        ".balancer_restore_other_abc.db",
        "balancer_restore_stage_abc.db",
        ".balancer_restore_stage_abc.db-extra",
        "balancer_20240101_000000.db",
        "live.db",
    ]
    for name in removable + untouched:
        (tmp_path / name).write_text("x")

    removed, failures = restore._remove_stale_restore_files(str(tmp_path))

    assert removed == len(removable)
    assert failures == []
    assert not any((tmp_path / name).exists() for name in removable)
    assert all((tmp_path / name).exists() for name in untouched)


def test_remove_stale_restore_files_reports_delete_failure(tmp_path, monkeypatch):
    name = ".balancer_restore_stage_cannot_delete.db"
    path = tmp_path / name
    path.write_text("x")
    real_remove = restore.os.remove

    def fail_remove(candidate):
        if os.path.basename(candidate) == name:
            raise OSError("locked")
        real_remove(candidate)

    monkeypatch.setattr(restore.os, "remove", fail_remove)

    assert restore._remove_stale_restore_files(str(tmp_path)) == (0, [name])
    assert path.exists()


def test_find_launch_restore_backup_uses_full_timestamp_and_skips_bad_files(tmp_path, monkeypatch):
    api, _, primary = _make_api(tmp_path, monkeypatch)
    default = tmp_path / "default-backups"
    default.mkdir()
    names = [
        "balancer_20240101_120000.db",
        "balancer_20240101_120000_000001.db",
        "balancer_20240101_120000_000002.db",
        "balancer_prerestore_20240102_000000_000000.db",
        "balancer_20240103_000000_000000.db",
    ]
    for name in names:
        (primary / name).write_bytes(b"backup")
    (default / "balancer_20240102_000000_000000.db").write_bytes(b"backup")
    corrupt = primary / "balancer_20240103_000000_000000.db"

    def check(path):
        if path == str(corrupt):
            return "That backup file is corrupt or unreadable."
        return None

    monkeypatch.setattr(restore, "_check_backup", check)
    full_path, timestamp, newer_count = restore._find_launch_restore_backup(
        api, [str(primary), str(default), str(primary)]
    )

    assert full_path == str(default / "balancer_20240102_000000_000000.db")
    assert timestamp.microsecond == 0
    assert newer_count == 0
    api.close_conn()


def test_find_launch_restore_backup_orders_microseconds_before_legacy_names(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    legacy = backups / "balancer_20240101_120000.db"
    first = backups / "balancer_20240101_120000_000001.db"
    newest = backups / "balancer_20240101_120000_000002.db"
    for path in (legacy, first, newest):
        path.write_bytes(b"backup")
    monkeypatch.setattr(restore, "_check_backup", lambda *_: None)

    full_path, timestamp, newer_count = restore._find_launch_restore_backup(api, [str(backups)])

    assert full_path == str(newest)
    assert timestamp.microsecond == 2
    assert newer_count == 0
    api.close_conn()


def test_find_launch_restore_backup_counts_newer_and_logs_unreadable_folder(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    newer = backups / "balancer_20240102_000000_000000.db"
    older = backups / "balancer_20240101_000000_000000.db"
    newer.write_bytes(b"newer")
    older.write_bytes(b"older")
    missing = tmp_path / "missing"

    def check(path):
        if path == str(newer):
            return "That backup was made by a newer version of Simple Account Balancer. Update the app to restore it."
        return "That backup file is corrupt or unreadable."

    monkeypatch.setattr(restore, "_check_backup", check)
    full_path, timestamp, newer_count = restore._find_launch_restore_backup(
        api, [str(missing), str(backups)]
    )

    assert full_path is None
    assert timestamp is None
    assert newer_count == 1
    api.close_conn()


def test_restore_damaged_database_keeps_copy_moves_journal_and_publishes(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    damaged = b"damaged live bytes"
    Path(db_path).write_bytes(damaged)
    Path(db_path + "-journal").write_bytes(b"damaged journal")

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert error is None
    assert _account_names(db_path) == ["Live", "Backup"]
    assert Path(kept_path).read_bytes() == damaged
    assert Path(kept_path + "-journal").read_bytes() == b"damaged journal"
    assert not Path(db_path + "-journal").exists()
    assert not list(tmp_path.glob(".balancer_restore_stage_*.db"))


@pytest.mark.parametrize("step", ["stage", "keep", "journal", "publish"])
def test_restore_damaged_database_failure_keeps_live_file(tmp_path, monkeypatch, step):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    damaged = b"damaged live bytes"
    Path(db_path).write_bytes(damaged)

    if step == "stage":
        monkeypatch.setattr(restore, "_stage_backup", lambda *_: (_ for _ in ()).throw(OSError("stage")))
    elif step == "keep":
        monkeypatch.setattr(restore, "_keep_damaged_database", lambda *_: (_ for _ in ()).throw(OSError("keep")))
    elif step == "journal":
        monkeypatch.setattr(restore, "_move_damaged_journal", lambda *_: (_ for _ in ()).throw(OSError("journal")))
    else:
        monkeypatch.setattr(restore.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("publish")))

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert kept_path is None
    assert error == "The automatic restore couldn't be completed. Your data file was not changed."
    assert Path(db_path).read_bytes() == damaged
    assert not list(tmp_path.glob(".balancer_restore_stage_*.db"))
    if step in {"journal", "publish"}:
        assert list(tmp_path.glob("simple_account_balancer.damaged_*.db"))


def test_restore_damaged_database_removes_partial_kept_copy(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    damaged = b"damaged live bytes"
    Path(db_path).write_bytes(damaged)
    logs = []
    monkeypatch.setattr(api, "_log", logs.append)
    real_copy2 = restore.shutil.copy2
    partial_paths = []

    def copy2(src, dst, *args, **kwargs):
        if os.path.abspath(src) != os.path.abspath(db_path):
            return real_copy2(src, dst, *args, **kwargs)
        Path(dst).write_bytes(damaged[:7])
        partial_paths.append(dst)
        raise OSError("disk full")

    monkeypatch.setattr(restore.shutil, "copy2", copy2)

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert len(partial_paths) == 1
    assert not Path(partial_paths[0]).exists()
    assert kept_path is None
    assert error == "The automatic restore couldn't be completed. Your data file was not changed."
    assert Path(db_path).read_bytes() == damaged
    assert not list(tmp_path.glob(".balancer_restore_stage_*.db"))
    assert not list(tmp_path.glob("simple_account_balancer.damaged_*.db"))
    assert any("OSError" in line for line in logs)


def _fail_publish_replace(monkeypatch, live_path):
    real_replace = os.replace

    def replace(src, dst):
        if dst == live_path:
            raise OSError("publish")
        return real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", replace)


def test_restore_damaged_database_publish_failure_returns_journal(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    Path(db_path).write_bytes(b"damaged live bytes")
    Path(db_path + "-journal").write_bytes(b"damaged journal")
    _fail_publish_replace(monkeypatch, db_path)

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert kept_path is None
    assert error == "The automatic restore couldn't be completed. Your data file was not changed."
    assert Path(db_path).read_bytes() == b"damaged live bytes"
    assert Path(db_path + "-journal").read_bytes() == b"damaged journal"
    assert not list(tmp_path.glob("simple_account_balancer.damaged_*.db-journal"))


def test_restore_damaged_database_journal_return_keeps_newer_journal(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    Path(db_path).write_bytes(b"damaged live bytes")
    Path(db_path + "-journal").write_bytes(b"damaged journal")
    real_replace = os.replace

    def replace(src, dst):
        if dst == db_path:
            Path(db_path + "-journal").write_bytes(b"newer journal")
            raise OSError("publish")
        return real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", replace)
    messages = []
    monkeypatch.setattr(api, "_log", messages.append)

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert kept_path is None
    assert error == "The automatic restore couldn't be completed. Your data file was not changed."
    assert Path(db_path + "-journal").read_bytes() == b"newer journal"
    kept_journals = list(tmp_path.glob("simple_account_balancer.damaged_*.db-journal"))
    assert [p.read_bytes() for p in kept_journals] == [b"damaged journal"]
    assert any("couldn't return the damaged database journal" in m for m in messages)


def test_restore_damaged_database_journal_move_failure_leaves_journal(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    backup_path = _backup_with_account(api, backups)
    api.close_conn()
    Path(db_path).write_bytes(b"damaged live bytes")
    Path(db_path + "-journal").write_bytes(b"damaged journal")
    real_replace = os.replace

    def replace(src, dst):
        if src == db_path + "-journal":
            raise OSError("journal")
        return real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", replace)

    kept_path, error = restore.restore_damaged_database(api, str(backup_path), db_path)

    assert kept_path is None
    assert error == "The automatic restore couldn't be completed. Your data file was not changed."
    assert Path(db_path).read_bytes() == b"damaged live bytes"
    assert Path(db_path + "-journal").read_bytes() == b"damaged journal"
    assert not list(tmp_path.glob("simple_account_balancer.damaged_*.db-journal"))


def test_kept_damaged_files_survive_restore_temp_cleanup(tmp_path):
    kept = tmp_path / "simple_account_balancer.damaged_20240101_000000_deadbeef.db"
    journal = Path(str(kept) + "-journal")
    kept.write_bytes(b"kept")
    journal.write_bytes(b"journal")

    assert restore._remove_stale_restore_files(str(tmp_path)) == (0, [])
    assert kept.exists()
    assert journal.exists()


def test_restore_from_file_round_trip(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    conn = sqlite3.connect(picked)
    try:
        _insert_account(conn, "Backup")
    finally:
        conn.close()
    original = picked.read_bytes()

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert preview["ok"] is True
    assert preview["source_name"] == picked.name
    assert result["ok"] is True
    assert _account_names(db_path) == ["Live", "Backup"]
    assert api._restore_import is None
    assert picked.read_bytes() == original
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_from_file_into_empty_database_lands_backup(tmp_path, monkeypatch):
    api, db_path, _ = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup_with_restore_data(tmp_path, api)

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is True
    assert db.has_accounts(api._conn) is True
    assert _account_names(db_path) == ["Backup One", "Backup Two"]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT name FROM categories WHERE name='Backup category'").fetchone()
        assert conn.execute("SELECT payee FROM transactions").fetchone()[0] == "Backup transaction"
        assert conn.execute("SELECT payee FROM autopays").fetchone()[0] == "Backup autopay"
    finally:
        conn.close()
    api.close_conn()


def test_restore_into_empty_database_takes_no_safety_backup(tmp_path, monkeypatch):
    api, _, backups = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup_with_restore_data(tmp_path, api)

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is True
    assert not list(backups.glob("balancer_prerestore_*.db"))
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_with_accounts_still_takes_safety_backup(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert list(backups.glob("balancer_prerestore_*.db"))
    api.close_conn()


def test_restore_from_file_refuses_backup_with_no_accounts_into_empty_database(tmp_path, monkeypatch):
    api, db_path, backups = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    before = Path(db_path).read_bytes()

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result == {
        "ok": False,
        "error": "That backup has no accounts in it.",
        "selection_cleared": True,
    }
    assert Path(db_path).read_bytes() == before
    assert not list(backups.glob("balancer_prerestore_*.db"))
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_backup_list_refuses_backup_with_no_accounts_into_empty_database(tmp_path, monkeypatch):
    api, db_path, backups = _make_empty_api(tmp_path, monkeypatch)
    _backup_from_conn(api._conn, str(backups / BACKUP_NAME))
    before = Path(db_path).read_bytes()

    result = api.restore_backup(BACKUP_NAME)

    assert result == {"ok": False, "error": "That backup has no accounts in it."}
    assert Path(db_path).read_bytes() == before
    assert not list(backups.glob("balancer_prerestore_*.db"))
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_into_live_with_accounts_accepts_backup_with_no_accounts(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    empty_path = tmp_path / "empty.db"
    empty_conn = db.open_db(str(empty_path))
    try:
        picked = tmp_path / "outside" / "SAB_Backup_01012026.db"
        picked.parent.mkdir()
        _backup_from_conn(empty_conn, str(picked))
    finally:
        empty_conn.close()

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is True
    assert db.has_accounts(api._conn) is False
    assert _account_names(db_path) == []
    api.close_conn()


def test_restore_from_file_upgrades_older_backup_into_empty_database(tmp_path, monkeypatch):
    api, db_path, _ = _make_empty_api(tmp_path, monkeypatch)
    picked = tmp_path / "outside" / "SAB_Backup_01012026.db"
    picked.parent.mkdir()
    _make_versioned_backup(picked, 0)

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is True
    assert _account_names(db_path) == ["Version 0"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_into_empty_database_reports_retry_when_rollback_reopens(tmp_path, monkeypatch):
    api, db_path, _ = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup_with_restore_data(tmp_path, api)
    preview = _choose_restore_file(api, picked)
    real_open_db = db.open_db
    failed = False

    def fail_once_after_replacement(path):
        nonlocal failed
        conn = real_open_db(path)
        if path == db_path and not failed:
            failed = True
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(db, "open_db", fail_once_after_replacement)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["error"] == "The restore failed. Nothing was changed, so you can try again."
    assert db.has_accounts(api._conn) is False
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_into_empty_database_reports_restart_when_rollback_reopen_fails(tmp_path, monkeypatch):
    api, db_path, _ = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup_with_restore_data(tmp_path, api)
    preview = _choose_restore_file(api, picked)
    real_open_db = db.open_db

    def fail_live_reopens(path):
        conn = real_open_db(path)
        if path == db_path:
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(db, "open_db", fail_live_reopens)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["error"] == (
        "The restore couldn't be finished. Close the app and open it again. "
        "Your backup file was not changed."
    )
    assert api._conn is None
    _assert_no_restore_temps(tmp_path)


def test_restore_into_empty_database_reports_restart_when_rollback_replace_fails(tmp_path, monkeypatch):
    api, db_path, _ = _make_empty_api(tmp_path, monkeypatch)
    picked = _outside_backup_with_restore_data(tmp_path, api)
    preview = _choose_restore_file(api, picked)
    real_open_db = db.open_db
    real_replace = restore.os.replace
    failed = False

    def fail_open_after_replacement(path):
        nonlocal failed
        conn = real_open_db(path)
        if path == db_path and not failed:
            failed = True
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    def fail_rollback_replace(src, dst):
        if os.path.basename(src).startswith(".balancer_restore_rollback_"):
            raise OSError("rollback replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(db, "open_db", fail_open_after_replacement)
    monkeypatch.setattr(restore.os, "replace", fail_rollback_replace)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["error"] == (
        "The restore couldn't be finished. Close the app and open it again. "
        "Your backup file was not changed."
    )
    assert api._conn is None
    _assert_no_restore_temps(tmp_path)


def test_restore_uses_safety_backup_when_live_account_check_raises(tmp_path, monkeypatch):
    api, _, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_has_accounts = db.has_accounts

    def fail_for_live_connection(conn):
        if conn is api._conn:
            raise RuntimeError("live account check failed")
        return real_has_accounts(conn)

    monkeypatch.setattr(db, "has_accounts", fail_for_live_connection)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert list(backups.glob("balancer_prerestore_*.db"))
    api.close_conn()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_restore_from_file_upgrades_each_older_schema_version(tmp_path, monkeypatch, version):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    picked = outside / "SAB_Backup_01012026.db"
    _make_versioned_backup(picked, version)

    preview = _choose_restore_file(api, picked)
    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is True
    assert _account_names(db_path) == [f"Version {version}"]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == config.SCHEMA_VERSION
    finally:
        conn.close()
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_from_file_upgrades_read_only_older_backup(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    picked = outside / "SAB_Backup_01012026.db"
    _make_versioned_backup(picked, 0)
    original = picked.read_bytes()
    os.chmod(picked, stat.S_IREAD)
    try:
        preview = _choose_restore_file(api, picked)
        result = api.restore_from_file(preview["token"], preview["fingerprint"])
    finally:
        os.chmod(picked, stat.S_IWRITE)

    assert result["ok"] is True
    assert _account_names(db_path) == ["Version 0"]
    assert picked.read_bytes() == original
    api.close_conn()


def test_restore_from_file_refuses_newer_version(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    conn = sqlite3.connect(picked)
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = Path(db_path).read_bytes()

    result = _choose_restore_file(api, picked)

    assert result["ok"] is False
    assert Path(db_path).read_bytes() == before
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


@pytest.mark.parametrize("contents", [b"text", b"SQLite format 3\x00", b"SQLite format 3\x00" + b"garbage"])
def test_restore_from_file_refuses_bad_files(tmp_path, monkeypatch, contents):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    picked = outside / "SAB_Backup_01012026.db"
    picked.write_bytes(contents)
    before = Path(db_path).read_bytes()

    result = _choose_restore_file(api, picked)

    assert result["ok"] is False
    assert Path(db_path).read_bytes() == before
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_from_file_refuses_truncated_real_backup(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    data = picked.read_bytes()
    picked.write_bytes(data[: len(data) // 2])
    before = Path(db_path).read_bytes()

    result = _choose_restore_file(api, picked)

    assert result["ok"] is False
    assert Path(db_path).read_bytes() == before
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()

def test_restore_from_file_refuses_live_database(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)

    result = _choose_restore_file(api, Path(db_path))

    assert result == {"ok": False, "error": "That's the app's live database. Choose a backup file instead."}
    assert api._restore_import is None
    api.close_conn()


def test_restore_from_file_refuses_live_database_hard_link(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = outside / "SAB_Backup_01012026.db"
    try:
        os.link(db_path, linked)
    except OSError:
        pytest.skip("hard links are unavailable")

    result = _choose_restore_file(api, linked)

    assert result == {"ok": False, "error": "That's the app's live database. Choose a backup file instead."}
    api.close_conn()


def test_restore_from_file_refuses_low_disk_space(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    usage = restore.shutil.disk_usage(str(tmp_path))
    monkeypatch.setattr(restore.shutil, "disk_usage", lambda _: usage._replace(free=0))

    result = _choose_restore_file(api, picked)

    assert result["ok"] is False
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_from_file_copy_failure_leaves_nothing(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)

    def fail_copy(_source, dest):
        Path(dest).write_bytes(b"partial")
        raise OSError("full")

    monkeypatch.setattr(restore.shutil, "copyfile", fail_copy)
    result = _choose_restore_file(api, picked)

    assert result == {"ok": False, "error": "Couldn't copy that file. Nothing was changed."}
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_from_file_rejects_wrong_or_stale_token(tmp_path, monkeypatch):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    first = _outside_backup(tmp_path, api, "SAB_Backup_01012026.db")
    second = _outside_backup(tmp_path, api, "SAB_Backup_01022026.db")
    one = _choose_restore_file(api, first)
    two = _choose_restore_file(api, second)
    before = Path(db_path).read_bytes()

    for token, fingerprint in (("wrong", two["fingerprint"]), (two["token"], None), (two["token"], "wrong"), (one["token"], one["fingerprint"])):
        assert api.restore_from_file(token, fingerprint)["ok"] is False
        assert Path(db_path).read_bytes() == before
    assert api.restore_from_file(two["token"], two["fingerprint"])["ok"] is True
    api.close_conn()


def test_restore_from_file_second_pick_replaces_first(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    first = _outside_backup(tmp_path, api, "SAB_Backup_01012026.db")
    second = _outside_backup(tmp_path, api, "SAB_Backup_01022026.db")

    _choose_restore_file(api, first)
    _choose_restore_file(api, second)

    assert len(list(tmp_path.glob(".balancer_restore_import_*.db"))) == 1
    api.discard_restore_file(api._restore_import["token"])
    api.close_conn()


def test_discard_restore_file_removes_copy_and_ignores_stale_token(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    preview = _choose_restore_file(api, picked)
    held = Path(api._restore_import["path"])

    assert api.discard_restore_file("stale") == {"ok": True}
    assert held.exists()
    assert api.discard_restore_file(preview["token"]) == {"ok": True}
    assert not held.exists()
    api.close_conn()


@pytest.mark.parametrize("failure", ["replace", "reopen", "safety"])
def test_restore_from_file_failure_paths_clear_selection(tmp_path, monkeypatch, failure):
    api, db_path, _ = _make_api(tmp_path, monkeypatch)
    picked = _outside_backup(tmp_path, api)
    preview = _choose_restore_file(api, picked)
    if failure == "replace":
        real_replace = restore.os.replace
        monkeypatch.setattr(
            restore.os, "replace",
            lambda src, dst: (_ for _ in ()).throw(OSError("replace"))
            if os.path.basename(src).startswith(".balancer_restore_stage_") else real_replace(src, dst),
        )
    elif failure == "reopen":
        real_open_db = db.open_db
        failed = False
        def fail_open(path):
            nonlocal failed
            conn = real_open_db(path)
            if path == db_path and not failed:
                failed = True
                conn.close()
                raise RuntimeError("reopen")
            return conn
        monkeypatch.setattr(db, "open_db", fail_open)
    else:
        monkeypatch.setattr(backup, "_make_prerestore_backup", lambda *_: (None, []))

    result = api.restore_from_file(preview["token"], preview["fingerprint"])

    assert result["ok"] is False
    assert result["selection_cleared"] is True
    assert _account_names(db_path) == ["Live"]
    assert api._restore_import is None
    _assert_no_restore_temps(tmp_path)
    if api._conn is not None:
        api.close_conn()


def test_restore_from_file_log_has_no_path(tmp_path, monkeypatch):
    api, _, _ = _make_api(tmp_path, monkeypatch)
    outside = tmp_path / "ZZsecretZZ"
    outside.mkdir()
    picked = outside / "SAB_Backup_01012026.db"
    _backup_from_conn(api._conn, str(picked))
    messages = []
    monkeypatch.setattr(api, "_log", messages.append)

    preview = _choose_restore_file(api, picked)
    assert api.restore_from_file(preview["token"], preview["fingerprint"])["ok"] is True
    picked.write_bytes(b"not sqlite")
    assert _choose_restore_file(api, picked)["ok"] is False

    assert all("ZZsecretZZ" not in message for message in messages)
    api.close_conn()
