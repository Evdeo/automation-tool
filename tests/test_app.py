"""Unit tests for `core.app.match` — the unified window locator.

`match(name, launch=...)` is the single user-facing entry point and
the inspector emits fingerprint sidecars for it. `launch` is required:

  * `launch="<exe path>"` — already-open fast path; if no candidate
    scores above threshold, run the exe and poll until one appears
    (or `timeout` elapses).
  * `launch="popup"` — temporal mode; only score HWNDs that appeared
    since the previous verb call (read from the verb-level baseline).

Both paths return `Control | None` and never raise — the canonical
error-handling pattern is `if match(...)`.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import app, verbs  # noqa: E402


class _FakeCtrl:
    """Stand-in for `auto.Control`. Only attributes match() touches are
    needed: the fingerprint comparison is mocked at the
    `core.tree.fingerprint` level, so the control is opaque."""

    def __init__(self, hwnd=1, pid=1234, name=""):
        self.NativeWindowHandle = hwnd
        self.ProcessId = pid
        self.Name = name


class _FingerprintEnv(unittest.TestCase):
    """Common setup for tests that load fingerprints from disk."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="app_test_"))
        self._orig_dir = config.WINDOW_FINGERPRINT_DIR
        config.WINDOW_FINGERPRINT_DIR = self.tmp
        # Clear verb-level state so popup-mode tests start fresh.
        self._saved_expected = set(verbs._expected_hwnds)
        self._saved_baseline = set(verbs._hwnd_baseline_set)
        verbs._expected_hwnds.clear()
        verbs._hwnd_baseline_set.clear()

    def tearDown(self):
        config.WINDOW_FINGERPRINT_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        verbs._expected_hwnds.clear()
        verbs._expected_hwnds.update(self._saved_expected)
        verbs._hwnd_baseline_set.clear()
        verbs._hwnd_baseline_set.update(self._saved_baseline)

    def _save_fp(self, name, fp):
        from core import tree
        tree.save_fingerprint(name, fp)


class TestMatchExeMode(_FingerprintEnv):
    """`launch="<exe>"` — find-or-launch path."""

    def test_returns_window_above_threshold(self):
        # Saved fingerprint matches one candidate exactly; another is
        # disjoint. match() returns the matching control.
        self._save_fp("notepad", [(0, "WindowControl"),
                                  (1, "ButtonControl")])
        winners = _FakeCtrl(hwnd=1, name="primary")
        loser = _FakeCtrl(hwnd=2, name="other")
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([loser, winners])), \
             mock.patch("core.tree.fingerprint",
                        side_effect=[[(0, "PaneControl")],
                                     [(0, "WindowControl"),
                                      (1, "ButtonControl")]]), \
             mock.patch.object(app.subprocess, "Popen") as mpopen:
            result = app.match("notepad", launch="notepad.exe")
        self.assertIs(result, winners)
        # Fast-path: already-open window means Popen never runs.
        mpopen.assert_not_called()

    def test_returns_none_when_no_sidecar(self):
        # No fingerprint file → match returns None silently. Doesn't
        # even try to launch.
        with mock.patch.object(app.subprocess, "Popen") as mpopen:
            result = app.match("never_inspected", launch="some.exe")
        self.assertIsNone(result)
        mpopen.assert_not_called()

    def test_launches_exe_when_no_candidate_open(self):
        # No live candidate at first; launch is provided. match()
        # invokes subprocess.Popen and retries until a window appears.
        self._save_fp("notepad", [(0, "WindowControl"),
                                  (1, "ButtonControl")])
        winner = _FakeCtrl(hwnd=99)
        # First scan: empty. Second scan (post-launch): winner.
        scan_results = [iter([]), iter([winner])]
        with mock.patch.object(app, "_candidate_controls",
                               side_effect=lambda **kw: scan_results.pop(0)), \
             mock.patch("core.tree.fingerprint",
                        return_value=[(0, "WindowControl"),
                                      (1, "ButtonControl")]), \
             mock.patch.object(app.subprocess, "Popen") as mpopen:
            result = app.match("notepad", launch="notepad.exe", timeout=2.0)
        self.assertIs(result, winner)
        mpopen.assert_called_once_with("notepad.exe", shell=False)

    def test_returns_none_when_window_never_appears(self):
        self._save_fp("notepad", [(0, "WindowControl")])
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([])), \
             mock.patch.object(app.subprocess, "Popen"):
            result = app.match("notepad", launch="notepad.exe", timeout=0.3)
        self.assertIsNone(result)

    def test_returns_none_when_popen_raises(self):
        # If subprocess.Popen blows up (bad path, perm denied, etc.)
        # match returns None — caller decides what to do, no raise.
        self._save_fp("notepad", [(0, "WindowControl")])
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([])), \
             mock.patch.object(app.subprocess, "Popen",
                               side_effect=FileNotFoundError):
            self.assertIsNone(app.match("notepad", launch="bogus.exe",
                                        timeout=0.5))

    def test_picks_best_scorer_among_multiple(self):
        # Three candidates with varying scores; best wins.
        self._save_fp("notepad", [(0, "A"), (1, "B"), (1, "C"), (2, "D")])
        c1, c2, c3 = _FakeCtrl(hwnd=1), _FakeCtrl(hwnd=2), _FakeCtrl(hwnd=3)
        # c1: 1/4 = 0.25 (just A)
        # c2: 4/4 = 1.0 (perfect) — wins
        # c3: 2/4 = 0.5 (A + B)
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([c1, c2, c3])), \
             mock.patch("core.tree.fingerprint",
                        side_effect=[
                            [(0, "A")],
                            [(0, "A"), (1, "B"), (1, "C"), (2, "D")],
                            [(0, "A"), (1, "B")],
                        ]), \
             mock.patch.object(app.subprocess, "Popen"):
            result = app.match("notepad", launch="notepad.exe")
        self.assertIs(result, c2)

    def test_never_raises_on_uia_failure(self):
        # If a candidate's fingerprint computation throws, match() skips
        # it and continues — it doesn't blow up the caller.
        self._save_fp("popup", [(0, "X"), (1, "Y")])
        good = _FakeCtrl(hwnd=2)
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([_FakeCtrl(hwnd=1), good])), \
             mock.patch("core.tree.fingerprint",
                        side_effect=[RuntimeError("UIA blew up"),
                                     [(0, "X"), (1, "Y")]]), \
             mock.patch.object(app.subprocess, "Popen"):
            result = app.match("popup", launch="any.exe")
        self.assertIs(result, good)

    def test_match_returns_register_hwnd_as_expected(self):
        # When match returns a Control, its HWND must be added to
        # `verbs._expected_hwnds` so subsequent verbs don't dismiss it.
        self._save_fp("notepad", [(0, "WindowControl")])
        winner = _FakeCtrl(hwnd=4242)
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([winner])), \
             mock.patch("core.tree.fingerprint",
                        return_value=[(0, "WindowControl")]), \
             mock.patch.object(app.subprocess, "Popen"):
            app.match("notepad", launch="notepad.exe")
        self.assertIn(4242, verbs._expected_hwnds)


