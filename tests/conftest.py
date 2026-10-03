"""Shared test isolation and repository-write protection."""

from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_DIRECTORY = REPOSITORY_ROOT / "app"
IGNORED_GUARD_DIRECTORIES = {".git", "__pycache__", ".pytest_cache", ".venv", ".ruff_cache"}
APP_DATA_PATCH_TARGETS = {
    "app_dir": "app.paths.app_dir",
    "effective_backup_dir": "simple_account_balancer.effective_backup_dir",
}


def _guarded_paths(folder: Path) -> set[Path]:
    """Return all non-cache paths below a guarded folder."""
    return {
        path.relative_to(REPOSITORY_ROOT)
        for path in folder.rglob("*")
        if not any(part in IGNORED_GUARD_DIRECTORIES for part in path.relative_to(REPOSITORY_ROOT).parts)
    }


def _repository_paths() -> set[Path]:
    return _guarded_paths(REPOSITORY_ROOT) | _guarded_paths(APP_DIRECTORY)


_paths_before_session: set[Path] = set()


def pytest_sessionstart(session):
    """Record the repository before collection imports any test module."""
    _paths_before_session.update(_repository_paths())


@pytest.fixture(scope="session", autouse=True)
def reject_new_repository_paths():
    """Fail loudly when a test leaves a new non-cache path in the repository."""
    yield
    created = sorted(_repository_paths() - _paths_before_session)
    if created:
        pytest.fail(
            "Repository write guard detected new path(s):\n"
            + "\n".join(f"- {path.as_posix()}" for path in created)
        )


@pytest.fixture(autouse=True)
def isolate_app_data(tmp_path, monkeypatch):
    """Keep app data and default backups within each test's temporary folder."""
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(APP_DATA_PATCH_TARGETS["app_dir"], lambda: str(tmp_path))
    monkeypatch.setattr(
        APP_DATA_PATCH_TARGETS["effective_backup_dir"],
        lambda: (str(backups_dir), False),
    )
