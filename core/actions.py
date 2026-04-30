import ctypes
import time

import pyautogui

import config
from core import apps, db, tree


pyautogui.FAILSAFE = False

_logged_diffs = {}


# --- Win32 SendInput-based cursor click ---------------------------------------
# WinUI / UWP apps (Win11 Notepad, Settings, etc.) silently ignore the legacy
# `mouse_event` API that pyautogui and uiautomation both use — the cursor
# moves, no error is raised, but the click never registers in the target's
# input pipeline. Only `SendInput` produces a click WinUI accepts. Move +
# down + up are issued as separate INPUT records, with brief sleeps so the
# target gets a chance to register hover before the down event.

_user32 = ctypes.windll.user32

_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_ABSOLUTE = 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


def _make_mouse_input(flags, x=0, y=0):
    inp = _INPUT()
    inp.type = _INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(x, y, 0, flags, 0, None)
    return inp


def _send_inputs(*inputs):
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    _user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))


def _abs_coords(x, y):
    sw = _user32.GetSystemMetrics(0)
    sh = _user32.GetSystemMetrics(1)
    # SendInput absolute coordinates are 0..65535 mapped onto the primary
    # display. Use sw-1/sh-1 to keep the rightmost/bottommost pixel reachable.
    return int(x * 65535 / max(sw - 1, 1)), int(y * 65535 / max(sh - 1, 1))


def _cursor_click(x, y, settle=0.15, hold=0.05):
    """Move the cursor to (x, y) and issue a real left click via SendInput.

    `settle` is the wait between move and mouse-down so the target sees a
    hover; `hold` is the wait between down and up so it counts as a click,
    not a flicker."""
    ax, ay = _abs_coords(x, y)
    _send_inputs(_make_mouse_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, ax, ay))
    time.sleep(settle)
    _send_inputs(_make_mouse_input(_MOUSEEVENTF_LEFTDOWN))
    time.sleep(hold)
    _send_inputs(_make_mouse_input(_MOUSEEVENTF_LEFTUP))


def _cursor_double_click(x, y, settle=0.15, hold=0.05, gap=0.08):
    ax, ay = _abs_coords(x, y)
    _send_inputs(_make_mouse_input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, ax, ay))
    time.sleep(settle)
    for _ in range(2):
        _send_inputs(_make_mouse_input(_MOUSEEVENTF_LEFTDOWN))
        time.sleep(hold)
        _send_inputs(_make_mouse_input(_MOUSEEVENTF_LEFTUP))
        time.sleep(gap)


# --- Tree resolution ----------------------------------------------------------


def _center(element):
    r = element.BoundingRectangle
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return None
    return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


def _check_drift(window, walked, snap=None):
    key = tree.snapshot_key(window)
    if snap is None:
        snap = tree.load_snapshot(window)
    if snap is None:
        if _logged_diffs.get(key) != "no_baseline":
            _logged_diffs[key] = "no_baseline"
            db.log("drift", key, 0, 0, "no_baseline_capture_with_inspector", [])
        return
    diff = tree.compute_diff(snap, walked)
    if not diff["added"] and not diff["removed"]:
        return
    sig = (frozenset(diff["added"]), frozenset(diff["removed"]))
    if _logged_diffs.get(key) == sig:
        return
    _logged_diffs[key] = sig
    db.log(
        "drift",
        key,
        len(diff["added"]),
        len(diff["removed"]),
        diff["added"][:10],
        diff["removed"][:10],
    )


def _resolve(window, tree_id):
    # Auto-foreground: every action ensures its window is on top before
    # clicking. Cheap (early-return when already foreground); removes
    # the burden from user code so state functions don't have to call
    # apps.bring_to_foreground manually before each press.
    apps.bring_to_foreground(window)

    # Snapshot is loaded once outside the retry loop — drift detection
    # and self-healing both consume it. If no snapshot exists yet (fresh
    # checkout, never ran inspector), the first successful resolve below
    # bootstraps one from the live tree so subsequent resolves can heal
    # against drift.
    snap = tree.load_snapshot(window)
    deadline = time.time() + config.RESOLVE_TIMEOUT_SEC
    while True:
        walked = tree.walk_live(window)
        _check_drift(window, walked, snap)
        element, healed = tree.find_or_heal(walked, tree_id, snap)
        if element is not None:
            center = _center(element)
            if center is not None:
                if snap is None:
                    tree.save_snapshot(window, walked)
                    snap = tree.to_serializable(walked)
                if healed:
                    live_struct = next(
                        (n.get("struct_id") for n in walked
                         if n["ctrl"] is element),
                        None,
                    )
                    db.log(
                        "healed",
                        tree.snapshot_key(window),
                        tree_id,
                        live_struct,
                    )
                return element, center
        db.log("missing", tree.snapshot_key(window), tree_id)
        if time.time() > deadline:
            key = tree.snapshot_key(window)
            snap_path = tree.snapshot_path(window)
            stem = tree._process_stem(window) or "<app>"
            raise TimeoutError(
                f"could not resolve {tree_id!r} within {config.RESOLVE_TIMEOUT_SEC}s "
                f"(window={key}, snapshot={snap_path}). "
                f"Run `python inspector.py {stem}.exe`, click the target, "
                f"and update the constant in run.py."
            )
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)


