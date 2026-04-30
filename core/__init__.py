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
    click_when_active,
    # text input
    fill,
    keys,
    hotkey,
    # checks / waits
    has,
    check_enabled,
    wait_gone,
    check_color,
    # popups / dialogs
    popup,
    dismiss_popups,
    # orchestrations
    save_as,
    screenshot,
    close,
    # misc
    now,
    wait,
    log,
)


__all__ = [
    "click", "double_click", "click_when_active",
    "fill", "keys", "hotkey",
    "has", "check_enabled", "wait_gone", "check_color",
    "popup", "dismiss_popups",
    "save_as", "screenshot", "close",
    "now", "wait", "log",
    "runner",
]
