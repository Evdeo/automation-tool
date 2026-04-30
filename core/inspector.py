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
from pathlib import Path

import uiautomation as auto
from pynput import mouse

from core import tree


# Per-session log of the paste-ready blocks. Truncated on startup
# so the file only contains the current run -- the user grabs the
# struct_ids they want at the end without scrolling past stale
# state. Errors / baseline notes are deliberately NOT written to
# this file; only the lines you'd actually paste into run.py.
_LOG_PATH = Path("data/inspector.txt")
_log_file = None


def _emit(line):
    """Print to stdout AND append to the session log."""
    print(line)
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


# Decoded HRESULTs we surface explicitly so the message tells the
# user *which* COM precondition tripped, not just the bare number.
_HRESULTS = {
    -2147417843: "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",  # 0x8001010D
    -2147418111: "RPC_E_CALL_REJECTED",                 # 0x80010001
    -2147417835: "RPC_E_SERVERCALL_RETRYLATER",         # 0x8001010A
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",            # 0x800706BA
    -2147221008: "CO_E_NOTINITIALIZED",                 # 0x800401F0
    -2147220991: "EVENT_E_INTERNALEXCEPTION",           # 0x80040201
    -2146233083: "COR_E_TIMEOUT",                       # 0x80131505
    -2147220984: "UIA_E_ELEMENTNOTAVAILABLE",           # 0x80040208
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


def _path_to(win, x, y):
    """Walk `win` top-down to the deepest descendant whose bounding
    rectangle contains the click point (x, y), using only one
    BoundingRectangle COM call per child per level.

    Returns (leaf_ctrl, name_path, struct_id). The struct_id is
    identical to what `tree.walk_live` records in the snapshot,
    because the descent uses the same enumeration order. `leaf_ctrl`
    is the same control the inspection should report on — using it
    instead of the original `ControlFromPoint` result avoids a
    second flaky cross-process query for properties like
    `BoundingRectangle`, which sometimes fails on the leaf returned
    by `ElementFromPoint` while succeeding on the bbox-descended
    leaf (they're often the same element accessed via different
    paths through the UIA tree).

    Element-comparison strategies (RuntimeId, ControlsAreSame) are
    deliberately *not* used here — they cost 2-3 extra COM calls
    per child per level, blowing _path_to runtime out to seconds on
    wide trees. Bbox-containment with smallest-area tie-break
    converges on the same leaf that `ElementFromPoint` would have
    returned, much faster.
    """
    chain = [(win, 0)]
    cur = win
    # Guard against pathological trees where a child's bbox keeps
    # containing the click point at every level forever (proxy
    # cycles, faulty providers). Real UI trees are well under 50
    # deep; 100 is a generous ceiling that still bounds runtime.
    for _ in range(100):
        try:
            children = cur.GetChildren()
        except Exception:
            break
        if not children:
            break
        best_idx = -1
        best_area = None
        for i, child in enumerate(children):
            try:
                r = child.BoundingRectangle
            except Exception:
                continue
            if r.left <= x <= r.right and r.top <= y <= r.bottom:
                area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if best_area is None or area < best_area:
                    best_idx = i
                    best_area = area
        if best_idx < 0:
            break
        chain.append((children[best_idx], best_idx))
        cur = children[best_idx]
    name_path = "/".join(tree._segment(c, i) for c, i in chain)
    struct_id = ".".join(str(i) for _, i in chain)
    return cur, name_path, struct_id


def _inspect(x, y):
    ctrl = auto.ControlFromPoint(x, y)
    if ctrl is None:
        print(f"[{x},{y}] no element under cursor")
        return

    win = _top_window(ctrl)
    _, created = tree.ensure_snapshot(win)
    if created:
        print(f"** baseline captured: {tree.snapshot_path(win)}")

    _, _, struct_id = _path_to(win, x, y)
    # Three paste-ready strings:
    #  - window: live title -- usable as-is when stable, or a substring
    #    works since apps.get_window() does substring matching.
    #  - process: executable stem, rock-solid across iterations when
    #    the live title drifts (run counters, document filenames,
    #    instrument serials). Use as the substring fallback.
    #  - struct_id: pasted into run.py as `scan = "0.14.2"` etc.
    _emit("-" * 60)
    _emit(f'window    : "{tree._name(win)}"')
    _emit(f'process   : "{tree._process_stem(win)}"')
    _emit(f'struct_id : "{struct_id}"')


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
    # The target's UIA provider was mid-update when we queried —
    # firing property-change events that errored out. Common when
    # a menu is animating open. Retry usually clears it.
    "EVENT_E_INTERNALEXCEPTION",
    # Cross-process UIA query exceeded its timeout because the
    # target's UI thread was busy. Retrying after a short wait
    # almost always works.
    "COR_E_TIMEOUT",
    # The element vanished between queries (popup closed). Retry
    # picks up whatever's now under the cursor.
    "UIA_E_ELEMENTNOTAVAILABLE",
}


def _inspect_with_retry(x, y, max_attempts=8):
    """Retry transient cross-process COM failures silently with
    exponential backoff (50ms doubling, capped at ~3.2s total).
    Print only on the final failure — successful retries are
    indistinguishable from a clean first attempt to the user."""
    delay = 0.05
    for attempt in range(1, max_attempts + 1):
        try:
            _inspect(x, y)
            return
        except Exception as e:
            hres = _hresult_name(e)
            if hres not in _TRANSIENT_HRESULTS or attempt == max_attempts:
                tag = f" [{hres}]" if hres else ""
                print(f"inspector error{tag}: {type(e).__name__}: {e}")
                if hres is None:
                    traceback.print_exc()
                return
            time.sleep(delay)
            delay *= 2


def _worker():
    # Single long-lived worker. Initializes COM once for this thread
    # and keeps the apartment alive for the program's lifetime, so
    # uiautomation's IUIAutomation singleton stays valid across all
    # clicks. See module-level comment.
    #
    # The inner try/except is the survival barrier: if any exception
    # ever escapes _inspect_with_retry (it shouldn't, but UIA
    # surprises happen), the worker keeps running so the inspector
    # stays responsive. Without this, one freak failure would kill
    # the worker silently and every subsequent click would be a
    # no-op while the listener appeared to still be running.
    with auto.UIAutomationInitializerInThread(debug=False):
        # Force singleton creation on THIS thread (not on whatever
        # thread happens to make the first UIA call later).
        auto.GetRootControl()
        while True:
            item = _clicks.get()
            if item is None:
                return
            try:
                _inspect_with_retry(*item)
            except Exception as e:
                print(f"inspector worker recovered from: "
                      f"{type(e).__name__}: {e}")


def _on_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.left:
        return
    _clicks.put((x, y))


def run():
    global _log_file
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("Inspector running. Left-click any element. Ctrl+C to quit.")
    print("Baselines auto-saved on first click in each window.")
    print(f"Session log: {_LOG_PATH}")
    with open(_LOG_PATH, "w") as f:
        _log_file = f
        threading.Thread(target=_worker, daemon=True).start()
        try:
            with mouse.Listener(on_click=_on_click) as listener:
                listener.join()
        finally:
            _log_file = None


if __name__ == "__main__":
    run()
