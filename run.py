"""End-to-end demo: drives Win11 Notepad through a full work cycle.

State machine: open → new_tab → zoom_in → (5s) → zoom_out → (5s) →
type_time → save → close. Each state function returns the name of the
next state (or None to end the pass). `ctx` is a per-pass dict — use it
to share state between functions (e.g. `ctx["window"]` is the Notepad
window every later state operates on).

Run modes:
  python run.py            one-shot with watchdog timeout safety net
  python run.py --loop     forever-loop, watchdog respawns each iteration
                           (kills + restarts if a pass exceeds
                            config.LOOP_TIMEOUT_MIN minutes)

Both modes share the same watchdog timeout — `config.LOOP_TIMEOUT_MIN`.
"""
import argparse
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


KILL_ON_TIMEOUT = [NOTEPAD]
"""Apps the watchdog terminates after killing a hung child. Wipes any
half-typed text / open menu / blocking dialog the child left behind so
the next iteration starts in a clean state."""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously: each iteration is a fresh child process "
             "supervised by the watchdog. The watchdog kills + respawns "
             "after every iteration (whether it exited cleanly or hit "
             "the timeout). Stop with Ctrl+C.",
    )
    args = parser.parse_args()
    if args.loop:
        runner.run_with_watchdog(state_machine, kill_on_timeout=KILL_ON_TIMEOUT)
    else:
        runner.run_once_with_watchdog(state_machine, kill_on_timeout=KILL_ON_TIMEOUT)


if __name__ == "__main__":
    main()
