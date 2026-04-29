from pathlib import Path

TARGET_WINDOW_TITLE = "Untitled - Notepad"

DB_PATH = str(Path(__file__).parent / "data" / "runs.db")

TREE_SNAPSHOT_DIR = Path(__file__).parent / "data" / "snapshots"

LOOP_TIMEOUT_MIN = 15

DRIFT_RETRY_BACKOFF_SEC = 0.2

ACTIVE_POLL_SEC = 0.1

DASHBOARD_REFRESH_SEC = 5
