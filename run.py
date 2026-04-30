"""Demo: open Notepad → new tab → zoom in/out → type time → save → close.

Multi-app:  APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}
            then ctx.notepad.menu(...), ctx.calc.click(...).

Every verb is a method on the window — type ctx.notepad. in your IDE
to see the full list. Most-used: click (one control), menu (chain
through a menu tree), fill (paste text into a field), hotkey, popup,
save_as, has, wait_gone.

For --loop runs that may inherit a leftover popup from a killed
iteration, add ctx.<app>.dismiss_popups() at the top of your first
state. One-shot runs from a clean state never need it.
"""
import time
from datetime import datetime

import config
from core import db, runner


# ─── USER: apps ──────────────────────────────────────────────────────────────
APPS = {"notepad": "notepad.exe"}
# Keys become attributes on ctx (ctx.notepad). For a list form,
# APPS = ["notepad.exe"] auto-derives ctx.notepad from the exe stem.


# ─── USER: controls (struct-ids from `python inspector.py notepad.exe`) ──────
FILE_MENU = "0.2.0.0.0"
VIEW_MENU = "0.2.0.0.2"
NEW_TAB   = "0.2.0.0.0.0.0"
ZOOM      = "0.2.0.0.2.0.2"
ZOOM_IN   = "0.2.0.0.2.0.2.0.0"
ZOOM_OUT  = "0.2.0.0.2.0.2.0.1"
CLOSE_TAB = "0.2.0.0.0.0.5"
EDITOR    = "0.0.0"


# ─── USER: states ────────────────────────────────────────────────────────────
def state_new_tab(ctx):
    ctx.notepad.menu(FILE_MENU, NEW_TAB)
    return "zoom_in"


def state_zoom_in(ctx):
    time.sleep(5.0)
    ctx.notepad.menu(VIEW_MENU, ZOOM, ZOOM_IN)
    return "zoom_out"


def state_zoom_out(ctx):
    time.sleep(5.0)
    ctx.notepad.menu(VIEW_MENU, ZOOM, ZOOM_OUT)
    return "type_time"


def state_type_time(ctx):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ctx.notepad.fill(EDITOR, now)
    db.log("results", "wrote_time", now)
    return "save"


def state_save(ctx):
    config.SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if config.SAVE_PATH.exists():
        config.SAVE_PATH.unlink()
    ctx.notepad.hotkey("ctrl", "s")
    ctx.save_dlg = ctx.notepad.popup("save")
    ctx.save_dlg.save_as(config.SAVE_PATH)
    db.log("results", "saved", str(config.SAVE_PATH))
    return "close"


def state_close(ctx):
    ctx.notepad.menu(FILE_MENU, CLOSE_TAB)
    return None


STATES = {
    "new_tab":   state_new_tab,
    "zoom_in":   state_zoom_in,
    "zoom_out":  state_zoom_out,
    "type_time": state_type_time,
    "save":      state_save,
    "close":     state_close,
}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="new_tab")
