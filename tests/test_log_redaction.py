"""Debug log redaction: no file paths or user-typed values reach the log."""
import pytest

import simple_account_balancer as sab
from test_rollback import _make_api


@pytest.mark.parametrize(
    "line, expected",
    [
        (r"Exported to C:\Users\John\Documents\Checking_Export.csv", "Exported to <path>"),
        ("Exported to C:/Users/John/Documents/x.csv", "Exported to <path>"),
        (r"tried \\nas\share\backups", "tried <path>"),
        ("tried //nas/share/backups", "tried <path>"),
        (
            r"failed: [Errno 13] Permission denied: 'C:\\Users\\John\\balancer.db'",
            "failed: [Errno 13] Permission denied: '<path>'",
        ),
        (
            r"""failed: [Errno 2] No such file or directory: "C:\\Users\\O'Brien\\x.db" """,
            """failed: [Errno 2] No such file or directory: "<path>" """,
        ),
        (
            "failed: invalid literal for int() with base 10: 'Rent'",
            "failed: invalid literal for int() with base 10: '<text>'",
        ),
    ],
)
def test_redact_log_text_strips_paths_and_quoted_values(line, expected):
    assert sab.redact_log_text(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "restore_backup couldn't clean up its staged backup",
        "Account 3 updated, starting as of 2024-01-01",
        "check_update failed: HTTPError: HTTP Error 403: rate limit exceeded",
        "check_update: https://api.github.com/repos/JDE-Projects/x/releases/latest",
        "find_compare_matches: exact=1 half=0 transposition_hint=False",
    ],
)
def test_redact_log_text_keeps_plain_lines(line):
    assert sab.redact_log_text(line) == line


def _debug_api(tmp_path, monkeypatch):
    log_dir = tmp_path / "logdir"
    log_dir.mkdir()
    monkeypatch.setattr(sab, "app_dir", lambda: str(log_dir))
    api, _, _ = _make_api(tmp_path, monkeypatch)
    assert api.set_debug(True) == {"ok": True, "enabled": True}
    return api, log_dir


def _log_text(log_dir):
    (log_file,) = log_dir.glob("Debug_Log_*.txt")
    return log_file.read_text(encoding="utf-8")


def test_log_redacts_every_line_written(tmp_path, monkeypatch):
    api, log_dir = _debug_api(tmp_path, monkeypatch)
    api.log(r"open failed: [Errno 13] Permission denied: 'C:\\Users\\John\\secret.db'")
    text = _log_text(log_dir)
    assert "John" not in text
    assert "Permission denied: '<path>'" in text


def test_export_log_line_has_no_path_or_account_name(tmp_path, monkeypatch):
    api, log_dir = _debug_api(tmp_path, monkeypatch)
    api._conn.execute("UPDATE accounts SET name='Joint Savings' WHERE id=1")
    api._conn.commit()
    out_path = tmp_path / "Joint Savings_Export.csv"

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return (str(out_path),)

    api.set_window(Window())
    assert api.export_csv(1, "", "")["ok"] is True
    text = _log_text(log_dir)
    assert "Exported 0 transactions to CSV" in text
    assert "Joint Savings" not in text
    assert str(tmp_path) not in text
