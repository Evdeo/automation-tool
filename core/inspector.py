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

import threading

import pyautogui
import uiautomation as auto
from pynput import mouse

from core import tree


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

    Sibling index comes from walking `GetPreviousSiblingControl()` —
    NOT from `parent.GetChildren()` + `==`. uiautomation.Control has
    no reliable cross-instance equality (each call returns a fresh
    wrapper around the same UIA element), so `sib == cur` would
    almost never match and `idx` would land at `len(children)` for
    every sibling, collapsing distinct controls onto the same path.
    """
    chain = []
    cur = element
    while cur is not None:
        parent = cur.GetParentControl()
        if parent is None:
            chain.append((cur, 0))
            break
        idx = 0
        prev = cur.GetPreviousSiblingControl()
        while prev is not None:
            idx += 1
            prev = prev.GetPreviousSiblingControl()
        chain.append((cur, idx))
        cur = parent
    chain.reverse()
    name_path = "/".join(tree._segment(c, i) for c, i in chain)
    struct_id = ".".join(str(i) for _, i in chain)
    return name_path, struct_id


def _inspect(x, y):
    # Runs on a worker thread spawned from the mouse-hook callback —
    # NOT on the hook thread itself. Windows low-level mouse hooks
    # execute while input is being dispatched input-synchronously,
    # and COM blocks any outgoing UIA call made from that state with
    # RPC_E_CANTCALLOUT_ININPUTSYNCCALL (HRESULT -2147417843,
    # surfacing as "An outgoing call cannot be made since the
    # application is dispatching an input-synchronous call"). Most
    # noticeable when clicking menus, which trigger an input-sync
    # SendMessage to the target. Off-thread inspection sidesteps it.
    #
    # uiautomation uses COM, which must be initialized per thread —
    # without this context manager every call below raises
    # `[WinError -2147221008] CoInitialize has not been called`. The
    # Initializer enters/exits MTA on this worker for the duration
    # of the inspection.
    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            ctrl = auto.ControlFromPoint(x, y)
            if ctrl is None:
                print(f"[{x},{y}] no element under cursor")
                return

            win = _top_window(ctrl)
            _, created = tree.ensure_snapshot(win)
            if created:
                print(f"** baseline captured: {tree.snapshot_path(win)}")

            tid, struct_id = _path_to(ctrl)
            rect = ctrl.BoundingRectangle
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
    except Exception as e:
        print(f"inspector error: {e}")


def _on_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.left:
        return
    # Hand off to a worker thread so the mouse hook returns immediately.
    # See _inspect() for why doing UIA work on the hook thread fails.
    threading.Thread(target=_inspect, args=(x, y), daemon=True).start()


def run():
    print("Inspector running. Left-click any element. Ctrl+C to quit.")
    print("Baselines auto-saved on first click in each window.")
    with mouse.Listener(on_click=_on_click) as listener:
        listener.join()


if __name__ == "__main__":
    run()
