"""Restore safety checks. Every database and backup lives under tmp_path."""
import os
import sqlite3

import pytest

import simple_account_balancer as sab


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
    monkeypatch.setattr(sab, "effective_backup_dir", lambda: (str(backups), False))
    db_path = str(tmp_path / "live.db")
    api = sab.Api()
    api.set_conn(sab.open_db(db_path))
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
    conn.execute(f"PRAGMA user_version = {sab.SCHEMA_VERSION}")
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == sab.SCHEMA_VERSION
    finally:
        conn.close()
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_refuses_version_three_backup_missing_sort_key(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _make_versioned_backup(backups / BACKUP_NAME, 2)
    conn = sqlite3.connect(backups / BACKUP_NAME)
    conn.execute(f"PRAGMA user_version = {sab.SCHEMA_VERSION}")
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
    real_replace = sab.os.replace

    def fail_stage_replace(src, dst):
        if os.path.basename(src).startswith(".balancer_restore_stage_"):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(sab.os, "replace", fail_stage_replace)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_rolls_back_when_reopen_after_replace_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = sab.open_db
    failed = False

    def fail_once_after_replacement(path):
        nonlocal failed
        conn = real_open_db(path)
        if path == db_path and not failed:
            failed = True
            conn.close()
            raise RuntimeError("reopen failed")
        return conn

    monkeypatch.setattr(sab, "open_db", fail_once_after_replacement)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    assert api._conn is not None
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_restore_reports_manual_recovery_when_rollback_replace_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = sab.open_db
    real_replace = sab.os.replace
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

    monkeypatch.setattr(sab, "open_db", fail_open_after_replacement)
    monkeypatch.setattr(sab.os, "replace", fail_rollback_replace)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert api._conn is None
    assert "balancer_prerestore_" in result["error"]
    assert _account_names(db_path) == ["Live", "Backup"]
    _assert_no_restore_temps(tmp_path)


def test_restore_succeeds_with_warning_when_rollback_cleanup_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_remove = sab._remove_db_artifacts

    def fail_rollback_cleanup(path):
        if os.path.basename(path).startswith(".balancer_restore_rollback_"):
            return [f"{path}: locked"]
        return real_remove(path)

    monkeypatch.setattr(sab, "_remove_db_artifacts", fail_rollback_cleanup)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is True
    assert "temporary copy" in result["warning"]
    assert _account_names(db_path) == ["Live", "Backup"]
    api.close_conn()


def test_restore_shows_plain_error_when_staging_raises(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    real_open_db = sab.open_db

    def fail_on_stage(path):
        if os.path.basename(path).startswith(".balancer_restore_stage_"):
            raise sqlite3.OperationalError("disk I/O error at C:\\secret\\path")
        return real_open_db(path)

    monkeypatch.setattr(sab, "open_db", fail_on_stage)
    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert result["error"] == "Couldn't prepare that backup for restore. Nothing was changed."
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()


def test_prerestore_backup_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / "missing.db"
    path, _ = sab._make_prerestore_backup(str(missing), str(tmp_path / "backups"))

    assert path is None
    assert not missing.exists()


def test_restore_cancels_when_safety_backup_fails(tmp_path, monkeypatch):
    api, db_path, backups = _make_api(tmp_path, monkeypatch)
    _backup_with_account(api, backups)
    monkeypatch.setattr(sab, "_make_prerestore_backup", lambda *_: (None, []))

    result = api.restore_backup(BACKUP_NAME)

    assert result["ok"] is False
    assert _account_names(db_path) == ["Live"]
    _assert_no_restore_temps(tmp_path)
    api.close_conn()
