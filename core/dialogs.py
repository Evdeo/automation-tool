"""Generic Win32 / UIA dialog helpers — reusable across any Win11 app.

These were originally inlined in run.py but the logic is not Notepad-specific:
finding a #32770 dialog by title substring, dismissing OK-button popups,
and driving a Save As dialog all generalise. Pulling them here keeps run.py
focused on the app-specific demo and lets other harnesses reuse the same
mechanics.
"""
import ctypes
import time
from ctypes import wintypes
from pathlib import Path

import pyperclip
import uiautomation as auto

from core import actions, apps, tree as tree_mod


_user32 = ctypes.windll.user32


def dismiss_ok_popups(window, max_passes=5, settle=0.6):
    """Walk `window`'s live tree and click any visible "OK" ButtonControl,
    repeating up to `max_passes` times in case dismissing one popup reveals
    another. Returns the number of clicks fired.

    Uses `actions._cursor_click` (SendInput-based) — `pyautogui.click` does
    NOT register on WinUI popup buttons (same root cause as the menu-item
    no-op fix; this helper used to silently do nothing on modern Notepad).
    """
    apps.bring_to_foreground(window)
    dismissed = 0
    for _ in range(max_passes):
        walked = tree_mod.walk_live(window)
        ok_btn = next(
            (n["ctrl"] for n in walked
             if n["name"] == "OK" and n["role"] == "ButtonControl"),
            None,
        )
        if ok_btn is None:
            return dismissed
        r = ok_btn.BoundingRectangle
        if r.right - r.left <= 0:
            return dismissed
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        actions._cursor_click(cx, cy)
        dismissed += 1
        time.sleep(settle)
    return dismissed


def save_as(dialog, path):
    """Drive a standard Win32 Save As dialog (`#32770`): focus the File-name
    combo, replace its content with `path`, click Save.

    Uses clipboard paste rather than synthetic typing because pyautogui's
    keystroke API is unreliable on the combo's inner edit on some locales —
    same family of issue as the SendInput click fix. Raises RuntimeError
    with a specific message on each failure mode rather than silently
    saving with the auto-suggested filename.
    """
    apps.bring_to_foreground(dialog)
    name_combo = dialog.ComboBoxControl(Name="File name:")
    if not name_combo.Exists(0, 0):
        raise RuntimeError("Save As: 'File name:' combo not found in dialog")

    edit = name_combo.EditControl()
    target = edit if edit.Exists(0, 0) else name_combo
    r = target.BoundingRectangle
    actions._cursor_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.2)

    # Ctrl+A then Delete clears any auto-suggested filename. Pasting via
    # clipboard sets the value reliably regardless of keyboard layout.
    import pyautogui
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    pyperclip.copy(str(Path(path)))
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)

    save_btn = dialog.ButtonControl(Name="Save")
    if not save_btn.Exists(0, 0):
        raise RuntimeError("Save As: 'Save' button not found in dialog")
    r = save_btn.BoundingRectangle
    actions._cursor_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
