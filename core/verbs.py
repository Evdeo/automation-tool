"""Top-level verbs — the entire user-facing surface.

Every action is a function taking the window as its first arg:
`click(window, id)`, `fill(window, id, text)`, `match(name, launch=...)`.

Synchronous popup dismiss runs before every action verb. The
"expected" set seeds from runner-start snapshot and grows with every
`match()` return — see `_dismiss_unexpected_popups`.
"""
import csv as _csv
import ctypes as _ctypes
import functools as _functools
import io as _io
import json as _json
import threading as _threading
import time as _time
from datetime import datetime as _datetime
from os import PathLike
from pathlib import Path
from typing import Tuple, Union

import psutil
import pyautogui
import pyperclip
import uiautomation as auto

import config
from core import actions, apps, db


Control = auto.Control
PathArg = Union[str, PathLike]


# --- Popup dismiss state ----------------------------------------------------

_user32 = _ctypes.windll.user32
_WM_CLOSE = 0x0010

# HWNDs the runner / user has declared "expected". Anything live but
# not in this set is dismissed before the next action verb runs.
_expected_hwnds = set()

# PIDs of every process whose top-level window has been registered via
# `match()`. The app's own popups (menus, dropdowns, modal dialogs) live
# in this PID — auto-dismiss skips them so user code doesn't have to
# `match()` every menu it opens.
_trusted_pids = set()

# HWNDs visible at the start of the most recent action verb call.
# Read by `core.app.match` in popup mode to find what's appeared since.
_hwnd_baseline_set = set()

# Thread-local nesting counter for `no_dismiss` and `each`'s internal
# block — when depth > 0, pre-dismiss is skipped.
_dismiss_paused = _threading.local()


def _is_dismiss_active():
    return getattr(_dismiss_paused, "depth", 0) == 0


def _hwnd_baseline_snapshot():
    """For `core.app.match` — frozenset of HWNDs at last verb start."""
    return frozenset(_hwnd_baseline_set)


def _hwnd_pid(hwnd):
    """PID owning `hwnd`, or 0 if Win32 fails."""
    try:
        from ctypes import wintypes as _wt
        pid = _wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, _ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _mark_hwnd_expected(hwnd):
    """For `core.app.match` — add a returned HWND to the expected set
    AND mark its owning PID as trusted (so the app's own menus and
    dialogs don't get auto-dismissed)."""
    if hwnd:
        _expected_hwnds.add(hwnd)
        pid = _hwnd_pid(hwnd)
        if pid:
            _trusted_pids.add(pid)


def _seed_expected_from_current():
    """Called once by runner.start to mark every currently-visible
    top-level HWND as expected (so initial state isn't dismissed)."""
    from core.app import _enumerate_top_level_hwnds
    _expected_hwnds.update(_enumerate_top_level_hwnds())


def _capture_hwnd_baseline():
    """Refresh `_hwnd_baseline_set` to the current top-level HWNDs.
    Run at the start of every action verb."""
    from core.app import _enumerate_top_level_hwnds
    _hwnd_baseline_set.clear()
    _hwnd_baseline_set.update(_enumerate_top_level_hwnds())


def _hwnd_title(hwnd):
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = _ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _send_dismiss_key(key):
    """Press the configured dismiss key. `key` is a str like "esc" or
    "alt+f4", or a callable taking no args."""
    if callable(key):
        key()
        return
    if "+" in key:
        pyautogui.hotkey(*key.split("+"))
    else:
        pyautogui.press(key)


def _dismiss_one(hwnd):
    """Try to dismiss a single HWND.

    Order: (1) configured dismiss key (default Esc) — works for most
    modal dialogs that have focus, (2) `WM_CLOSE` Win32 message —
    polite "please close" that doesn't require focus, (3) log + skip.
    Never escalates to process termination.
    """
    title = ""
    try:
        title = _hwnd_title(hwnd)
    except Exception:
        pass

    # Step 1: configured key.
    try:
        _send_dismiss_key(config.POPUP_DISMISS_KEY)
        _time.sleep(0.15)
        if not _user32.IsWindowVisible(hwnd):
            db.log("popup_dismiss", hwnd, title, "key")
            return True
    except Exception:
        pass

    # Step 2: WM_CLOSE.
    try:
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        _time.sleep(0.15)
        if not _user32.IsWindowVisible(hwnd):
            db.log("popup_dismiss", hwnd, title, "wm_close")
            return True
    except Exception:
        pass

    db.log("popup_dismiss", hwnd, title, "stuck")
    return False


