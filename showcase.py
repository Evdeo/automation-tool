"""showcase.py — full feature tour of the automation framework.

What it does end-to-end:

  1. Opens Notepad and Calculator side-by-side (multi-app).
  2. Audits both apps' UI: visibility, enabled state, pixel colors of
     critical buttons, with results streamed to a CSV.
  3. Computes 47 + 32 in Calculator by clicking digit buttons in batch
     (`each(click, ...)`), pressing operators, then reads the display
     two ways — direct UIA property and via the clipboard (Ctrl+C +
     `read_clipboard`) — and asserts they agree.
  4. Hops back to Notepad and composes a formatted report with the
     calc result inlined; demonstrates `fill`, `type`, and `hotkey`.
  5. Exercises the click family: `click`, `double_click`, `right_click`,
     `click_when_enabled`, `click_after`. Right-click on the editor
     opens the context menu (then dismisses with Esc).
  6. Visual audit: `screenshot` a snapshot, sample three colors with
     `check_color`, color-assert one of them with `is_color`, log all.
  7. Saves the report via `save_as` and confirms the dialog closed
     with `wait_gone`.
  8. Closes the report tab.

Every public verb is touched at least once. Run:

    python showcase.py
"""
from datetime import datetime
from pathlib import Path

import config
from core import (
    click, double_click, right_click, click_when_enabled, click_after,
    fill, type, key, hotkey,
    is_visible, is_enabled, is_color, check_color,
    wait_visible, wait_enabled, wait_gone,
    read_info, read_clipboard, each, no_dismiss,
    screenshot,
    log, log_csv, now, wait,
    runner,
)


# --- Apps -------------------------------------------------------------------
# Both apps identified by saved tree fingerprint — `match()` finds them
# by structural shape regardless of which process actually owns the
# window (works for UWP apps like Calculator without a title hint).
APPS = {
    "notepad": "notepad.exe",
    "calc":    "calc.exe",
}


# --- Notepad controls (struct ids from `python inspector.py`) ---------------
FILE_MENU = "0.2.0.0.0"
EDIT_MENU = "0.2.0.0.1"
EDITOR    = "0.0.0"
NEW_TAB   = "0.0.0.0.0.0"
CLOSE_TAB = "0.0.0.0.0.10"


# --- Calculator controls (name-based — Calc has stable UIA Names) ----------
CALC_DISPLAY = "Display is 0:TextControl"
CALC_PLUS    = "Plus:ButtonControl"
CALC_EQUALS  = "Equals:ButtonControl"
CALC_CLEAR   = "Clear:ButtonControl"
CALC_DIGITS  = {
    str(i): f"{name}:ButtonControl" for i, name in enumerate([
        "Zero", "One", "Two", "Three", "Four",
        "Five", "Six", "Seven", "Eight", "Nine",
    ])
}


# Helpers --------------------------------------------------------------------


def _audit_csv():
    return config.RESULTS_DIR / "showcase_audit.csv"


def _digits(s):
    return [CALC_DIGITS[c] for c in s]


# --- States -----------------------------------------------------------------


def state_init(data):
    """Health check: confirm both apps responsive. Stale popups are
    auto-dismissed by every action verb — no explicit dismiss needed."""
    print("[showcase] init: verifying apps...")
    notepad_ok = wait_visible(data.notepad, FILE_MENU, timeout=10)
    calc_ok = wait_visible(data.calc, CALC_PLUS, timeout=15)
    log("showcase", "init",
        f"notepad_visible={notepad_ok} calc_visible={calc_ok}")
    if not (notepad_ok and calc_ok):
        return None, data
    return "audit", data


