"""Remove approved Qt components from a PyInstaller app bundle."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# Qt developer tools. QtWebEngineProcess.exe is intentionally not listed.
DEVELOPER_TOOLS = (
    "assistant.exe", "balsam.exe", "balsamui.exe", "designer.exe", "linguist.exe",
    "lrelease.exe", "lupdate.exe", "qmlcachegen.exe", "qmlformat.exe",
    "qmlimportscanner.exe", "qmllint.exe", "qmlls.exe", "qmltyperegistrar.exe",
    "qsb.exe", "rcc.exe", "svgtoqml.exe", "uic.exe",
)

# GPL-only Qt module DLLs.
GPL_DLLS = tuple(
    f"{name}.dll"
    for name in (
        "Qt6CanvasPainter", "Qt6Charts", "Qt6ChartsQml", "Qt6DataVisualization",
        "Qt6DataVisualizationQml", "Qt6Graphs", "Qt6GraphsWidgets", "Qt6HttpServer",
        "Qt6Lottie", "Qt6LottieVectorImageGenerator", "Qt6LottieVectorImageHelpers",
        "Qt6NetworkAuth", "Qt6Quick3D", "Qt6Quick3DAssetImport", "Qt6Quick3DAssetUtils",
        "Qt6Quick3DEffects", "Qt6Quick3DGlslParser", "Qt6Quick3DHelpers",
        "Qt6Quick3DHelpersImpl", "Qt6Quick3DIblBaker", "Qt6Quick3DParticleEffects",
        "Qt6Quick3DParticles", "Qt6Quick3DRuntimeRender", "Qt6Quick3DSpatialAudio",
        "Qt6Quick3DUtils", "Qt6Quick3DXr", "Qt6QuickTimeline",
        "Qt6QuickTimelineBlendTrees", "Qt6VirtualKeyboard", "Qt6VirtualKeyboardQml",
        "Qt6VirtualKeyboardSettings",
    )
)

# GPL-only Qt Python bindings.
PYTHON_BINDINGS = tuple(
    f"{name}{suffix}"
    for name in (
        "QtCanvasPainter", "QtCharts", "QtDataVisualization", "QtGraphs",
        "QtGraphsWidgets", "QtHttpServer", "QtNetworkAuth", "QtQuick3D",
    )
    for suffix in (".pyd", ".pyi")
)

# QML modules, plugins, and the Quick 3D asset importer.
QML_AND_PLUGINS = (
    "qml/QtCharts", "qml/QtDataVisualization", "qml/QtGraphs", "qml/QtQuick3D",
    "qml/QtQuick/Timeline", "qml/QtQuick/VirtualKeyboard",
    "plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "plugins/qmltooling/qmldbg_quick3dprofiler.dll",
    "plugins/vectorimageformats/qlottievectorimage.dll", "plugins/assetimporters",
)

# Developer leftovers for the removed GPL-only modules.
DEVELOPER_LEFTOVERS = (
    "glue/qtcanvaspainter.cpp", "glue/qtcharts.cpp", "glue/qtdatavisualization.cpp",
    "glue/qtgraphs.cpp", "glue/qtnetworkauth.cpp", "glue/qtquick3d.cpp",
    "doc/qtcanvaspainter.rst", "include/QtCanvasPainter", "include/QtCharts",
    "include/QtDataVisualization", "include/QtGraphs", "include/QtGraphsWidgets",
    "include/QtHttpServer", "include/QtNetworkAuth", "include/QtQuick3D",
    "typesystems/datavisualization_common.xml",
    "typesystems/typesystem_canvaspainter.xml", "typesystems/typesystem_charts.xml",
    "typesystems/typesystem_datavisualization.xml", "typesystems/typesystem_graphs.xml",
    "typesystems/typesystem_graphswidgets.xml", "typesystems/typesystem_httpserver.xml",
    "typesystems/typesystem_networkauth.xml", "typesystems/typesystem_quick3d.xml",
)

# Metatype files for the approved GPL-only modules. The wildcard covers private variants.
METATYPE_PATTERNS = tuple(
    f"metatypes/qt6{name}*_metatypes.json"
    for name in (
        "canvaspainter", "charts", "chartsqml", "datavisualization",
        "datavisualizationqml", "graphs", "graphswidgets", "httpserver", "lottie",
        "lottievectorimagegeneratorprivate", "lottievectorimagehelpers", "networkauth",
        "quick3d", "quick3dassetimport", "quick3dassetutils", "quick3deffects",
        "quick3dglslparserprivate", "quick3dhelpers", "quick3diblbaker",
        "quick3dparticleeffects", "quick3dparticles", "quick3druntimerender",
        "quick3dutils", "quick3dxr", "quicktimeline", "virtualkeyboard",
    )
)

LITERAL_PATHS = DEVELOPER_TOOLS + GPL_DLLS + PYTHON_BINDINGS + QML_AND_PLUGINS + DEVELOPER_LEFTOVERS
EXPECTED_BUNDLE_PATH = Path("_internal") / "PySide6"


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _resolve_bundle_folder(app_folder: Path, target_folder: Path | None = None) -> Path:
    """Return a safe resolved PySide6 folder or raise ValueError."""
    app_folder = app_folder.resolve(strict=False)
    target_folder = target_folder or app_folder / EXPECTED_BUNDLE_PATH
    try:
        target_folder = target_folder.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"Qt bundle folder does not exist: {target_folder}") from error

    if not _is_within(target_folder, app_folder):
        raise ValueError(f"Qt bundle folder is outside the app folder: {target_folder}")
    if target_folder.relative_to(app_folder) != EXPECTED_BUNDLE_PATH:
        raise ValueError(f"Qt bundle folder must end in _internal\\PySide6: {target_folder}")
    return target_folder


def _matching_paths(bundle_folder: Path) -> list[Path]:
    """Return approved paths without searching outside the bundle."""
    paths = [bundle_folder.joinpath(*path.split("/")) for path in LITERAL_PATHS]
    metatypes = bundle_folder / "metatypes"
    if _path_exists(metatypes):
        resolved_metatypes = metatypes.resolve(strict=True)
        if not _is_within(resolved_metatypes, bundle_folder):
            raise OSError(f"Refusing to inspect outside the Qt bundle: {metatypes}")
        paths.extend(
            item for pattern in METATYPE_PATTERNS for item in bundle_folder.glob(pattern)
        )
    return paths


def _remove_item(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _display_path(path: Path, bundle_folder: Path) -> str:
    return path.relative_to(bundle_folder).as_posix()


def trim_bundle(app_folder: Path, target_folder: Path | None = None) -> int:
    """Remove approved paths and return zero only when none remain."""
    try:
        bundle_folder = _resolve_bundle_folder(app_folder, target_folder)
        candidates = _matching_paths(bundle_folder)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1

    removed = 0
    errors = False
    for path in candidates:
        if not _path_exists(path):
            continue
        resolved_path = path.resolve(strict=False)
        if not _is_within(resolved_path, bundle_folder):
            print(f"ERROR: Refusing to remove outside the Qt bundle: {path}")
            errors = True
            continue
        try:
            _remove_item(path)
        except OSError as error:
            print(f"ERROR: Could not remove {_display_path(path, bundle_folder)}: {error}")
            errors = True
        else:
            print(f"Removed: {_display_path(path, bundle_folder)}")
            removed += 1

    try:
        remaining = [path for path in _matching_paths(bundle_folder) if _path_exists(path)]
    except OSError as error:
        print(f"ERROR: {error}")
        return 1
    for path in remaining:
        print(f"ERROR: Listed item still present: {_display_path(path, bundle_folder)}")
    print(f"Removed {removed} approved item(s).")
    return 1 if errors or remaining else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "app_folder",
        nargs="?",
        default=r"dist\Simple Account Balancer",
        help="PyInstaller app folder to trim",
    )
    args = parser.parse_args(argv)
    raise SystemExit(trim_bundle(Path(args.app_folder)))


if __name__ == "__main__":
    main()
