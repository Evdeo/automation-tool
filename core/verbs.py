"""Top-level verbs — the entire user-facing surface.

Every action is a function taking the window as its first arg:
`click(window, id)`, `fill(window, id, text)`, `save_as(window, path)`.
Auto-complete on `from core import ...` lists everything available.
"""
import csv as _csv
import io as _io
import json as _json
import time as _time
from datetime import datetime as _datetime
from os import PathLike
from pathlib import Path
from typing import Tuple, Union

import psutil
import pyautogui
import pyperclip
import uiautomation as auto

from core import actions, app, apps, db, dialogs


Control = auto.Control
PathArg = Union[str, PathLike]


# --- Click family -----------------------------------------------------------


def click(window: Control, control_id: str) -> bool:
    """Click a single control inside `window`."""
    return actions.press(window, control_id)


def double_click(window: Control, control_id: str) -> bool:
    """Double-click a control."""
    return actions.double_press(window, control_id)


def click_when_enabled(window: Control, control_id: str, timeout: float = 30) -> bool:
    """Click as soon as the control becomes enabled (e.g. a button that's
    disabled while a background task runs)."""
    return actions.press_when_active(window, control_id, timeout=timeout)


def click_after(window: Control, control_id: str, delay: float) -> bool:
    """Sleep for `delay` seconds, then click `control_id`. Useful when
    you need a fixed pause before a click — collapses
    `wait(delay); click(window, id)` into one line."""
    _time.sleep(delay)
    return actions.press(window, control_id)


# --- Text input -------------------------------------------------------------


def fill(window: Control, control_id: str, text: str) -> bool:
    """Click a text field and paste `text` (clipboard-based; works
    regardless of keyboard layout)."""
    return actions.write_text(window, control_id, text)


def type(text: str, interval: float = 0.02) -> None:
    """Type `text` letter-by-letter into whatever currently has keyboard
    focus. No window argument — does NOT click or change focus. Use
    when a field is already focused (e.g. when a dialog opens with its
    primary input pre-selected). For targeting a specific control, use
    `fill(window, id, text)` instead."""
    pyautogui.write(text, interval=interval)


def hotkey(window: Control, *combo: str) -> None:
    """Send a key combo (e.g. `hotkey(notepad, "ctrl", "s")`). Auto-
    foregrounds `window` first."""
    apps.bring_to_foreground(window)
    pyautogui.hotkey(*combo)


# --- Checks / waits ---------------------------------------------------------


def check_visible(window: Control, control_id: str, timeout: float = 0) -> bool:
    """True if the control is visible within `timeout` seconds."""
    return actions.is_present(window, control_id, timeout=timeout)


def check_enabled(window: Control, control_id: str, timeout: float = 0) -> bool:
    """True if the control is visible AND enabled within `timeout` seconds."""
    return actions.check_active(window, control_id, timeout=timeout)


def wait_gone(window: Control, control_id: str, timeout: float = 10) -> bool:
    """Wait until the control disappears. Returns True once gone, False on
    timeout."""
    return actions.wait_until_absent(window, control_id, timeout=timeout)


def check_color(
    window: Control, control_id: str, dx: int = 0, dy: int = 0
) -> Tuple[int, int, int]:
    """Sample the pixel color at the control's center, optionally offset
    by `(dx, dy)`. Returns `(r, g, b)`."""
    return actions.get_color(window, control_id, x_offset=dx, y_offset=dy)


# --- Popups / dialogs -------------------------------------------------------


def popup(parent: Control, title: str, timeout: float = 8.0) -> Control:
    """Find a sub-window or in-window dialog by title substring. Two
    passes: top-level HWNDs first (Save As, modal dialogs), then
    `parent`'s UIA tree (WPF/XAML in-window popups). Returns the control.
    Does NOT close it."""
    return app.popup(parent, title, timeout=timeout)


def dismiss_popups(window: Control, max_passes: int = 5) -> int:
    """Click any button named exactly `"OK"` inside `window`, up to
    `max_passes` times. Closes ONLY OK-button popups. Cancel-only or
    other-button popups are left alone."""
    return dialogs.dismiss_ok_popups(window, max_passes=max_passes)


# --- Orchestrations ---------------------------------------------------------


def save_as(window: Control, path: PathArg) -> None:
    """Save the document open in `window` to `path`. Ensures parent dir
    exists, removes any stale file at `path`, brings `window` to the
    foreground, sends Ctrl+S, finds the Save dialog, and drives it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    apps.bring_to_foreground(window)
    pyautogui.hotkey("ctrl", "s")
    dlg = app.popup(window, "save", timeout=10)
    dialogs.save_as(dlg, p)


def screenshot(window: Control, path: PathArg) -> None:
    """Save a PNG of `window`'s bounding rectangle to `path`. Auto-
    foregrounds first."""
    apps.bring_to_foreground(window)
    r = window.BoundingRectangle
    region = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = pyautogui.screenshot(region=region)
    img.save(path)


def close(window: Control) -> None:
    """Terminate the process owning `window`. Use at the end of a `--loop`
    iteration or to clean up after a hung run."""
    try:
        psutil.Process(window.ProcessId).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


# --- Misc -------------------------------------------------------------------


def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Current datetime formatted as a string. Default format is
    'YYYY-MM-DD HH:MM:SS'."""
    return _datetime.now().strftime(fmt)


def wait(seconds: float) -> None:
    """Pause execution for `seconds`. Alias for `time.sleep`."""
    _time.sleep(seconds)


def log(table: str, *values) -> None:
    """Append a row to a SQLite table. Columns auto-typed from `values`:
    int → INTEGER, float → REAL, str/list/dict/set → TEXT (lists & friends
    JSON-encoded). Numpy scalars/arrays supported. Table auto-created."""
    db.log(table, *values)


def read_clipboard() -> str:
    """Return the current clipboard contents as a string. Useful for
    grabbing tabular data copied from Excel (TSV) or a webpage table —
    pass the result straight to `log_csv` and it'll auto-detect the
    delimiter."""
    return pyperclip.paste()


def log_csv(path: PathArg, *rows, header=None, delimiter: str = ",") -> None:
    """Append `rows` to a CSV file. Creates parent dirs; writes `header`
    once, only when the file is first created.

    Each row is an iterable of cells. Cells of type list / tuple / set /
    dict are JSON-encoded into a single string cell; everything else
    (int, float, str, bool, None) is written as-is.

    A single raw string is auto-parsed as CSV/TSV (tab / comma / semicolon
    detected from the first line) — use this for clipboard pass-through:

        log_csv("data/out.csv", read_clipboard(), header=["a", "b"])

    Examples:
        log_csv("data/out.csv", [1, "alpha", 3.14])                # one row
        log_csv("data/out.csv", [1, "a"], [2, "b"], [3, "c"])      # multiple
        log_csv("data/out.csv", row, header=["i", "name", "x"])    # with header
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()

    # Single raw-string arg → treat as clipboard-style table, auto-detect
    # delimiter from the first line.
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
