from datetime import datetime
from pathlib import Path

DB_PATH = "data/runs.db"

TREE_SNAPSHOT_DIR = Path("data/snapshots")

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
