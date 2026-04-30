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


def _path_to(element):
    """Returns (name_path, struct_id) — the full slash-separated
    name+role path AND the dotted-index structural path for the
    same element. Both are computed from the same parent-chain walk.

    Sibling index uses `auto.ControlsAreSame` — which calls
    `IUIAutomation::CompareElements` — to identify the clicked
    element among `parent.GetChildren()`. Plain `==` compares Python
    object identity (uiautomation.Control has no `__eq__`) and almost
    never matches across freshly-instantiated wrappers, so two
    distinct children would otherwise both fall through to
    `idx == len(children)` and collapse onto the same path.
    `GetPreviousSiblingControl` would also work in theory, but it
    walks the RawViewWalker which on some WPF providers doesn't
    expose the element returned by `ElementFromPoint` as a sibling
    of itself — children of one parent both report no previous
    sibling and produce idx 0.
    """
    chain = []
    cur = element
    while cur is not None:
        parent = cur.GetParentControl()
        if parent is None:
            chain.append((cur, 0))
            break
        children = parent.GetChildren()
        idx = len(children)  # past-the-end if nothing matches
        for i, sib in enumerate(children):
            if auto.ControlsAreSame(sib, cur):
                idx = i
                break
        chain.append((cur, idx))
        cur = parent
    chain.reverse()
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

    tid, struct_id = _step("_path_to", _path_to, ctrl)
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
            try:
                _inspect(x, y, click_id)
            except Exception as e:
                hres = _hresult_name(e)
                tag = f" [{hres}]" if hres else ""
                print(f"[click #{click_id}] inspector error{tag}: "
                      f"{type(e).__name__}: {e}")
                traceback.print_exc()
                if hres == "RPC_E_CANTCALLOUT_ININPUTSYNCCALL":
                    # If this fires from our long-lived MTA-or-STA worker,
                    # the input-sync state must be on the *target* app's
                    # UI thread (the WPF app dispatching its own click).
                    # Probe by retrying after a short backoff: if it now
                    # succeeds, we've identified the cause and can build
                    # in retry. If it still fails, the cause is on our
                    # side and the worker apartment isn't actually clean.
                    print(f"[click #{click_id}] retry probe in 250 ms…")
                    time.sleep(0.25)
                    try:
                        _inspect(x, y, click_id)
                        print(f"[click #{click_id}] retry SUCCEEDED — "
                              "input-sync was in the target app, not us")
                    except Exception as e2:
                        hres2 = _hresult_name(e2)
                        print(f"[click #{click_id}] retry also failed "
                              f"[{hres2 or '?'}]: {e2}")


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
