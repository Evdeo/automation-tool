from pathlib import Path

TARGET_WINDOW_TITLE = "Notepad"

DB_PATH = "data/runs.db"

TREE_SNAPSHOT_DIR = Path("data/snapshots")

# Output for the demo's save step. run.py builds the full path as
# RESULTS_DIR / "<timestamp>" / RESULT_FILENAME, so each pass writes
# into its own dated folder. Filename stays generic so downstream
# tooling can find "the data file" without parsing names.
RESULTS_DIR = Path("data/results")
RESULT_FILENAME = "data.txt"

LOOP_TIMEOUT_MIN = 15

DRIFT_RETRY_BACKOFF_SEC = 0.2

RESOLVE_TIMEOUT_SEC = 10

ACTIVE_POLL_SEC = 0.1

DASHBOARD_REFRESH_SEC = 5