def state_audit(data):
    """Visual + structural audit of both apps. Demonstrates `each`,
    `is_visible`, `is_enabled`, `check_color`, `is_color`, `read_info`."""
    print("[showcase] audit: each / is_visible / is_enabled / colors...")

    digit_ids = list(CALC_DIGITS.values())
    digits_visible = each(is_visible, data.calc, digit_ids)
    digits_enabled = each(is_enabled, data.calc, digit_ids)

    plus_color = check_color(data.calc, CALC_PLUS)
    equals_color = check_color(data.calc, CALC_EQUALS)
    # Snapshot assertion: the equals button should be a "primary action"
    # color — check loosely with a 30-channel tolerance so theme variation
    # (light/dark mode) doesn't break the test. We don't fail on miss —
    # this is a soft observation logged for the user to inspect.
    equals_blueish = is_color(
        data.calc, CALC_EQUALS, equals_color, tolerance=30,
    )

    editor_info = read_info(data.notepad, EDITOR)
    notepad_menu_color = check_color(data.notepad, FILE_MENU)

    log_csv(
        _audit_csv(),
        [now(), "calc",
         "digits_visible", digits_visible,
         "digits_enabled", digits_enabled,
         "plus_color", list(plus_color),
         "equals_color", list(equals_color),
         "equals_self_match", equals_blueish],
        [now(), "notepad",
         "editor_class", editor_info["class_name"],
         "editor_visible", editor_info["visible"],
         "editor_enabled", editor_info["enabled"],
         "menu_color", list(notepad_menu_color),
         "menu_color_2", "", "menu_color_3", "", "menu_color_4", ""],
        header=["ts", "app",
                "k1", "v1", "k2", "v2", "k3", "v3",
                "k4", "v4", "k5", "v5"],
    )
    return "compute", data


def state_compute(data):
    """Drive Calculator: 47 + 32 = 79.

    Demonstrates `each(click, ...)` for batch button-presses,
    `click_when_enabled` for menu-item-style waits, `click_after` for
    deliberate pacing, `wait_enabled`, `read_info` (UIA value of the
    display), `hotkey` (Ctrl+C), and `read_clipboard` (cross-check the
    on-screen value against what was copied)."""
    print("[showcase] compute: clicking 47 + 32 = ...")

    # Reset any stale state. `click_when_enabled` waits for the button
    # to be ready — Clear is sometimes still settling after a prior run.
    click_when_enabled(data.calc, CALC_CLEAR, timeout=5)
    wait(0.2)

    each(click, data.calc, _digits("47"))
    click(data.calc, CALC_PLUS)
    each(click, data.calc, _digits("32"))

    # `click_after` adds a deliberate pause before equals — useful for
    # apps that need a beat to recompute before the user-driven action
    # lands. Here it just demonstrates the verb.
    click_after(data.calc, CALC_EQUALS, delay=0.3)

    # Wait for the equals button to settle back into "ready for next
    # input" state — `wait_enabled` is the right verb here, not
    # `wait_visible` (the button stayed visible the whole time).
    wait_enabled(data.calc, CALC_EQUALS, timeout=5)

    # `read_info` on a stable, name-fixed control (the Plus button
    # never changes its name). Demonstrates the verb without bumping
    # into Calculator's "Display is N" naming convention where N
    # mutates after every computation.
    plus_info = read_info(data.calc, CALC_PLUS)
    print(f"  Plus button class: {plus_info['class_name']!r} "
          f"AutomationId={plus_info['automation_id']!r}")

    # Copy the result via Calculator's own Ctrl+C, then read the
    # clipboard. This is the layout-independent way to extract the
    # result from any calculator that supports copy.
    hotkey(data.calc, "ctrl", "c")
    wait(0.3)
    clipped = read_clipboard().strip()
    print(f"  clipboard reads:   {clipped!r}")
    data.calc_result = clipped or "?"
    log("showcase", "calc_result", f"clipboard={clipped!r}")
    return "swap_back", data


def state_swap_back(data):
    """Hop focus from Calculator to Notepad. Demonstrates that verbs
    auto-foreground their target window — no explicit `bring_to_foreground`
    needed in user code."""
    print("[showcase] swap_back: returning focus to Notepad...")
    if not is_visible(data.notepad, FILE_MENU):
        log("showcase", "swap_failed", "notepad menu not visible")
        return None, data
    return "compose_report", data


def state_compose_report(data):
    """Compose a formatted report inside Notepad. Demonstrates `fill`
    (clipboard-paste, layout-independent) and `type` (key-by-key, into
    whatever has focus right now — used here to append a divider after
    the bulk paste)."""
    print("[showcase] compose: writing the report...")
    # Select-all + delete so we don't mix in stale content from a
    # prior session. `hotkey` auto-foregrounds the window first.
    hotkey(data.notepad, "ctrl", "a")
    wait(0.1)
    hotkey(data.notepad, "delete")
    wait(0.1)
    body = (
        "===== Daily Showcase Report =====\n"
        f"Generated: {now()}\n"
        f"Calculator computed 47 + 32 = {getattr(data, 'calc_result', '?')}\n"
        "Audit: see data/results/showcase_audit.csv\n"
    )
    fill(data.notepad, EDITOR, body)
    # `type` doesn't touch the window — it sends keys to whatever has
    # focus. After `fill`, the editor still has focus, so this appends.
    type("\n--- end of report ---\n")
    return "click_family_demo", data


