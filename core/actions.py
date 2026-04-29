import time

import pyautogui

import config
from core import db, tree


pyautogui.FAILSAFE = False

_logged_diffs = {}


def _center(element):
    r = element.BoundingRectangle
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return None
    return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


def _check_drift(window, walked):
    key = tree.snapshot_key(window)
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
    while True:
        walked = tree.walk_live(window)
        _check_drift(window, walked)
        element = tree.find(walked, tree_id)
        if element is not None:
            center = _center(element)
            if center is not None:
                return element, center
        db.log("missing", tree.snapshot_key(window), tree_id)
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)


def press(window, tree_id):
    _, (x, y) = _resolve(window, tree_id)
    pyautogui.click(x, y)
    db.log("press", tree_id, x, y)
    return True


def double_press(window, tree_id):
    _, (x, y) = _resolve(window, tree_id)
    pyautogui.doubleClick(x, y)
    db.log("double_press", tree_id, x, y)
    return True


def press_when_active(window, tree_id, timeout=30):
    deadline = time.time() + timeout
    while True:
        element, (x, y) = _resolve(window, tree_id)
        if element.IsEnabled:
            pyautogui.click(x, y)
            db.log("press_when_active", tree_id, x, y)
            return True
        if time.time() > deadline:
            db.log("press_when_active", tree_id, x, y, "timeout_waiting_enabled")
            deadline = time.time() + timeout
        time.sleep(config.ACTIVE_POLL_SEC)


def press_after_delay(window, tree_id, delay):
    time.sleep(delay)
    return press(window, tree_id)


def get_color(window, tree_id, x_offset=0, y_offset=0):
    _, (x, y) = _resolve(window, tree_id)
    px, py = x + x_offset, y + y_offset
    color = pyautogui.pixel(px, py)
    db.log("color", tree_id, px, py, list(color))
    return tuple(color)
