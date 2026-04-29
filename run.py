"""End-to-end demo: drives Win11 Notepad through a full work cycle and
loops under the watchdog so a hung iteration is killed and restarted.

State machine: open → new_tab → zoom_in → (5s) → zoom_out → (5s) →
type_time → save → close. Each state function returns the name of the
next state (or None to end the pass). `ctx` is a per-pass dict — use it
to share state between functions (e.g. `ctx["window"]` is the Notepad
window every later state operates on).

Run with:    python run.py
"""
import time
from datetime import datetime
from pathlib import Path

import pyautogui

from core import actions, apps, db, dialogs, runner


NOTEPAD = "notepad.exe"
TITLE = "Notepad"
SAVE_PATH = Path("data/notepad_demo.txt").resolve()

FILE_MENU = "File:MenuItemControl"
VIEW_MENU = "View:MenuItemControl"
NEW_TAB = "New tab:MenuItemControl"
ZOOM = "Zoom:MenuItemControl"
ZOOM_IN = "Zoom in:MenuItemControl"
ZOOM_OUT = "Zoom out:MenuItemControl"
CLOSE_TAB = "Close tab:MenuItemControl"
EDITOR = "Text editor:DocumentControl"
SAVE_DLG_FILENAME = "File name:ComboBoxControl"


def state_open(ctx):
    if not apps.is_running(NOTEPAD):
        apps.open_app(NOTEPAD)
    win = apps.get_window(TITLE)
    apps.bring_to_foreground(win)
    dialogs.dismiss_ok_popups(win)
    ctx["window"] = win
    db.log("results", "opened", win.Name)
    return "new_tab"


def state_new_tab(ctx):
    actions.press_path(ctx["window"], FILE_MENU, NEW_TAB)
    db.log("results", "new_tab", 1)
    return "zoom_in"


def state_zoom_in(ctx):
    time.sleep(5.0)  # demo pacing — user-visible
    actions.press_path(ctx["window"], VIEW_MENU, ZOOM, ZOOM_IN)
    db.log("results", "zoom_in", 1)
    return "zoom_out"


def state_zoom_out(ctx):
    time.sleep(5.0)  # demo pacing — user-visible
    actions.press_path(ctx["window"], VIEW_MENU, ZOOM, ZOOM_OUT)
    db.log("results", "zoom_out", 1)
    return "type_time"


def state_type_time(ctx):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actions.write_text(ctx["window"], EDITOR, now)
    ctx["written_text"] = now
    db.log("results", "wrote_time", now)
    return "save"


def state_save(ctx):
    SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SAVE_PATH.exists():
        SAVE_PATH.unlink()
    pyautogui.hotkey("ctrl", "s")
    dlg = dialogs.find_dialog("save")
    if dlg is None:
        raise RuntimeError("Save As dialog did not appear")
    dialogs.save_as(dlg, SAVE_PATH)
    actions.wait_until_absent(ctx["window"], SAVE_DLG_FILENAME, timeout=10)
    apps.bring_to_foreground(ctx["window"])
    db.log("results", "saved", str(SAVE_PATH))
    return "close"


def state_close(ctx):
    dialogs.dismiss_ok_popups(ctx["window"])
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


def state_machine():
    ctx = {}
    state = "open"
    while state is not None:
        state = STATES[state](ctx)
    return ctx


def loop():
    while True:
        state_machine()
        time.sleep(2)


def main():
    runner.run_with_watchdog(loop)


if __name__ == "__main__":
    main()
