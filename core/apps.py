import subprocess
import time

import psutil
import uiautomation as auto

import config


def open_app(path_or_name):
    return subprocess.Popen(path_or_name, shell=False)


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
