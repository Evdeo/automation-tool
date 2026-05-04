"""Top-level verbs — the entire user-facing surface.

Every action is a function taking the window as its first arg:
`click(window, id)`, `fill(window, id, text)`. App lifecycle lives on
`core.window` — `window.open(name)`, `window.close(name)`,
`window.get(name)`, `window.popup(name)`.

Synchronous popup dismiss runs before every action verb. The
"expected" set seeds from runner-start snapshot and grows with every
`window.open` / `window.popup` return — see `_dismiss_unexpected_popups`.
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


def _hwnd_class(hwnd):
    """Win32 class name of `hwnd`, or "" if it can't be read."""
    buf = _ctypes.create_unicode_buffer(256)
    try:
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


# Hard skip list — windows we will NEVER auto-dismiss, regardless of
# whether they're in `_expected_hwnds` or owned by a trusted PID.
# Defense in depth: a bug in test setup (or anywhere else) can't
# cascade into killing the developer's terminal / shell / IDE.
# Browser / Electron window classes — used in two places: protected by
# the auto-dismiss skip list (so a coord-based click can't accidentally
# close the host browser), and consulted by the inspector to detect
# "this capture is a web element" so it can extract a CSS selector
# instead of a positional struct_id.
_BROWSER_WINDOW_CLASSES = frozenset({
    "Chrome_WidgetWin_1",                                # Chromium + Electron
    "Chrome_WidgetWin_0",                                # variant
    "MozillaWindowClass",                                # Firefox
})

_SYSTEM_WINDOW_CLASSES = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd",          # taskbar
    "Progman", "WorkerW",                                # desktop shell
    "ConsoleWindowClass",                                # cmd.exe
    "WindowsTerminal",                                   # Win Terminal
    "CASCADIA_HOSTING_WINDOW_CLASS",                     # Win Terminal host
    "mintty",                                            # Git Bash
}) | _BROWSER_WINDOW_CLASSES


_SYSTEM_PROCESS_NAMES = frozenset({
    "explorer.exe", "dwm.exe", "winlogon.exe", "csrss.exe",
    "windowsterminal.exe", "openconsole.exe", "conhost.exe",
    "code.exe", "code - insiders.exe",
    "pycharm64.exe", "idea64.exe", "rider64.exe",
    "devenv.exe",                                        # Visual Studio
})


def _is_system_window(hwnd):
    """True if `hwnd` is part of the OS shell, a terminal, or an IDE
    we should never close. Checked by class name first (cheap) and
    falling through to the owning process name."""
    if _hwnd_class(hwnd) in _SYSTEM_WINDOW_CLASSES:
        return True
    pid = _hwnd_pid(hwnd)
    if not pid:
        return False
    try:
        name = (psutil.Process(pid).name() or "").lower()
    except Exception:
        return False
    return name in _SYSTEM_PROCESS_NAMES


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
    Never escalates to process termination. System windows (shell,
    terminals, IDEs) are protected by `_is_system_window` even if a
    caller bypasses the expected/trusted sets.
    """
    if _is_system_window(hwnd):
        db.log("popup_dismiss", hwnd, "", "skipped_system")
        return False

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
        # `_dismiss_one` itself enforces the system-window skip list,
        # but checking here too avoids the unnecessary log row.
        if _is_system_window(hwnd):
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
    before `window.popup()` has had a chance to register it.

        with no_dismiss():
            hotkey(window.notepad, "ctrl", "s")
            # Save dialog appears here; without no_dismiss it'd be
            # killed before window.popup() can register it.
            dlg = window.popup("save_dialog")
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


@_action_verb
def move(window: Control, control_id: str) -> bool:
    """Move the cursor to `control_id`'s center without clicking.

    Use when you want to position over a control to read state,
    surface a tooltip, capture pixel data, or set up a screenshot —
    anywhere a click would risk activating something you don't want
    to actuate (toggles, submenu triggers, links).
    """
    return actions.move(window, control_id)


@_action_verb
def hold_and_drag(window: Control, src_id: str, dst_id: str) -> bool:
    """Press at `src_id`'s center, drag to `dst_id`'s center, release.

    Use for sliders (drag the thumb to a track position), drag-and-drop
    targets, resize handles, paint strokes — anywhere a single click
    isn't enough and the gesture is "press here, drag there." Both
    controls must be resolvable in `window`'s tree at call time.
    """
    return actions.drag(window, src_id, dst_id)


# --- Coordinate-based variants (no UIA resolution) ------------------------
#
# Bypass `_resolve` and act on raw OS screen (x, y). Use when the target
# isn't in the UIA tree — most commonly a DOM element on a Playwright
# page (combine with `web_coords` below), but also image-search hits or
# hard-coded positions. Caller is responsible for putting the target
# window in front first; for Playwright that's `page.bring_to_front()`.


@_action_verb_no_window
def click_at(x: int, y: int) -> None:
    """Click at OS screen coordinates `(x, y)`."""
    actions._cursor_click(x, y)


@_action_verb_no_window
def move_at(x: int, y: int) -> None:
    """Move the cursor to OS screen coordinates `(x, y)` without clicking."""
    actions._cursor_move(x, y)


@_action_verb_no_window
def hold_and_drag_at(x1: int, y1: int, x2: int, y2: int) -> None:
    """Press at `(x1, y1)`, drag to `(x2, y2)`, release."""
    actions._cursor_drag(x1, y1, x2, y2)


def web_coords(page, selector):
    """Screen `(x, y)` center of the DOM element matching `selector` on
    a Playwright `page`, or `None` if no such element.

    Translates the element's viewport-relative
    `getBoundingClientRect()` into absolute OS coordinates by adding
    the browser window's `screenX/Y` plus the chrome (toolbar/tabs)
    offset. The returned tuple feeds straight into `click_at`,
    `move_at`, or `hold_and_drag_at` — Playwright handles the part
    it's actually good at (DOM-aware element finding on dynamic
    pages) while OS-level click delivery stays in the framework.

    Caveats:
      - Browser must be visible. Call `page.bring_to_front()` first
        (Playwright headless mode won't work — no on-screen window).
      - Coords go stale if the page scrolls between the call and the
        click. Re-query immediately before clicking.
      - DPI scaling: if Windows is at 125%/150% display scale and
        Playwright reports CSS pixels, the result drifts. Apply a
        `devicePixelRatio` multiply if that becomes an issue.
    """
    return page.evaluate("""(sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const dx = window.screenX + (window.outerWidth - window.innerWidth) / 2;
        const dy = window.screenY + (window.outerHeight - window.innerHeight);
        return [Math.round(r.x + r.width / 2 + dx),
                Math.round(r.y + r.height / 2 + dy)];
    }""", selector)


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
                        and not _is_system_window(h)
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
