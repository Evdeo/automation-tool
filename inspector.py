"""Hover-and-press inspector with multi-app support and recovery mode.

    python inspector.py                 # capture mode
    python inspector.py --recover       # recovery mode

Usage (capture)
---------------
Hover the mouse cursor over any element, then press one of:

  * Middle mouse button (MMB)
  * F8

Each press is interpreted as either a COMMIT or an INFO dump:

  * **First press on an element** = COMMIT. The cursor jumps to the
    bounding-rect center, minimal info is printed (struct_id, name,
    control type, sampled center color, plus a `checkbox: <state>`
    line if the control exposes UIA's TogglePattern). A red-rectangle
    screenshot is saved under
    ``data/inspector_steps/<window>/<element>.png``. A name prompt
    opens; press Enter for the suggested default, or type your own
    name + Enter — your name is used verbatim, no prefix is added.
    The snippet is appended to ``data/inspector_snippets/session_<ts>.py``
    as a quiet audit trail; the clipboard is NOT touched per-step.
  * **Second press on the same element or a descendant** = INFO dump
    (full UIA properties). Doesn't commit again. The name prompt stays
    open if it was active.

Hotkeys
-------
  * F2 — colour sampler. Press F2 to fire Windows' built-in Snipping
    Tool (Win+Shift+S); drag any region in Windows' native overlay
    and release. The inspector reads the snip from the clipboard,
    buckets each channel to the nearest 16, and prints the top 20
    colours sorted by pixel count. A reference PNG with fat colour
    swatches is saved alongside. Useful for surveying a palette
    before deciding which `is_color` / `is_color_area` target to
    assert against. Press Esc in the snip overlay to cancel.
  * F8 — same as MMB. Useful when you can't middle-click (laptops
    without a real third button).

Multi-select with Ctrl
----------------------
Hold Ctrl and middle-click a series of elements. Each press is
captured into a group buffer, with a one-line `[group] +N` confirm
on the terminal. No per-element name prompt while Ctrl is held.
Release Ctrl and the inspector prompts once for the group's name;
the session block then emits a list literal:

    DIGIT_BUTTONS = [
        "0.0.0.0.1",   # ButtonControl "One"
        "0.0.0.0.2",   # ButtonControl "Two"
        "0.0.0.0.3",   # ButtonControl "Three"
    ]

Pairs naturally with `each` and `sequence` — those verbs take a
list of ids as their second argument, so a Ctrl-captured group
drops straight in: `each(click, window.calc, DIGIT_BUTTONS)`.

Multi-app support
-----------------
The inspector no longer locks to a single process. Every window the
user inspects is registered:

  * The first HWND seen for each exe stem becomes a primary "app"
    window. Its exe stem is the window's name (e.g. ``notepad``).
  * Any additional HWND in an already-known process is a "popup". The
    inspector prompts once for the popup's name.

At session end (Ctrl+C) the clipboard is filled — once — with a
paste-ready block:

  * ``APPS = {<stem>: "<full exe path>", ...}`` — one entry per app.
    Full paths so the runner can launch installs that aren't on PATH
    (Riot Client, Steam games, custom installs).
  * Constants grouped by window under ``# --- <name> ---`` headers.

Per-window tree fingerprints (depth-limited UIA shape) are written to
``data/window_fingerprints/<name>.json`` ONLY at session end so a
cancelled session leaves no artefacts behind. The runtime
``core.app.match`` and ``core.app.popup`` use these fingerprints to
locate live windows by structural shape rather than fragile titles.

Recovery mode
-------------
``python inspector.py --recover`` walks every saved capture from the
most recent session:

  * For each saved window: matches against live windows by fingerprint;
    silently updates the saved fingerprint on a strong match, prompts
    the user when ambiguous, asks for a fresh MMB on a miss.
  * For each saved element: tries ``tree.find_or_heal`` against the
    matched window's snapshot; silently updates the struct_id on a
    heal, displays the saved screenshot and asks the user to re-press
    on a miss.

Updated values are written back as a fresh paste-ready block, exactly
like a normal session end.
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import msvcrt
import os
import queue
import re
import sys
import threading
import _thread
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil
import pyautogui
import pyperclip
import uiautomation as auto
from pynput import keyboard, mouse

import config
from core import tree


_PROJECT_ROOT = Path(__file__).resolve().parent
_LOG_PATH = _PROJECT_ROOT / "data" / "inspector.txt"
_STEPS_DIR = _PROJECT_ROOT / config.INSPECTOR_STEPS_DIR
_SNIPPETS_DIR = _PROJECT_ROOT / config.INSPECTOR_SNIPPETS_DIR
_COLOR_SAMPLES_DIR = _PROJECT_ROOT / config.INSPECTOR_COLOR_SAMPLES_DIR
_FINGERPRINTS_DIR = _PROJECT_ROOT / config.WINDOW_FINGERPRINT_DIR

_log_file = None
_snippets_file = None
_step_counter = 0


# Worker-owned state -------------------------------------------------------
# `_windows` is keyed by lowercased window name (the same name that goes
# into the user's `APPS = {...}` dict, or that they pass to `popup()`).
# `_window_by_hwnd` is the reverse lookup so a press on an already-known
# HWND short-circuits classification. `_stems_seen` is the
# stem-to-primary-name map so additional HWNDs in the same exe become
# popups, not duplicate apps.
_windows = {}
_window_by_hwnd = {}
_stems_seen = {}
# HWNDs the user declined to save as a known popup. Subsequent presses
# in such an HWND are dropped silently rather than re-prompting.
_skip_popup_hwnds = set()

# Ctrl+middle-click multi-select. While Ctrl is held, MMB presses
# accumulate into `_group_buffer` instead of opening individual name
# prompts. On Ctrl release the buffer is committed as one list-valued
# capture: `GROUP_NAME = ["id_a", "id_b", ...]` in the session block.
_ctrl_held = False
_group_buffer = []          # list of element-info dicts
_group_counter = 0          # for default name suggestions

_last_committed = None
_pending_name = None
_captures = []
_used_names = set()

_events: "queue.Queue[tuple[int, int] | None]" = queue.Queue()

# Cursor-snap fast path. Every click is enqueued on both `_events`
# (the main worker, which does the slow gather + name prompt) and
# `_snap_events` (a dedicated worker that only does ControlFromPoint
# + BoundingRectangle + SetCursorPos). The snap worker races ahead
# of the main worker so click N+1's cursor feedback is not blocked
# by click N's full gather, and there's no backward-flicker from a
# late snap inside _commit.
_snap_events: "queue.Queue[tuple[int, int] | None]" = queue.Queue()


_NON_INTERACTABLE = {
    "TextControl", "GroupControl", "PaneControl", "ImageControl",
}
_INTERACTABLE = {
    "ButtonControl", "MenuItemControl", "ListItemControl", "HyperlinkControl",
    "TabItemControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "EditControl", "SplitButtonControl", "TreeItemControl",
    "DataItemControl", "HeaderItemControl", "MenuBarControl",
}


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

_TRANSIENT_HRESULTS = {
    "RPC_E_CANTCALLOUT_ININPUTSYNCCALL",
    "RPC_E_CALL_REJECTED",
    "RPC_E_SERVERCALL_RETRYLATER",
    "EVENT_E_INTERNALEXCEPTION",
    "COR_E_TIMEOUT",
    "UIA_E_ELEMENTNOTAVAILABLE",
}


def _hresult_name(exc):
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return _HRESULTS.get(args[0])
    return None


# --- Output -----------------------------------------------------------------


def _emit(line):
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


def _erase_prompt_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def _redraw_prompt_line():
    if _pending_name is None:
        return
    sys.stdout.write(
        f"name [{_pending_name['default']}]: {_pending_name['buffer']}"
    )
    sys.stdout.flush()


# --- UIA traversal ----------------------------------------------------------


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


def _path_to_chain(win, x, y, walked=None):
    """Find the deepest UIA element under (x, y) inside `win` and build
    its index chain back up to the window.

    `walked` may be passed in by callers that already walked the tree
    (`_gather_unsafe`) so we don't pay for two walks per press.

    The previous implementation descended one level at a time picking
    the smallest immediate child whose `BoundingRectangle` contains
    (x, y). That fails on WinUI / WinUI3 popups (modern Notepad menus,
    Calculator submenus, ContentDialogs) because intermediate
    rendering Panes have phantom bboxes that don't span their visible
    descendants — descent halts at `Pop-upHost` and the cursor jumps
    to the dropdown's geometric centre instead of the menu item.

    The fix descends with `tree.walk_live` (full recursive enumeration
    — same path the runtime uses) and picks the smallest-area
    descendant whose stored bbox contains (x, y). The chain is then
    reconstructed from the leaf's struct_id by walking each ancestor
    prefix back up the walked list.
    """
    if walked is None:
        walked = tree.walk_live(win)
    candidates = []
    for n in walked:
        bb = n.get("bbox") or [0, 0, 0, 0]
        if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]:
            w = bb[2] - bb[0]
            h = bb[3] - bb[1]
            if w <= 0 or h <= 0:
                continue
            candidates.append((w * h, n))
    if not candidates:
        # Cursor is outside `win`'s subtree (foreign window, off-screen).
        chain = [(win, 0)]
        return win, chain, tree._segment(win, 0), "0"
    candidates.sort(key=lambda t: t[0])
    leaf_node = candidates[0][1]

    by_struct = {n["struct_id"]: n for n in walked}
    parts = leaf_node["struct_id"].split(".")
    chain = []
    for d in range(len(parts)):
        sid = ".".join(parts[: d + 1])
        n = by_struct.get(sid)
        if n is None:
            break
        chain.append((n["ctrl"], int(parts[d])))

    # Promote non-interactable leaves to their deepest interactable
    # ancestor. The cursor over a menu item lands on whichever
    # TextControl child it happens to be over (the "Zoom in" label, the
    # "Ctrl+Plus" shortcut, etc.), so without promotion two presses on
    # the same button capture different sub-elements. Users want the
    # MenuItemControl/ButtonControl/etc. — the thing you'd actually
    # click in automation. Standalone TextControls (no interactable
    # ancestor in the chain) are left untouched.
    leaf_ctrl = chain[-1][0]
    if leaf_ctrl.ControlTypeName in _NON_INTERACTABLE:
        for depth in range(len(chain) - 2, -1, -1):
            anc_ctrl, _ = chain[depth]
            if anc_ctrl.ControlTypeName in _INTERACTABLE:
                chain = chain[: depth + 1]
                break

    name_path = "/".join(tree._segment(c, i) for c, i in chain)
    struct_id = ".".join(str(i) for _, i in chain)
    return chain[-1][0], chain, name_path, struct_id


def _runtime_id(ctrl):
    """UIA RuntimeId of `ctrl` as a tuple, or () on failure. RuntimeId
    is the UIA-level stable identifier for a live element — used by
    `_is_same_or_descendant` to ask 'is this the same element again'
    after a tree reshape. struct_id can't answer that question because
    it's a positional path, not an identity (a submenu opening can
    leave two different elements sharing the same struct_id at
    different times)."""
    try:
        return tuple(ctrl.GetRuntimeId() or ())
    except Exception:
        return ()


# ARIA-role aliases used when building `role[name="..."]` composite
# selectors as a fallback for elements with non-unique accessible names.
_ARIA_KNOWN_ROLES = {
    "ButtonControl": "button",
    "HyperlinkControl": "link",
    "EditControl": "textbox",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
}


def _is_browser_window(win):
    """True if `win`'s top-level Win32 class is a known browser /
    Electron container (Chromium, Firefox). Used to decide whether to
    extract a web-style CSS selector from the captured leaf instead of
    emitting a positional struct_id (the latter is unstable across
    page re-renders)."""
    try:
        from core.verbs import _BROWSER_WINDOW_CLASSES
        return (win.ClassName or "") in _BROWSER_WINDOW_CLASSES
    except Exception:
        return False


def _extract_web_selector(leaf, walked):
    """Build a stable CSS selector for a captured web element from
    UIA properties the browser exposes. Returns None when nothing
    usable is available (caller falls back to struct_id).

    Priority:
      1. DOM `id` attribute (UIA AutomationId on browsers) → `#login`
      2. Unique accessible name (visible text / aria-label) →
         `[aria-label="Sign in"]`
      3. Role + name composite for non-unique names where role is in
         a small ARIA-known set → `button[name="Save"]`
      4. HTML `class` attribute (UIA ClassName on Firefox / Chromium
         when no name and no id) → `.btn.btn-primary`
      5. None — caller emits struct_id with a warning comment.

    Uniqueness in (2) is checked against the already-walked tree
    (`tree.walk_live(win)` ran in `_path_to_chain`); we don't pay for
    a second tree walk."""
    try:
        auto_id = (leaf.AutomationId or "").strip()
    except Exception:
        auto_id = ""
    if auto_id:
        return f"#{auto_id}"

    try:
        name = (leaf.Name or "").strip()
        role = leaf.ControlTypeName or ""
    except Exception:
        name, role = "", ""

    if name:
        same_name = sum(1 for n in walked if (n.get("name") or "") == name)
        if same_name == 1:
            return f'[aria-label="{name}"]'

        role_short = _ARIA_KNOWN_ROLES.get(role)
        if role_short:
            same_pair = sum(
                1 for n in walked
                if (n.get("name") or "") == name and n.get("role") == role
            )
            if same_pair == 1:
                return f'{role_short}[name="{name}"]'

    # Priority 4: HTML `class` attribute. Browsers expose it via UIA
    # ClassName (Firefox always; Chromium when accessibility is on).
    # Multiple classes on one element come through space-separated:
    # `"btn btn-primary"` -> `.btn.btn-primary`. Non-unique by nature
    # but better than nothing — tells the user roughly which element
    # they clicked, and they can refine in their script if needed.
    try:
        cls = (leaf.ClassName or "").strip()
    except Exception:
        cls = ""
    if cls:
        parts = [p for p in cls.split() if p]
        if parts:
            return "." + ".".join(parts)

    return None


def _find_interactable_ancestor(chain):
    if not chain or len(chain) < 2:
        return None
    leaf_ctrl, _ = chain[-1]
    if leaf_ctrl.ControlTypeName not in _NON_INTERACTABLE:
        return None
    for depth in range(len(chain) - 2, -1, -1):
        ctrl, _ = chain[depth]
        if ctrl.ControlTypeName in _INTERACTABLE:
            ancestor_struct = ".".join(str(i) for _, i in chain[: depth + 1])
            return {
                "struct_id": ancestor_struct,
                "control_type": ctrl.ControlTypeName,
                "name": ctrl.Name or "",
            }
    return None


def _exe_stem_for_pid(pid):
    try:
        return (psutil.Process(pid).name() or "").rsplit(".", 1)[0].lower()
    except Exception:
        return ""


def _exe_path_for_pid(pid):
    """Full executable path for a PID (e.g.
    `C:\\Riot Games\\Riot Client\\RiotClientServices.exe`). The runner
    feeds this directly to `subprocess.Popen`, so we record the full
    path — not just the basename — to support apps that aren't on PATH.

    Falls back to `<exe_stem>.exe` if the path can't be resolved
    (some sandboxed processes refuse `Process.exe()`); for in-PATH
    apps like notepad.exe that fallback still works."""
    try:
        path = psutil.Process(pid).exe() or ""
        if path:
            return path
    except Exception:
        pass
    stem = _exe_stem_for_pid(pid)
    return f"{stem}.exe" if stem else ""


# --- Naming -----------------------------------------------------------------


def _sanitize_const(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_").upper()


def _sanitize_lower(name):
    """Lowercase identifier — for window names. Used as `data.<name>`
    attribute and as the popup-lookup key."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_").lower()
    return s