class TestMatchExeModeFastPath(_FingerprintEnv):
    """When the target is already open, match returns immediately —
    Popen is never called even if it's still in the args."""

    def test_skips_launch_when_window_already_present(self):
        self._save_fp("notepad", [(0, "WindowControl")])
        existing = _FakeCtrl(hwnd=10)
        with mock.patch.object(app, "_candidate_controls",
                               return_value=iter([existing])), \
             mock.patch("core.tree.fingerprint",
                        return_value=[(0, "WindowControl")]), \
             mock.patch.object(app.subprocess, "Popen") as mpopen, \
             mock.patch("time.sleep") as msleep:
            result = app.match("notepad", launch="notepad.exe", timeout=10.0)
        self.assertIs(result, existing)
        mpopen.assert_not_called()
        msleep.assert_not_called()


class TestMatchPopupMode(_FingerprintEnv):
    """`launch="popup"` — temporal mode: only score HWNDs that appeared
    since the previous verb call. Does NOT spawn a subprocess."""

    def test_only_new_hwnds_are_considered(self):
        # Baseline (set by the previous verb call) had {1, 2}. Live
        # enum now returns {1, 2, 3} — only HWND 3 is new.
        self._save_fp("save_dlg", [(0, "WindowControl"),
                                   (1, "EditControl")])
        verbs._hwnd_baseline_set.update({1, 2})
        new_ctrl = _FakeCtrl(hwnd=3)

        captured_filter = {}

        def fake_score(expected_fp, *, restrict_pid=None, parent=None,
                       hwnd_filter=None, threshold=None):
            captured_filter["filter"] = hwnd_filter
            return new_ctrl

        with mock.patch.object(app, "_enumerate_top_level_hwnds",
                               return_value=[1, 2, 3]), \
             mock.patch.object(app, "_score_candidates",
                               side_effect=fake_score), \
             mock.patch.object(app.subprocess, "Popen") as mpopen:
            result = app.match("save_dlg", launch="popup")
        self.assertIs(result, new_ctrl)
        # No subprocess in popup mode.
        mpopen.assert_not_called()
        # The hwnd_filter passed to scorer accepts only HWNDs not in the
        # baseline.
        f = captured_filter["filter"]
        self.assertTrue(f(3))
        self.assertFalse(f(1))
        self.assertFalse(f(2))

    def test_returns_none_when_no_new_hwnds(self):
        self._save_fp("save_dlg", [(0, "WindowControl")])
        verbs._hwnd_baseline_set.update({1, 2, 3})
        with mock.patch.object(app, "_enumerate_top_level_hwnds",
                               return_value=[1, 2, 3]), \
             mock.patch.object(app, "_score_candidates") as msc, \
             mock.patch.object(app.subprocess, "Popen"):
            result = app.match("save_dlg", launch="popup")
        self.assertIsNone(result)
        # No new HWNDs → the scorer is never even called.
        msc.assert_not_called()

    def test_no_sidecar_returns_none_in_popup_mode_too(self):
        # Same silent-fail contract regardless of `launch`.
        with mock.patch.object(app.subprocess, "Popen") as mpopen:
            self.assertIsNone(app.match("never_seen", launch="popup"))
        mpopen.assert_not_called()

    def test_popup_match_marks_hwnd_expected(self):
        # On a successful popup match, the HWND joins `_expected_hwnds`
        # so the next verb's pre-dismiss leaves it alone.
        self._save_fp("save_dlg", [(0, "WindowControl")])
        verbs._hwnd_baseline_set.update({1})
        new_ctrl = _FakeCtrl(hwnd=99)
        with mock.patch.object(app, "_enumerate_top_level_hwnds",
                               return_value=[1, 99]), \
             mock.patch.object(app, "_score_candidates",
                               return_value=new_ctrl):
            app.match("save_dlg", launch="popup")
        self.assertIn(99, verbs._expected_hwnds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
