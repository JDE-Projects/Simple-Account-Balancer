"""Database recovery and launch-backup orchestration."""

import os
from dataclasses import dataclass

from app import config, db, paths, utils
from app.services import backup


@dataclass
class StartupResult:
    """The database preparation outcome needed by the launch entry point."""

    outcome: str
    folders: list[str]


def prepare_database(api, db_path: str) -> StartupResult:
    """Recover, check, and snapshot an existing database before open_db."""
    if not os.path.exists(db_path):
        return StartupResult("continue", [])

    recovery = db.recover_and_check(db_path)
    if recovery.status == "damaged":
        api.log(f"Launch database check failed: {recovery.detail}")
        effective_dir, is_custom = backup.effective_backup_dir()
        folders = [effective_dir]
        default_dir = os.path.join(paths.app_dir(), config.BACKUP_DIRNAME)
        if is_custom:
            folders.append(default_dir)
        return StartupResult("damaged", folders)
    if recovery.status == "unavailable":
        api.log(f"Launch database unavailable: {recovery.detail}")
        return StartupResult("unavailable", [])
    if recovery.status == "empty":
        # Nothing to protect, so no backup: open_db sets the file up.
        api.log(f"Launch database check: {recovery.detail}")
        return StartupResult("continue", [])

    try:
        # Launch backup precedes open_db after recovery, so it remains a
        # pre-migration snapshot while SQLite has already resolved any hot journal.
        ok, used_fallback, actual_dir, prune_failed = backup._run_backup_with_fallback(
            db_path, source_conn=recovery.conn
        )
    finally:
        recovery.conn.close()

    folder_kind = "the fallback folder" if used_fallback else "the backup folder"
    if ok:
        api.log(f"Launch backup created in {folder_kind}")
    else:
        api.log(f"Launch backup failed, tried {folder_kind}")
    if used_fallback:
        api.log("Backup folder unreachable at launch, used the fallback folder")
    if not ok:
        api._add_backup_notice(f"Today's backup couldn't be saved. Tried to write it to {actual_dir}.")
    elif used_fallback:
        api._add_backup_notice(
            f"Backup folder wasn't reachable. Today's backup was saved to {actual_dir} instead."
        )
    if ok and prune_failed:
        api.log(f"Launch prune couldn't delete {len(prune_failed)} file(s) in {folder_kind}")
        api._add_backup_notice(utils._prune_failed_message(len(prune_failed)))
    if not ok and recovery.version < config.SCHEMA_VERSION:
        return StartupResult("upgrade_blocked", [actual_dir])
    return StartupResult("continue", [])
