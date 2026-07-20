"""Tests for how the Api handles a failed .pref write (save_prefs returning
False): user-facing settings must surface a real error, while account
operations whose db change already succeeded must not be failed just because
the pref write failed. All db access happens against a fresh sqlite file
inside tmp_path; save_prefs itself is monkeypatched so no real .pref file
needs to be involved."""
import simple_account_balancer as sab
from simple_account_balancer import Api, open_db


class _FakeWindow:
    """Stands in for the pywebview window; only create_file_dialog is used."""

    def __init__(self, folder):
        self._folder = folder

    def create_file_dialog(self, *_args, **_kwargs):
        return (self._folder,)


def _make_api(tmp_path, monkeypatch):
    # Redirect app_dir so any accidental real pref read/write during a test
    # stays inside tmp_path rather than touching the repo's own .pref file.
    monkeypatch.setattr(sab, "app_dir", lambda: str(tmp_path))
    conn = open_db(str(tmp_path / "test.db"))
    api = Api()
    api.set_conn(conn)
    api.set_db_path(str(tmp_path / "test.db"))
    return api


# --- Fix 2a: user-facing settings must report the failure -------------------

def test_choose_backup_folder_reports_error_when_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    api.set_window(_FakeWindow(str(tmp_path)))
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.choose_backup_folder()
    assert result["ok"] is False
    assert "error" in result


def test_choose_backup_folder_succeeds_when_pref_write_succeeds(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    api.set_window(_FakeWindow(str(tmp_path)))
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: True)
    result = api.choose_backup_folder()
    assert result["ok"] is True
    assert result["backup_folder"] == str(tmp_path)


def test_reset_backup_folder_reports_error_when_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.reset_backup_folder()
    assert result["ok"] is False
    assert "error" in result


def test_reset_backup_folder_succeeds_when_pref_write_succeeds(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: True)
    result = api.reset_backup_folder()
    assert result["ok"] is True
    assert result["backup_folder_is_custom"] is False


def test_set_backup_keep_reports_error_when_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.set_backup_keep(10)
    assert result["ok"] is False
    assert "error" in result


def test_set_backup_keep_succeeds_when_pref_write_succeeds(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: True)
    result = api.set_backup_keep(10)
    assert result["ok"] is True
    assert result["backup_keep"] == 10


# --- Fix 2b: account ops must succeed even when the pref write fails --------

def test_create_account_succeeds_even_if_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.create_account("Checking", "100.00", "2024-01-01")
    assert result["ok"] is True
    count = api._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert count == 1


def test_set_active_account_succeeds_even_if_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    # Real save_prefs (redirected to tmp_path via app_dir) so the setup
    # accounts actually land in the .pref file before the failure is forced.
    api.create_account("Checking", "100.00", "2024-01-01")
    second = api.create_account("Savings", "50.00", "2024-01-01")
    second_id = next(a["id"] for a in second["accounts"] if a["name"] == "Savings")

    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.set_active_account(second_id)
    assert result["ok"] is True
    assert result["account"]["id"] == second_id


def test_delete_account_succeeds_even_if_pref_write_fails(tmp_path, monkeypatch):
    api = _make_api(tmp_path, monkeypatch)
    # Real save_prefs (redirected to tmp_path via app_dir) so the setup
    # accounts actually land in the .pref file before the failure is forced.
    api.create_account("Checking", "100.00", "2024-01-01")
    second = api.create_account("Savings", "50.00", "2024-01-01")
    second_id = next(a["id"] for a in second["accounts"] if a["name"] == "Savings")

    monkeypatch.setattr(sab, "save_prefs", lambda prefs: False)
    result = api.delete_account(second_id)
    assert result["ok"] is True
    remaining = api._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert remaining == 1
