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
from unittest import mock

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


class TestRunWithWatchdogErrorRouting(unittest.TestCase):
    """`run_with_watchdog` switches between `test_loop` and `error_loop`
    based on each iteration's outcome. We mock `_supervise` to feed it
    a scripted sequence of outcomes, then break the otherwise-infinite
    loop when the script is exhausted."""

    def _route(self, outcomes, error_loop="error"):
        targets_seen = []
        outcomes_iter = iter(outcomes)

        def fake_supervise(target, *_a, **_kw):
            targets_seen.append(target)
            try:
                return next(outcomes_iter)
            except StopIteration:
                raise _StopLoop()

        with mock.patch.object(runner, "_supervise",
                               side_effect=fake_supervise):
            try:
                runner.run_with_watchdog("normal", error_loop=error_loop)
            except _StopLoop:
                pass
        return targets_seen

    def test_clean_exit_keeps_normal_target(self):
        # iter 1 = normal → clean → iter 2 = normal → clean → iter 3 starts normal.
        targets = self._route([("exited", 0), ("exited", 0)])
        self.assertEqual(targets, ["normal", "normal", "normal"])

    def test_timeout_switches_to_error_target(self):
        targets = self._route([
            ("exited", 0),               # iter 1 clean
            ("killed_timeout", None),    # iter 2 timeout → next is error
            ("exited", 0),               # iter 3 clean → next is normal
        ])
        self.assertEqual(targets, ["normal", "normal", "error", "normal"])

    def test_nonzero_exit_switches_to_error_target(self):
        targets = self._route([("exited", 1)])
        self.assertEqual(targets, ["normal", "error"])

    def test_default_error_loop_falls_back_to_test_loop(self):
        # error_loop=None → on failure, re-run test_loop ("normal").
        targets = self._route(
            [("killed_timeout", None), ("exited", 0)],
            error_loop=None,
        )
        self.assertEqual(targets, ["normal", "normal", "normal"])


class _StopLoop(Exception):
    """Sentinel for breaking out of run_with_watchdog's `while True`."""


class TestStartValidatesStates(unittest.TestCase):
    """`start()` validates both `start_state` and `error_state` against
    the STATES dict before doing any process work."""

    def _states(self):
        return {"a": lambda d: (None, d), "b": lambda d: (None, d)}

    def test_unknown_start_state_raises(self):
        with self.assertRaisesRegex(ValueError, "start_state="):
            runner.start(self._states(), apps={}, start_state="missing")

    def test_unknown_error_state_raises(self):
        with self.assertRaisesRegex(ValueError, "error_state="):
            runner.start(self._states(), apps={}, start_state="a",
                         error_state="missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