def _walk_active_window_for_in_window_popups(window):
    """Deep-mode helper: walk `window`'s direct UIA children for new
    Window/Pane/ContentDialog nodes that didn't exist at the start of
    the test session. Returns a list of `Control`s."""
    if window is None:
        return []
    in_window_roles = {
        "WindowControl", "PaneControl", "ContentDialogControl",
    }
    out = []
    try:
        for child in window.GetChildren():
            if child.ControlTypeName not in in_window_roles:
                continue
            try:
                hwnd = child.NativeWindowHandle
            except Exception:
                hwnd = 0
            if hwnd in _expected_hwnds:
                continue
            out.append(child)
    except Exception:
        pass
    return out


def _dismiss_unexpected_popups(window=None):
    """Find every visible HWND not in the expected set and try to
    dismiss it. No-op when `_dismiss_paused` is set (no_dismiss
    context manager or `each`'s internal block).

    With `config.POPUP_CHECK_DEEP=True`, also walks the active
    window's UIA tree for in-window popups (slower).
    """
    if not _is_dismiss_active():
        return
    from core.app import _enumerate_top_level_hwnds
    current = _enumerate_top_level_hwnds()
    for hwnd in current:
        if hwnd in _expected_hwnds:
            continue
        # Same-process popups are the app's own menus / dropdowns /
        # modal dialogs — leave them alone. Foreign popups (UAC,
        # toasts, antivirus) come from a different PID and still get
        # dismissed.
        if _hwnd_pid(hwnd) in _trusted_pids:
            continue
        _dismiss_one(hwnd)
    if config.POPUP_CHECK_DEEP and window is not None:
        for child in _walk_active_window_for_in_window_popups(window):
            try:
                from core.actions import _cursor_click
                # In-window popups don't have HWNDs to PostMessage; press
                # the dismiss key with the popup focused (best effort).
                _send_dismiss_key(config.POPUP_DISMISS_KEY)
                _time.sleep(0.1)
            except Exception:
                pass


class no_dismiss:
    """Context manager: suppress popup dismiss inside the block. Use
    when a sequence intentionally creates / interacts with a popup
    before `match()` has had a chance to register it.

        with no_dismiss():
            hotkey(window, "ctrl", "s")
            # Save dialog appears here; without no_dismiss it'd be
            # killed before the next match() can register it.
        dlg = match("save_dialog", launch="popup")
    """

    def __enter__(self):
        _dismiss_paused.depth = getattr(_dismiss_paused, "depth", 0) + 1
        return self

    def __exit__(self, *exc):
        _dismiss_paused.depth -= 1
        return False


def _action_verb(fn):
    """Decorator for verbs that perform an OS action. Captures the
    HWND baseline (for `match("popup")`) and pre-dismisses any
    unexpected popups before delegating."""

    @_functools.wraps(fn)
    def wrapper(window, *args, **kwargs):
        _capture_hwnd_baseline()
        _dismiss_unexpected_popups(window)
        return fn(window, *args, **kwargs)

    return wrapper


def _action_verb_no_window(fn):
    """Variant for `type` — no window arg, no targeted UIA walk."""

    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _capture_hwnd_baseline()
        _dismiss_unexpected_popups(None)
        return fn(*args, **kwargs)

    return wrapper


# --- Click family -----------------------------------------------------------


@_action_verb
def click(window: Control, control_id: str) -> bool:
    """Click a single control inside `window`."""
    return actions.press(window, control_id)


@_action_verb
def double_click(window: Control, control_id: str) -> bool:
    """Double-click a control."""
    return actions.double_press(window, control_id)


@_action_verb
def right_click(window: Control, control_id: str) -> bool:
    """Right-click a control (typically opens its context menu)."""
    return actions.right_press(window, control_id)


@_action_verb
def click_when_enabled(window: Control, control_id: str, timeout: float = 30) -> bool:
    """Click as soon as the control becomes enabled."""
    return actions.press_when_active(window, control_id, timeout=timeout)


