#!/usr/bin/env python3
"""Regenerate screenshots/account-balancer-light-dark.png.

Simple Account Balancer is a desktop app: at screenshot time there is no web
server and no Python backend to talk to. Its UI is a self-contained HTML file
that already degrades gracefully outside pywebview (every backend call is
guarded by `if (!api()) return;`), so this tool serves the page and its
assets from a temp folder, seeds a sample account and register straight into
the state the backend would normally fill, and drives the page's own render
functions to produce the picture.

Nothing here touches the working copy. The UI file, the icon, and the fonts
folder are copied into a temp folder and served from there; the real files
are only ever read, never written.

    python tools/screenshot/make_screenshot.py

Options:
    --keep            leave the temp folder in place for inspection
    --build-tools P   path to the build-tools repo (default: sibling folder)
"""

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

OUT_IMAGE = os.path.join(REPO_ROOT, "screenshots",
                         "account-balancer-light-dark.png")

# Each theme is laid out at this size and captured at half scale, giving two
# 900x500 halves and the 1800x500 composite the README uses. LAYOUT_HEIGHT is
# a starting guess; the exact value is tuned by trial when the shot is
# actually regenerated, closing just under the last panel with no dead band.
LAYOUT_WIDTH = 1800
LAYOUT_HEIGHT = 1000
CAPTURE_SCALE = 0.5


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_app_version() -> str:
    path = os.path.join(REPO_ROOT, "simple_account_balancer.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', source)
    if not match:
        fail(f"could not find APP_VERSION in {path}")
    return match.group(1)


def stage_ui(temp_dir: str) -> None:
    """Copy just what the page needs into temp_dir."""
    shutil.copy2(os.path.join(REPO_ROOT, "simple_account_balancer-UI.html"),
                 os.path.join(temp_dir, "index.html"))
    shutil.copy2(os.path.join(REPO_ROOT, "simple_account_balancer.png"),
                 temp_dir)
    shutil.copytree(os.path.join(REPO_ROOT, "fonts"),
                     os.path.join(temp_dir, "fonts"))


def build_setup_script(version: str) -> str:
    """JavaScript that seeds STATE and TX with the sample account and drives
    the page's own render path.

    This app is backend-driven, not a single-document STATE: boot() returns
    immediately with no pywebview, and every data path (the account, the
    register rows) is normally filled by a backend call. This calls the same
    functions boot() would once the config and register have loaded:
    initRegisterUI(), showRegisterView(), renderHeader(), renderRegister(),
    renderEstimateNotice(), each guarded so the script still works if a
    function is renamed or removed.
    """
    account = dict(scene.ACCOUNT)
    account["current_balance_cents"] = scene.CURRENT_BALANCE_CENTS
    state = {
        "version": version,
        "theme": "dark",
        "hasAccount": True,
        "account": account,
        "accounts": [account],
        "estimatedDueCount": scene.ESTIMATED_DUE_COUNT,
    }
    return (
        f"STATE = {json.dumps(state)};"
        f"TX = {json.dumps(scene.TX)};"
        f"document.getElementById('verLabel').textContent = 'v' + {json.dumps(version)};"
        "if (typeof initRegisterUI === 'function') initRegisterUI();"
        "if (typeof showRegisterView === 'function') showRegisterView();"
        "if (typeof renderHeader === 'function') renderHeader();"
        "if (typeof renderRegister === 'function') renderRegister();"
        "if (typeof renderEstimateNotice === 'function') renderEstimateNotice();"
    )


def write_capture_config(temp_dir: str, port: int, version: str) -> str:
    config = {
        "url": f"http://127.0.0.1:{port}/index.html",
        "width": LAYOUT_WIDTH,
        "height": LAYOUT_HEIGHT,
        "scale": CAPTURE_SCALE,
        "outDir": "shots",
        "waitFor": "typeof renderRegister === 'function'",
        "setup": build_setup_script(version),
        "settleMs": 500,
        "shots": [
            {"name": "light", "script": "applyTheme('light')"},
            {"name": "dark", "script": "applyTheme('dark')"},
        ],
    }
    path = os.path.join(temp_dir, "shots.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return path


def run(cmd: list, label: str) -> None:
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        fail(f"{label} failed with exit code {result.returncode}")


def main(argv: list) -> None:
    keep = "--keep" in argv
    build_tools = os.path.join(os.path.dirname(REPO_ROOT), "build-tools")
    if "--build-tools" in argv:
        index = argv.index("--build-tools") + 1
        if index >= len(argv):
            fail("--build-tools needs a path after it")
        build_tools = argv[index]

    capture_script = os.path.join(build_tools, "screenshot", "capture.mjs")
    compose_script = os.path.join(build_tools, "screenshot", "compose.py")
    for path in (capture_script, compose_script):
        if not os.path.exists(path):
            fail(f"missing {path}. Pass --build-tools with the repo path.")

    version = read_app_version()
    temp_dir = tempfile.mkdtemp(prefix="sab-screenshot-")
    httpd = None

    try:
        stage_ui(temp_dir)

        port = free_port()

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def __init__(self, *a, **kw):
                super().__init__(*a, directory=temp_dir, **kw)

        httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        config_path = write_capture_config(temp_dir, port, version)
        run(["node", capture_script, config_path], "capture")

        shots_dir = os.path.join(temp_dir, "shots")
        run([sys.executable, compose_script, OUT_IMAGE,
             os.path.join(shots_dir, "light.png"),
             os.path.join(shots_dir, "dark.png")], "compose")
    finally:
        if httpd is not None:
            httpd.shutdown()
        if keep:
            print(f"temp folder kept at {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(temp_dir):
                print(f"WARNING: could not remove {temp_dir}", file=sys.stderr)

    print(f"seeded version: v{version}")
    print(f"updated {OUT_IMAGE}")


if __name__ == "__main__":
    main(sys.argv[1:])
