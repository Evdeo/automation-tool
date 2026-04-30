"""Public API.

Everything a user needs is here. Import what you use:

    from core import click, fill, save_as, popup, log, runner

Verbs all take the window as the first argument.
"""
from core import runner
from core.verbs import (
    # clicks
    click,
    double_click,
    right_click,
    click_when_enabled,
    click_after,
    # text input
    fill,
    type,
    hotkey,
    # checks / waits
    is_visible,
    is_enabled,
    is_color,
    check_color,
    wait_visible,
    wait_enabled,
    wait_gone,
    # reads
    read_info,
    # popups / dialogs
    popup,
    dismiss_popups,
    # orchestrations
    save_as,
    screenshot,
    close,
    # batch
    each,
    # misc
    now,
    wait,
    log,
    read_clipboard,
    log_csv,
)


__all__ = [
    "click", "double_click", "right_click", "click_when_enabled", "click_after",
    "fill", "type", "hotkey",
    "is_visible", "is_enabled", "is_color", "check_color",
    "wait_visible", "wait_enabled", "wait_gone",
    "read_info",
    "popup", "dismiss_popups",
    "save_as", "screenshot", "close",
    "each",
    "now", "wait", "log", "read_clipboard", "log_csv",
    "runner",
]
