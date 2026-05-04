"""Minimal example: drive Notepad, then swap to Calculator.

  - state machine wiring via `runner.start`
  - swap apps that can't coexist via `window.open` / `window.close`
    (`prelaunch=False` so the runner doesn't open both up front)
  - `fill`, `click_after`, `click_when_enabled`, `hotkey`, `wait_visible`
  - `each` for batched verb calls (per-call popup dismiss still runs)
  - `log` + `now` for a run-scoped audit trail

Full feature tour: showcase.py.  Run:  python run.py
"""
from core import (
    click_after, click_when_enabled, fill, hotkey,
    wait_visible, each, read_clipboard,
    log, now, runner, window,
)


APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}


# Capture control ids with `python inspector.py`.
EDITOR    = "0.0.0"
FILE_MENU = "0.2.0.0.0"

# Calculator buttons — name-based ids (Calc exposes stable UIA Names).
PLUS    = "Plus:ButtonControl"
EQUALS  = "Equals:ButtonControl"
CLEAR   = "Clear:ButtonControl"
TWO     = "Two:ButtonControl"
THREE   = "Three:ButtonControl"
FOUR    = "Four:ButtonControl"
SEVEN   = "Seven:ButtonControl"


def state_notepad(data):
    """Open Notepad, paste a timestamp, close it."""
    window.open("notepad")
    if not wait_visible(window.notepad, FILE_MENU, timeout=10):
        log("results", "notepad_init_failed", "")
        return None, data
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

    # `each` calls click_after on every id in order, with the same
    # auto-dismiss every action verb does on its own.
    each(click_after, window.calc,
         [FOUR, SEVEN, PLUS, THREE, TWO, EQUALS], delay=0.1)

    hotkey(window.calc, "ctrl", "c")
    log("results", "calc_result", read_clipboard().strip())
    window.close("calc")
    return None, data


STATES = {"notepad": state_notepad, "calc": state_calc}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="notepad", prelaunch=False)
