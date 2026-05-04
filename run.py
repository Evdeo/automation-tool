"""Minimal example: drive Notepad, then swap to Calculator.

Demonstrates the everyday surface in two states:
  - state machine wiring via `runner.start`
  - swapping apps that can't coexist with `window.open` / `window.close`
    (`prelaunch=False` so the runner doesn't open both up front)
  - `fill`, `click`, `click_when_enabled`, `hotkey`, `wait_visible`
  - `each` as an atomic batch — popup mid-loop restarts from id 0
  - `log` + `now` for a run-scoped audit trail

For the full feature tour (popups, screenshots, save dialogs, color
audits, etc.) see showcase.py. Run:  python run.py
"""
from core import (
    click, click_when_enabled, fill, hotkey,
    wait_visible, is_enabled, each, read_clipboard,
    log, now, runner, window,
)


# Both apps registered; prelaunch=False so we open/close them per state.
APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}


# Notepad controls (capture with `python inspector.py`).
EDITOR    = "0.0.0"
FILE_MENU = "0.2.0.0.0"

# Calculator controls (Calc has stable UIA Names — name:role works).
CALC_PLUS    = "Plus:ButtonControl"
CALC_EQUALS  = "Equals:ButtonControl"
CALC_CLEAR   = "Clear:ButtonControl"
CALC_DIGITS  = {str(i): f"{n}:ButtonControl" for i, n in enumerate([
    "Zero", "One", "Two", "Three", "Four",
    "Five", "Six", "Seven", "Eight", "Nine",
])}


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
    """Swap to Calculator, audit the keypad, compute 47 + 32, log it."""
    window.open("calc")
    if not wait_visible(window.calc, CALC_PLUS, timeout=15):
        log("results", "calc_init_failed", "")
        return None, data

    # `each` runs a verb across many ids as one atomic block: if a
    # popup interrupts mid-sequence, the popup is dismissed and the
    # whole loop restarts from id 0. Use it whenever a partial run
    # would corrupt state — here, to confirm every digit button is
    # enabled before we trust the keypad. Bail out if any are missing
    # rather than producing a wrong result.
    if not all(each(is_enabled, window.calc, list(CALC_DIGITS.values()))):
        log("results", "calc_keypad_unhealthy", "")
        return None, data

    click_when_enabled(window.calc, CALC_CLEAR, timeout=5)
    for d in "47":
        click(window.calc, CALC_DIGITS[d])
    click(window.calc, CALC_PLUS)
    for d in "32":
        click(window.calc, CALC_DIGITS[d])
    click(window.calc, CALC_EQUALS)

    hotkey(window.calc, "ctrl", "c")
    log("results", "calc_result", read_clipboard().strip())
    window.close("calc")
    return None, data


STATES = {"notepad": state_notepad, "calc": state_calc}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="notepad", prelaunch=False)
