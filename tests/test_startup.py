"""Tests for safe database recovery and launch-backup preparation."""

import os
import shutil
import sqlite3

import pytest

from app import config, db
from app.api import Api
from app.services import backup
from app.startup import StartupResult, prepare_database
import simple_account_balancer as launcher


def _make_version_two_database(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "starting_balance_cents INTEGER NOT NULL, starting_date TEXT NOT NULL, "
            "created_at TEXT NOT NULL, starting_balance_prev_cents INTEGER, "
            "starting_balance_changed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
            "date TEXT NOT NULL, payee TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', "
            "notes TEXT NOT NULL DEFAULT '', amount_cents INTEGER NOT NULL, "
            "cleared INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE autopays (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
            "payee TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', "
            "amount_cents INTEGER NOT NULL, next_pay_date TEXT NOT NULL, next_post_date TEXT NOT NULL, "
            "pay_day INTEGER NOT NULL, post_day INTEGER NOT NULL, is_variable INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO accounts VALUES (1, 'Older', 0, '2024-01-01', '2024-01-01', NULL, NULL)"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _make_hot_journal_copy(tmp_path):
    original = tmp_path / "original.db"
    crash_copy = tmp_path / "crash.db"
    conn = db.open_db(str(original))
    try:
        conn.execute("CREATE TABLE rows (value TEXT NOT NULL)")
        conn.executemany("INSERT INTO rows VALUES (?)", [(f"committed-{i}",) for i in range(20)])
        conn.commit()
        committed_count = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        conn.execute("PRAGMA cache_size = 1")
        conn.execute("BEGIN EXCLUSIVE")
        conn.executemany("INSERT INTO rows VALUES (?)", [("x" * 400,) for _ in range(40)])
        journal = str(original) + "-journal"
        assert os.path.exists(journal)
        shutil.copy2(original, crash_copy)
        shutil.copy2(journal, str(crash_copy) + "-journal")
    finally:
        conn.close()
    return original, crash_copy, committed_count


def test_recover_hot_journal_then_snapshot_has_only_committed_rows(tmp_path):
    original, crash_copy, committed_count = _make_hot_journal_copy(tmp_path)
    raw = sqlite3.connect(f"file:{crash_copy.as_posix()}?immutable=1", uri=True)
    try:
        raw_count = raw.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    finally:
        raw.close()
    assert raw_count != committed_count or crash_copy.read_bytes() != original.read_bytes()

    recovery = db.recover_and_check(str(crash_copy))
    assert recovery.status == "ok"
    assert not os.path.exists(str(crash_copy) + "-journal")
    backups = tmp_path / "backups"
    try:
        assert backup._make_backup(str(crash_copy), str(backups), source_conn=recovery.conn) == (True, [])
    finally:
        recovery.conn.close()
    snapshot = sqlite3.connect(next(backups.iterdir()))
    try:
        assert snapshot.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == committed_count
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        snapshot.close()


@pytest.mark.parametrize("contents", [b"not sqlite", b"SQLite format 3\x00truncated"])
def test_damaged_database_does_not_change_existing_backups(tmp_path, contents):
    live = tmp_path / config.DB_FILENAME
    live.write_bytes(contents)
    backups = tmp_path / config.BACKUP_DIRNAME
    backups.mkdir()
    existing = []
    for index in range(5):
        path = backups / f"balancer_2024010{index + 1}_000000.db"
        data = f"backup-{index}".encode()
        path.write_bytes(data)
        existing.append((path.name, data))
    api = Api("test")

    result = prepare_database(api, str(live))

    assert result.outcome == "damaged"
    assert result.folders == [str(backups)]
    assert [(path.name, path.read_bytes()) for path in backups.iterdir()] == existing


def test_locked_database_is_unavailable_and_unchanged(tmp_path, monkeypatch):
    live = tmp_path / config.DB_FILENAME
    conn = db.open_db(str(live))
    conn.close()
    before = live.read_bytes()
    lock = sqlite3.connect(live, timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    lock.execute("INSERT INTO categories (name) VALUES ('Lock holder')")
    try:
        assert db.recover_and_check(str(live), timeout=0.1).status == "unavailable"
        real_recover = db.recover_and_check
        monkeypatch.setattr(db, "recover_and_check", lambda path: real_recover(path, timeout=0.1))
        assert prepare_database(Api("test"), str(live)).outcome == "unavailable"
    finally:
        lock.rollback()
        lock.close()
    assert live.read_bytes() == before


def test_missing_database_is_not_created_and_startup_continues(tmp_path):
    missing = tmp_path / config.DB_FILENAME
    assert db.recover_and_check(str(missing)).status == "unavailable"
    assert not missing.exists()
    assert prepare_database(Api("test"), str(missing)).outcome == "continue"
    assert not missing.exists()


def test_backup_failure_blocks_upgrade_without_changing_live_database(tmp_path, monkeypatch):
    live = tmp_path / config.DB_FILENAME
    _make_version_two_database(live)
    before = live.read_bytes()
    monkeypatch.setattr(backup, "_run_backup_with_fallback", lambda *args, **kwargs: (False, False, "x", []))

    result = prepare_database(Api("test"), str(live))

    assert result.outcome == "upgrade_blocked"
    assert result.folders == ["x"]
    assert live.read_bytes() == before


def test_backup_failure_at_current_version_continues_with_existing_notice(tmp_path, monkeypatch):
    live = tmp_path / config.DB_FILENAME
    conn = db.open_db(str(live))
    conn.close()
    api = Api("test")
    monkeypatch.setattr(backup, "_run_backup_with_fallback", lambda *args, **kwargs: (False, False, "x", []))

    assert prepare_database(api, str(live)).outcome == "continue"
    assert api._backup_notice == "Today's backup couldn't be saved. Tried to write it to x."


def test_pre_upgrade_snapshot_keeps_old_version_then_open_db_migrates(tmp_path):
    live = tmp_path / config.DB_FILENAME
    _make_version_two_database(live)
    result = prepare_database(Api("test"), str(live))
    assert result.outcome == "continue"
    snapshot = next((tmp_path / config.BACKUP_DIRNAME).iterdir())
    conn = sqlite3.connect(snapshot)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()
    migrated = db.open_db(str(live))
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == config.SCHEMA_VERSION
    finally:
        migrated.close()


def test_newer_database_is_backed_up_then_open_db_refuses_it(tmp_path):
    live = tmp_path / config.DB_FILENAME
    conn = db.open_db(str(live))
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    assert prepare_database(Api("test"), str(live)).outcome == "continue"
    assert len(list((tmp_path / config.BACKUP_DIRNAME).iterdir())) == 1
    with pytest.raises(db.NewerSchemaError):
        db.open_db(str(live))


@pytest.mark.parametrize("tables", [[], ["accounts"]])
def test_empty_first_launch_file_continues_without_backup(tmp_path, tables):
    live = tmp_path / config.DB_FILENAME
    conn = sqlite3.connect(live)
    for name in tables:
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert db.recover_and_check(str(live)).status == "empty"
    assert prepare_database(Api("test"), str(live)).outcome == "continue"
    assert not (tmp_path / config.BACKUP_DIRNAME).exists()


def test_old_file_with_rows_but_broken_schema_is_damaged(tmp_path):
    live = tmp_path / config.DB_FILENAME
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO accounts VALUES (1)")
    conn.commit()
    conn.close()

    result = db.recover_and_check(str(live))
    assert result.status == "damaged"
    assert "missing" in result.detail


def test_damaged_startup_restores_then_continues_with_notice(tmp_path, monkeypatch):
    api = Api("test")
    db_path = str(tmp_path / config.DB_FILENAME)
    backup_path = str(tmp_path / "backups" / "balancer_20240101_000000.db")
    kept_path = str(tmp_path / "simple_account_balancer.damaged_20240101_000000_deadbeef.db")
    timestamp = __import__("datetime").datetime(2024, 1, 1, 12, 30)
    monkeypatch.setattr(launcher.restore, "_find_launch_restore_backup", lambda *_: (backup_path, timestamp, 0))
    monkeypatch.setattr(launcher.restore, "restore_damaged_database", lambda *_: (kept_path, None))
    monkeypatch.setattr(launcher.startup, "prepare_database", lambda *_: StartupResult("continue", []))

    result = launcher._recover_damaged_database(api, db_path, ["backups"], True)

    assert result.outcome == "continue"
    assert "restored from the backup of January 1, 2024 at 12:30:00 PM" in api._backup_notice
    assert os.path.basename(kept_path) in api._backup_notice


def test_damaged_startup_without_usable_backup_uses_hand_restore(tmp_path, monkeypatch):
    api = Api("test")
    live = tmp_path / config.DB_FILENAME
    live.write_bytes(b"damaged")
    monkeypatch.setattr(launcher.restore, "_find_launch_restore_backup", lambda *_: (None, None, 2))
    shown = []
    monkeypatch.setattr(launcher.platform_win, "_show_damaged_db_error", lambda *args: shown.append(args))
    monkeypatch.setattr(launcher.restore, "restore_damaged_database", lambda *_: pytest.fail("must not restore"))

    assert launcher._recover_damaged_database(api, str(live), ["backups"], True) is None
    assert live.read_bytes() == b"damaged"
    assert len(shown) == 1
    folders, leading_text = shown[0]
    assert folders == ["backups"]
    assert leading_text.startswith("No usable backup was found.")
    assert "2 newer backups could not be used" in leading_text


def test_damaged_startup_notice_mentions_skipped_newer_backups(tmp_path, monkeypatch):
    api = Api("test")
    timestamp = __import__("datetime").datetime(2024, 1, 1)
    monkeypatch.setattr(launcher.restore, "_find_launch_restore_backup", lambda *_: ("backup.db", timestamp, 1))
    monkeypatch.setattr(launcher.restore, "restore_damaged_database", lambda *_: ("kept.db", None))
    monkeypatch.setattr(launcher.startup, "prepare_database", lambda *_: StartupResult("continue", []))

    launcher._recover_damaged_database(api, str(tmp_path / config.DB_FILENAME), ["backups"], True)

    assert "1 newer backup could not be used" in api._backup_notice


def test_damaged_startup_not_exclusive_never_restores_or_touches_files(tmp_path, monkeypatch):
    api = Api("test")
    live = tmp_path / config.DB_FILENAME
    live.write_bytes(b"damaged")
    monkeypatch.setattr(launcher.restore, "_find_launch_restore_backup", lambda *_: pytest.fail("must not search"))
    shown = []
    monkeypatch.setattr(launcher.platform_win, "_show_damaged_db_error", lambda *args: shown.append(args))

    assert launcher._recover_damaged_database(api, str(live), ["backups"], False) is None
    assert live.read_bytes() == b"damaged"
    assert shown == [(["backups"],)]


def test_damaged_startup_recheck_does_not_offer_a_second_restore(tmp_path, monkeypatch):
    api = Api("test")
    timestamp = __import__("datetime").datetime(2024, 1, 1)
    monkeypatch.setattr(launcher.restore, "_find_launch_restore_backup", lambda *_: ("backup.db", timestamp, 0))
    monkeypatch.setattr(launcher.restore, "restore_damaged_database", lambda *_: ("kept.db", None))
    calls = []

    def recheck(*_):
        calls.append(True)
        return StartupResult("damaged", ["backups"])

    monkeypatch.setattr(launcher.startup, "prepare_database", recheck)

    result = launcher._recover_damaged_database(api, "live.db", ["backups"], True)

    assert result.outcome == "damaged"
    assert calls == [True]
