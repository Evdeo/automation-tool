"""Public API.

Everything a user needs is here. Import what you use:

    from core import click, fill, match, log, runner

Verbs all take the window as the first argument (except `type`, which
sends keys to whatever currently has focus).
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
    key,
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
    # window matching (replaces popup, save_as, dismiss_popups)
    match,
    no_dismiss,
    # orchestrations
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
    "fill", "type", "key", "hotkey",
    "is_visible", "is_enabled", "is_color", "check_color",
    "wait_visible", "wait_enabled", "wait_gone",
    "read_info",
    "match", "no_dismiss",
    "screenshot", "close",
    "each",
    "now", "wait", "log", "read_clipboard", "log_csv",
    "runner",
]
