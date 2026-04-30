import ctypes
import shutil
import subprocess
import time
from pathlib import Path

import psutil
import uiautomation as auto

import config


_user32 = ctypes.windll.user32


def open_app(path_or_name):
    return subprocess.Popen(path_or_name, shell=False)


def verify_installed(paths):
    """Pre-flight: confirm every launch path is reachable. Resolved
    via shutil.which for bare names (PATH lookup handles things like
    "notepad.exe" -> System32) and Path.exists for anything that
    looks like a directory path. Collects ALL misses into one error
    so the user fixes them in a single edit instead of one-at-a-time."""
    missing = []
    for path in paths:
        p = Path(path)
        # Treat anything with a separator OR drive letter as a literal
        # filesystem path; bare names (e.g. "notepad.exe") fall through
        # to PATH resolution.
        if p.is_absolute() or "/" in path or "\\" in path:
            if not p.exists():
                missing.append(path)
        elif shutil.which(path) is None:
            missing.append(path)
    if missing:
        bullets = "\n  - ".join(missing)
        raise FileNotFoundError(
            "Required apps not found:\n  - " + bullets +
            "\nFix the paths in REQUIRED_APPS."
        )


def is_running(name):
    """Return True if any process whose executable name contains `name`
    (case-insensitive) is currently running."""
    target = name.lower()
    for p in psutil.process_iter(["name"]):
        try:
            if target in (p.info.get("name") or "").lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def bring_to_foreground(window):
    hwnd = window.NativeWindowHandle
    if _user32.GetForegroundWindow() == hwnd:
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE in case it's minimized
        return
    # Minimize-then-restore reliably brings a window to the foreground without
    # the SetForegroundWindow restrictions or any keyboard side-effects.
    _user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    time.sleep(0.05)
    _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _user32.SetForegroundWindow(hwnd)
    _user32.BringWindowToTop(hwnd)
    time.sleep(0.3)


def close_app(name):
    closed = 0
    target = name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and target in proc.info["name"].lower():
                proc.terminate()
                closed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(
        [p for p in psutil.process_iter() if target in (p.info.get("name") or "").lower()],
        timeout=5,
    )
    return closed


def get_window(title=None):
    title = title or config.TARGET_WINDOW_TITLE
    while True:
        win = auto.WindowControl(searchDepth=1, Name=title)
        if win.Exists(0, 0):
            return win
        for w in auto.GetRootControl().GetChildren():
            if isinstance(w, auto.WindowControl) and w.Name and title in w.Name:
                return w
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)


_window_cache = {}


def window(title_or_control):
    """Resolve a window title (string) to a live Control, caching the result.

    Idempotent — a Control passes through unchanged. The cache lets every
    `actions.*` / `dialogs.*` call site accept the same WINDOWS["main"]
    string without re-walking the desktop on each call. Stale cache
    entries (window closed) are detected via `Exists(0, 0)` and
    re-resolved transparently.
    """
    if not isinstance(title_or_control, str):
        return title_or_control
    title = title_or_control
    cached = _window_cache.get(title)
    if cached is not None:
        try:
            if cached.Exists(0, 0):
                return cached
        except Exception:
            pass
    win = get_window(title)
    _window_cache[title] = win
    return win


def _other_top_windows(window):
    """Sibling top-level windows of the same process. Used by the
    resolver's failure-path diagnostic to point the user at a window
    that DOES contain a struct_id they pasted."""
    try:
        pid = window.ProcessId
        my_handle = window.NativeWindowHandle
    except AttributeError:
        return []
    out = []
    for w in auto.GetRootControl().GetChildren():
        if not isinstance(w, auto.WindowControl):
            continue
        try:
            if w.ProcessId != pid:
                continue
            if w.NativeWindowHandle == my_handle:
                continue
        except Exception:
            continue
        out.append(w)
    return out
