"""Window-locator: `match(name, launch=...)` is the single user entry.

`launch="<exe>"` finds an open window matching the saved fingerprint;
if none, runs the exe and waits for a new window to appear.

`launch="popup"` looks for a top-level HWND that appeared since the
last verb call (temporal detection) and scores it against the saved
fingerprint.

Returns `Control | None` — never raises, so `if match(...)` is the
canonical error-handling pattern.
"""
import ctypes
import subprocess
import time
from ctypes import wintypes
from typing import Optional

import uiautomation as auto

import config


_user32 = ctypes.windll.user32
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _exe_stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _enumerate_top_level_hwnds():
    """Visible top-level HWNDs in EnumWindows order."""
    out = []

    def cb(hwnd, _lp):
        if _user32.IsWindowVisible(hwnd):
            out.append(hwnd)
        return True

    _user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return out


def _candidate_controls(restrict_pid=None, parent=None):
    """Yield candidate `Control`s for fingerprint scoring.

    - Every visible top-level HWND, optionally filtered to `restrict_pid`.
    - If `parent` provided, also yield direct UIA children of role
      Window/Pane/ContentDialog (covers WinUI in-window popups).
    """
    seen = set()
    for hwnd in _enumerate_top_level_hwnds():
        if restrict_pid is not None:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != restrict_pid:
                continue
        try:
            ctrl = auto.ControlFromHandle(hwnd)
        except Exception:
            continue
        if ctrl is None:
            continue
        seen.add(hwnd)
        yield ctrl

    if parent is not None:
        in_window_roles = {
            "WindowControl", "PaneControl", "ContentDialogControl",
        }
        try:
            for child in parent.GetChildren():
                if child.ControlTypeName in in_window_roles:
                    try:
                        h = child.NativeWindowHandle
                    except Exception:
                        h = 0
                    if h and h in seen:
                        continue
                    yield child
        except Exception:
            pass


def _score_candidates(expected_fp, *, restrict_pid=None, parent=None,
                      hwnd_filter=None, threshold=None):
    """Walk candidates, return the highest scorer above threshold or
    None. `hwnd_filter` (callable hwnd→bool) restricts the candidate
    set further (used for popup mode's "new since baseline" filter).
    """
    from core import tree
    if threshold is None:
        threshold = config.FINGERPRINT_THRESHOLD
    best, best_score = None, -1.0
    for cand in _candidate_controls(restrict_pid=restrict_pid, parent=parent):
        if hwnd_filter is not None:
            try:
                if not hwnd_filter(cand.NativeWindowHandle):
                    continue
            except Exception:
                continue
        try:
            fp = tree.fingerprint(cand)
        except Exception:
            continue
        if not fp:
            continue
        s = tree.similarity(expected_fp, fp)
        if s > best_score:
            best, best_score = cand, s
    if best is not None and best_score >= threshold:
        return best
    return None


def find(name: str, restrict_pid: Optional[int] = None,
         parent=None) -> Optional["auto.Control"]:
    """Match an already-open window by saved fingerprint. No launch,
    no popup-baseline check — just score the live top-level windows
    against the fingerprint and return the best fit (or None).

    Used by `core.window.get()` for the "find existing only" case.
    """
    from core import tree
    expected_fp = tree.load_fingerprint(name)
    if expected_fp is None:
        return None
    hit = _score_candidates(expected_fp, restrict_pid=restrict_pid,
                            parent=parent)
    if hit is not None:
        from core import verbs as verbs_mod
        verbs_mod._mark_hwnd_expected(hit.NativeWindowHandle)
    return hit


def match(name: str, launch: str, timeout: float = 15.0,
          restrict_pid: Optional[int] = None,
          parent=None) -> Optional["auto.Control"]:
    """Locate a live window for `name`. `launch` is required.

    `launch="<exe path>"` — already-open fast path: if any visible
    window scores above threshold against the saved fingerprint,
    return it. Otherwise launch the exe and poll until a new matching
    window appears (or `timeout` elapses).

    `launch="popup"` — temporal mode: only scores HWNDs that appeared
    since the previous verb call (read from the verb-level baseline).
    Returns `None` if nothing new matches.

    Returns `None` on failure — never raises.
    """
    from core import tree
    expected_fp = tree.load_fingerprint(name)
    if expected_fp is None:
        return None

    if launch == "popup":
        # `core.verbs` maintains the last-verb-call HWND baseline.
        from core import verbs as verbs_mod
        baseline = verbs_mod._hwnd_baseline_snapshot()
        new_hwnds = set(_enumerate_top_level_hwnds()) - baseline
        if not new_hwnds:
            return None
        hit = _score_candidates(
            expected_fp,
            restrict_pid=restrict_pid,
            parent=parent,
            hwnd_filter=lambda h: h in new_hwnds,
        )
        if hit is not None:
            # Register as expected so subsequent verbs don't dismiss it.
            verbs_mod._mark_hwnd_expected(hit.NativeWindowHandle)
        return hit

    # launch is an exe path — fast path then launch fallback.
    hit = _score_candidates(expected_fp, restrict_pid=restrict_pid,
                            parent=parent)
    if hit is not None:
        from core import verbs as verbs_mod
        verbs_mod._mark_hwnd_expected(hit.NativeWindowHandle)
        return hit

    # Launch and poll.
    try:
        subprocess.Popen(launch, shell=False)
    except Exception:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = _score_candidates(expected_fp, restrict_pid=restrict_pid,
                                parent=parent)
        if hit is not None:
            from core import verbs as verbs_mod
            verbs_mod._mark_hwnd_expected(hit.NativeWindowHandle)
            return hit
        time.sleep(config.DRIFT_RETRY_BACKOFF_SEC)
    return None
