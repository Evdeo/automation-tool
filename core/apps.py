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


def verify_installed(required):
    """Pre-flight: confirm every (path, title) entry's launch path is
    reachable. Resolved via shutil.which for bare names (PATH lookup
    handles things like "notepad.exe" -> System32) and Path.exists for
    anything that looks like a directory path. Collects ALL misses
    into one error so the user fixes them in a single edit instead of
    one-at-a-time. Title is unused here -- it can only be verified
    when the app is actually running, which get_window() handles."""
    missing = []
    for path, _title in required:
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
