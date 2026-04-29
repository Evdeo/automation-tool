import ctypes
import subprocess
import time

import psutil
import uiautomation as auto

import config


_user32 = ctypes.windll.user32


def open_app(path_or_name):
    return subprocess.Popen(path_or_name, shell=False)


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
