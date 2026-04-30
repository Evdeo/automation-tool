# When this file is invoked directly (`python core/inspector.py`) it isn't
# imported as part of the `core` package, so `from core import tree` would
# fail. Detect that case and prepend the project root to sys.path so the
# package-relative imports below resolve. Has no effect when imported
# normally (as `core.inspector`) or via the project-root entrypoint
# (`inspector.py`).
if __name__ == "__main__" and __package__ is None:
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import queue
import threading
import time
import traceback

import pyautogui
import uiautomation as auto
from pynput import mouse

from core import tree


# Decoded HRESULTs we surface explicitly so the message tells the
# user *which* COM precondition tripped, not just the bare number.
_HRESULTS = {
    -2147417843: "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",  # 0x8001010D
    -2147418111: "RPC_E_CALL_REJECTED",                 # 0x80010001
    -2147417835: "RPC_E_SERVERCALL_RETRYLATER",         # 0x8001010A
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",            # 0x800706BA
    -2147221008: "CO_E_NOTINITIALIZED",                 # 0x800401F0
}


def _hresult_name(exc):
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return _HRESULTS.get(args[0])
    return None


# Mouse-hook callbacks fire on pynput's listener thread *while Windows is
# dispatching input synchronously*. Any COM call made from that state
# fails with RPC_E_CANTCALLOUT_ININPUTSYNCCALL (HRESULT -2147417843,
# "An outgoing call cannot be made since the application is dispatching
# an input-synchronous call") — most visible when clicking menus, which
# trigger input-sync SendMessage on the target.
#
# Spawning a fresh thread per click is *not* enough. comtypes defaults
# CoInitializeEx to STA (COINIT_APARTMENTTHREADED), and uiautomation's
# `_AutomationClient` is a lazy singleton: the IUIAutomation proxy is
# created on whichever thread first uses it and is bound to that
# thread's STA. A short-lived per-click worker creates the singleton
# in its own apartment, then dies — leaving the singleton bound to a
# dead apartment. Subsequent workers' calls into it cross apartments
# and re-trigger the input-sync error.
#
# Fix: one persistent worker thread that owns the COM apartment for
# the program's lifetime. The mouse callback only enqueues coords;
# the worker pulls them and runs all UIA calls on its own thread.
_clicks: "queue.Queue[tuple[int, int]]" = queue.Queue()


def _top_window(ctrl):
    root = auto.GetRootControl()
    cur = ctrl
    while True:
        parent = cur.GetParentControl()
        if parent is None:
            return cur
        try:
            if parent.NativeWindowHandle == root.NativeWindowHandle:
                return cur
        except Exception:
            pass
        cur = parent


def _runtime_id(ctrl):
    try:
        rid = ctrl.GetRuntimeId()
        return tuple(rid) if rid else None
    except Exception:
        return None


def _same_element(a, b, a_rid=None):
    """Best-effort comparison of two Control wrappers. uiautomation
    Control has no __eq__, so we layer three strategies:

    1. RuntimeId equality — usually unique across the desktop and
       cheap to compute.
    2. IUIAutomation::CompareElements via ControlsAreSame — the
       canonical UIA comparison.
    3. Fall back to bounding-rect + role equality.

    Any one strategy returning a positive match is enough; weird
    providers (WPF-in-WinForms element host, custom UIA bridges)
    misbehave with one approach but not all three.
    """
    if a_rid is None:
        a_rid = _runtime_id(a)
    b_rid = _runtime_id(b)
    if a_rid and b_rid and a_rid == b_rid:
        return True
    try:
        if auto.ControlsAreSame(a, b):
            return True
    except Exception:
        pass
    try:
        ra, rb = a.BoundingRectangle, b.BoundingRectangle
        if (a.ControlType == b.ControlType
                and ra.left == rb.left and ra.top == rb.top
                and ra.right == rb.right and ra.bottom == rb.bottom
                and (ra.right - ra.left) > 0):
            return True
    except Exception:
        pass
    return False


def _path_to(element, win):
    """Returns (name_path, struct_id) for `element` within `win`'s
    tree.

    Strategy: walk `win` top-down with the same logic as
    `tree.walk_live` (so the resulting struct_id is identical to
    what the snapshot recorded), and at each level pick the child
    that *is* — or contains — the clicked element. This sidesteps
    every flaky bottom-up case we hit before:

      * `cur.GetParentControl()` returning a parent whose
        `GetChildren()` doesn't list `cur`,
      * `auto.ControlsAreSame` returning False for elements that
        really are the same (broken WPF UIA providers),
      * RawViewWalker not exposing the element returned by
        `ElementFromPoint` as a sibling.

    The descent prefers an exact element match; when no child is
    *the* element, it follows the child whose bounding rectangle
    contains the click point. The deepest reachable match wins —
    so two distinct sibling controls always end up at distinct
    struct_ids, even when their parents lie about who their
    children are.
    """
    target_rid = _runtime_id(element)
    target_rect = element.BoundingRectangle
    px = (target_rect.left + target_rect.right) // 2
    py = (target_rect.top + target_rect.bottom) // 2

    chain = [(win, 0)]
    cur = win
    while True:
        children = cur.GetChildren()
        if not children:
            break
        # Prefer an exact element match.
        match_idx = -1
        for i, child in enumerate(children):
            if _same_element(child, element, a_rid=None):
                match_idx = i
                break
        if match_idx >= 0:
            chain.append((children[match_idx], match_idx))
            cur = children[match_idx]
            # Element found — but maybe it has children too. UIA
            # leaves are usually controls without children, so we
            # stop. (If the matched child *also* contains the
            # click, descending further would just find the same
            # element again.)
            if _runtime_id(cur) == target_rid:
                break
            continue
        # No exact match at this level. Descend by bbox containment.
        contains_idx = -1
        contains_area = None
        for i, child in enumerate(children):
            try:
                r = child.BoundingRectangle
            except Exception:
                continue
            if r.left <= px <= r.right and r.top <= py <= r.bottom:
                area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if contains_area is None or area < contains_area:
                    contains_idx = i
                    contains_area = area
        if contains_idx < 0:
            break
        chain.append((children[contains_idx], contains_idx))
        cur = children[contains_idx]

    name_path = "/".join(tree._segment(c, i) for c, i in chain)
    struct_id = ".".join(str(i) for _, i in chain)
    return name_path, struct_id


