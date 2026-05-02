"""Demo: open Notepad -> new tab -> zoom in/out -> type time -> verify -> save -> close.

This file is intentionally a richer-than-minimal example: every state
shows *one* stability pattern that pays off when a real app behaves
flakily (slow menu open, transient popup, button momentarily disabled).
Each state still does ONE clearly-defined task; the patterns are
optional sugar layered on top of the bare verbs.

Multi-app
---------
APPS is a dict mapping a name -> the launchable spec. Add a second
entry to drive two apps in one test:

    APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}
    # then click(data.notepad, ...) and click(data.calc, ...)

Verb reference
--------------
Every verb takes the window as the first arg (except `type`, which
sends keys to whichever control currently has focus):

    click(window, id)                     single click
    double_click(window, id)
    right_click(window, id)               opens a context menu
    click_when_enabled(window, id)        wait for enabled, then click
    click_after(window, id, delay)        sleep, then click
    fill(window, id, text)                click + paste text into a field
    type("hello")                         type at current focus (no click)
    key("enter") / key("ctrl", "c")       press a key/combo at current focus
    hotkey(window, "ctrl", "s")           send a key combo (foregrounds first)
    is_visible(window, id)                snapshot bool (timeout=0)
    is_enabled(window, id)                snapshot bool (timeout=0)
    is_color(window, id, rgb, tol=0)      pixel match -> bool
    check_color(window, id)               pixel sample -> (r, g, b)
    wait_visible(window, id, timeout=10)  block until visible
    wait_enabled(window, id, timeout=10)  block until visible + enabled
    wait_gone(window, id, timeout=10)     block until disappears
    read_info(window, id)                 dict of every UIA property
    each(verb, window, [id, ...])         apply a verb to many ids -> list
    match(name, launch=...)               find a window by saved fingerprint
    no_dismiss()                          context manager: skip auto-dismiss
    screenshot(window, path)              PNG of the window
    read_clipboard()                      grab clipboard contents
    log(table, *cols)                     append to SQLite table
    log_csv(path, *rows, header=...)      append to a CSV file
    now() / wait(seconds) / close(window)

Each state function takes `data` and returns `(next_state, data)`.
Returning `(None, data)` ends the run.

Recovery from a leftover popup (after a killed --loop iteration) is
handled by `dismiss_popups` in `state_init`; one-shot runs from a
clean state pay no overhead because there are no popups to dismiss.
"""
import config
from core import (
    click, double_click, right_click, click_when_enabled, click_after,
    fill, hotkey, type, key,
    is_visible, is_enabled, is_color, check_color,
    wait_visible, wait_enabled, wait_gone,
    read_info, read_clipboard, each, no_dismiss,
    screenshot, close,
    log, log_csv, now, wait,
    runner,
)


# --- USER: apps ------------------------------------------------------------
APPS = {"notepad": "notepad.exe"}


# --- USER: controls (struct-ids from `python inspector.py`) ---------------
FILE_MENU = "0.2.0.0.0"
VIEW_MENU = "0.2.0.0.2"
NEW_TAB   = "0.0.0.0.0.0"
ZOOM      = "0.0.0.0.0.0"
ZOOM_IN   = "0.0.0.0.0.0"
ZOOM_OUT  = "0.0.0.0.0.1"
CLOSE_TAB = "0.0.0.0.0.10"
EDITOR    = "0.0.0"


# --- USER: states ----------------------------------------------------------
def state_init(data):
    """Wait for the File menu to be visible — proves the window is
    responsive before we drive it. Stale popups are auto-dismissed by
    every action verb; no explicit dismiss call needed.
    """
    if not wait_visible(data.notepad, FILE_MENU, timeout=10):
        log("results", "init_failed", "FILE_MENU never visible")
        return None, data
    return "new_tab", data


def state_new_tab(data):
    """Open the File menu, then wait for the popup before clicking
    "New tab". Without `wait_visible`, fast machines race ahead and
    miss the menu item that's still rendering.

    Demonstrates: `click_when_enabled`, `wait_visible`, `wait_gone`.
    """
    click_when_enabled(data.notepad, FILE_MENU)
    if not wait_visible(data.notepad, NEW_TAB, timeout=5):
        log("results", "new_tab_failed", "NEW_TAB never appeared")
        return "close", data
    click(data.notepad, NEW_TAB)
    # The menu should close after the click — confirm before next state.
    wait_gone(data.notepad, NEW_TAB, timeout=3)
    return "zoom_in", data


def state_zoom_in(data):
    """View > Zoom > Zoom In. Each menu hop uses `click_when_enabled`
    to absorb the brief moment the next item is animating in."""
    click_when_enabled(data.notepad, VIEW_MENU)
    wait_visible(data.notepad, ZOOM, timeout=5)
    click_when_enabled(data.notepad, ZOOM)
    wait_visible(data.notepad, ZOOM_IN, timeout=5)
    click(data.notepad, ZOOM_IN)
    return "zoom_out", data


