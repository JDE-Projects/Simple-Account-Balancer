"""Autopay posting and failure reporting coverage."""
import datetime

import pytest

from app import db
import app.api as sab
from app.services import autopays, backup

POST_FAILURE_NOTICE = (
    "Autopays couldn't be added to the register today. Nothing was posted, and "
    "the app will try again next launch."
)
POST_FAILURE_ERROR = (
    "The autopay was saved, but payments already due couldn't be added. "
    "The app will try again next launch."
)


def _make_api(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(backup, "effective_backup_dir", lambda: (str(backups), False))
    db_path = str(tmp_path / "live.db")
    api = sab.Api("test")
    api.set_conn(db.open_db(db_path))
    api.set_db_path(db_path)
    api._conn.execute(
        "INSERT INTO accounts (name, starting_balance_cents, starting_date, created_at) "
        "VALUES ('Checking', 0, '2024-01-01', '2024-01-01')"
    )
    api._conn.commit()
    return api


def _freeze_today(monkeypatch, iso_date):
    real_date = datetime.date

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(iso_date)

    monkeypatch.setattr(autopays.datetime, "date", FrozenDate)


def _add_rule(api, payee, post_date, pay_date, is_variable=0):
    api._conn.execute(
        "INSERT INTO autopays "
        "(account_id, payee, category, notes, amount_cents, next_pay_date, next_post_date, "
        "pay_day, post_day, is_variable, created_at) "
        "VALUES (1, ?, 'Bills', 'note', -12345, ?, ?, ?, ?, ?, '2024-01-01')",
        (payee, pay_date, post_date, int(pay_date[-2:]), int(post_date[-2:]), is_variable),
    )
    api._conn.commit()


def _transaction_rows(api):
    return [
        tuple(row)
        for row in api._conn.execute(
            "SELECT date, payee, estimated FROM transactions ORDER BY id"
        ).fetchall()
    ]


def _rule_dates(api):
    return [
        tuple(row)
        for row in api._conn.execute(
            "SELECT next_post_date, next_pay_date FROM autopays ORDER BY id"
        ).fetchall()
    ]


def test_posts_on_register_date_with_pay_date(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    _add_rule(api, "Rent", "2024-02-10", "2024-02-15")

    assert api.post_due_autopays() == 1
    assert _transaction_rows(api) == [("2024-02-15", "Rent", 0)]
    api.close_conn()


def test_not_yet_due_posts_nothing(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-09")
    _add_rule(api, "Rent", "2024-02-10", "2024-02-15")

    assert api.post_due_autopays() == 0
    assert _transaction_rows(api) == []
    api.close_conn()


def test_catch_up_posts_missed_months_and_advances_dates(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-03-15")
    _add_rule(api, "Rent", "2024-01-01", "2024-01-05")

    assert api.post_due_autopays() == 3
    assert _transaction_rows(api) == [
        ("2024-01-05", "Rent", 0),
        ("2024-02-05", "Rent", 0),
        ("2024-03-05", "Rent", 0),
    ]
    assert _rule_dates(api) == [("2024-04-01", "2024-04-05")]
    api.close_conn()


def test_variable_and_fixed_autopays_set_estimated_flag(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    _add_rule(api, "Fixed", "2024-02-10", "2024-02-10")
    _add_rule(api, "Variable", "2024-02-10", "2024-02-10", is_variable=1)

    assert api.post_due_autopays() == 2
    assert _transaction_rows(api) == [
        ("2024-02-10", "Fixed", 0),
        ("2024-02-10", "Variable", 1),
    ]
    api.close_conn()


def test_second_run_same_day_does_not_double_post(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    _add_rule(api, "Rent", "2024-02-10", "2024-02-15")

    assert api.post_due_autopays() == 1
    assert api.post_due_autopays() == 0
    assert _transaction_rows(api) == [("2024-02-15", "Rent", 0)]
    api.close_conn()


def test_post_failure_rolls_back_rules_and_transactions_and_sets_notice(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    _add_rule(api, "First", "2024-02-10", "2024-02-10")
    _add_rule(api, "Second", "2024-02-10", "2024-02-10")
    before_dates = _rule_dates(api)
    api._conn.execute(
        "CREATE TRIGGER abort_second_autopay BEFORE INSERT ON transactions "
        "WHEN NEW.payee = 'Second' BEGIN SELECT RAISE(ABORT, 'abort'); END"
    )
    api._conn.commit()

    assert api.post_due_autopays() is None
    assert _transaction_rows(api) == []
    assert _rule_dates(api) == before_dates
    assert api.autopay_notice == POST_FAILURE_NOTICE
    assert api.autopay_notice_is_error is True
    config = api.get_config()
    assert config["autopay_notice"] == POST_FAILURE_NOTICE
    assert config["autopay_notice_is_error"] is True
    api.close_conn()


def test_failed_database_guard_reports_failed_autopay_post(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    api._database_failed = True

    assert api.post_due_autopays() is None
    api._conn.close()


@pytest.mark.parametrize("method", ["add", "update"])
def test_saved_autopay_reports_failed_due_post(tmp_path, monkeypatch, method):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    monkeypatch.setattr(api, "post_due_autopays", lambda: None)

    if method == "add":
        result = api.add_autopay(
            1, "Rent", "Bills", "", "123.45", "withdraw", "2024-02-10", "2024-02-10"
        )
    else:
        _add_rule(api, "Rent", "2024-03-10", "2024-03-10")
        autopay_id = api._conn.execute("SELECT id FROM autopays").fetchone()[0]
        result = api.update_autopay(
            autopay_id, "Rent", "Bills", "", "123.45", "withdraw", "2024-02-10", "2024-02-10"
        )

    assert result["ok"] is True
    assert result["posted"] == 0
    assert result["post_failed"] is True
    assert result["post_error"] == POST_FAILURE_ERROR
    assert [tuple(row) for row in api._conn.execute("SELECT payee FROM autopays").fetchall()] == [("Rent",)]
    assert _transaction_rows(api) == []
    assert api.autopay_notice is None
    assert api.autopay_notice_is_error is False
    api.close_conn()


def test_success_keeps_existing_notice_wording_and_clears_error_flag(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    _freeze_today(monkeypatch, "2024-02-10")
    _add_rule(api, "Rent", "2024-02-10", "2024-02-10")
    api.autopay_notice_is_error = True

    assert api.post_due_autopays() == 1
    assert api.autopay_notice == "Added 1 autopay to the register."
    assert api.autopay_notice_is_error is False
    api.close_conn()
