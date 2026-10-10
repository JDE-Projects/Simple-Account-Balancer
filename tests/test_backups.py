"""Tests for the backup-file helpers: keep-count clamping, timestamp
parsing, and pruning. All file operations happen inside tmp_path; nothing
here touches a real backups/ folder."""
import datetime
import os
import sqlite3

from app import config, db as app_db, paths, prefs, utils
import app.services.backup as sab
from app.api import Api
from app.services.backup import (
    _clamp_backup_keep,
    _list_backup_files,
    _parse_backup_timestamp,
    _prune_backups,
    _prune_prerestore_backups,
    _make_backup,
    _make_prerestore_backup,
    _run_backup_with_fallback,
)


# --- _clamp_backup_keep -------------------------------------------------------

def test_clamp_backup_keep_within_bounds_unchanged():
    assert _clamp_backup_keep(10) == 10


def test_clamp_backup_keep_floors_at_min():
    assert _clamp_backup_keep(0) == config.BACKUP_KEEP_MIN


def test_clamp_backup_keep_ceils_at_max():
    assert _clamp_backup_keep(100) == config.BACKUP_KEEP_MAX


def test_clamp_backup_keep_non_numeric_string_falls_back_to_default():
    assert _clamp_backup_keep("abc") == config.BACKUP_KEEP


def test_clamp_backup_keep_none_falls_back_to_default():
    assert _clamp_backup_keep(None) == config.BACKUP_KEEP


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
    _prune_prerestore_backups(str(tmp_path))  # default keep=config.PRERESTORE_KEEP
    remaining = _list_backup_files(str(tmp_path), prerestore=True)
    assert len(remaining) == config.PRERESTORE_KEEP
    assert remaining == names[-config.PRERESTORE_KEEP:]


# --- exact-name matching: look-alike files are never listed or pruned ------------

# Files a shared backup folder might hold that share the balancer_*.db shape
# but are not this app's backups.
LOOK_ALIKES = [
    "balancer_old.db",
    "balancer_.db",
    "balancer_main.db",
    "balancer_x_20240101_000000.db",
    "balancer_2024010_000000.db",           # 7-digit date
    "balancer_20240101_0000000.db",         # 7-digit time
    "balancer_20240101000000.db",           # no separator
    "balancer_20240101_000000.db.bak",
    "balancer_20230101_000000.DB",          # dates unused elsewhere: Windows
    "Balancer_20230102_000000.db",          # names are case-insensitive
    "balancer_prerestore_old.db",
    "balancer_prerestore_x_20240101_000000.db",
    "balancer_٢٠٢٤٠١٠١_000000.db",  # non-ASCII digits
    # Script-shaped names the restore list once pasted into an onclick.
    "balancer_');api().export_csv(1);('.db",
    "balancer_20240101_000000');alert(1);('.db",
    "balancer_prerestore_20240101_000000');alert(1);('.db",
]

# Right shape, impossible date or time. All but the last two sort before every
# real backup, so a shape-only check would prune them first.
IMPOSSIBLE_DATES = [
    "balancer_00000000_000000.db",
    "balancer_20230230_000000.db",          # Feb 30
    "balancer_20230001_000000.db",          # month 00
    "balancer_20230100_000000_000001.db",   # day 00
    "balancer_20230229_000000.db",          # Feb 29, not a leap year
    "balancer_20231301_000000.db",          # month 13
    "balancer_20230105_250000.db",          # hour 25
    "balancer_20230106_006000.db",          # minute 60
    "balancer_prerestore_00000000_000000.db",
    "balancer_prerestore_20230230_000000_000001.db",
]
LOOK_ALIKES += IMPOSSIBLE_DATES


def test_backup_filename_re_rejects_trailing_newline():
    assert not sab.BACKUP_FILENAME_RE.match("balancer_20240101_000000.db\n")


def test_is_backup_filename_rejects_impossible_dates():
    for n in IMPOSSIBLE_DATES:
        assert not sab._is_backup_filename(n), n


def test_is_backup_filename_accepts_real_dates():
    for n in ["balancer_20240229_235959.db",            # leap day
              "balancer_20240101_000000_000001.db",
              "balancer_prerestore_19991231_120000.db"]:
        assert sab._is_backup_filename(n), n


def test_restore_refuses_impossible_date_names(tmp_path, monkeypatch):
    # Each bad name is a real, restorable copy, so only the name check can
    # stop it.
    api, backups = _api_with_one_backup(tmp_path, monkeypatch)
    for n in IMPOSSIBLE_DATES:
        sab.shutil.copy2(backups / "balancer_20240101_000000.db", backups / n)
    try:
        for n in IMPOSSIBLE_DATES:
            assert api.restore_backup(n) == {
                "ok": False,
                "error": "That doesn't look like one of this app's backup files.",
            }, n
    finally:
        api.close_conn()
    assert sorted(os.listdir(backups)) == sorted(IMPOSSIBLE_DATES + ["balancer_20240101_000000.db"])