def state_zoom_out(data):
    click_when_enabled(data.notepad, VIEW_MENU)
    wait_visible(data.notepad, ZOOM, timeout=5)
    click_when_enabled(data.notepad, ZOOM)
    wait_visible(data.notepad, ZOOM_OUT, timeout=5)
    click(data.notepad, ZOOM_OUT)
    return "type_time", data


def state_type_time(data):
    """Drop the current timestamp into the editor. `fill` is the
    paste-based variant — no per-keystroke timing flakiness."""
    fill(data.notepad, EDITOR, now())
    return "extra", data


def state_extra(data):
    """Exercise the click family + clipboard verbs that the main
    narrative doesn't naturally hit:

      * `double_click` — selects a word in the editor.
      * `hotkey` Ctrl+C → `read_clipboard` round-trip.
      * `right_click` — opens the editor context menu.
      * `key("escape")` — dismisses the context menu without yanking
        focus back to the parent (proves `key` is the focus-targeted
        sibling of `hotkey`).
      * `click_after` — same as `click` but with a built-in pacing
        delay; demonstrates the verb without inventing a use case.
    """
    double_click(data.notepad, EDITOR)
    wait(0.2)
    hotkey(data.notepad, "ctrl", "c")
    wait(0.2)
    selected = read_clipboard().strip()
    log("results", "selected_word", selected)

    right_click(data.notepad, EDITOR)
    wait(0.4)
    key("escape")
    wait(0.2)

    # `click_after` is `click` with a leading `wait` baked in — useful
    # when the next click should land after a short settle period.
    click_after(data.notepad, EDITOR, delay=0.1)
    return "verify", data


def state_verify(data):
    """Self-check before saving. Confirms every key control is still
    present + enabled (`each` + `is_visible` + `is_enabled`), reads
    the editor's UIA properties, samples a screen pixel, color-asserts
    it with `is_color`, and waits for the menu to be ready with
    `wait_enabled`. Writes a verification row to a CSV.

    Demonstrates: `each`, `is_visible`, `is_enabled`, `is_color`,
    `wait_enabled`, `read_info`, `check_color`, `log_csv`. Fail-soft —
    anomalies are logged but the state machine continues.
    """
    wait_enabled(data.notepad, FILE_MENU, timeout=5)

    targets = [FILE_MENU, VIEW_MENU, EDITOR]
    visible = each(is_visible, data.notepad, targets)
    enabled = each(is_enabled, data.notepad, targets)
    info = read_info(data.notepad, EDITOR)
    color = check_color(data.notepad, FILE_MENU)
    # Self-match: tolerance=30 so theme drift (light/dark mode) doesn't
    # fail the check. The bool is logged for the user to inspect.
    color_match = is_color(data.notepad, FILE_MENU, color, tolerance=30)

    log_csv(
        config.RESULTS_DIR / "verify.csv",
        [now(), "visibility", visible, "enabled", enabled,
         "editor_class", info["class_name"], "menu_color", list(color),
         "self_match", color_match],
        header=["ts", "kind1", "vals1", "kind2", "vals2",
                "info_kind", "info_value", "color_kind", "color_value",
                "match_kind", "match_value"],
    )

    if not all(visible) or not all(enabled):
        log("results", "verify_warning",
            "missing or disabled controls", visible, enabled)
    return "snapshot", data


def state_snapshot(data):
    """Capture the window state as a PNG before the destructive save.
    Useful for after-the-fact debugging if the save flow misbehaves."""
    out = config.RESULTS_DIR / "before_save.png"
    screenshot(data.notepad, out)
    log("results", "screenshot", str(out))
    return "save", data


def state_save(data):
    """Save via Ctrl+S → type path → Enter, all inside `no_dismiss()`
    so the auto-dismiss never sees the Save dialog. `key("enter")` —
    not `hotkey(data.notepad, "enter")` — sends the confirm without
    yanking focus back to Notepad's main window (which would type the
    Enter into the editor instead of submitting the dialog)."""
    target = str(config.SAVE_PATH)
    config.SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if config.SAVE_PATH.exists():
        config.SAVE_PATH.unlink()
    with no_dismiss():
        hotkey(data.notepad, "ctrl", "s")
        wait(0.6)
        type(target)
        wait(0.2)
        key("enter")
        wait(0.8)
    log("results", "saved", target)
    return "close", data


def state_close(data):
    """Close the active tab via the File menu. As a final teardown
    step, `close(window)` terminates the Notepad process — the same
    verb a user would call to clean up after a watchdog timeout."""
    if not is_visible(data.notepad, FILE_MENU):
        log("results", "close_skipped", "FILE_MENU not visible")
        close(data.notepad)
        return None, data
    click(data.notepad, FILE_MENU)
    wait_visible(data.notepad, CLOSE_TAB, timeout=3)
    click(data.notepad, CLOSE_TAB)
    wait(0.4)
    close(data.notepad)
    return None, data


STATES = {
    "init":      state_init,
    "new_tab":   state_new_tab,
    "zoom_in":   state_zoom_in,
    "zoom_out":  state_zoom_out,
    "type_time": state_type_time,
    "extra":     state_extra,
    "verify":    state_verify,
    "snapshot":  state_snapshot,
    "save":      state_save,
    "close":     state_close,
}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="init")
