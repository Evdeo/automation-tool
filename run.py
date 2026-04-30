"""Demo: open Notepad → new tab → zoom in/out → type time → save → close.

Multi-app:  APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}
            then click(data.notepad, ...), click(data.calc, ...).

Every verb is a top-level function taking the window first:
    click(window, control_id)         single click
    double_click(window, control_id)
    fill(window, control_id, text)    paste text into a field
    keys(window, "hello")             type at current focus
    hotkey(window, "ctrl", "s")       send a key combo
    has(window, control_id)           is it visible?
    check_enabled(window, control_id) is it visible AND enabled?
    check_color(window, control_id)   pixel sample, returns (r, g, b)
    wait_gone(window, control_id)     wait until it disappears
    popup(window, "title")            find a sub-window/dialog → returns control
    save_as(window, path)             full Save flow in one call
    screenshot(window, path)          PNG of the window
    keys / hotkey / wait / now / log / close

Each state function takes `data` and returns `(next_state, data)`.
Returning `(None, data)` ends the run.

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
NEW_TAB   = "0.2.0.0.0.0.0"
ZOOM      = "0.2.0.0.2.0.2"
ZOOM_IN   = "0.2.0.0.2.0.2.0.0"
ZOOM_OUT  = "0.2.0.0.2.0.2.0.1"
CLOSE_TAB = "0.2.0.0.0.0.5"
EDITOR    = "0.0.0"


# ─── USER: states ────────────────────────────────────────────────────────────
def state_open_file_menu(data):
    click(data.notepad, FILE_MENU)
    return "click_new_tab", data


def state_click_new_tab(data):
    click(data.notepad, NEW_TAB)
    return "wait_a", data


def state_wait_a(data):
    wait(5)
    return "open_view_menu_in", data


def state_open_view_menu_in(data):
    click(data.notepad, VIEW_MENU)
    return "click_zoom_submenu_in", data


def state_click_zoom_submenu_in(data):
    click(data.notepad, ZOOM)
    return "click_zoom_in", data


def state_click_zoom_in(data):
    click(data.notepad, ZOOM_IN)
    return "wait_b", data


def state_wait_b(data):
    wait(5)
    return "open_view_menu_out", data


def state_open_view_menu_out(data):
    click(data.notepad, VIEW_MENU)
    return "click_zoom_submenu_out", data


def state_click_zoom_submenu_out(data):
    click(data.notepad, ZOOM)
    return "click_zoom_out", data


def state_click_zoom_out(data):
    click(data.notepad, ZOOM_OUT)
    return "type_time", data


def state_type_time(data):
    fill(data.notepad, EDITOR, now())
    return "save", data


def state_save(data):
    save_as(data.notepad, config.SAVE_PATH)
    log("results", "saved", str(config.SAVE_PATH))
    return "open_file_for_close", data


def state_open_file_for_close(data):
    click(data.notepad, FILE_MENU)
    return "click_close_tab", data


def state_click_close_tab(data):
    click(data.notepad, CLOSE_TAB)
    return None, data


STATES = {
    "open_file_menu":         state_open_file_menu,
    "click_new_tab":          state_click_new_tab,
    "wait_a":                 state_wait_a,
    "open_view_menu_in":      state_open_view_menu_in,
    "click_zoom_submenu_in":  state_click_zoom_submenu_in,
    "click_zoom_in":          state_click_zoom_in,
    "wait_b":                 state_wait_b,
    "open_view_menu_out":     state_open_view_menu_out,
    "click_zoom_submenu_out": state_click_zoom_submenu_out,
    "click_zoom_out":         state_click_zoom_out,
    "type_time":              state_type_time,
    "save":                   state_save,
    "open_file_for_close":    state_open_file_for_close,
    "click_close_tab":        state_click_close_tab,
}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="open_file_menu")
