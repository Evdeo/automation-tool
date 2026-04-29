import ctypes
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pyautogui
import uiautomation as auto

from core import actions, apps, db


_user32 = ctypes.windll.user32


def _find_save_dialog(timeout=8):
    deadline = time.time() + timeout
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    while time.time() < deadline:
        found = []

        def cb(hwnd, _lp):
            if not _user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "#32770":
                length = _user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                if "save" in buf.value.lower():
                    found.append(hwnd)
            return True

        _user32.EnumWindows(EnumWindowsProc(cb), 0)
        if found:
            return auto.ControlFromHandle(found[0])
        time.sleep(0.2)
    return None


NOTEPAD = "notepad.exe"
TITLE = "Notepad"
SAVE_PATH = Path("data/notepad_demo.txt").resolve()

FILE_MENU = "File:MenuItemControl"
VIEW_MENU = "View:MenuItemControl"
NEW_TAB = "New tab:MenuItemControl"
ZOOM = "Zoom:MenuItemControl"
ZOOM_IN = "Zoom in:MenuItemControl"
ZOOM_OUT = "Zoom out:MenuItemControl"
SAVE = "Save:MenuItemControl"
CLOSE_TAB = "Close tab:MenuItemControl"
EDITOR = "Text editor:DocumentControl"


def _focus(ctx):
    apps.bring_to_foreground(ctx["window"])


def _dismiss_modal_popups(window, max_passes=5):
    from core import tree as tree_mod
    for _ in range(max_passes):
        walked = tree_mod.walk_live(window)
        ok_btn = None
        for n in walked:
            if n["name"] == "OK" and n["role"] == "ButtonControl":
                ok_btn = n["ctrl"]
                break
        if ok_btn is None:
            return
        r = ok_btn.BoundingRectangle
        if r.right - r.left <= 0:
            return
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        pyautogui.click(cx, cy)
        time.sleep(0.6)


def state_open(ctx):
    if not apps.is_running(NOTEPAD):
        apps.open_app(NOTEPAD)
        time.sleep(2.5)
    win = apps.get_window(TITLE)
    ctx["window"] = win
    _focus(ctx)
    _dismiss_modal_popups(win)
    db.log("results", "opened", win.Name)
    return "new_tab"


def state_new_tab(ctx):
    _focus(ctx)
    actions.press_path(ctx["window"], FILE_MENU, NEW_TAB)
    time.sleep(0.8)
    db.log("results", "new_tab", 1)
    return "zoom_in"


def state_zoom_in(ctx):
    time.sleep(5.0)
    _focus(ctx)
    actions.press_path(ctx["window"], VIEW_MENU, ZOOM, ZOOM_IN)
    db.log("results", "zoom_in", 1)
    return "zoom_out"


def state_zoom_out(ctx):
    time.sleep(5.0)
    _focus(ctx)
    actions.press_path(ctx["window"], VIEW_MENU, ZOOM, ZOOM_OUT)
    db.log("results", "zoom_out", 1)
    return "type_time"


def state_type_time(ctx):
    time.sleep(0.5)
    _focus(ctx)
    pyautogui.press("escape")
    time.sleep(0.3)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actions.write_text(ctx["window"], EDITOR, now)
    time.sleep(0.5)
    ctx["written_text"] = now
    db.log("results", "wrote_time", now)
    return "save"


def state_save(ctx):
    import pyperclip
    SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SAVE_PATH.exists():
        SAVE_PATH.unlink()
    _focus(ctx)
    pyautogui.hotkey("ctrl", "s")
    dlg = _find_save_dialog()
    if dlg is None:
        raise RuntimeError("Save As dialog did not appear")

    name_combo = dlg.ComboBoxControl(Name="File name:")
    if not name_combo.Exists(0, 0):
        raise RuntimeError("File name combo not found in Save As dialog")
    edit = name_combo.EditControl()
    target = edit if edit.Exists(0, 0) else name_combo
    r = target.BoundingRectangle
    pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    pyperclip.copy(str(SAVE_PATH))
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)

    save_btn = dlg.ButtonControl(Name="Save")
    if save_btn.Exists(0, 0):
        try:
            save_btn.GetInvokePattern().Invoke()
        except Exception:
            r = save_btn.BoundingRectangle
            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    else:
        pyautogui.press("enter")
    time.sleep(2.5)
    db.log("results", "saved", str(SAVE_PATH))
    return "close"


def state_close(ctx):
    _focus(ctx)
    _dismiss_modal_popups(ctx["window"])
    _focus(ctx)
    actions.press_path(ctx["window"], FILE_MENU, CLOSE_TAB)
    db.log("results", "closed", 1)
    return None


STATES = {
    "open": state_open,
    "new_tab": state_new_tab,
    "zoom_in": state_zoom_in,
    "zoom_out": state_zoom_out,
    "type_time": state_type_time,
    "save": state_save,
    "close": state_close,
}


def run_once():
    ctx = {}
    state = "open"
    while state is not None:
        state = STATES[state](ctx)
    return ctx


if __name__ == "__main__":
    ctx = run_once()
    print(f"DONE: wrote {ctx.get('written_text')!r} to {SAVE_PATH}")
