"""Windows window and message-box helpers."""

import ctypes
import ctypes.wintypes as wintypes
import os
import time

from app import config, prefs, utils


# Save and restore the ABSOLUTE window frame rectangle via Win32, found by the
# window title but filtered to a window owned by this process (see
# `_own_window_handle` below). GetWindowRect (save) and SetWindowPos (restore)
# share one frame-based, physical-pixel coordinate space. A saved rect that
# fits its restored monitor's work area round-trips exactly. Do NOT pass x/y into
# create_window and do NOT use window.move: pywebview's Qt backend applies
# those pre-show and relative to the primary screen, so the window lands on
# the wrong monitor, drifts down by the title-bar height each launch, and
# slides sideways at non-100% scaling.
def _win32():
    u = ctypes.windll.user32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return u


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _monitor_work_area(hmonitor):
    """Return hmonitor's work area as (left, top, right, bottom), or None."""
    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return None
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom


def _frame_insets(hwnd):
    """Return DWM frame insets for hwnd, or zero insets when unavailable."""
    try:
        frame = wintypes.RECT()
        if not _win32().GetWindowRect(hwnd, ctypes.byref(frame)):
            return 0, 0, 0, 0
        extended = wintypes.RECT()
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        if dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(extended), ctypes.sizeof(extended)
        ) != 0:
            return 0, 0, 0, 0
        return (
            max(0, extended.left - frame.left),
            max(0, extended.top - frame.top),
            max(0, frame.right - extended.right),
            max(0, frame.bottom - extended.bottom),
        )
    except Exception:
        return 0, 0, 0, 0


def fit_rect_to_work_area(x, y, w, h, work, insets):
    """Fit a frame rect so its DWM-visible rect remains in a monitor work area."""
    left, top, right, bottom = work
    inset_left, inset_top, inset_right, inset_bottom = insets
    visible_x = x + inset_left
    visible_y = y + inset_top
    visible_w = min(max(0, w - inset_left - inset_right), right - left)
    visible_h = min(max(0, h - inset_top - inset_bottom), bottom - top)

    if visible_x + visible_w > right:
        visible_x = right - visible_w
    if visible_y + visible_h > bottom:
        visible_y = bottom - visible_h
    if visible_x < left:
        visible_x = left
    if visible_y < top:
        visible_y = top
    return (
        visible_x - inset_left,
        visible_y - inset_top,
        visible_w + inset_left + inset_right,
        visible_h + inset_top + inset_bottom,
    )


