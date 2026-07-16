"""Tests for the backup-file helpers: keep-count clamping, timestamp
parsing, and pruning. All file operations happen inside tmp_path; nothing
here touches a real backups/ folder."""
import datetime
import os

from simple_account_balancer import (
    BACKUP_KEEP,
    BACKUP_KEEP_MIN,
    BACKUP_KEEP_MAX,
    PRERESTORE_KEEP,
    _clamp_backup_keep,
    _list_backup_files,
    _parse_backup_timestamp,
    _prune_backups,
    _prune_prerestore_backups,
)


# --- _clamp_backup_keep -------------------------------------------------------

def test_clamp_backup_keep_within_bounds_unchanged():
    assert _clamp_backup_keep(10) == 10


def test_clamp_backup_keep_floors_at_min():
    assert _clamp_backup_keep(0) == BACKUP_KEEP_MIN


def test_clamp_backup_keep_ceils_at_max():
    assert _clamp_backup_keep(100) == BACKUP_KEEP_MAX


def test_clamp_backup_keep_non_numeric_string_falls_back_to_default():
    assert _clamp_backup_keep("abc") == BACKUP_KEEP


def test_clamp_backup_keep_none_falls_back_to_default():
    assert _clamp_backup_keep(None) == BACKUP_KEEP


# --- _parse_backup_timestamp ---------------------------------------------------

def test_parse_backup_timestamp_valid_filename(tmp_path):
    name = "balancer_20240115_143000.db"
    full_path = str(tmp_path / name)
    assert _parse_backup_timestamp(name, full_path) == "2024-01-15T14:30:00"


def test_parse_backup_timestamp_malformed_name_falls_back_to_mtime(tmp_path):
    f = tmp_path / "not_a_backup.db"
    f.write_text("x")
    # Pin the mtime so the expected value is deterministic.
    stamp = datetime.datetime(2022, 6, 1, 8, 0, 0).timestamp()
    os.utime(f, (stamp, stamp))
    expected = datetime.datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
    assert _parse_backup_timestamp("not_a_backup.db", str(f)) == expected


def test_parse_backup_timestamp_malformed_name_and_missing_file_falls_back_to_min(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    result = _parse_backup_timestamp("does_not_exist.db", missing)
    assert result == datetime.datetime.min.isoformat(timespec="seconds")


# --- _list_backup_files / pruning ------------------------------------------------

def _touch(dir_path, name):
    (dir_path / name).write_text("x")


def test_list_backup_files_regular_excludes_prerestore(tmp_path):
    _touch(tmp_path, "balancer_20240101_000000.db")
    _touch(tmp_path, "balancer_20240102_000000.db")
    _touch(tmp_path, "balancer_prerestore_20240103_000000.db")
    _touch(tmp_path, "unrelated.txt")
    files = _list_backup_files(str(tmp_path), prerestore=False)
    assert files == ["balancer_20240101_000000.db", "balancer_20240102_000000.db"]


def test_list_backup_files_prerestore_only(tmp_path):
    _touch(tmp_path, "balancer_20240101_000000.db")
    _touch(tmp_path, "balancer_prerestore_20240102_000000.db")
    _touch(tmp_path, "balancer_prerestore_20240103_000000.db")
    files = _list_backup_files(str(tmp_path), prerestore=True)
    assert files == [
        "balancer_prerestore_20240102_000000.db",
        "balancer_prerestore_20240103_000000.db",
    ]


def test_prune_backups_removes_oldest_beyond_keep(tmp_path):
    names = [f"balancer_2024010{i}_000000.db" for i in range(1, 8)]  # 7 files
    for n in names:
        _touch(tmp_path, n)
    _prune_backups(str(tmp_path), keep=5)
    remaining = _list_backup_files(str(tmp_path), prerestore=False)
    assert remaining == names[2:]  # oldest 2 removed, 5 newest kept


def test_prune_backups_noop_when_under_keep(tmp_path):
    names = [f"balancer_2024010{i}_000000.db" for i in range(1, 4)]  # 3 files
    for n in names:
        _touch(tmp_path, n)
    _prune_backups(str(tmp_path), keep=5)
    remaining = _list_backup_files(str(tmp_path), prerestore=False)
    assert remaining == names


def test_prune_prerestore_backups_keeps_default_count(tmp_path):
    names = [f"balancer_prerestore_2024010{i}_000000.db" for i in range(1, 6)]  # 5 files
    for n in names:
        _touch(tmp_path, n)
    _prune_prerestore_backups(str(tmp_path))  # default keep=PRERESTORE_KEEP
    remaining = _list_backup_files(str(tmp_path), prerestore=True)
    assert len(remaining) == PRERESTORE_KEEP
    assert remaining == names[-PRERESTORE_KEEP:]