@_action_verb
def click_after(window: Control, control_id: str, delay: float) -> bool:
    """Sleep for `delay` seconds, then click `control_id`."""
    _time.sleep(delay)
    return actions.press(window, control_id)


# --- Text input -------------------------------------------------------------


@_action_verb
def fill(window: Control, control_id: str, text: str) -> bool:
    """Click a text field and paste `text` (clipboard-based)."""
    return actions.write_text(window, control_id, text)


@_action_verb_no_window
def type(text: str, interval: float = 0.02) -> None:
    """Type `text` letter-by-letter into whatever currently has focus."""
    pyautogui.write(text, interval=interval)


@_action_verb_no_window
def key(*combo: str) -> None:
    """Press a key or key combo at the current keyboard focus.

    Mirrors `type()` for non-character keys: no window argument and no
    auto-foreground, so keystrokes go to whichever window currently
    holds focus. Use this instead of `hotkey()` to confirm a popup
    (e.g. press Enter on a Save dialog) without yanking focus back to
    the parent app.

        key("enter")        # single key
        key("ctrl", "c")    # combo
    """
    if len(combo) == 1:
        pyautogui.press(combo[0])
    else:
        pyautogui.hotkey(*combo)


@_action_verb
def hotkey(window: Control, *combo: str) -> None:
    """Send a key combo (e.g. `hotkey(notepad, "ctrl", "s")`).

    Foregrounds `window` first — if you need keys to land on a popup
    that shouldn't lose focus (e.g. a Save dialog), use `key()` instead.
    """
    apps.bring_to_foreground(window)
    pyautogui.hotkey(*combo)


# --- Checks / waits (no pre-dismiss — these are observers) -----------------


def is_visible(window: Control, control_id: str, timeout: float = 0) -> bool:
    """True if the control is visible (snapshot question)."""
    return actions.is_present(window, control_id, timeout=timeout)


def is_enabled(window: Control, control_id: str, timeout: float = 0) -> bool:
    """True if the control is visible AND enabled (snapshot question)."""
    return actions.check_active(window, control_id, timeout=timeout)


def is_color(
    window: Control,
    control_id: str,
    rgb: Tuple[int, int, int],
    dx: int = 0,
    dy: int = 0,
    tolerance: int = 0,
) -> bool:
    """True if the control's center pixel matches `rgb`."""
    actual = actions.get_color(window, control_id, x_offset=dx, y_offset=dy)
    return all(abs(a - e) <= tolerance for a, e in zip(actual, rgb))


def wait_visible(window: Control, control_id: str, timeout: float = 10) -> bool:
    return actions.is_present(window, control_id, timeout=timeout)


def wait_enabled(window: Control, control_id: str, timeout: float = 10) -> bool:
    return actions.check_active(window, control_id, timeout=timeout)


def wait_gone(window: Control, control_id: str, timeout: float = 10) -> bool:
    return actions.wait_until_absent(window, control_id, timeout=timeout)


def check_color(
    window: Control, control_id: str, dx: int = 0, dy: int = 0
) -> Tuple[int, int, int]:
    """Sample the pixel color at the control's center."""
    return actions.get_color(window, control_id, x_offset=dx, y_offset=dy)


def read_info(window: Control, control_id: str) -> dict:
    """Return a dict of every useful UIA property of `control_id`."""
    element, (cx, cy) = actions._resolve(window, control_id)
    r = element.BoundingRectangle
    visible = (r.right - r.left) > 0 and (r.bottom - r.top) > 0
    try:
        value = element.GetValuePattern().Value
    except Exception:
        value = ""
    return {
        "name": element.Name or "",
        "value": value or "",
        "role": element.ControlTypeName or "",
        "enabled": bool(element.IsEnabled),
        "visible": visible,
        "bbox": (r.left, r.top, r.right, r.bottom),
        "bbox_center": (cx, cy),
        "class_name": element.ClassName or "",
        "automation_id": element.AutomationId or "",
        "struct_id": control_id,
    }


# --- each (atomic retry boundary) ------------------------------------------


