"""Click-to-capture inspector.

    python inspector.py                 # locks on first click
    python inspector.py notepad.exe     # pre-binds to a process

Each click prints (and appends to data/inspector.txt):
  window  : the live title
  process : the owning exe stem
  struct_id : the dotted index path you paste into run.py

A red-outlined screenshot is also saved to data/inspector_steps/ for
each click — easy to label opaque struct-ids later.

At session end (Ctrl+C) the log gets a paste-ready Python block:
  named constants for every unique struct-id, then state_stepN
  skeletons in click order. Copy the whole block into run.py.
"""
import argparse
import queue
import re
import threading
import time
import traceback
from pathlib import Path

import psutil
import uiautomation as auto
from pynput import mouse

from core import tree


_PROJECT_ROOT = Path(__file__).resolve().parent
_LOG_PATH = _PROJECT_ROOT / "data" / "inspector.txt"
_STEPS_DIR = _PROJECT_ROOT / "data" / "inspector_steps"
_log_file = None
_step_counter = 0

# Process scope: clicks outside this exe are silently ignored.
# None = "lock in on first valid click". After lock-in this becomes
# the lowercase exe stem (e.g. "notepad").
_scope_stem = None

# Dedupe consecutive duplicate captures (drag, double-click, repeat
# clicks on the same control). Stores the last emitted struct_id for
# the active window key — re-emit is suppressed.
_last_struct_per_window = {}

# Per-session capture log: every (non-duplicate) click is appended.
# At session end `_emit_session_block` writes a paste-ready Python
# block to inspector.txt summarizing this list. Each entry:
#   {"struct_id": str, "name_path": str, "win_stem": str}
_captures = []


def _emit(line):
    # Some Windows terminals are cp1252; window titles often contain
    # characters that don't encode there (VS Code's "●", emoji, em-dash).
    # The file is opened utf-8 — print falls back to ascii on miss.
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


_HRESULTS = {
    -2147417843: "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",
    -2147418111: "RPC_E_CALL_REJECTED",
    -2147417835: "RPC_E_SERVERCALL_RETRYLATER",
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",
    -2147221008: "CO_E_NOTINITIALIZED",
    -2147220991: "EVENT_E_INTERNALEXCEPTION",
    -2146233083: "COR_E_TIMEOUT",
    -2147220984: "UIA_E_ELEMENTNOTAVAILABLE",
}


def _hresult_name(exc):
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return _HRESULTS.get(args[0])
    return None


# Mouse-hook callbacks fire on pynput's listener thread *while Windows is
# dispatching input synchronously*. Any COM call made from that state
# fails with RPC_E_CANTCALLOUT_ININPUTSYNCCALL. The single persistent
# worker below owns the COM apartment for the program's lifetime; the
# mouse callback only enqueues coords.
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
    rectangle contains the click point, returning (leaf_ctrl, name_path,
    struct_id). The struct_id matches what `tree.walk_live` records.
    """
    chain = [(win, 0)]
    cur = win
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


def _exe_stem_for_pid(pid):
    try:
        return (psutil.Process(pid).name() or "").rsplit(".", 1)[0].lower()
    except Exception:
        return ""


def _inspect(x, y):
    global _scope_stem

    ctrl = auto.ControlFromPoint(x, y)
    if ctrl is None:
        return

    win = _top_window(ctrl)
    try:
        win_pid = win.ProcessId
    except Exception:
        return
    win_stem = _exe_stem_for_pid(win_pid)
    if not win_stem:
        return

    # Scope filter: lock on first click, ignore everything else.
    if _scope_stem is None:
        _scope_stem = win_stem
        _emit(f"** locked to process: {win_stem}.exe")
    elif win_stem != _scope_stem:
        return

    _, created = tree.ensure_snapshot(win)
    if created:
        _emit(f"** baseline captured: {tree.snapshot_path(win)}")

    leaf, name_path, struct_id = _path_to(win, x, y)

    # Suppress consecutive duplicates per window so drag / double-click
    # / repeat clicks on the same control don't spam the log.
    win_key = tree.snapshot_key(win)
    if _last_struct_per_window.get(win_key) == struct_id:
        return
    _last_struct_per_window[win_key] = struct_id

    _captures.append({
        "struct_id": struct_id,
        "name_path": name_path,
        "win_stem": win_stem,
    })

    _save_step_screenshot(leaf, struct_id)

    _emit("-" * 60)
    _emit(f'window    : "{tree._name(win)}"')
    _emit(f'process   : "{tree._process_stem(win)}"')
    _emit(f'struct_id : "{struct_id}"')


def _save_step_screenshot(leaf_ctrl, struct_id):
    """Take a screenshot with a red rectangle around the clicked element
    and save to data/inspector_steps/step_NNN_<struct_id>.png.

    The actual screenshot + draw + save runs on a fire-and-forget daemon
    thread so disk I/O and PIL work don't block the UIA worker. The
    bounding-rect query stays on the calling thread because it's a UIA
    call that must run in the apartment that owns the singleton."""
    global _step_counter
    _step_counter += 1
    step_n = _step_counter
    try:
        r = leaf_ctrl.BoundingRectangle
    except Exception as e:
        print(f"inspector: bbox query failed for step {step_n} ({e})")
        return
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return
    rect = (r.left, r.top, r.right, r.bottom)
    safe_id = struct_id.replace(".", "_")
    path = _STEPS_DIR / f"step_{step_n:03d}_{safe_id}.png"
    threading.Thread(
        target=_screenshot_worker,
        args=(rect, path, step_n),
        daemon=True,
    ).start()


def _screenshot_worker(rect, path, step_n):
    """Runs on its own daemon thread. Takes the screenshot, draws the
    red rectangle, saves to disk. Exceptions are printed and swallowed
    so a failure here can't affect the inspector's main worker."""
    try:
        import pyautogui
        from PIL import ImageDraw
        img = pyautogui.screenshot()
        draw = ImageDraw.Draw(img)
        draw.rectangle(list(rect), outline="red", width=4)
        img.save(path)
    except Exception as e:
        print(f"inspector: screenshot for step {step_n} skipped ({e})")


_TRANSIENT_HRESULTS = {
    "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",
    "RPC_E_CALL_REJECTED",
    "RPC_E_SERVERCALL_RETRYLATER",
    "EVENT_E_INTERNALEXCEPTION",
    "COR_E_TIMEOUT",
    "UIA_E_ELEMENTNOTAVAILABLE",
}


def _inspect_with_retry(x, y, max_attempts=8):
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
    with auto.UIAutomationInitializerInThread(debug=False):
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


def _sanitize_const(name):
    """Convert a leaf control name to UPPER_SNAKE_CASE; empty if nothing
    usable remains."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_").upper()


