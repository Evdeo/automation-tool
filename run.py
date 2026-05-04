"""Minimal example: drive Notepad, then swap to Calculator.

Demonstrates the everyday surface in two states:
  - state machine wiring via `runner.start`
  - swapping apps that can't coexist with `window.open` / `window.close`
    (`prelaunch=False` so the runner doesn't open both up front)
  - `fill`, `click`, `click_when_enabled`, `hotkey`, `each`, `wait_visible`
  - `log` + `now` for a run-scoped audit trail

For the full feature tour (popups, screenshots, save dialogs, color
audits, etc.) see showcase.py. Run:  python run.py
"""
from core import (
    click, click_when_enabled, fill, hotkey,
    wait_visible, each, read_clipboard,
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
    """Swap to Calculator, compute 47 + 32, log the clipboard result."""
    window.open("calc")
    if not wait_visible(window.calc, CALC_PLUS, timeout=15):
        log("results", "calc_init_failed", "")
        return None, data

    digits = lambda s: [CALC_DIGITS[c] for c in s]
    click_when_enabled(window.calc, CALC_CLEAR, timeout=5)
    each(click, window.calc, digits("47"))
    click(window.calc, CALC_PLUS)
    each(click, window.calc, digits("32"))
    click(window.calc, CALC_EQUALS)

    hotkey(window.calc, "ctrl", "c")
    log("results", "calc_result", read_clipboard().strip())
    window.close("calc")
    return None, data


STATES = {"notepad": state_notepad, "calc": state_calc}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="notepad", prelaunch=False)
