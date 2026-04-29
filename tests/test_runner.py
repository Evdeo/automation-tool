"""Unit tests for core/runner.py — multiprocess watchdog supervision.

These don't need a UI; they exercise the supervisor with trivial target
functions (sleep, return) and assert the watchdog table records the
expected state transitions.
"""
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import db, runner  # noqa: E402


def _quick_target():
    """Exits cleanly within 1 second."""
    time.sleep(0.3)


def _slow_target():
    """Exceeds the 0.1-minute (6s) watchdog timeout used by these tests."""
    time.sleep(30)


class TestRunOnceWithWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runner_test_"))
        self._orig_db = config.DB_PATH
        self._orig_timeout = config.LOOP_TIMEOUT_MIN
        config.DB_PATH = str(self.tmp / "runs.db")
        db._known_tables.clear()

    def tearDown(self):
        config.DB_PATH = self._orig_db
        config.LOOP_TIMEOUT_MIN = self._orig_timeout
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _watchdog_rows(self):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            return conn.execute("SELECT * FROM watchdog ORDER BY ts").fetchall()
        finally:
            conn.close()

    def test_clean_exit_returns_exited_and_exitcode_zero(self):
        result = runner.run_once_with_watchdog(_quick_target, timeout_min=1)
        self.assertEqual(result, ("exited", 0))
        rows = self._watchdog_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "started")
        self.assertEqual(rows[1][2], "exited")
        # exitcode column stores native int (0), not the string '0'
        self.assertEqual(rows[1][3], 0)

    def test_timeout_kills_child_and_records_killed_timeout(self):
        # 0.1 minutes = 6 seconds; _slow_target sleeps 30s.
        result = runner.run_once_with_watchdog(_slow_target, timeout_min=0.1)
        kind, exitcode = result
        self.assertEqual(kind, "killed_timeout")
        self.assertIsNone(exitcode)
        rows = self._watchdog_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "started")
        self.assertEqual(rows[1][2], "killed_timeout")

    def test_default_timeout_uses_config_loop_timeout_min(self):
        # Set config to 1 minute, run a quick target — should not timeout.
        config.LOOP_TIMEOUT_MIN = 1
        result = runner.run_once_with_watchdog(_quick_target)
        self.assertEqual(result[0], "exited")

    def test_kill_on_timeout_runs_cleanup_after_kill(self):
        # 0.1 minutes = 6s; _slow_target sleeps 30s -> guaranteed timeout.
        # Pass a name that won't have any running processes — close_app
        # returns 0, but we still expect the "closed:..." log row.
        result = runner.run_once_with_watchdog(
            _slow_target,
            timeout_min=0.1,
            kill_on_timeout=["__nonexistent_process_xyz.exe"],
        )
        self.assertEqual(result[0], "killed_timeout")
        rows = self._watchdog_rows()
        events = [r[2] for r in rows]
        # Schema: started -> killed_timeout -> closed:<name>
        self.assertEqual(events[0], "started")
        self.assertEqual(events[1], "killed_timeout")
        self.assertEqual(events[2], "closed:__nonexistent_process_xyz.exe")
        # exitcode column for the cleanup row holds the close_app count (0)
        self.assertEqual(rows[2][3], 0)

    def test_kill_on_timeout_skipped_on_clean_exit(self):
        # If the target exits cleanly, the cleanup hook must NOT fire.
        result = runner.run_once_with_watchdog(
            _quick_target,
            timeout_min=1,
            kill_on_timeout=["__nonexistent_process_xyz.exe"],
        )
        self.assertEqual(result[0], "exited")
        rows = self._watchdog_rows()
        events = [r[2] for r in rows]
        self.assertEqual(events, ["started", "exited"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
