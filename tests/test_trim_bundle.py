"""Coverage for the approved Qt bundle trimming list and safety checks."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def trimmer():
    script = Path(__file__).parents[1] / "tools" / "trim_bundle.py"
    spec = importlib.util.spec_from_file_location("trim_bundle", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _bundle_folder(tmp_path):
    bundle = tmp_path / "app" / "_internal" / "PySide6"
    bundle.mkdir(parents=True)
    return bundle


def _write_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test", encoding="utf-8")


def test_removes_approved_items_and_leaves_neighbours(tmp_path, trimmer, capsys):
    bundle = _bundle_folder(tmp_path)
    _write_file(bundle / "assistant.exe")
    _write_file(bundle / "Qt6Charts.dll")
    _write_file(bundle / "QtCharts.pyd")
    _write_file(bundle / "qml" / "QtCharts" / "qmldir")
    _write_file(bundle / "plugins" / "assetimporters" / "importer.dll")
    _write_file(bundle / "metatypes" / "qt6quick3d_metatypes.json")
    _write_file(bundle / "QtWebEngineProcess.exe")
    _write_file(bundle / "Qt6Quick.dll")

    assert trimmer.trim_bundle(bundle.parents[1]) == 0

    assert not (bundle / "assistant.exe").exists()
    assert not (bundle / "Qt6Charts.dll").exists()
    assert not (bundle / "QtCharts.pyd").exists()
    assert not (bundle / "qml" / "QtCharts").exists()
    assert not (bundle / "plugins" / "assetimporters").exists()
    assert not (bundle / "metatypes" / "qt6quick3d_metatypes.json").exists()
    assert (bundle / "QtWebEngineProcess.exe").exists()
    assert (bundle / "Qt6Quick.dll").exists()
    assert "Removed 6 approved item(s)." in capsys.readouterr().out


def test_tolerates_already_absent_items(tmp_path, trimmer, capsys):
    bundle = _bundle_folder(tmp_path)

    assert trimmer.trim_bundle(bundle.parents[1]) == 0

    assert capsys.readouterr().out == "Removed 0 approved item(s).\n"


def test_refuses_outside_or_wrong_qt_folder(tmp_path, trimmer, capsys):
    bundle = _bundle_folder(tmp_path)
    app = bundle.parents[1]
    outside = tmp_path / "outside" / "_internal" / "PySide6"
    outside.mkdir(parents=True)
    wrong_folder = app / "_internal" / "NotPySide6"
    wrong_folder.mkdir(parents=True)

    assert trimmer.trim_bundle(app, outside) == 1
    assert "outside the app folder" in capsys.readouterr().out
    assert trimmer.trim_bundle(app, wrong_folder) == 1
    assert "must end in _internal\\PySide6" in capsys.readouterr().out


def test_exits_nonzero_when_an_item_cannot_be_removed(
    tmp_path, trimmer, monkeypatch, capsys
):
    bundle = _bundle_folder(tmp_path)
    blocked = bundle / "assistant.exe"
    _write_file(blocked)

    def fail_remove(path):
        raise OSError("simulated locked file")

    monkeypatch.setattr(trimmer, "_remove_item", fail_remove)

    with pytest.raises(SystemExit) as exit_info:
        trimmer.main([str(bundle.parents[1])])

    assert exit_info.value.code == 1
    assert blocked.exists()
    output = capsys.readouterr().out
    assert "assistant.exe" in output
    assert "simulated locked file" in output
