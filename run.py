"""End-to-end demo: drives Win11 Notepad through a full work cycle.

State machine: open → new_tab → zoom_in → (5s) → zoom_out → (5s) →
type_time → save → close. Each state function returns the name of the
next state (or None to end the pass). `ctx` is a per-pass dict — use it
to share state between functions (e.g. `ctx["window"]` is the Notepad
window every later state operates on).

# Addressing controls — two schemes both work via `actions.press(window, id)`

The harness accepts an element identifier in either form:

1. **Name-based path** — what this demo uses for Notepad's menus, since
   Notepad's menu items have stable Names. The leaf form
   "File:MenuItemControl" is enough; tree.find's leaf+role tier matches
   it. The full path "Untitled - Notepad:WindowControl/.../File:MenuItemControl"
   from the inspector also works (it's just more specific).

2. **Structural id** (`struct_id`) — dotted 0-indexed position in the
   tree, e.g. "0.2.0.0.0". Use this for apps whose controls have NO
   useful Name / AutomationId (the inspector still emits a struct_id
   alongside the name path; copy whichever you prefer). Struct ids
   self-heal across drift via tree correlation: if the live tree shifts
   (sibling inserted, parent reorganised), `find_or_heal` walks the
   saved snapshot upward to a still-stable ancestor and descends by
   role + bbox shape to locate the moved control.

Same `actions.press(window, id)` call site, same caller code — only
the constant changes. Mix-and-match per-control is fine; the format
dispatcher routes by syntax.

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

# Pre-flight inventory: (launch path, window-title substring) for every
# app this script depends on. apps.verify_installed() runs through this
# at startup and raises a single error listing any missing paths -- so
# you fix them all in one edit instead of crashing mid-run on the first
# subprocess.Popen. For installed software the launch path is usually
# a full path, e.g. r"C:\Program Files\ValSuite\ValSuitePro.exe".
REQUIRED_APPS = [
    (NOTEPAD, TITLE),
]

# ---------------------------------------------------------------------------
# Element identifiers
# ---------------------------------------------------------------------------
# Each constant below is an `actions.press(window, id)` argument. For
# each one the LEFT column is what this demo actually uses — Notepad's
# menus have stable Names, so the leaf+role form is the most readable
# choice. The RIGHT column shows the structural-id (`struct_id`)
# alternative captured from the inspector for the SAME control. Either
# form works; the dispatcher in tree.find routes by syntax.
#
# Capture struct_ids by running `python inspector.py`, clicking the
# control, and copying the `struct_id:` line. Struct ids are app-
# specific — the values commented here were captured against Win11
# Notepad and may shift slightly with Notepad updates. The harness
# self-heals across that drift via tree-correlation in find_or_heal.
# ---------------------------------------------------------------------------
FILE_MENU = "File:MenuItemControl"            # struct_id: e.g. "0.2.0.0.0"
VIEW_MENU = "View:MenuItemControl"            # struct_id: e.g. "0.2.0.0.2"
NEW_TAB = "New tab:MenuItemControl"           # struct_id: inside File popup
ZOOM = "Zoom:MenuItemControl"                 # struct_id: inside View popup
ZOOM_IN = "Zoom in:MenuItemControl"           # struct_id: inside Zoom submenu
ZOOM_OUT = "Zoom out:MenuItemControl"         # struct_id: inside Zoom submenu
CLOSE_TAB = "Close tab:MenuItemControl"       # struct_id: inside File popup
EDITOR = "Text editor:DocumentControl"        # struct_id: e.g. "0.0.0"
SAVE_DLG_FILENAME = "File name:ComboBoxControl"

# Apps the watchdog terminates after killing a hung child. The list lets
# the next `--loop` iteration start in a clean state — no leftover menu,
# half-typed text, or modal dialog.
#
# To kill multiple apps, just add them — each entry is matched as a
# case-insensitive substring against process executable names:
#
#     KILL_ON_TIMEOUT = ["notepad.exe", "winword.exe", "calc.exe"]
#
# Empty list → no cleanup (only the python child is killed).
KILL_ON_TIMEOUT = [NOTEPAD]


def state_open(ctx):
    if not apps.is_running(NOTEPAD):
        apps.open_app(NOTEPAD)
    win = apps.get_window(TITLE)
    # No explicit bring_to_foreground here — actions / dialogs.dismiss_ok_popups
    # do it automatically. The first auto-foreground happens inside
    # dismiss_ok_popups below.
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
    # No bring_to_foreground here either — state_close's first press_path
    # auto-foregrounds the Notepad window through actions._resolve.
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
    apps.verify_installed(REQUIRED_APPS)
    if args.loop:
        runner.run_with_watchdog(state_machine, kill_on_timeout=KILL_ON_TIMEOUT)
    else:
        runner.run_once_with_watchdog(state_machine, kill_on_timeout=KILL_ON_TIMEOUT)


if __name__ == "__main__":
    main()
