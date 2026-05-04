"""Smoke tests for the minimal run.py demo.

Each state takes `data` (scratch) and returns `(next_state, data)`.
Live windows live on `core.window`; tests stub `window.open` and
`window.close` so the state functions don't try to spawn real
processes, and pre-populate `window._windows` with mocks so
`window.notepad` and `window.calc` resolve.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402
from core import window  # noqa: E402


class _WindowFixture(unittest.TestCase):
    """Stub `window.open`/`window.close` and pre-install mock handles
    so state functions can run without launching anything."""

    def setUp(self):
        self.notepad = mock.MagicMock(name="notepad_window")
        self.calc = mock.MagicMock(name="calc_window")
        window._windows["notepad"] = self.notepad
        window._windows["calc"] = self.calc
        self._patch_open = mock.patch.object(window, "open")
        self._patch_close = mock.patch.object(window, "close")
        self.mock_open = self._patch_open.start()
        self.mock_close = self._patch_close.start()

    def tearDown(self):
        self._patch_open.stop()
        self._patch_close.stop()
        window._reset()


class TestStateNotepad(_WindowFixture):
    def test_writes_timestamp_then_closes(self):
        data = SimpleNamespace()
        with mock.patch.object(run, "wait_visible", return_value=True), \
             mock.patch.object(run, "fill") as mfill, \
             mock.patch.object(run, "now", return_value="2026-01-01"):
            nxt, _ = run.state_notepad(data)
        self.mock_open.assert_called_once_with("notepad")
        mfill.assert_called_once_with(window.notepad, run.EDITOR,
                                      "timestamp: 2026-01-01\n")
        self.mock_close.assert_called_once_with("notepad")
        self.assertEqual(nxt, "calc")

    def test_aborts_when_notepad_unresponsive(self):
        data = SimpleNamespace()
        with mock.patch.object(run, "wait_visible", return_value=False), \
             mock.patch.object(run, "fill") as mfill, \
             mock.patch.object(run, "log") as mlog:
            nxt, _ = run.state_notepad(data)
        self.assertIsNone(nxt)
        mfill.assert_not_called()
        mlog.assert_called_once()
        self.assertEqual(mlog.call_args[0][:2],
                         ("results", "notepad_init_failed"))


class TestStateCalc(_WindowFixture):
    def test_full_compute_pipeline(self):
        data = SimpleNamespace()
        with mock.patch.object(run, "wait_visible", return_value=True), \
             mock.patch.object(run, "click_when_enabled"), \
             mock.patch.object(run, "each") as meach, \
             mock.patch.object(run, "hotkey") as mhk, \
             mock.patch.object(run, "read_clipboard", return_value="79"), \
             mock.patch.object(run, "log") as mlog:
            nxt, _ = run.state_calc(data)
        # One each(click_after, ...) call drives the full sequence.
        meach.assert_called_once()
        args, kwargs = meach.call_args
        self.assertIs(args[0], run.click_after)
        self.assertIs(args[1], window.calc)
        self.assertEqual(len(args[2]), 6)  # 4, 7, +, 3, 2, =
        self.assertEqual(kwargs.get("delay"), 0.1)
        mhk.assert_called_once_with(window.calc, "ctrl", "c")
        mlog.assert_called_once_with("results", "calc_result", "79")
        self.mock_open.assert_called_once_with("calc")
        self.mock_close.assert_called_once_with("calc")
        self.assertIsNone(nxt)

    def test_aborts_when_calc_unresponsive(self):
        data = SimpleNamespace()
        with mock.patch.object(run, "wait_visible", return_value=False), \
             mock.patch.object(run, "log") as mlog:
            nxt, _ = run.state_calc(data)
        self.assertIsNone(nxt)
        mlog.assert_called_once()
        self.assertEqual(mlog.call_args[0][:2],
                         ("results", "calc_init_failed"))


class TestWiring(unittest.TestCase):
    def test_states_registered(self):
        self.assertEqual(set(run.STATES), {"notepad", "calc"})

    def test_apps_dict_well_formed(self):
        self.assertEqual(set(run.APPS), {"notepad", "calc"})
        for path in run.APPS.values():
            self.assertTrue(path.endswith(".exe"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
