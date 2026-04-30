"""User-facing app handle: one call to launch + locate the window.

Replaces the manual is_running / open_app / get_window dance in user
scripts. `app.launch("notepad.exe")` is enough; `app.spec(path, title=)`
is the escape hatch for apps where the title can't be auto-derived.
`app.popup(parent, title)` finds child dialogs / sub-windows.
"""
import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional, Union

import psutil
import uiautomation as auto

import config
from core import apps


_user32 = ctypes.windll.user32


@dataclass(frozen=True)
class Spec:
    path: str
    title: Optional[str] = None


def spec(path: str, title: Optional[str] = None) -> Spec:
    """Wrap a launch path with optional title override.

    Bare paths (`"notepad.exe"`) and full paths
    (`r"C:\\Program Files\\App\\app.exe"`) both work; verify_installed
    routes by syntax. `title` is only needed when title auto-detection
    can't pin down the right window (multi-window apps, slow boot).
    """
    return Spec(path=path, title=title)


def _coerce(item: Union[str, Spec]) -> Spec:
    return item if isinstance(item, Spec) else Spec(path=item)


def normalize(items):
    """Return [Spec, ...] from a mixed list of strings and Specs."""
    return [_coerce(i) for i in items]


def paths(items):
    """Return [path, ...] from a mixed list — for verify_installed."""
    return [_coerce(i).path for i in items]


def _exe_stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _find_window_by_pid(pid: int):
    """Return the first top-level WindowControl whose owning process is
    `pid` and which has a non-empty title. Returns None if none found."""
    for w in auto.GetRootControl().GetChildren():
        if not isinstance(w, auto.WindowControl):
            continue
        try:
            if w.ProcessId != pid:
                continue
        except Exception:
            continue
        if w.Name:
            return w
    return None


def _find_window_by_exe(stem: str):
    """Return the first top-level WindowControl whose owning process's
    executable stem matches `stem` (case-insensitive). Used as a
    fallback when the launched PID is a stub that exits immediately
    (e.g., notepad.exe on Win11 forwards to the packaged app)."""
    target = stem.lower()
    for w in auto.GetRootControl().GetChildren():
        if not isinstance(w, auto.WindowControl) or not w.Name:
            continue
        try:
            p = psutil.Process(w.ProcessId)
        except Exception:
            continue
        try:
            name = (p.name() or "").lower()
        except Exception:
            continue
        if name.rsplit(".", 1)[0] == target:
            return w
    return None


def launch(item: Union[str, Spec], wait: float = 15.0):
    """Ensure the app is running and return its window.

    - If the process is already running, just locates the window.
    - Otherwise launches it and polls for the window to appear.
    - If `spec.title` is set, uses it as a substring match against
      window titles (same rule as `apps.get_window`). Otherwise the
      window is auto-derived from the owning process's executable.
    """
    s = _coerce(item)
    stem = _exe_stem(s.path)

    if s.title:
        if not apps.is_running(stem):
            apps.open_app(s.path)
        return apps.get_window(s.title)

    if not apps.is_running(stem):
        proc = apps.open_app(s.path)
        pid = proc.pid
    else:
        pid = None

    deadline = time.time() + wait
    while True:
        if pid is not None:
            win = _find_window_by_pid(pid)
            if win is not None:
                return win
        win = _find_window_by_exe(stem)
        if win is not None:
            return win
        if time.time() > deadline:
            raise TimeoutError(
                f"could not locate a window for {s.path!r} within {wait}s "
                f"(stem={stem!r}). Pass app.spec(path, title=...) to override."
            )
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)


_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _hwnd_pid(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _hwnd_title(hwnd):
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def popup(parent, title, timeout=8.0):
    """Find a popup / dialog / sub-window with title containing `title`.

    Two-pass search, run on every retry until `timeout`:

    1. **Top-level HWND scan** — finds separate-window popups (modal
       dialogs, WinUI ContentDialogs hosted in a popup window, native
       Win32 ``#32770`` file dialogs, sub-app windows). Prefers ones
       owned by `parent`'s process; falls back to any process.
    2. **`parent`'s UIA tree walk** — finds in-window popups that have
       no separate HWND (WPF modal adorners, XAML overlays, custom
       in-content dialogs). Returns the first descendant whose Name
       contains `title`.

    Returns the `Control`. Raises `TimeoutError` on miss.
    """
    target = title.lower()
    try:
        parent_pid = parent.ProcessId
    except Exception:
        parent_pid = None
    try:
        parent_handle = parent.NativeWindowHandle
    except Exception:
        parent_handle = None

    deadline = time.time() + timeout
    while True:
        same_proc = []
        other = []

        def cb(hwnd, _lp):
            if not _user32.IsWindowVisible(hwnd) or hwnd == parent_handle:
                return True
            if target not in _hwnd_title(hwnd).lower():
                return True
            if parent_pid is not None and _hwnd_pid(hwnd) == parent_pid:
                same_proc.append(hwnd)
            else:
                other.append(hwnd)
            return True

        _user32.EnumWindows(_EnumWindowsProc(cb), 0)
        hit = (same_proc or other or [None])[0]
        if hit:
            return auto.ControlFromHandle(hit)

        # Pass 2: walk parent's UIA tree for in-window popups with a
        # matching Name. Only reached when no top-level HWND matched.
        from core import tree as tree_mod
        try:
            for n in tree_mod.walk_live(parent):
                if n["ctrl"] is parent:
                    continue
                if n["name"] and target in n["name"].lower():
                    return n["ctrl"]
        except Exception:
            pass

        if time.time() > deadline:
            raise TimeoutError(
                f"popup with title containing {title!r} not found within {timeout}s "
                f"(searched top-level windows and {parent.Name!r}'s UIA tree)"
            )
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)