def _step(label, fn, *args, **kwargs):
    """Run `fn(*args)` while logging which step is in flight + how
    long it took. On exception, the surrounding try/except prints
    the label so we know *which* COM call tripped without having
    to read line numbers off a traceback."""
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        dt = (time.perf_counter() - t0) * 1000
        print(f"  [step] {label}: {dt:.1f} ms")


def _inspect(x, y, click_id):
    th = threading.current_thread().name
    print(f"[click #{click_id}] worker={th} coords=({x},{y}) "
          f"queue_depth={_clicks.qsize()}")

    ctrl = _step("ControlFromPoint", auto.ControlFromPoint, x, y)
    if ctrl is None:
        print(f"[{x},{y}] no element under cursor")
        return

    win = _step("_top_window", _top_window, ctrl)
    _, created = _step("ensure_snapshot", tree.ensure_snapshot, win)
    if created:
        print(f"** baseline captured: {tree.snapshot_path(win)}")

    tid, struct_id = _step("_path_to", _path_to, ctrl, win)
    rect = _step("BoundingRectangle", lambda: ctrl.BoundingRectangle)
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    try:
        color = pyautogui.pixel(cx, cy)
    except Exception:
        color = None
    print("-" * 60)
    print(f"window    : {tree.snapshot_key(win)}")
    print(f"struct_id : {struct_id}")
    print(f"tree_id   : {tid}")
    print(f"name      : {tree._name(ctrl)}")
    print(f"role      : {tree._role(ctrl)}")
    print(f"bbox      : ({rect.left},{rect.top}) -> ({rect.right},{rect.bottom})")
    print(f"center    : ({cx},{cy})")
    print(f"color     : {color}")
    print(f"enabled   : {ctrl.IsEnabled}")


# HRESULTs that mean "the target server is busy / dispatching input;
# try again in a moment". UIAutomation calls into the WPF app cross-
# process; while the app's UI thread is in the middle of dispatching
# its own click (e.g., opening a menu), it can't service incoming
# COM calls and the request bounces back. These are transient — a
# short backoff usually clears them.
_TRANSIENT_HRESULTS = {
    "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",
    "RPC_E_CALL_REJECTED",
    "RPC_E_SERVERCALL_RETRYLATER",
}


def _inspect_with_retry(x, y, click_id, max_attempts=8):
    delay = 0.05  # 50ms, doubling up to ~3.2s total over 8 attempts
    for attempt in range(1, max_attempts + 1):
        try:
            _inspect(x, y, click_id)
            if attempt > 1:
                print(f"[click #{click_id}] succeeded on attempt {attempt}")
            return
        except Exception as e:
            hres = _hresult_name(e)
            transient = hres in _TRANSIENT_HRESULTS
            tag = f" [{hres}]" if hres else ""
            if not transient or attempt == max_attempts:
                print(f"[click #{click_id}] inspector error{tag} "
                      f"(attempt {attempt}/{max_attempts}): "
                      f"{type(e).__name__}: {e}")
                traceback.print_exc()
                return
            print(f"[click #{click_id}] transient{tag} on attempt "
                  f"{attempt}; backing off {delay*1000:.0f} ms")
            time.sleep(delay)
            delay *= 2


def _worker():
    # Single long-lived worker. Initializes COM once for this thread
    # and keeps the apartment alive for the program's lifetime, so
    # uiautomation's IUIAutomation singleton stays valid across all
    # clicks. See module-level comment.
    th = threading.current_thread().name
    print(f"[worker] starting on {th}")
    with auto.UIAutomationInitializerInThread(debug=False):
        # Force singleton creation on THIS thread (not on whatever
        # thread happens to make the first UIA call later).
        auto.GetRootControl()
        print(f"[worker] UIA initialized on {th}, ready")
        click_id = 0
        while True:
            item = _clicks.get()
            if item is None:
                return
            click_id += 1
            x, y, t_enq = item
            latency = (time.perf_counter() - t_enq) * 1000
            print(f"[click #{click_id}] dequeued after {latency:.1f} ms")
            _inspect_with_retry(x, y, click_id)


def _on_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.left:
        return
    _clicks.put((x, y, time.perf_counter()))


def run():
    print("Inspector running. Left-click any element. Ctrl+C to quit.")
    print("Baselines auto-saved on first click in each window.")
    threading.Thread(target=_worker, daemon=True).start()
    with mouse.Listener(on_click=_on_click) as listener:
        listener.join()


if __name__ == "__main__":
    run()