def test_list_backup_files_ignores_look_alikes(tmp_path):
    _touch(tmp_path, "balancer_20240101_000000.db")
    _touch(tmp_path, "balancer_prerestore_20240102_000000.db")
    for n in LOOK_ALIKES:
        _touch(tmp_path, n)
    assert _list_backup_files(str(tmp_path), prerestore=False) == ["balancer_20240101_000000.db"]
    assert _list_backup_files(str(tmp_path), prerestore=True) == ["balancer_prerestore_20240102_000000.db"]


def test_prune_backups_never_deletes_look_alikes(tmp_path):
    # The look-alikes would sort before every real backup, so a loose match
    # would prune them first.
    for n in LOOK_ALIKES:
        _touch(tmp_path, n)
    names = [f"balancer_2024010{i}_000000.db" for i in range(1, 8)]
    for n in names:
        _touch(tmp_path, n)
    _prune_backups(str(tmp_path), keep=5)
    _prune_prerestore_backups(str(tmp_path), keep=1)
    left = set(os.listdir(tmp_path))
    assert set(LOOK_ALIKES) <= left
    assert _list_backup_files(str(tmp_path), prerestore=False) == names[2:]


def test_list_backups_shows_only_exact_names(tmp_path, monkeypatch):
    monkeypatch.setattr(sab, "effective_backup_dir", lambda: (str(tmp_path), False))
    _touch(tmp_path, "balancer_20240101_000000.db")
    _touch(tmp_path, "balancer_prerestore_20240102_000000.db")
    for n in LOOK_ALIKES:
        _touch(tmp_path, n)
    result = Api("test").list_backups()
    assert result["ok"] is True
    assert [b["filename"] for b in result["backups"]] == [
        "balancer_prerestore_20240102_000000.db",
        "balancer_20240101_000000.db",
    ]


# --- _run_backup_with_fallback -------------------------------------------------

def test_run_backup_with_fallback_reports_failure_when_source_missing(tmp_path, monkeypatch):
    # No backup_folder pref is set (app_dir is redirected to an empty
    # tmp_path), so this exercises the default-folder path; the source db
    # simply doesn't exist, so the copy itself can't succeed either way.
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    missing_db = str(tmp_path / "does_not_exist.db")
    ok, used_fallback, actual_dir, prune_failed = _run_backup_with_fallback(missing_db)
    assert ok is False
    assert used_fallback is False
    assert prune_failed == []


# --- collision-safe names: microseconds, old names still work -------------------

def test_parse_backup_timestamp_name_with_microseconds(tmp_path):
    name = "balancer_20240115_143000_123456.db"
    assert _parse_backup_timestamp(name, str(tmp_path / name)) == "2024-01-15T14:30:00"


def test_list_backup_files_mixes_old_and_new_names_in_time_order(tmp_path):
    names = [
        "balancer_20240101_000000.db",
        "balancer_20240101_000000_000001.db",
        "balancer_20240101_000000_999999.db",
        "balancer_20240101_000001.db",
        "balancer_20240101_000001_500000.db",
    ]
    for n in reversed(names):
        _touch(tmp_path, n)
    assert _list_backup_files(str(tmp_path), prerestore=False) == names


def test_prune_mixed_names_removes_oldest(tmp_path):
    names = [
        "balancer_20240101_000000.db",
        "balancer_20240101_000000_000001.db",
        "balancer_20240102_000000.db",
        "balancer_20240102_000000_000001.db",
    ]
    for n in names:
        _touch(tmp_path, n)
    _prune_backups(str(tmp_path), keep=2)
    assert _list_backup_files(str(tmp_path), prerestore=False) == names[2:]


class _SteppingNow:
    """Stands in for the datetime module so now() returns the given times in
    order, to force a backup name that is already taken."""

    def __init__(self, times):
        pending = list(times)

        class _DT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return pending.pop(0)

        self.datetime = _DT


def test_make_backup_never_overwrites_a_taken_name(tmp_path, monkeypatch):
    t = datetime.datetime(2024, 1, 1, 9, 0, 0, 0)
    monkeypatch.setattr(sab, "datetime", _SteppingNow([t, t + datetime.timedelta(microseconds=1)]))
    backups = tmp_path / "backups"
    backups.mkdir()
    taken = backups / "balancer_20240101_090000_000000.db"
    taken.write_text("earlier backup")
    db = tmp_path / "live.db"
    conn = app_db.open_db(str(db))
    conn.close()
    ok, prune_failed = _make_backup(str(db), str(backups), keep=5)
    assert ok is True and prune_failed == []
    assert taken.read_text() == "earlier backup"
    assert (backups / "balancer_20240101_090000_000001.db").read_bytes() == db.read_bytes()