def _segment_name(seg):
    name, sep, _ = seg.rpartition(":")
    return name if sep else seg


def _suggest_name(name_path, control_type, window_prefix=""):
    leaf_seg = name_path.split("/")[-1]
    base = _sanitize_const(_segment_name(leaf_seg))
    if not base or base[0].isdigit():
        base = f"STEP_{len(_captures) + 1}"
    if window_prefix:
        base = f"{window_prefix}_{base}"
    name = base
    n = 2
    while name in _used_names:
        name = f"{base}_{n}"
        n += 1
    return name


def _readable_label(commit):
    return (
        _segment_name(commit["name_path"].split("/")[-1])
        or commit["name"]
        or "?"
    )


# --- Window registry -------------------------------------------------------


def _disambiguate_window_name(base):
    if base not in _windows:
        return base
    n = 2
    while f"{base}_{n}" in _windows:
        n += 1
    return f"{base}_{n}"


def _prompt_save_popup(title, default_name):
    """Synchronous y/N prompt: should this newly-detected popup HWND
    be saved as a known fingerprint? Returns the chosen name, or None
    if the user declined. Runs on the inspector worker thread; safe
    because the inspector is interactive and single-driver."""
    sys.stdout.write(
        f"\n[popup detected: {title!r}] save as fingerprint? (y/N): "
    )
    sys.stdout.flush()
    try:
        ch = msvcrt.getwch()
    except Exception:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return None
    if ch.lower() != "y":
        sys.stdout.write("no\n")
        sys.stdout.flush()
        return None
    sys.stdout.write(f"y\nname [{default_name}]: ")
    sys.stdout.flush()
    buf = ""
    while True:
        try:
            c = msvcrt.getwch()
        except Exception:
            break
        if c in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if c == "\b":
            if buf:
                buf = buf[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        elif c == "\x03":
            sys.stdout.write("\n")
            sys.stdout.flush()
            return None
        elif c.isprintable():
            buf += c
            sys.stdout.write(c)
            sys.stdout.flush()
    return buf.strip() or default_name


def _classify_window(win):
    """Map a top-level UIA window to a registry name.

    Returns ``(window_name, kind)`` where ``kind`` is one of:
      * ``"app"`` — first HWND we've seen for its exe stem
      * ``"popup"`` — additional HWND in an already-registered exe
      * ``"existing"`` — HWND already registered in this session

    Returns ``(None, None)`` if the window has no resolvable PID. The
    caller silently drops the press in that case.
    """
    try:
        win_hwnd = win.NativeWindowHandle
    except Exception:
        return None, None

    if win_hwnd in _window_by_hwnd:
        return _window_by_hwnd[win_hwnd], "existing"

    try:
        win_pid = win.ProcessId
    except Exception:
        return None, None
    win_stem = _exe_stem_for_pid(win_pid)
    if not win_stem:
        return None, None

    if win_stem not in _stems_seen:
        name = _disambiguate_window_name(win_stem)
        title = ""
        try:
            title = win.Name or ""
        except Exception:
            pass
        spec = _exe_path_for_pid(win_pid)
        _windows[name] = {
            "hwnd": win_hwnd,
            "is_app": True,
            "spec": spec,
            "title_hint": title,
            "fingerprint": None,
            "first_seen_idx": len(_windows),
        }
        _stems_seen[win_stem] = name
        _window_by_hwnd[win_hwnd] = name
        _emit(f"** registered app: {name} ({spec})")
        return name, "app"

    # Same exe, new HWND → candidate popup. Ask the user whether to
    # save it as a fingerprint; previously-declined HWNDs are skipped
    # silently so we don't re-prompt on every press inside them.
    if win_hwnd in _skip_popup_hwnds:
        return None, None

    title = ""
    try:
        title = win.Name or ""
    except Exception:
        pass
    default_base = _sanitize_lower(title) or f"{win_stem}_dlg"
    chosen = _prompt_save_popup(title, default_base)
    if chosen is None:
        _skip_popup_hwnds.add(win_hwnd)
        return None, None

    name = _disambiguate_window_name(_sanitize_lower(chosen) or default_base)
    _windows[name] = {
        "hwnd": win_hwnd,
        "is_app": False,
        "spec": None,
        "title_hint": title,
        "fingerprint": None,
        "first_seen_idx": len(_windows),
    }
    _window_by_hwnd[win_hwnd] = name
    _emit(f"** registered popup: {name} (title hint: {title!r})")
    return name, "popup"


def _capture_fingerprint(win, window_name):
    """Compute and cache the depth-limited fingerprint for a registered
    window. Stored in `_windows[name]["fingerprint"]`; written to disk
    only at session end."""
    try:
        fp = tree.fingerprint(win)
    except Exception as e:
        _emit(f"inspector: fingerprint failed for {window_name}: {e}")
        return
    if window_name in _windows:
        _windows[window_name]["fingerprint"] = fp


# --- Cursor + screenshot ----------------------------------------------------


def _move_cursor(x, y):
    try:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
    except Exception:
        pass


def _quick_snap_cursor(x, y):
    """Snap the cursor to the centre of the control under (x, y) as
    early as possible. Runs at the top of click handlers so the user
    sees instant visual feedback (~80ms) rather than waiting for
    `_gather_element_info` to finish the full tree walk (~400ms).
    Cheap: one `ControlFromPoint` + one `BoundingRectangle` read +
    one `SetCursorPos`. The slower gather still runs after, and its
    own snap (in `_commit` / `_handle_group_click`) becomes a no-op
    correction that handles the rare case where `_path_to_chain`
    resolves to a different leaf than `ControlFromPoint`."""
    try:
        ctrl = auto.ControlFromPoint(int(x), int(y))
        if ctrl is None:
            return
        r = ctrl.BoundingRectangle
        if r.right - r.left > 0 and r.bottom - r.top > 0:
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            _move_cursor(cx, cy)
    except Exception:
        pass


def _get_cursor_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _read_pixel(x, y):
    """Read RGB at virtual-screen (x, y) via PIL.ImageGrab — BitBlt
    from the DWM compositor over the full virtual screen.

    Two earlier approaches both failed in real use:
      * pyautogui.pixel takes a primary-monitor-only screenshot, so
        coords on a secondary display return wrong / None.
      * GetPixel(GetDC(0), ...) is clipped to the primary monitor's
        DC region too — coords outside it return CLR_INVALID.

    ImageGrab.grab(all_screens=True) covers every monitor in the
    virtual screen rectangle, with DPI scaling resolved by the OS
    compositor. Returns (r, g, b) on success, None on failure.
    """
    from PIL import ImageGrab
    try:
        img = ImageGrab.grab(bbox=(int(x), int(y), int(x) + 1, int(y) + 1),
                             all_screens=True)
    except Exception:
        return None
    try:
        rgb = img.getpixel((0, 0))
    except Exception:
        return None
    if not rgb or isinstance(rgb, int) or len(rgb) < 3:
        return None
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _screenshot_path(window_name, suggested_name, struct_id):
    """Path under data/inspector_steps/<window>/. The element name is
    used so recovery mode can find the right screenshot by name later."""
    safe_window = _sanitize_lower(window_name) or "main"
    safe_elem = (suggested_name or struct_id.replace(".", "_")) + ".png"
    return _STEPS_DIR / safe_window / safe_elem


def _save_step_screenshot(bbox, window_name, suggested_name, struct_id):
    global _step_counter
    _step_counter += 1
    step_n = _step_counter
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    if right - left <= 0 or bottom - top <= 0:
        return None
    path = _screenshot_path(window_name, suggested_name, struct_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    threading.Thread(
        target=_screenshot_worker,
        args=((left, top, right, bottom), path, step_n),
        daemon=True,
    ).start()
    return path


def _screenshot_worker(rect, path, step_n):
    try:
        from PIL import ImageDraw
        img = pyautogui.screenshot()
        draw = ImageDraw.Draw(img)
        draw.rectangle(list(rect), outline="red", width=4)
        img.save(path)
    except Exception as e:
        print(f"inspector: screenshot for step {step_n} skipped ({e})")


# --- Element gathering ------------------------------------------------------


def _gather_element_info(x, y):
    delay = 0.05
    for attempt in range(8):
        try:
            return _gather_unsafe(x, y)
        except Exception as e:
            hres = _hresult_name(e)
            if hres not in _TRANSIENT_HRESULTS or attempt == 7:
                tag = f" [{hres}]" if hres else ""
                print(f"inspector error{tag}: {type(e).__name__}: {e}")
                if hres is None:
                    traceback.print_exc()
                return None
            time.sleep(delay)
            delay *= 2
    return None


def _gather_unsafe(x, y):
    ctrl = auto.ControlFromPoint(x, y)
    if ctrl is None:
        return None

    win = _top_window(ctrl)
    window_name, kind = _classify_window(win)
    if window_name is None:
        return None

    # Snapshot the app window's tree so `find_or_heal` has a baseline.
    # Popups skip — their snapshot key collides with the app's, and
    # their identity lives in the fingerprint instead.
    if kind == "app":
        try:
            _, created = tree.ensure_snapshot(win)
            if created:
                _emit(f"** baseline captured: {tree.snapshot_path(win)}")
        except Exception as e:
            _emit(f"inspector: snapshot failed for {window_name}: {e}")

    # Compute fingerprint on first sighting (kept in memory; written to
    # disk only at session end).
    if kind in ("app", "popup") and _windows[window_name]["fingerprint"] is None:
        _capture_fingerprint(win, window_name)

    # Walk the tree once and reuse the result for `_path_to_chain` so
    # we don't pay for two walks per press. (The earlier refresh-cache
    # approach has been retired in favour of using RuntimeId directly
    # in `_is_same_or_descendant` — see `_runtime_id` docstring.)
    walked = tree.walk_live(win)
    leaf, chain, name_path, struct_id = _path_to_chain(win, x, y, walked=walked)

    # Web captures: when the top-level window is a browser, try to
    # build a stable CSS selector from UIA properties (DOM id /
    # aria-label / role+name). Falls back to struct_id with a warning
    # comment at emit time when nothing usable is available.
    web_capture = _is_browser_window(win)
    web_selector = _extract_web_selector(leaf, walked) if web_capture else None

    bbox = None
    bbox_center = (None, None)
    color = None
    color_reason = None
    try:
        r = leaf.BoundingRectangle
        if r.right - r.left > 0 and r.bottom - r.top > 0:
            bbox = (r.left, r.top, r.right, r.bottom)
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            bbox_center = (cx, cy)
            color = _read_pixel(cx, cy)
            if color is None:
                color_reason = f"ImageGrab at ({cx},{cy}) failed"
        else:
            color_reason = "control bbox is zero-size"
    except Exception as e:
        # Some controls (minimized windows, virtualised list items)
        # have BoundingRectangle implementations that overflow inside
        # uiautomation's COM marshaling. Fall back to the cursor
        # position so the user at least gets the pixel they clicked.
        color = _read_pixel(x, y)
        bbox_center = (x, y)
        color_reason = (
            f"BoundingRectangle raised: {type(e).__name__}: {e}; "
            f"sampled cursor position ({x},{y}) instead"
        )

    # UIA TogglePattern — present on checkboxes, radio buttons, switches,
    # and tri-state widgets. Surfacing it tells the user to reach for
    # is_checked / set_checkbox instead of plain click.
    toggle_state = None
    try:
        state = leaf.GetTogglePattern().ToggleState
        if state == 1:
            toggle_state = True
        elif state == 0:
            toggle_state = False
        elif state == 2:
            toggle_state = "indeterminate"
    except Exception:
        toggle_state = None

    return {
        "struct_id": struct_id,
        "name_path": name_path,
        "name": leaf.Name or "",
        "control_type": leaf.ControlTypeName or "",
        "class_name": leaf.ClassName or "",
        "automation_id": leaf.AutomationId or "",
        "bbox": bbox,
        "bbox_center": bbox_center,
        "color": color,
        "color_reason": color_reason,
        "toggle_state": toggle_state,
        "window_name": window_name,
        "runtime_id": _runtime_id(leaf),
        "web_capture": web_capture,
        "web_selector": web_selector,
        "interactable_ancestor": _find_interactable_ancestor(chain),
    }


# --- Print blocks -----------------------------------------------------------


def _format_color(color, reason=None):
    if not color:
        return f"(unavailable — {reason})" if reason else "(unavailable)"
    r, g, b = color
    return f"({r}, {g}, {b})  #{r:02x}{g:02x}{b:02x}"


def _format_toggle(state):
    if state is True:
        return "checked"
    if state is False:
        return "unchecked"
    return str(state)  # "indeterminate" or other


def _emit_minimal(info):
    _emit("-" * 60)
    _emit(f'window       : {info.get("window_name", "?")}')
    _emit(f'commit       : "{info["struct_id"]}"')
    if info.get("web_selector"):
        _emit(f'identifier   : "{info["web_selector"]}"')
    elif info.get("web_capture"):
        _emit(f'identifier   : (none — DevTools may help; struct_id will be emitted)')
    _emit(f'name         : "{info["name"]}"')
    _emit(f'control type : {info["control_type"]}')
    _emit(f'color        : {_format_color(info["color"], info.get("color_reason"))}')
    if info.get("toggle_state") is not None:
        _emit(f'checkbox     : {_format_toggle(info["toggle_state"])} '
              f'— use is_checked / set_checkbox')
    if info["interactable_ancestor"]:
        anc = info["interactable_ancestor"]
        _emit(
            f'note         : this is {info["control_type"]} and can be '
            f'used; nearest interactable ancestor is '
            f'"{anc["struct_id"]}" ({anc["control_type"]} "{anc["name"]}")'
        )


def _emit_full(commit):
    in_prompt = _pending_name is not None
    if in_prompt:
        _erase_prompt_line()

    _emit("- - - full info - - -")
    _emit(f'  window       : {commit.get("window_name", "?")}')
    _emit(f'  struct_id    : "{commit["struct_id"]}"')
    _emit(f'  name         : "{commit["name"]}"')
    _emit(f'  control type : {commit["control_type"]}')
    _emit(f'  class name   : {commit["class_name"]}')
    _emit(f'  automation id: {commit["automation_id"]}')
    if commit["bbox"]:
        l, t, r, b = commit["bbox"]
        cx, cy = commit["bbox_center"]
        _emit(f'  bbox         : ({l}, {t}) -> ({r}, {b})')
        _emit(f'  bbox center  : ({cx}, {cy})')
    _emit(f'  color        : {_format_color(commit["color"], commit.get("color_reason"))}')
    if commit.get("toggle_state") is not None:
        _emit(f'  checkbox     : {_format_toggle(commit["toggle_state"])}')
    _emit(f'  parent path  : {commit["name_path"]}')
    if commit["interactable_ancestor"]:
        anc = commit["interactable_ancestor"]
        _emit(
            f'  ancestor     : "{anc["struct_id"]}" '
            f'({anc["control_type"]} "{anc["name"]}")'
        )

    if in_prompt:
        _redraw_prompt_line()


# --- Commit / finalize ------------------------------------------------------


def _is_same_or_descendant(info, last):
    """True if `info`'s element is the same on-screen control as
    `last`'s, or rendered geometrically inside `last`'s bounding rect.

    Replaces the old `_is_descendant_or_same(struct_id, last_struct_id)`
    which compared positional struct_ids. struct_id is a path index,
    not an element identity — a submenu opening between presses can
    leave two different elements (e.g. View>Zoom and Zoom>Zoom in)
    sharing the same struct_id at different times, which made the old
    check fire spurious info-dumps for genuine new presses.

    UIA's `RuntimeId` is stable per-element-lifetime — that's the
    canonical 'same element' test. Bbox containment handles the
    descendant case geometrically (also robust to tree reshape; a
    child element's screen rect is by definition inside its parent's).
    """
    if info.get("window_name") != last.get("window_name"):
        return False
    rid_a = info.get("runtime_id")
    rid_b = last.get("runtime_id")
    if rid_a and rid_b and rid_a == rid_b:
        return True
    nb = info.get("bbox")
    lb = last.get("bbox")
    if nb and lb:
        nl, nt, nr, nbo = nb
        ll, lt, lr, lbo = lb
        if ll <= nl and nr <= lr and lt <= nt and nbo <= lbo:
            return True
    return False


def _commit(info):
    global _last_committed, _pending_name

    window_name = info.get("window_name", "")
    window_prefix = _sanitize_const(window_name) if window_name else ""
    suggested = _suggest_name(
        info["name_path"], info["control_type"],
        window_prefix=window_prefix,
    )
    _used_names.add(suggested)

    # Cursor snap is handled by the dedicated snap thread on click;
    # snapping again here would risk a backward-flicker if the user
    # has already clicked the next element while this one is still
    # being committed.

    screenshot_path = _save_step_screenshot(
        info["bbox"], window_name, suggested, info["struct_id"],
    )
    _emit_minimal(info)

    commit = {
        "struct_id": info["struct_id"],
        "name_path": info["name_path"],
        "name": info["name"],
        "control_type": info["control_type"],
        "class_name": info["class_name"],
        "automation_id": info["automation_id"],
        "bbox": info["bbox"],
        "bbox_center": info["bbox_center"],
        "color": info["color"],
        "window_name": window_name,
        "runtime_id": info.get("runtime_id", ()),
        "web_capture": info.get("web_capture", False),
        "web_selector": info.get("web_selector"),
        "interactable_ancestor": info["interactable_ancestor"],
        "default_name": suggested,
        "final_name": None,
        "screenshot_path": screenshot_path,
    }
    _last_committed = commit
    _pending_name = {"buffer": "", "default": suggested, "commit": commit}

    sys.stdout.write(f"name [{suggested}]: ")
    sys.stdout.flush()


def _finalize_prompt():
    global _pending_name

    if _pending_name is None:
        return

    buffer = _pending_name["buffer"].strip()
    commit = _pending_name["commit"]
    default = commit["default_name"]
    window_name = commit.get("window_name", "")

    if buffer:
        # User typed a custom name — use exactly what they wrote, do
        # NOT prepend the window prefix. The default suggestion already
        # has the prefix; if the user is typing their own name they're
        # explicitly overriding it.
        sanitized = _sanitize_const(buffer)
        if sanitized and not sanitized[0].isdigit():
            _used_names.discard(default)
            base = sanitized
            n = 2
            final = base
            while final in _used_names:
                final = f"{base}_{n}"
                n += 1
            _used_names.add(final)
        else:
            final = default
    else:
        final = default

    commit["final_name"] = final

    if commit.get("kind") == "group":
        # Group commit: no per-commit screenshot to rename (each
        # member already has its own struct-id-named file). The
        # snippet is a multi-line list assignment; sidecar shows it
        # as such for the audit trail.
        members = commit["members"]
        ids = ", ".join(f'"{m["struct_id"]}"' for m in members)
        snippet = f"{final} = [{ids}]"
    else:
        # Rename the screenshot file to match the final element name so
        # recovery mode can locate it.
        old_path = commit.get("screenshot_path")
        if old_path is not None and old_path != _screenshot_path(
            window_name, final, commit["struct_id"]
        ):
            new_path = _screenshot_path(window_name, final, commit["struct_id"])
            try:
                # The screenshot worker may not have flushed yet — wait briefly.
                for _ in range(20):
                    if old_path.exists():
                        break
                    time.sleep(0.05)
                if old_path.exists():
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    old_path.replace(new_path)
                    commit["screenshot_path"] = new_path
            except Exception:
                pass

        label = _readable_label(commit)
        snippet = f'{final} = "{commit["struct_id"]}"  # {label}'

    # Sidecar file is a quiet audit trail — kept so a crashed session
    # doesn't lose captures. Per-step clipboard copy was removed: the
    # full session block is copied at Ctrl+C end via `_emit_session_end`,
    # which is the only point the user actually pastes into run.py.
    if _snippets_file is not None:
        try:
            with open(_snippets_file, "a", encoding="utf-8") as f:
                f.write(snippet + "\n")
        except Exception as e:
            print(f"inspector: sidecar append failed ({e})")

    _captures.append(commit)

    sys.stdout.write("\n")
    sys.stdout.flush()

    _pending_name = None


# --- Press handling ---------------------------------------------------------


def _handle_press(x, y):
    # Cursor snap is handled by the dedicated snap thread (queued
    # from _on_click); the main worker just does the slow gather.
    info = _gather_element_info(x, y)
    if info is None:
        return

    if _last_committed is not None and _is_same_or_descendant(
        info, _last_committed
    ):
        _emit_full(_last_committed)
        return

    if _pending_name is not None:
        _finalize_prompt()

    _commit(info)


def _handle_prompt_char(ch):
    global _pending_name
    if _pending_name is None:
        return

    if ch in ("\r", "\n"):
        _finalize_prompt()
    elif ch == "\b":
        if _pending_name["buffer"]:
            _pending_name["buffer"] = _pending_name["buffer"][:-1]
            sys.stdout.write("\b \b")
            sys.stdout.flush()
    elif ch == "\x03":
        _finalize_prompt()
        _thread.interrupt_main()
    elif ch.isprintable():
        _pending_name["buffer"] += ch
        sys.stdout.write(ch)
        sys.stdout.flush()


# --- Worker -----------------------------------------------------------------


def _poll_during_prompt():
    while _pending_name is not None:
        if msvcrt.kbhit():
            try:
                ch = msvcrt.getwch()
            except Exception:
                continue
            _handle_prompt_char(ch)
            continue
        try:
            item = _events.get(timeout=0.03)
        except queue.Empty:
            continue
        if item is None:
            _events.put(None)
            return
        _dispatch_event(item)


def _dispatch_event(item):
    """Route a queued event. Plain (x, y) tuples are solo click
    captures. Tagged tuples carry their own handlers."""
    if isinstance(item, tuple) and item and isinstance(item[0], str):
        tag = item[0]
        if tag == "color_sample_via_snip":
            _handle_color_sample()
            return
        if tag == "group_click":
            _, x, y = item
            _handle_group_click(x, y)
            return
        if tag == "finalize_group":
            _finalize_group()
            return
    _handle_press(*item)


def _handle_group_click(x, y):
    """Append the element under the cursor to the current Ctrl-group
    buffer. Skips name prompt — the buffer is named once on Ctrl
    release. Same-element repeats are de-duped against the last entry
    so an accidental double-press doesn't add the same id twice."""
    # Cursor snap is handled by the dedicated snap thread.
    info = _gather_element_info(x, y)
    if info is None:
        return
    if _group_buffer:
        last = _group_buffer[-1]
        if (last.get("struct_id") == info.get("struct_id")
                and last.get("window_name") == info.get("window_name")):
            _emit(f"[group] (skipped duplicate {info['struct_id']!r})")
            return
    # Save a per-element screenshot named by struct_id; we don't have
    # a final name yet so we use the struct_id-based default.
    _save_step_screenshot(info["bbox"], info.get("window_name", ""),
                          None, info["struct_id"])
    _group_buffer.append(info)
    _emit(f'[group] +{len(_group_buffer)}: "{info["struct_id"]}" '
          f'({info["control_type"]} "{info["name"]}")')


def _finalize_group():
    """Snapshot the group buffer into a list-valued capture and open
    the name prompt. The prompt's commit dict carries `kind="group"`
    and a `members` list; `_finalize_prompt` and `_render_group`
    branch on that to emit `NAME = [...]` rather than `NAME = "..."`.
    """
    global _pending_name, _group_counter
    if not _group_buffer:
        return
    members = list(_group_buffer)
    _group_buffer.clear()
    _group_counter += 1

    window_name = members[0].get("window_name", "")
    window_prefix = _sanitize_const(window_name) if window_name else ""
    suggested = (f"{window_prefix}_GROUP_{_group_counter}"
                 if window_prefix else f"GROUP_{_group_counter}")
    _used_names.add(suggested)

    commit = {
        "kind": "group",
        "window_name": window_name,
        "members": [{
            "struct_id": m["struct_id"],
            "name": m.get("name", ""),
            "control_type": m.get("control_type", ""),
            "name_path": m.get("name_path", ""),
        } for m in members],
        "default_name": suggested,
        "final_name": None,
    }
    _pending_name = {"buffer": "", "default": suggested, "commit": commit}
    sys.stdout.write(f"group name [{suggested}]: ")
    sys.stdout.flush()


def _capture_via_snipping_tool(timeout=60.0):
    """Fire Win+Shift+S, then poll the clipboard for a new image.
    Returns a PIL Image, or None if the user cancelled (Esc) or the
    deadline passed before anything landed on the clipboard.

    Detects "new" via GetClipboardSequenceNumber so a stale image
    sitting on the clipboard from before the call doesn't get picked
    up by mistake."""
    from PIL import ImageGrab
    seq_before = ctypes.windll.user32.GetClipboardSequenceNumber()
    pyautogui.hotkey("win", "shift", "s")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        seq_now = ctypes.windll.user32.GetClipboardSequenceNumber()
        if seq_now == seq_before:
            continue
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            return None
        if hasattr(img, "size"):
            return img
        # Sequence advanced but the new content isn't an image (e.g.,
        # the user copied text in another window). Refresh baseline
        # and keep waiting.
        seq_before = seq_now
    return None


def _handle_color_sample():
    """F2 sampler: trigger Win+Shift+S, wait for the snip on the
    clipboard, bucket each channel to the nearest 16, print the most
    common colours by pixel count, and save a PNG reference card with
    one fat swatch per colour for easy visual matching."""
    import numpy as np
    _emit("[color-sample] Snipping Tool armed — drag a region, "
          "release to sample (Esc to cancel).")
    img = _capture_via_snipping_tool()
    if img is None:
        _emit("[color-sample] cancelled or timed out; nothing sampled.")
        return
    w, h = img.size
    arr = np.asarray(img)[..., :3]
    bucketed = (arr // 16) * 16
    flat = bucketed.reshape(-1, 3)
    pixels, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(-counts)
    total = int(counts.sum())
    _emit(f"\n[color-sample] {w}x{h} snip; {total} px scanned; "
          f"top colours (bucketed by 16):")
    rows = []
    for idx in order[:20]:
        r, g, b = (int(v) for v in pixels[idx])
        c = int(counts[idx])
        pct = 100.0 * c / total
        _emit(f"  ({r:3}, {g:3}, {b:3})  {c:>7}  {pct:5.1f}%")
        rows.append(((r, g, b), c, pct))
    if rows:
        try:
            path = _save_color_card(rows)
            _emit(f"  → reference card: {path}")
            try:
                os.startfile(str(path))
            except Exception as e:
                _emit(f"  (auto-open failed: {e})")
        except Exception as e:
            _emit(f"[color-sample] PNG card skipped: {e}")


def _save_color_card(rows):
    """Render `rows` (list of ((r, g, b), count, pct)) into a PNG with
    one row per colour: RGB + count + percent on the left, a wide
    swatch of the actual colour on the right. Saved under
    data/inspector_color_samples/<timestamp>.png; path returned."""
    from PIL import Image, ImageDraw, ImageFont

    row_height = 90
    padding = 24
    text_width = 480
    swatch_width = 540
    img_w = padding + text_width + padding + swatch_width + padding
    img_h = padding * 2 + row_height * len(rows)

    img = Image.new("RGB", (img_w, img_h), color=(248, 248, 248))
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in ("DejaVuSansMono.ttf", "Consolas.ttf", "consola.ttf",
                      "cour.ttf"):
        try:
            font = ImageFont.truetype(candidate, 30)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    for i, ((r, g, b), count, pct) in enumerate(rows):
        y = padding + i * row_height
        text = f"({r:3}, {g:3}, {b:3})   #{r:02x}{g:02x}{b:02x}\n" \
               f"{count:>8} px   {pct:5.1f}%"
        draw.multiline_text((padding, y + 12), text, fill=(20, 20, 20),
                            font=font, spacing=6)
        sx0 = padding + text_width + padding
        sy0 = y + 8
        sx1 = sx0 + swatch_width
        sy1 = y + row_height - 8
        draw.rectangle([sx0, sy0, sx1, sy1], fill=(int(r), int(g), int(b)),
                       outline=(0, 0, 0), width=2)

    _COLOR_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = _COLOR_SAMPLES_DIR / (
        f"sample_{datetime.now():%Y-%m-%d_%H-%M-%S}.png")
    img.save(path)
    return path


def _worker():
    with auto.UIAutomationInitializerInThread(debug=False):
        auto.GetRootControl()
        while True:
            if _pending_name is not None:
                _poll_during_prompt()
                continue
            try:
                item = _events.get()
            except Exception as e:
                print(f"inspector worker recovered from: "
                      f"{type(e).__name__}: {e}")
                continue
            if item is None:
                return
            try:
                _dispatch_event(item)
            except Exception as e:
                print(f"inspector worker recovered from: "
                      f"{type(e).__name__}: {e}")


def _snap_worker():
    """Dedicated cursor-snap worker. Initializes UIA in its own COM
    apartment and processes only `_snap_events` — pure hit-test +
    SetCursorPos, no tree walks or prompts. Runs in parallel with
    the main worker so click N+1's snap is not blocked by click N's
    slow gather. Drains coalesce: if multiple snaps stack up, only
    the most recent matters (the cursor is going to settle there
    anyway), so we skip stale events when we're behind."""
    with auto.UIAutomationInitializerInThread(debug=False):
        auto.GetRootControl()
        while True:
            item = _snap_events.get()
            if item is None:
                return
            # Coalesce: if more snap events are already queued, skip
            # to the latest. The cursor only needs to land on the
            # newest target.
            while True:
                try:
                    nxt = _snap_events.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    _snap_events.put(None)  # propagate shutdown
                    return
                item = nxt
            try:
                x, y = item
                _quick_snap_cursor(x, y)
            except Exception as e:
                print(f"inspector snap worker recovered from: "
                      f"{type(e).__name__}: {e}")


# --- Listeners --------------------------------------------------------------


_CTRL_KEYS = (
    getattr(keyboard.Key, "ctrl", None),
    getattr(keyboard.Key, "ctrl_l", None),
    getattr(keyboard.Key, "ctrl_r", None),
)


def _on_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.middle:
        return
    # Snap immediately on the dedicated snap thread — the main worker
    # may be busy finishing the previous click's gather, and we don't
    # want cursor feedback to wait for that.
    _snap_events.put((x, y))
    if _ctrl_held:
        _events.put(("group_click", x, y))
    else:
        _events.put((x, y))


def _on_key_press(key):
    global _ctrl_held
    if key in _CTRL_KEYS:
        _ctrl_held = True
        return
    if key == keyboard.Key.f2:
        # Trigger the Windows Snipping Tool (Win+Shift+S). The user
        # drags a region in Windows' native overlay; the resulting
        # snip lands on the clipboard, which the worker polls for.
        _events.put(("color_sample_via_snip",))
        return
    if key == keyboard.Key.f8:
        try:
            x, y = _get_cursor_pos()
        except Exception:
            return
        _snap_events.put((x, y))
        if _ctrl_held:
            _events.put(("group_click", x, y))
        else:
            _events.put((x, y))


def _on_key_release(key):
    global _ctrl_held
    if key in _CTRL_KEYS:
        _ctrl_held = False
        # Ctrl up — close the group if anything was captured while it
        # was held. Empty release is a no-op.
        if _group_buffer:
            _events.put(("finalize_group",))


# --- Session lifecycle ------------------------------------------------------


def _build_session_block():
    """Return the multi-line clipboard block for the captures collected
    this session. APPS dict + grouped constants by window. Used by both
    the normal session-end and recovery emit paths."""
    if not _captures:
        return ""

    apps_pairs = []
    for name in sorted(_windows, key=lambda n: _windows[n]["first_seen_idx"]):
        meta = _windows[name]
        if meta["is_app"]:
            apps_pairs.append((name, meta["spec"]))

    lines = []
    if apps_pairs:
        rendered = ", ".join(f'"{n}": "{s}"' for n, s in apps_pairs)
        lines.append(f"APPS = {{{rendered}}}")
        lines.append("")

    by_window = {}
    unbound = []
    for cap in _captures:
        wn = cap.get("window_name") or ""
        if wn:
            by_window.setdefault(wn, []).append(cap)
        else:
            unbound.append(cap)

    def _render_group(window, caps):
        if window:
            lines.append(f"# --- {window} ---")
        # Width is computed across single captures only — group
        # captures span their own multi-line block, so they don't
        # share the column with `NAME = "id"  # label` lines.
        singles = [c for c in caps if c.get("kind") != "group"
                   and c.get("final_name")]
        width = max((len(c["final_name"]) for c in singles), default=0)
        for cap in caps:
            final = cap.get("final_name")
            if not final:
                continue
            if cap.get("kind") == "group":
                _render_group_capture(cap)
                continue
            label = _readable_label(cap)
            web_selector = cap.get("web_selector")
            if web_selector:
                # Web capture with a stable CSS selector — preferred locator.
                lines.append(f'{final:<{width}} = "{web_selector}"  # {label}')
            elif cap.get("web_capture"):
                # Web capture but no usable selector — emit struct_id and
                # warn the user that this snippet is brittle.
                lines.append(
                    f'{final:<{width}} = "{cap["struct_id"]}"  # {label}'
                    f'  (no stable web selector — DevTools may help)'
                )
            else:
                # Native capture — struct_id as today.
                lines.append(f'{final:<{width}} = "{cap["struct_id"]}"  # {label}')
        lines.append("")

    def _render_group_capture(cap):
        """Render a Ctrl+click multi-select capture as a list literal:

            GROUP_NAME = [
                "0.0.0",   # Save
                "0.0.1",   # Cancel
            ]

        Members keep their per-element struct_id and a short label so
        the array is self-documenting in the user's source file.
        """
        final = cap["final_name"]
        members = cap["members"]
        lines.append(f"{final} = [")
        # Width over the quoted struct_id strings so the comments line up.
        id_width = max(len(f'"{m["struct_id"]}",') for m in members)
        for m in members:
            quoted = f'"{m["struct_id"]}",'
            label_bits = []
            if m.get("control_type"):
                label_bits.append(m["control_type"])
            if m.get("name"):
                label_bits.append(f'"{m["name"]}"')
            label = " ".join(label_bits) if label_bits else m["struct_id"]
            lines.append(f"    {quoted:<{id_width}}  # {label}")
        lines.append("]")

    if unbound:
        _render_group("", unbound)
    for name in sorted(by_window, key=lambda n: _windows.get(n, {}).get("first_seen_idx", 0)):
        _render_group(name, by_window[name])

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _persist_fingerprints():
    """Write `data/window_fingerprints/<name>.json` for every registered
    window with a captured fingerprint. Only called at session end —
    a Ctrl+C-cancelled session leaves no sidecars behind."""
    written = []
    for name, meta in _windows.items():
        fp = meta.get("fingerprint")
        if not fp:
            continue
        try:
            tree.save_fingerprint(
                name, fp,
                hints={
                    "title_hint": meta.get("title_hint", ""),
                    "is_app": meta.get("is_app", False),
                    "spec": meta.get("spec"),
                },
            )
            written.append(name)
        except Exception as e:
            _emit(f"inspector: failed to save fingerprint {name}: {e}")
    return written


def _emit_session_end():
    if _pending_name is not None:
        _finalize_prompt()

    if not _captures:
        print()
        print("No captures this session.")
        return

    written = _persist_fingerprints()
    block = _build_session_block()

    try:
        pyperclip.copy(block)
        copied = True
    except Exception as e:
        copied = False
        print(f"inspector: clipboard copy failed ({e})")

    print()
    if copied:
        print(f"[OK] {len(_captures)} captures copied to clipboard. "
              f"Paste into run.py with Ctrl+V.")
    else:
        print(f"[!] {len(_captures)} captures collected but clipboard write "
              f"failed. Lines below:")
    if written:
        print(f"     fingerprints saved: {', '.join(written)}")
    print()
    print(block)


def run(scope=None):
    """Capture mode. `scope` is accepted for backward compat but ignored —
    the inspector no longer locks to a process. Pass `--recover` instead
    to enter recovery mode."""
    global _log_file, _snippets_file, _step_counter

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STEPS_DIR.mkdir(parents=True, exist_ok=True)
    _SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    _FINGERPRINTS_DIR.mkdir(parents=True, exist_ok=True)

    _step_counter = 0

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _snippets_file = _SNIPPETS_DIR / f"session_{timestamp}.py"

    os.system("")  # enable VT processing on cmd.exe

    print("Inspector running (multi-app mode).")
    print("  Hover over an element + press MMB or F8 -> COMMIT.")
    print("  Press again on same element (or descendant) -> full info dump.")
    print("  Hold Ctrl + MMB across many elements -> group capture "
          "as NAME = [...].")
    print("  F2 -> colour sampler: opens Win+Shift+S; drag a snip, "
          "get the top colours.")
    print("  Inspect across multiple apps freely; APPS dict generated at end.")
    print("  Ctrl+C to end and copy all captures to clipboard.")
    print(f"Session log    : {_LOG_PATH}")
    print(f"Snippets file  : {_snippets_file}")

    with open(_LOG_PATH, "w", encoding="utf-8") as f:
        _log_file = f

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        snap_thread = threading.Thread(target=_snap_worker, daemon=True)
        snap_thread.start()

        mouse_listener = mouse.Listener(on_click=_on_click)
        keyboard_listener = keyboard.Listener(
            on_press=_on_key_press, on_release=_on_key_release,
        )
        mouse_listener.start()
        keyboard_listener.start()

        try:
            mouse_listener.join()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                mouse_listener.stop()
            except Exception:
                pass
            try:
                keyboard_listener.stop()
            except Exception:
                pass
            _events.put(None)
            _snap_events.put(None)
            worker_thread.join(timeout=2)
            snap_thread.join(timeout=2)
            _emit_session_end()
            _log_file = None


# --- Recovery mode ----------------------------------------------------------

_CONST_RE = re.compile(
    r'^(?P<name>[A-Z_][A-Z0-9_]*)\s*=\s*"(?P<sid>[\d.]+)"\s*(?:#\s*(?P<label>.*))?$'
)
_HEADER_RE = re.compile(r'^#\s*---\s*(?P<window>[a-z0-9_]+)\s*---\s*$')
_APPS_RE = re.compile(r'^APPS\s*=\s*(?P<dict>\{.*\})\s*$')


def _parse_session_file(path):
    """Parse a session-py sidecar into a dict:
        {"apps": {name: spec, ...},
         "windows": {name: [{"name": str, "struct_id": str, "label": str}, ...]}}
    Tolerates the structured grouped-by-window format produced by
    `_build_session_block`. Constants without a preceding header are
    grouped under "" (unbound)."""
    apps = {}
    windows = {"": []}
    current = ""
    if not path.exists():
        return {"apps": apps, "windows": windows}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        m = _APPS_RE.match(s)
        if m:
            try:
                # Restrict eval to literals.
                import ast
                apps = ast.literal_eval(m.group("dict"))
            except Exception:
                pass
            continue
        m = _HEADER_RE.match(s)
        if m:
            current = m.group("window")
            windows.setdefault(current, [])
            continue
        m = _CONST_RE.match(s)
        if m:
            windows.setdefault(current, []).append({
                "name": m.group("name"),
                "struct_id": m.group("sid"),
                "label": (m.group("label") or "").strip(),
            })
    return {"apps": apps, "windows": windows}


def _latest_session_file():
    if not _SNIPPETS_DIR.exists():
        return None
    candidates = sorted(_SNIPPETS_DIR.glob("session_*.py"))
    return candidates[-1] if candidates else None


def _find_live_window(saved_fp, restrict_pid=None):
    """Walk all visible top-level HWNDs, score against `saved_fp`.
    Returns (control, score) for the best match, or (None, 0.0)."""
    best, best_score = None, -1.0
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    hwnds = []

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    for hwnd in hwnds:
        try:
            ctrl = auto.ControlFromHandle(hwnd)
        except Exception:
            continue
        if ctrl is None:
            continue
        try:
            fp = tree.fingerprint(ctrl)
        except Exception:
            continue
        if not fp:
            continue
        s = tree.similarity(saved_fp, fp)
        if s > best_score:
            best, best_score = ctrl, s
    return best, max(best_score, 0.0)


def _recover():
    """Walk the latest session's saved data; for each window, find the
    live equivalent and update its fingerprint; for each element, run
    `find_or_heal` and update struct_id; on either miss, prompt the user.

    Writes the refreshed values back as a paste-ready block, exactly
    like a normal session end.
    """
    global _captures, _windows, _stems_seen, _window_by_hwnd, _used_names

    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STEPS_DIR.mkdir(parents=True, exist_ok=True)
    _SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    _FINGERPRINTS_DIR.mkdir(parents=True, exist_ok=True)

    latest = _latest_session_file()
    if latest is None:
        print("Recovery mode: no session sidecar found in "
              f"{_SNIPPETS_DIR}. Capture once first.")
        return

    print(f"Recovery mode: reading {latest}")
    parsed = _parse_session_file(latest)
    apps = parsed["apps"]
    by_window = parsed["windows"]

    # Reset session state — recovery emits a fresh block at the end.
    _captures.clear()
    _windows.clear()
    _stems_seen.clear()
    _window_by_hwnd.clear()
    _used_names.clear()

    # Resolve each window first.
    live_windows = {}  # window_name -> live Control
    threshold = config.FINGERPRINT_THRESHOLD
    relaxed = config.FINGERPRINT_RECOVERY_THRESHOLD

    for name, entries in by_window.items():
        if not name and not entries:
            continue
        saved_fp = tree.load_fingerprint(name) if name else None
        if saved_fp is None:
            print(f"  [{name or 'unbound'}] no fingerprint on disk — skipping window match")
            continue

        # Filter candidate enumeration to the app's process if the app
        # spec is known. For popups (no spec) we scan everything.
        spec = apps.get(name)
        restrict_pid = None
        if spec:
            stem = spec.split(".")[0].lower() if "." in spec else spec.lower()
            for proc in psutil.process_iter(["name"]):
                try:
                    if (proc.info.get("name") or "").rsplit(".", 1)[0].lower() == stem:
                        restrict_pid = proc.pid
                        break
                except Exception:
                    continue

        live, score = _find_live_window(saved_fp, restrict_pid=restrict_pid)
        if live is not None and score >= threshold:
            print(f"  [{name}] auto-matched (score {score:.2f})")
            live_windows[name] = live
            new_fp = tree.fingerprint(live)
            tree.save_fingerprint(name, new_fp, hints={"title_hint": live.Name or "",
                                                       "is_app": name in apps,
                                                       "spec": spec})
            _windows[name] = {
                "hwnd": live.NativeWindowHandle,
                "is_app": name in apps,
                "spec": spec,
                "title_hint": live.Name or "",
                "fingerprint": new_fp,
                "first_seen_idx": len(_windows),
            }
            if name in apps:
                _stems_seen[stem] = name
        elif live is not None and score >= relaxed:
            print(f"  [{name}] ambiguous (score {score:.2f}). "
                  "Skipping — re-run inspector if this window has changed shape.")
        else:
            print(f"  [{name}] no live candidate above {relaxed:.2f} "
                  f"(best {score:.2f}). Skipping.")

    # Element-level recovery: try find_or_heal for each saved element.
    # Windows without a matched live counterpart still pass through —
    # their saved struct_ids are kept as-is (recovery degrades to a
    # no-op rather than dropping data).
    for window_name, entries in by_window.items():
        if not entries:
            continue
        win = live_windows.get(window_name) if window_name else None
        snap = tree.load_snapshot(win) if win is not None else None
        walked = tree.walk_live(win) if win is not None else None
        # Re-register the window so the emitted block keeps grouping
        # even when fingerprint match was skipped.
        if window_name and window_name not in _windows:
            _windows[window_name] = {
                "hwnd": 0,
                "is_app": window_name in apps,
                "spec": apps.get(window_name),
                "title_hint": "",
                "fingerprint": None,
                "first_seen_idx": len(_windows),
            }
        for entry in entries:
            updated_struct = entry["struct_id"]
            healed = False
            if walked is not None and snap is not None:
                ctrl, was_healed = tree.find_or_heal(
                    walked, entry["struct_id"], snap,
                )
                if ctrl is not None:
                    new_node = next(
                        (n for n in walked if n["ctrl"] is ctrl), None,
                    )
                    if new_node is not None:
                        updated_struct = new_node["struct_id"]
                        healed = True
            if healed:
                print(f"    {entry['name']}: {entry['struct_id']} -> "
                      f"{updated_struct}{' (healed)' if updated_struct != entry['struct_id'] else ''}")
            else:
                print(f"    {entry['name']}: kept {entry['struct_id']} "
                      "(no heal — re-run inspector if broken)")
            _captures.append({
                "struct_id": updated_struct,
                "name_path": "",
                "name": entry["label"],
                "control_type": "",
                "class_name": "",
                "automation_id": "",
                "bbox": None,
                "bbox_center": (None, None),
                "color": None,
                "window_name": window_name,
                "interactable_ancestor": None,
                "default_name": entry["name"],
                "final_name": entry["name"],
                "screenshot_path": None,
            })
            _used_names.add(entry["name"])

    block = _build_session_block()
    if block:
        try:
            pyperclip.copy(block)
            print()
            print(f"[OK] recovery: refreshed {len(_captures)} entries, "
                  "copied to clipboard.")
        except Exception:
            print()
            print("[!] clipboard copy failed; block follows:")
        print()
        print(block)
    else:
        print("Recovery mode: nothing to emit.")


def _parse_args():
    p = argparse.ArgumentParser(
        description="Hover-and-press inspector. Multi-app capture mode "
                    "(default) or --recover to refresh a previous session."
    )
    p.add_argument(
        "process",
        nargs="?",
        help="(documentation only — the inspector no longer locks scope)",
    )
    p.add_argument(
        "--recover",
        action="store_true",
        help="Recovery mode: re-validate the last session's fingerprints "
             "and struct_ids against the current UI; emit a refreshed "
             "paste-ready block.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.recover:
        _recover()
    else:
        run(scope=args.process)