def _own_window_handle(title):
    """HWND of our own top-level window with this title.

    FindWindowW matches by title across the whole desktop, so with a second
    instance open it can return the other copy's window. Enumerate instead and
    keep only a window owned by this process.
    """
    try:
        u = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL

        own_pid = os.getpid()
        found = {"hwnd": None}

        def _callback(hwnd, lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own_pid:
                return True
            length = u.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value != title:
                return True
            found["hwnd"] = hwnd
            return False   # stop enumerating, we found it

        proc = WNDENUMPROC(_callback)   # kept alive for the duration of the call below
        u.EnumWindows(proc, 0)
        return found["hwnd"]
    except Exception:
        return None


def _save_geometry(win) -> None:
    """Save the absolute frame rect (physical px) via Win32. Wired to
    `closing`. Guarded end-to-end so a failure here can never interfere with
    closing the app."""
    try:
        u = _win32()
        hwnd = _own_window_handle(win.title)
        if not hwnd:
            return
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return
        x, y, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        # A minimized window reports a position around -32000; don't save
        # that as if it were the user's chosen spot.
        if x <= -30000 or y <= -30000:
            return
        if w <= 0 or h <= 0:
            return
        prefs_data = prefs.load_prefs()
        prefs_data["window"] = {"x": x, "y": y, "width": w, "height": h}
        prefs.save_prefs(prefs_data)
    except Exception:
        pass


def _restore_geometry(win) -> None:
    """Restore the saved frame rect via Win32, fitted to its monitor's work
    area. Wired to `shown` after the OS window exists; never raises."""
    try:
        geo = prefs.load_prefs().get("window")
        if not isinstance(geo, dict):
            return
        x, y, w, h = geo.get("x"), geo.get("y"), geo.get("width"), geo.get("height")
        for v in (x, y, w, h):
            if not isinstance(v, int) or isinstance(v, bool):
                return
        if w <= 0 or h <= 0:
            return
        # Confirm a point inside the title bar area is still on a connected
        # monitor; MonitorFromPoint returns NULL if it isn't (for example the
        # saved monitor has been unplugged since the last launch).
        point = wintypes.POINT(x + 100, y + 30)
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        MONITOR_DEFAULTTONULL = 0
        hmonitor = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONULL)
        if not hmonitor:
            return
        work = _monitor_work_area(hmonitor)
        if work is None:
            return
        u = _win32()
        hwnd = _own_window_handle(win.title)
        if not hwnd:
            return
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        rect = fit_rect_to_work_area(x, y, w, h, work, _frame_insets(hwnd))
        u.SetWindowPos(hwnd, None, *rect, SWP_NOZORDER | SWP_NOACTIVATE)

        # pywebview runs `shown` callbacks on a worker thread, so this delay
        # does not block Qt while it finishes any DPI-driven resize.
        time.sleep(0.3)
        current = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(current)):
            return
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        MONITOR_DEFAULTTONEAREST = 2
        hmonitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmonitor:
            return
        work = _monitor_work_area(hmonitor)
        if work is None:
            return
        current_rect = (
            current.left, current.top, current.right - current.left, current.bottom - current.top,
        )
        fitted_rect = fit_rect_to_work_area(*current_rect, work, _frame_insets(hwnd))
        if fitted_rect != current_rect:
            u.SetWindowPos(hwnd, None, *fitted_rect, SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def _show_write_error(folder: str):
    msg = (
        "Simple Account Balancer keeps its data in a file next to the app, "
        f"but this folder isn't writable:\n\n{folder}\n\n"
        "This often happens when the app is placed in Program Files. Move it "
        "to a writable folder (like your Desktop or Documents) and try again."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _show_backup_fallback_notice(actual_dir: str):
    msg = (
        "The backup folder wasn't reachable, so the closing backup was saved "
        f"to {actual_dir} instead."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


def _show_prune_failed_notice(count: int, folder: str):
    msg = f"The closing backup was saved. {utils._prune_failed_message(count)}\n\nFolder: {folder}"
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x30)  # MB_ICONWARNING
    except Exception:
        pass


def _show_backup_failed_notice(folder: str):
    msg = (
        "The closing backup couldn't be saved. Tried to write it to "
        f"{folder}."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _show_newer_schema_error():
    msg = (
        "This data file was created by a newer version of Simple Account "
        "Balancer than this one.\n\n"
        "Update to the latest version of the app to open it."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _show_damaged_db_error(folders: list, leading_text: str | None = None):
    msg = (
        (f"{leading_text}\n\n" if leading_text else "")
        + "Simple Account Balancer can't open your data file because it is damaged.\n\n"
        "Nothing was changed and no backups were removed. Your backups are in:\n"
        + "\n".join(folders)
        + f"\n\nTo recover, keep a copy of the damaged file, then copy the newest backup "
        f"into the app folder and rename it {config.DB_FILENAME}, replacing the damaged file."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _show_db_unavailable_error():
    msg = (
        "Simple Account Balancer couldn't open your data file. Another program, "
        "such as a backup or sync tool, may be using it, or the drive may be unavailable.\n\n"
        "Nothing was changed. Close other programs that might be using it, then try again."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _show_upgrade_blocked_error(folder: str):
    msg = (
        "This version of Simple Account Balancer needs to update your data file, "
        "but it couldn't save a backup first, so it stopped without changing anything.\n\n"
        f"It tried to save the backup to:\n{folder}\n\n"
        "Make sure that folder is reachable and has free space, then try again."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Simple Account Balancer", 0x10)  # MB_ICONERROR
    except Exception:
        pass