def test_new_backups_carry_microseconds(tmp_path):
    db = tmp_path / "live.db"
    conn = app_db.open_db(str(db))
    conn.close()
    backups = tmp_path / "backups"
    ok, _ = _make_backup(str(db), str(backups), keep=5)
    path, _ = _make_prerestore_backup(str(db), str(backups))
    assert ok is True and path is not None
    names = os.listdir(backups)
    assert len(names) == 2
    for n in names:
        m = sab.BACKUP_FILENAME_RE.match(n)
        assert m and m.group(3)


def test_list_backups_breaks_same_second_ties_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sab, "effective_backup_dir", lambda: (str(tmp_path), False))
    for n in ["balancer_20240101_000000.db", "balancer_20240101_000000_000002.db",
              "balancer_20240101_000000_000001.db"]:
        _touch(tmp_path, n)
    listed = [b["filename"] for b in Api("test").list_backups()["backups"]]
    assert listed == [
        "balancer_20240101_000000_000002.db",
        "balancer_20240101_000000_000001.db",
        "balancer_20240101_000000.db",
    ]


# --- failed prunes are reported, not swallowed ----------------------------------

def _fail_removing(monkeypatch, bad_names):
    real_remove = os.remove

    def remove(path):
        if os.path.basename(path) in bad_names:
            raise PermissionError("locked")
        real_remove(path)

    monkeypatch.setattr(os, "remove", remove)


def test_prune_reports_files_it_could_not_delete(tmp_path, monkeypatch):
    names = [f"balancer_2024010{i}_000000.db" for i in range(1, 6)]
    for n in names:
        _touch(tmp_path, n)
    _fail_removing(monkeypatch, {names[0]})
    assert _prune_backups(str(tmp_path), keep=2) == [names[0]]
    assert sorted(os.listdir(tmp_path)) == [names[0]] + names[3:]


def test_prune_prerestore_reports_files_it_could_not_delete(tmp_path, monkeypatch):
    names = [f"balancer_prerestore_2024010{i}_000000.db" for i in range(1, 5)]
    for n in names:
        _touch(tmp_path, n)
    _fail_removing(monkeypatch, {names[0]})
    assert _prune_prerestore_backups(str(tmp_path), keep=2) == [names[0]]


