"""Demo: open Notepad → new tab → zoom in/out → type time → save → close.

Multi-app:  APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}
            then click(data.notepad, ...), click(data.calc, ...).

Every verb is a top-level function taking the window first
(except `type`, which has no window — it just sends keys to current
focus, useful when a dialog opens with a field already selected):
    click(window, control_id)             single click
    double_click(window, control_id)
    right_click(window, control_id)       opens a context menu
    click_when_enabled(window, id)        wait for enabled, then click
    click_after(window, id, delay)        sleep, then click
    fill(window, control_id, text)        click + paste text into a field
    type("hello")                         type at current focus (no click)
    hotkey(window, "ctrl", "s")           send a key combo
    check_visible(window, control_id)     is it visible? (snapshot)
    check_enabled(window, control_id)     is it visible AND enabled? (snapshot)
    check_color(window, control_id)       pixel sample, returns (r, g, b)
    wait_visible(window, id, timeout=10)  block until visible
    wait_enabled(window, id, timeout=10)  block until visible AND enabled
    wait_gone(window, id, timeout=10)     block until it disappears (popup closes)
    read_info(window, control_id)         dict of every UIA property
    each(verb, window, [id, id, ...])     apply a verb to many ids → list
    popup(window, "title")                find a sub-window/dialog → returns control
    save_as(window, path)                 full Save flow in one call
    screenshot(window, path)              PNG of the window
    wait, now, log, close
    read_clipboard()                      grab clipboard contents
    log_csv(path, *rows, header=...)      append to a CSV file

Each state function takes `data` and returns `(next_state, data)`.
Returning `(None, data)` ends the run. Each state should perform one
clearly-defined task — multiple clicks are fine inside a state when
they belong to the same logical step.

For --loop runs that may inherit a leftover popup from a killed
iteration, add `dismiss_popups(data.notepad)` at the top of your
first state. One-shot runs from a clean state never need it.
"""
import config
from core import (
    click, fill, hotkey, popup, save_as,
    log, now, wait,
    runner,
)


# ─── USER: apps ──────────────────────────────────────────────────────────────
APPS = {"notepad": "notepad.exe"}


# ─── USER: controls (struct-ids from `python inspector.py notepad.exe`) ──────
FILE_MENU = "0.2.0.0.0"
VIEW_MENU = "0.2.0.0.2"
NEW_TAB   = "0.0.0.0.0.0"
ZOOM      = "0.0.0.0.0.0"
ZOOM_IN   = "0.0.0.0.0.0"
ZOOM_OUT  = "0.0.0.0.0.1"
CLOSE_TAB = "0.0.0.0.0.10"
EDITOR    = "0.0.0"


# ─── USER: states ────────────────────────────────────────────────────────────
def state_new_tab(data):
    click(data.notepad, FILE_MENU)
    click(data.notepad, NEW_TAB)
    return "zoom_in", data


def state_zoom_in(data):
    wait(5)
    click(data.notepad, VIEW_MENU)
    click(data.notepad, ZOOM)
    click(data.notepad, ZOOM_IN)
    return "zoom_out", data


def state_zoom_out(data):
    wait(5)
    click(data.notepad, VIEW_MENU)
    click(data.notepad, ZOOM)
    click(data.notepad, ZOOM_OUT)
    return "type_time", data


def state_type_time(data):
    fill(data.notepad, EDITOR, now())
    return "save", data


def state_save(data):
    save_as(data.notepad, config.SAVE_PATH)
    log("results", "saved", str(config.SAVE_PATH))
    return "close", data


def state_close(data):
    click(data.notepad, FILE_MENU)
    click(data.notepad, CLOSE_TAB)
    return None, data


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