def _segment_name(seg):
    """Extract the name portion of a 'Name:Role' or '#idx:Role' segment."""
    name, sep, _ = seg.rpartition(":")
    return name if sep else seg


def _emit_session_block():
    """Append a paste-ready Python block to the session log: one constant
    per unique struct_id captured this session, then skeleton state
    functions in the order the user clicked. Names come from the leaf
    control's Name; collisions are disambiguated by parent name."""
    if not _captures or _log_file is None:
        return

    # Order unique struct_ids by first-seen click index.
    first_idx = {}
    for i, cap in enumerate(_captures):
        first_idx.setdefault(cap["struct_id"], i)
    unique = [_captures[first_idx[s]] for s in
              sorted(first_idx, key=first_idx.get)]

    # Suggest a constant name from each leaf segment.
    suggested = {}
    for n, cap in enumerate(unique, 1):
        leaf_seg = cap["name_path"].split("/")[-1]
        base = _sanitize_const(_segment_name(leaf_seg))
        # Empty or starts with a digit → not a valid Python identifier.
        if not base or base[0].isdigit():
            base = f"STEP_{n}"
        suggested[cap["struct_id"]] = base

    # Disambiguate collisions: prefix with parent leaf name; if still
    # colliding, suffix _2, _3, ...
    def regroup():
        groups = {}
        for sid, name in suggested.items():
            groups.setdefault(name, []).append(sid)
        return groups

    for name, sids in list(regroup().items()):
        if len(sids) <= 1:
            continue
        for sid in sids:
            cap = next(c for c in unique if c["struct_id"] == sid)
            segs = cap["name_path"].split("/")
            parent = _sanitize_const(_segment_name(segs[-2])) if len(segs) > 1 else ""
            if parent:
                suggested[sid] = f"{parent}_{name}"

    for name, sids in list(regroup().items()):
        if len(sids) <= 1:
            continue
        for i, sid in enumerate(sids[1:], 2):
            suggested[sid] = f"{name}_{i}"

    win_stem = unique[0]["win_stem"]
    width = max(len(suggested[c["struct_id"]]) for c in unique)

    lines = [
        "",
        "# --- paste into run.py ---------------------------------------------",
    ]
    for cap in unique:
        const = suggested[cap["struct_id"]]
        readable = _segment_name(cap["name_path"].split("/")[-1]) or "?"
        lines.append(f'{const:<{width}} = "{cap["struct_id"]}"  # {readable}')
    lines.append("")

    for i, cap in enumerate(_captures, 1):
        const = suggested[cap["struct_id"]]
        nxt = f'"step{i + 1}"' if i < len(_captures) else "None"
        lines.append(f"def state_step{i}(ctx):")
        lines.append(f"    click(ctx.{win_stem}, {const})")
        lines.append(f"    return {nxt}, ctx")
        lines.append("")

    block = "\n".join(lines)
    _log_file.write(block + "\n")
    _log_file.flush()
    print(block)


def run(scope=None):
    global _log_file, _scope_stem, _step_counter

    if scope:
        stem = scope.rsplit(".", 1)[0] if "." in scope else scope
        _scope_stem = stem.lower()

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Fresh inspector_steps/ each session so screenshot numbering starts
    # at 001 and stale shots from prior runs don't pollute the dir.
    if _STEPS_DIR.exists():
        for old in _STEPS_DIR.glob("step_*.png"):
            try:
                old.unlink()
            except OSError:
                pass
    _STEPS_DIR.mkdir(parents=True, exist_ok=True)
    _step_counter = 0
    print("Inspector running. Left-click any element. Ctrl+C to quit.")
    if _scope_stem:
        print(f"Pre-bound to process: {_scope_stem}.exe")
    else:
        print("Will lock to the first clicked window's process; "
              "later clicks elsewhere are ignored.")
    print(f"Session log: {_LOG_PATH}")
    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        _log_file = f
        threading.Thread(target=_worker, daemon=True).start()
        try:
            with mouse.Listener(on_click=_on_click) as listener:
                listener.join()
        except KeyboardInterrupt:
            pass
        finally:
            _emit_session_block()
            _log_file = None


def _parse_args():
    p = argparse.ArgumentParser(
        description="Click-to-capture inspector. Locks onto the first "
                    "clicked process; pass an exe name to pre-bind."
    )
    p.add_argument(
        "process",
        nargs="?",
        help="Optional process name to pre-bind (e.g. notepad.exe). "
             "Without this, the inspector locks on the first click.",
    )
    return p.parse_args()


if __name__ == "__main__":
    run(scope=_parse_args().process)
