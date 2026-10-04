"""Restore safety checks. Every database and backup lives under tmp_path."""
import os
import sqlite3
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


def _insert_autopay(conn, payee="Rent", is_variable=0):
    conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, next_post_date, "
        "pay_day, post_day, is_variable, created_at) "
        "VALUES (1, ?, 'Home', 'note', -100000, '2024-02-01', '2024-02-01', 1, 1, ?, '2024-01-01T00:00:00')",
        (payee, is_variable),
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
    assert _account_names(db_path) == ["Live"]
    assert api._conn is not None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


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