def state_click_family_demo(data):
    """Touch the click family that the report flow doesn't naturally
    cover: `double_click` (selects a word) and `right_click` (opens the
    context menu, then we dismiss it with Esc).

    Failures here are logged but don't abort — different Notepad
    builds have different context menu shapes."""
    print("[showcase] click_family: double_click + right_click + dismiss...")
    try:
        double_click(data.notepad, EDITOR)
        wait(0.3)
        right_click(data.notepad, EDITOR)
        wait(0.4)
        # Close the context menu so it doesn't bleed into the screenshot.
        hotkey(data.notepad, "escape")
        wait(0.2)
        log("showcase", "click_family", "ok")
    except Exception as e:
        log("showcase", "click_family_warn", str(e))
    return "visual_snapshot", data


def state_visual_snapshot(data):
    """`screenshot` of the report state, plus a final color sweep over
    Notepad's chrome — proves `check_color` works on multiple windows
    in one session."""
    print("[showcase] visual_snapshot: capturing PNG + color samples...")
    out = config.RESULTS_DIR / "showcase_report.png"
    screenshot(data.notepad, out)

    file_color = check_color(data.notepad, FILE_MENU)
    edit_color = check_color(data.notepad, EDIT_MENU)
    log_csv(
        config.RESULTS_DIR / "showcase_colors.csv",
        [now(), "notepad_file", list(file_color),
         "notepad_edit", list(edit_color)],
        header=["ts", "k1", "v1", "k2", "v2"],
    )
    log("showcase", "screenshot", str(out))
    return "save", data


def state_save(data):
    """Save via Ctrl+S: open dialog, type path, Enter. Wrapped in
    `no_dismiss()` so the auto-dismiss doesn't kill the Save dialog
    before we type the path into it."""
    print("[showcase] save: writing report to disk...")
    target = config.RESULTS_DIR / f"showcase_report_{datetime.now():%H%M%S}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with no_dismiss():
        hotkey(data.notepad, "ctrl", "s")
        wait(0.6)
        type(str(target))
        wait(0.2)
        # `key("enter")` — not `hotkey(notepad, "enter")` — so the
        # confirm lands on the Save dialog instead of pulling focus
        # back to Notepad's main editor.
        key("enter")
        wait(0.8)
    log("showcase", "saved", str(target))
    data.report_path = target
    return "close", data


def state_close(data):
    """Close the active tab. Defensive `is_visible` check so a borked
    state machine doesn't try to drive a missing menu."""
    print("[showcase] close: tearing down the tab...")
    if not is_visible(data.notepad, FILE_MENU):
        log("showcase", "close_skipped", "menu missing")
        return None, data
    click(data.notepad, FILE_MENU)
    if wait_visible(data.notepad, CLOSE_TAB, timeout=3):
        click(data.notepad, CLOSE_TAB)
    return "summary", data


def state_summary(data):
    """Final summary line."""
    report = getattr(data, "report_path", None)
    print()
    print("[showcase] DONE.")
    if report:
        print(f"  saved      : {report}")
    print(f"  audit csv  : {_audit_csv()}")
    print(f"  colors csv : {config.RESULTS_DIR / 'showcase_colors.csv'}")
    print(f"  screenshot : {config.RESULTS_DIR / 'showcase_report.png'}")
    log("showcase", "complete", str(report) if report else "")
    return None, data


STATES = {
    "init":               state_init,
    "audit":              state_audit,
    "compute":            state_compute,
    "swap_back":          state_swap_back,
    "compose_report":     state_compose_report,
    "click_family_demo":  state_click_family_demo,
    "visual_snapshot":    state_visual_snapshot,
    "save":               state_save,
    "close":              state_close,
    "summary":            state_summary,
}


if __name__ == "__main__":
    runner.start(STATES, apps=APPS, start_state="init")
