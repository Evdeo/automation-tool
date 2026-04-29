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
    chain = []
    cur = element
    while cur is not None:
        parent = cur.GetParentControl()
        if parent is None:
            chain.append((cur, 0))
            break
        idx = 0
        for sib in parent.GetChildren():
            if sib == cur:
                break
            idx += 1
        chain.append((cur, idx))
        cur = parent
    chain.reverse()
    return "/".join(tree._segment(c, i) for c, i in chain)


def _on_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.left:
        return
    try:
        ctrl = auto.ControlFromPoint(x, y)
        if ctrl is None:
            print(f"[{x},{y}] no element under cursor")
            return

        win = _top_window(ctrl)
        _, created = tree.ensure_snapshot(win)
        if created:
            print(f"** baseline captured: {tree.snapshot_path(win)}")

        tid = _path_to(ctrl)
        rect = ctrl.BoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        try:
            color = pyautogui.pixel(cx, cy)
        except Exception:
            color = None
        print("-" * 60)
        print(f"window  : {tree.snapshot_key(win)}")
        print(f"tree_id : {tid}")
        print(f"name    : {tree._name(ctrl)}")
        print(f"role    : {tree._role(ctrl)}")
        print(f"bbox    : ({rect.left},{rect.top}) -> ({rect.right},{rect.bottom})")
        print(f"center  : ({cx},{cy})")
        print(f"color   : {color}")
        print(f"enabled : {ctrl.IsEnabled}")
    except Exception as e:
        print(f"inspector error: {e}")


def run():
    print("Inspector running. Left-click any element. Ctrl+C to quit.")
    print("Baselines auto-saved on first click in each window.")
    with mouse.Listener(on_click=_on_click) as listener:
        listener.join()


if __name__ == "__main__":
    run()
