"""Tests for open_db and NewerSchemaError, using a fresh SQLite file inside
tmp_path. No app data folder or UI involved."""
import sqlite3

import pytest

from simple_account_balancer import (
    NewerSchemaError,
    SCHEMA_VERSION,
    SEED_CATEGORIES,
    open_db,
)


def test_open_db_creates_expected_tables(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"accounts", "transactions", "categories", "autopays"}.issubset(tables)
    finally:
        conn.close()


def test_open_db_sets_schema_version(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_open_db_seeds_categories_on_fresh_db(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == len(SEED_CATEGORIES)
    finally:
        conn.close()


def test_open_db_does_not_reseed_deleted_categories_on_reopen(tmp_path):
    path = str(tmp_path / "test.db")
    conn = open_db(path)
    conn.execute("DELETE FROM categories")
    conn.commit()
    conn.close()

    # Reopening must not re-add the categories the user deliberately deleted.
    conn2 = open_db(path)
    try:
        count = conn2.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == 0
    finally:
        conn2.close()


def test_open_db_migration_adds_starting_balance_columns(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
        assert "starting_balance_prev_cents" in cols
        assert "starting_balance_changed_at" in cols
    finally:
        conn.close()


def test_open_db_migration_adds_estimated_and_sort_key_columns(tmp_path):
    conn = open_db(str(tmp_path / "test.db"))
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        assert "estimated" in cols
        assert "sort_key" in cols
    finally:
        conn.close()


def test_open_db_raises_on_newer_schema_version(tmp_path):
    path = str(tmp_path / "test.db")
    # Simulate a database written by a future build.
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(NewerSchemaError):
        open_db(path)


def test_open_db_leaves_newer_schema_db_untouched(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(NewerSchemaError):
        open_db(path)

    # The rejected database must still report its original (newer) version,
    # not have been altered or migrated.
    conn2 = sqlite3.connect(path)
    version = conn2.execute("PRAGMA user_version").fetchone()[0]
    conn2.close()
    assert version == SCHEMA_VERSION + 1
