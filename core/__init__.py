"""Public API.

Everything a user needs is here. Import what you use:

    from core import click, fill, log, runner, window

Verbs all take the window as the first argument (except `type`, which
sends keys to whatever currently has focus). Live windows live on the
`window` module — `window.notepad`, `window.open("calc")`,
`window.close("calc")`, `window.get("notepad")`.
"""
from core import runner, window
from core.window import Target
from core.verbs import (
    # clicks
    click,
    double_click,
    right_click,
    click_when_enabled,
    click_after,
    move,
    hold_and_drag,
    # coord-based variants (raw screen x,y — for Playwright via web_coords)
    click_at,
    move_at,
    hold_and_drag_at,
    web_coords,
    # text input
    fill,
    type,
    key,
    hotkey,
    # checks / waits
    is_visible,
    is_enabled,
    is_color,
    is_color_area,
    is_checked,
    check_color,
    wait_visible,
    wait_enabled,
    wait_gone,
    set_checkbox,
    # reads
    read_info,
    # popup-dismiss control (window lifecycle lives on core.window)
    no_dismiss,
    # orchestrations
    screenshot,
    # batch
    each,
    sequence,
    popup,
    # misc
    now,
    wait,
    log,
    read_clipboard,
    log_csv,
)


__all__ = [
    "click", "double_click", "right_click", "click_when_enabled", "click_after", "move", "hold_and_drag",
    "click_at", "move_at", "hold_and_drag_at", "web_coords",
    "fill", "type", "key", "hotkey",
    "is_visible", "is_enabled", "is_color", "is_color_area", "is_checked",
    "check_color",
    "wait_visible", "wait_enabled", "wait_gone",
    "set_checkbox",
    "read_info",
    "no_dismiss",
    "screenshot",
    "each", "sequence", "popup",
    "now", "wait", "log", "read_clipboard", "log_csv",
    "runner", "window", "Target",
]