# --- Public actions -----------------------------------------------------------


def press(window, tree_id):
    _, (x, y) = _resolve(window, tree_id)
    _cursor_click(x, y)
    db.log("press", tree_id, x, y)
    return True


def double_press(window, tree_id):
    _, (x, y) = _resolve(window, tree_id)
    _cursor_double_click(x, y)
    db.log("double_press", tree_id, x, y)
    return True


def press_when_active(window, tree_id, timeout=30):
    deadline = time.time() + timeout
    while True:
        element, (x, y) = _resolve(window, tree_id)
        if element.IsEnabled:
            _cursor_click(x, y)
            db.log("press_when_active", tree_id, x, y)
            return True
        if time.time() > deadline:
            db.log("press_when_active", tree_id, x, y, "timeout_waiting_enabled")
            deadline = time.time() + timeout
        time.sleep(config.ACTIVE_POLL_SEC)


def check_active(window, tree_id, timeout=0):
    """Return True if `tree_id` resolves to an enabled, on-screen element
    within `timeout` seconds; False otherwise (not present, off-screen, or
    disabled).  Non-throwing — safe to use in `if` statements.

    Default `timeout=0` is an immediate one-walk check; pass a positive
    value to wait for the element to appear (e.g. for a button that
    enables once a background task finishes)."""
    deadline = time.time() + timeout
    while True:
        walked = tree.walk_live(window)
        element = tree.find(walked, tree_id)
        if element is not None and _center(element) is not None:
            try:
                if element.IsEnabled:
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(config.ACTIVE_POLL_SEC)


def is_present(window, tree_id, timeout=0):
    """Return True if `tree_id` resolves to a visible element (regardless of
    enabled state) within `timeout` seconds.  Useful for branching on
    "did this dialog/menu open?" without caring whether its primary
    button is clickable yet."""
    deadline = time.time() + timeout
    while True:
        walked = tree.walk_live(window)
        element = tree.find(walked, tree_id)
        if element is not None and _center(element) is not None:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(config.ACTIVE_POLL_SEC)


def wait_until_absent(window, tree_id, timeout=10.0):
    """Inverse of `is_present`: wait until `tree_id` no longer resolves to a
    visible element, up to `timeout` seconds.  Returns True once it's gone,
    False on timeout.  Use this to wait for dialogs / menus to close
    without resorting to a magic `time.sleep`."""
    deadline = time.time() + timeout
    while True:
        walked = tree.walk_live(window)
        element = tree.find(walked, tree_id)
        if element is None or _center(element) is None:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(config.ACTIVE_POLL_SEC)


def press_path(window, *tree_ids):
    # Cascade clicks for menu/submenu chains (e.g. View → Zoom → Zoom in).
    # Each step's element only enters the live tree after the previous click
    # opens its parent; _resolve's retry loop bridges that gap.
    for tid in tree_ids:
        press(window, tid)
    return True


def type_text(text, interval=0.02):
    pyautogui.write(text, interval=interval)
    db.log("type", text, len(text))
    return True


def write_text(window, tree_id, text, settle=0.1):
    import pyperclip
    _, (x, y) = _resolve(window, tree_id)
    _cursor_click(x, y)
    if settle > 0:
        time.sleep(settle)
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    db.log("write_text", tree_id, x, y, text, len(text))
    return True


def get_color(window, tree_id, x_offset=0, y_offset=0):
    _, (x, y) = _resolve(window, tree_id)
    px, py = x + x_offset, y + y_offset
    color = pyautogui.pixel(px, py)
    db.log("color", tree_id, px, py, list(color))
    return tuple(color)
