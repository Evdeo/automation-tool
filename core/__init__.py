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
    type,
    hotkey,
    # checks / waits
    check_visible,
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
    "fill", "type", "hotkey",
    "check_visible", "check_enabled", "wait_gone", "check_color",
    "popup", "dismiss_popups",
    "save_as", "screenshot", "close",
    "now", "wait", "log",
    "runner",
]
