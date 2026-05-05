"""Minimal example: drive Notepad, then swap to Calculator.

================================  VERBS REFERENCE  ============================

Window lifecycle (`from core import window`):
  window.open(name) -> Control            launch or rebind to registered app
  window.close(name) -> None              terminate process, drop handle
  window.get(name, timeout=0) -> Control  match existing only, no launch
  window.<name>                           cached handle for an opened app

Popups:
  popup(name, trigger_call, timeout=5) -> Control | None
      Wrap an action verb that triggers a popup; polls for the popup
      to appear. Reads top-down: name the popup, then the action.
        dlg = popup("save_dialog", click(window.notepad, SAVE_BTN))
      Omitting `trigger_call` polls for a popup that's already visible
      or about to be (snapshot mode).

Clicks (return bool, take window first):
  click(window, id)                       single click
  double_click(window, id)                double click
  right_click(window, id)                 opens context menu
  click_when_enabled(window, id, t=30)    wait for enabled, then click
  click_after(window, id, delay)          sleep delay seconds, then click
  move(window, id)                        move cursor without clicking
  hold_and_drag(window, src_id, dst_id)   press src, drag to dst, release
  set_checkbox(window, id, value=True, attempts=3) -> bool
      Click the checkbox/toggle until is_checked == value. No-op if
      already correct. Pairs with each(set_checkbox, ..., value=True)
      to set many at once.

Coord-based clicks (no UIA — for browser/Playwright targets):
  click_at(x, y) / move_at(x, y) / hold_and_drag_at(x1, y1, x2, y2)
  web_coords(page, selector) -> (x, y)    Playwright DOM -> screen coords

Text input:
  fill(window, id, text)                  click + paste (clipboard-based)
  type(text, interval=0.02)               type at current focus
  key(*combo)                             press at current focus, e.g. key("enter")
  hotkey(window, *combo)                  foreground window, then press combo

Snapshot checks (no waiting):
  is_visible(window, id) -> bool
  is_enabled(window, id) -> bool
  is_color(window, id, rgb, tolerance=0) -> bool   centre pixel match
  is_color_area(window, id, rgb, tolerance=0, padding=0) -> bool
      True if ANY pixel in the control's bbox matches `rgb`. Use for
      colored icons / dots / glyphs that don't sit at the centre.
      `padding` (percent) trims each edge before scanning.
  is_checked(window, id) -> True | False | None
      UIA TogglePattern state. None = indeterminate or not toggleable.
  check_color(window, id) -> (r, g, b)    sample center pixel
  read_info(window, id) -> dict           every UIA property

Waits (return bool, default timeout 10s):
  wait_visible(window, id, timeout=10)
  wait_enabled(window, id, timeout=10)
  wait_gone(window, id, timeout=10)

Batched calls:
  each(verb, window, ids, **kwargs) -> list
      Apply verb to each id; per-call popup dismiss runs as normal.
      Use for INDEPENDENT calls (color audits, multi-button checks).
  sequence(verb, window, ids, attempts=3, **kwargs) -> list
      Same shape, but if a popup interrupts mid-flow it's dismissed
      and the loop restarts from id 0. Use for DEPENDENT sequences
      (menu navigation: File -> Save As -> Confirm). `verb` may be
      one callable (applied to all ids) or a list of callables the
      same length as `ids` (verb[i] applied to ids[i]).

Other:
  no_dismiss()                            context manager: skip auto-dismiss
  screenshot(window, path) -> None        PNG of window's bounding rect
  log(table, *values) -> None             append a row to SQLite table
  log_csv(path, *rows, header=None) -> None
  read_clipboard() -> str
  now(fmt="%Y-%m-%d %H:%M:%S") -> str
  wait(seconds) -> None                   sleep

Runner:
  runner.start(STATES, apps=APPS, start_state=..., error_state=None,
               prelaunch=True)
      State machine driver. `error_state` defaults to `start_state`;
      when --loop is set, the next iteration after a kill or crash
      starts there instead. `prelaunch=False` skips the up-front
      app-open loop for apps you'll open/close yourself per state.

================================================================================

Full feature tour: showcase.py.  Run:  python run.py
"""
from core import (
    click_after, click_when_enabled, fill, hotkey,
    wait_visible, each, sequence, read_clipboard,
    log, now, runner, window,
)


APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}


# Capture control ids with `python inspector.py`.
EDITOR    = "0.0.0"
FILE_MENU = "0.2.0.0.0"
NEW_TAB   = "0.0.0.0.0.0"
CLOSE_TAB = "0.0.0.0.0.10"

# Calculator buttons — name-based ids (Calc exposes stable UIA Names).
PLUS    = "Plus:ButtonControl"
EQUALS  = "Equals:ButtonControl"
CLEAR   = "Clear:ButtonControl"
TWO     = "Two:ButtonControl"
THREE   = "Three:ButtonControl"
FOUR    = "Four:ButtonControl"
SEVEN   = "Seven:ButtonControl"


def state_notepad(data):
    """Open Notepad, drive a File-menu sequence, paste a timestamp."""
    window.open("notepad")
    if not wait_visible(window.notepad, FILE_MENU, timeout=10):
        log("results", "notepad_init_failed", "")
        return None, data

    # `sequence` because each step depends on the previous: clicking
    # NEW_TAB only works while the File menu is still open.
    sequence(click_when_enabled, window.notepad, [FILE_MENU, NEW_TAB])

    fill(window.notepad, EDITOR, f"timestamp: {now()}\n")
    window.close("notepad")
    return "calc", data


def state_calc(data):
    """Swap to Calculator, click 47 + 32 = via each + click_after."""
    window.open("calc")
    if not wait_visible(window.calc, PLUS, timeout=15):
        log("results", "calc_init_failed", "")
        return None, data
    click_when_enabled(window.calc, CLEAR, timeout=5)

    # `each` — these clicks are independent; per-call auto-dismiss
    # handles any popup between presses.
    each(click_after, window.calc,
         [FOUR, SEVEN, PLUS, THREE, TWO, EQUALS], delay=0.1)

    hotkey(window.calc, "ctrl", "c")
    log("results", "calc_result", read_clipboard().strip())
    window.close("calc")
    return None, data


STATES = {
    "notepad": state_notepad,
    "calc":    state_calc,
}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="notepad", prelaunch=False)