def test_make_backup_passes_prune_failures_up(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    old = "balancer_20200101_000000.db"
    _touch(backups, old)
    db = tmp_path / "live.db"
    conn = app_db.open_db(str(db))
    conn.close()
    _fail_removing(monkeypatch, {old})
    assert _make_backup(str(db), str(backups), keep=1) == (True, [old])


def test_run_backup_with_fallback_passes_prune_failures_up(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(prefs, "load_prefs", lambda: {"backup_keep": config.BACKUP_KEEP_MIN})
    backups = tmp_path / config.BACKUP_DIRNAME
    backups.mkdir()
    old = [f"balancer_2020010{i}_000000.db" for i in range(1, 4)]
    for n in old:
        _touch(backups, n)
    db = tmp_path / "live.db"
    conn = app_db.open_db(str(db))
    conn.close()
    _fail_removing(monkeypatch, {old[0]})
    ok, used_fallback, _, prune_failed = _run_backup_with_fallback(str(db))
    assert ok is True and used_fallback is False
    assert prune_failed == [old[0]]


def test_prune_failed_message_names_the_count():
    assert "2 old backups." in utils._prune_failed_message(2)
    assert "1 old backup." in utils._prune_failed_message(1)


def _api_with_one_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(sab, "effective_backup_dir", lambda: (str(backups), False))
    db_path = str(tmp_path / "live.db")
    api = Api("test")
    api.set_conn(app_db.open_db(db_path))
    api.set_db_path(db_path)
    api.create_account("Checking", "100.00", "2024-01-01")
    api._conn.commit()
    sab.shutil.copy2(db_path, backups / "balancer_20240101_000000.db")
    return api, backups


def test_restore_backup_warns_when_prerestore_prune_fails(tmp_path, monkeypatch):
    api, backups = _api_with_one_backup(tmp_path, monkeypatch)
    old_safety = [f"balancer_prerestore_2020010{i}_000000.db" for i in range(1, 5)]
    for n in old_safety:
        _touch(backups, n)
    _fail_removing(monkeypatch, {old_safety[0]})
    result = api.restore_backup("balancer_20240101_000000.db")
    api.close_conn()
    assert result["ok"] is True
    assert "Couldn't delete 1 old backup" in result["warning"]


def test_restore_backup_has_no_warning_when_prune_succeeds(tmp_path, monkeypatch):
    api, _ = _api_with_one_backup(tmp_path, monkeypatch)
    result = api.restore_backup("balancer_20240101_000000.db")
    api.close_conn()
    assert result["ok"] is True
    assert "warning" not in result


def test_backup_notice_messages_join_in_order():
    api = Api("test")
    assert api._backup_notice is None
    api._add_backup_notice("First.")
    api._add_backup_notice("Second.")
    assert api._backup_notice == "First. Second."


# --- candidate validation and publication ---------------------------------------

def _raise_permission_error(*args):
    raise PermissionError("locked")


def _make_real_db(path, marker=0):
    conn = app_db.open_db(str(path))
    conn.execute(f"PRAGMA application_id = {marker}")
    conn.close()


def test_make_backup_rejects_invalid_live_dbs_without_pruning(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    originals = {}
    for index in range(5):
        name = f"balancer_2024010{index + 1}_000000.db"
        path = backups / name
        _make_real_db(path, index + 1)
        originals[name] = path.read_bytes()
    corrupt = tmp_path / "corrupt.db"
    _make_real_db(corrupt)
    with corrupt.open("r+b") as stream:
        stream.seek(100)
        stream.write(b"not a database")
    non_sqlite = tmp_path / "not-sqlite.db"
    non_sqlite.write_bytes(b"not sqlite")
    for live_db in (corrupt, non_sqlite):
        for _ in range(6):
            assert _make_backup(str(live_db), str(backups), keep=5) == (False, [])
        assert {name: (backups / name).read_bytes() for name in originals} == originals
        assert sorted(os.listdir(backups)) == sorted(originals)


def test_make_backup_rejects_empty_live_db(tmp_path):
    live_db = tmp_path / "empty.db"
    live_db.touch()
    backups = tmp_path / "backups"
    assert _make_backup(str(live_db), str(backups), keep=5) == (False, [])
    assert os.listdir(backups) == []


def test_make_backup_accepts_newer_version_and_prunes_valid_backup(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(5):
        _make_real_db(backups / f"balancer_2024010{index + 1}_000000.db", index)
    live_db = tmp_path / "live.db"
    _make_real_db(live_db, 99)
    conn = sqlite3.connect(live_db)
    conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION + 1}")
    conn.close()
    assert _make_backup(str(live_db), str(backups), keep=5) == (True, [])
    retained = _list_backup_files(str(backups), prerestore=False)
    assert len(retained) == 5
    conn = app_db._readonly_connection(str(backups / retained[-1]))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
    finally:
        conn.close()
    assert not any(sab.BACKUP_CANDIDATE_FILENAME_RE.fullmatch(n) for n in os.listdir(backups))


def test_make_backup_removes_matching_leftover_candidate_only(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    candidate = backups / ".balancer_backup_candidate_0123456789abcdef0123456789abcdef.db"
    candidate.write_bytes(b"left over")
    look_alike = backups / ".balancer_backup_candidate_not-hex.db"
    look_alike.write_bytes(b"keep")
    live_db = tmp_path / "live.db"
    _make_real_db(live_db)
    assert _make_backup(str(live_db), str(backups), keep=5) == (True, [])
    assert not candidate.exists()
    assert look_alike.read_bytes() == b"keep"


def test_make_backup_removes_candidate_when_publication_fails(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    original = backups / "balancer_20240101_000000.db"
    _make_real_db(original)
    original_bytes = original.read_bytes()
    live_db = tmp_path / "live.db"
    _make_real_db(live_db)
    monkeypatch.setattr(sab.os, "rename", _raise_permission_error)
    assert _make_backup(str(live_db), str(backups), keep=1) == (False, [])
    assert original.read_bytes() == original_bytes
    assert _list_backup_files(str(backups), prerestore=False) == [original.name]
    assert not any(sab.BACKUP_CANDIDATE_FILENAME_RE.fullmatch(n) for n in os.listdir(backups))


def test_make_backup_continues_when_stale_candidate_cleanup_fails(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    stale = backups / ".balancer_backup_candidate_0123456789abcdef0123456789abcdef.db"
    stale.write_bytes(b"left over")
    live_db = tmp_path / "live.db"
    _make_real_db(live_db)
    monkeypatch.setattr(sab.db, "_remove_db_artifacts", _raise_permission_error)
    assert _make_backup(str(live_db), str(backups), keep=5) == (True, [])
    assert stale.exists()
