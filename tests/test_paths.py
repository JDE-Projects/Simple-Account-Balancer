"""Where the app finds its bundled files and keeps its data."""
import os
import sys
from pathlib import Path

from app import paths

REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
# The shared isolation fixture replaces paths.app_dir during each test; keep
# the real function so these tests check it, not the replacement.
REAL_APP_DIR = paths.app_dir


def test_source_run_uses_repo_root_for_data_and_resources(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert REAL_APP_DIR() == REPOSITORY_ROOT
    assert paths.resource_path("simple_account_balancer-UI.html") == os.path.join(
        REPOSITORY_ROOT, "simple_account_balancer-UI.html"
    )
    assert os.path.isfile(paths.resource_path("simple_account_balancer-UI.html"))


def test_frozen_run_keeps_data_next_to_exe_and_resources_in_bundle(monkeypatch, tmp_path):
    exe = tmp_path / "install" / "Simple Account Balancer.exe"
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert REAL_APP_DIR() == str(exe.parent)
    assert paths.resource_path("fonts") == os.path.join(str(bundle), "fonts")