def each(verb, window: Control, ids, **kwargs) -> list:
    """Apply `verb(window, id, **kwargs)` to each id in `ids`.

    Treated as one popup-retry boundary: if an unexpected popup
    appears between any two elements (not after the last), the popup
    is dismissed and the whole sequence restarts from element 0.
    Max 3 attempts. Returns the final results list either way.

    Internal verb calls bypass their own pre-dismiss (a thread-local
    flag suppresses it) so the each retains the boundary.

    `ids` must be idempotent for the typical re-click case.
    """
    _capture_hwnd_baseline()
    _dismiss_unexpected_popups(window)

    last_idx = len(ids) - 1
    results = []
    for attempt in range(3):
        # Start the each block: suppress per-call dismiss inside.
        _dismiss_paused.depth = getattr(_dismiss_paused, "depth", 0) + 1
        try:
            results = []
            interrupted = False
            for i, ctrl_id in enumerate(ids):
                results.append(verb(window, ctrl_id, **kwargs))
                if i < last_idx:
                    from core.app import _enumerate_top_level_hwnds
                    current = set(_enumerate_top_level_hwnds())
                    new_unexpected = {
                        h for h in current - _hwnd_baseline_set - _expected_hwnds
                        if _hwnd_pid(h) not in _trusted_pids
                    }
                    if new_unexpected:
                        for hwnd in new_unexpected:
                            _dismiss_one(hwnd)
                        # Refresh baseline for the retry.
                        _capture_hwnd_baseline()
                        interrupted = True
                        break
            if not interrupted:
                return results
        finally:
            _dismiss_paused.depth -= 1
    return results


# --- Popups / window matching -----------------------------------------------


def match(name: str, launch: str, timeout: float = 15.0,
          restrict_pid=None, parent=None):
    """Locate a window by saved fingerprint. `launch` required.

    `launch="<exe>"`: find an open window matching the saved
    fingerprint; if none, run the exe and wait for one to appear.
    `launch="popup"`: return a top-level HWND that appeared since
    the last verb call and matches the fingerprint.

    Returns `Control | None` — never raises.
    """
    from core import app as app_mod
    return app_mod.match(name, launch=launch, timeout=timeout,
                         restrict_pid=restrict_pid, parent=parent)


# --- Orchestrations ---------------------------------------------------------


@_action_verb
def screenshot(window: Control, path: PathArg) -> None:
    """Save a PNG of `window`'s bounding rectangle to `path`."""
    apps.bring_to_foreground(window)
    r = window.BoundingRectangle
    region = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = pyautogui.screenshot(region=region)
    img.save(path)


def close(window: Control) -> None:
    """Terminate the process owning `window`. Use for end-of-test
    cleanup. NOT used for popup dismissal — that's `_dismiss_one`,
    which sends Esc/WM_CLOSE without killing the host process."""
    try:
        psutil.Process(window.ProcessId).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


# --- Misc -------------------------------------------------------------------


def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Current datetime formatted as a string."""
    return _datetime.now().strftime(fmt)


def wait(seconds: float) -> None:
    """Pause execution for `seconds`. Alias for `time.sleep`."""
    _time.sleep(seconds)


def log(table: str, *values) -> None:
    """Append a row to a SQLite table."""
    db.log(table, *values)


def read_clipboard() -> str:
    """Return the current clipboard contents."""
    return pyperclip.paste()


def log_csv(path: PathArg, *rows, header=None, delimiter: str = ",") -> None:
    """Append `rows` to a CSV file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()

    if len(rows) == 1 and isinstance(rows[0], str):
        text = rows[0]
        first = text.splitlines()[0] if text else ""
        if "\t" in first:
            src_delim = "\t"
        elif ";" in first:
            src_delim = ";"
        else:
            src_delim = ","
        rows = list(_csv.reader(_io.StringIO(text), delimiter=src_delim))

    with open(p, "a", newline="", encoding="utf-8") as f:
        w = _csv.writer(f, delimiter=delimiter)
        if not existed and header is not None:
            w.writerow(header)
        for row in rows:
            cells = []
            for cell in row:
                if isinstance(cell, set):
                    cells.append(_json.dumps(sorted(cell, key=str)))
                elif isinstance(cell, (list, tuple, dict)):
                    cells.append(_json.dumps(list(cell) if isinstance(cell, tuple) else cell))
                else:
                    cells.append(cell)
            w.writerow(cells)
