from datetime import datetime
from pathlib import Path

DB_PATH = "data/runs.db"

TREE_SNAPSHOT_DIR = Path("data/snapshots")
WINDOW_FINGERPRINT_DIR = Path("data/window_fingerprints")
INSPECTOR_STEPS_DIR = Path("data/inspector_steps")
INSPECTOR_SNIPPETS_DIR = Path("data/inspector_snippets")

FINGERPRINT_MAX_DEPTH = 4
FINGERPRINT_THRESHOLD = 0.75
FINGERPRINT_RECOVERY_THRESHOLD = 0.5

# Popup auto-dismiss config. Every action verb scans top-level HWNDs
# before running and dismisses any window not in the "expected" set.
# POPUP_DISMISS_KEY: keystroke (str, e.g. "esc" or "alt+f4") OR a
#   callable taking (hwnd) — used as the first dismiss attempt.
# POPUP_CHECK_DEEP: when True, also walk the active window's UIA tree
#   for new in-window popups (no separate HWND). ~50ms vs ~5ms.
POPUP_DISMISS_KEY = "esc"
POPUP_CHECK_DEEP = False

RESULTS_DIR = Path("data/results")
RESULT_FILENAME = "data.txt"

# Per-run output: a fresh timestamped folder so each pass writes its
# own data file. Computed at import; --loop respawns the process per
# iteration, so each iteration gets a fresh stamp.
SAVE_PATH = (
    RESULTS_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S") / RESULT_FILENAME
).resolve()

LOOP_TIMEOUT_MIN = 15

DRIFT_RETRY_BACKOFF_SEC = 0.2

RESOLVE_TIMEOUT_SEC = 10

ACTIVE_POLL_SEC = 0.1

DASHBOARD_REFRESH_SEC = 5
