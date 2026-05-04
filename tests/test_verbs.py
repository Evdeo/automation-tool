"""Unit tests for core/verbs.py — every top-level verb plus the
synchronous popup-dismiss machinery (`_expected_hwnds`, `no_dismiss`,
`_hwnd_baseline_set`, the `_action_verb` decorator).

The verbs are user-facing wrappers around `actions.*`, `app.*`, `db.*`,
`pyautogui`, `pyperclip`, and `psutil`. The behaviour worth verifying
here is the WIRING:

* the right callee receives the right arguments,
* defaults (timeouts, intervals) match the public docstring,
* return values pass through correctly,
* every action verb refreshes `_hwnd_baseline_set` and calls
  `_dismiss_unexpected_popups` before delegating.

Anything that requires a live UIA tree lives in test_integration.py.
"""

import csv
import io
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import verbs  # noqa: E402


def setUpModule():
    """Seed `_expected_hwnds` with everything currently visible so the
    pre-dismiss step inside every action verb is a no-op during these
    unit tests. Tests that exercise the dismiss machinery itself reset
    `_expected_hwnds` in their own setUp."""
    verbs._seed_expected_from_current()


class _FakeWindow:
    """Stand-in for an `auto.Control` window. Every test that hands a
    window to a verb passes one of these — verbs don't introspect the
    window, they just forward it."""

    def __init__(self, hwnd=42, pid=1234):
        self.NativeWindowHandle = hwnd
        self.ProcessId = pid


# --- Pure / near-pure verbs -------------------------------------------------


class TestNow(unittest.TestCase):
    def test_default_format_parses_back(self):
        s = verbs.now()
        # Default format is "%Y-%m-%d %H:%M:%S" — must parse cleanly.
        parsed = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        self.assertIsNotNone(parsed)

    def test_custom_format_honoured(self):
        s = verbs.now("%Y%m%d")
        self.assertEqual(len(s), 8)
        self.assertTrue(s.isdigit())

    def test_returns_close_to_real_time(self):
        s = verbs.now()
        delta = abs(
            (datetime.now() - datetime.strptime(s, "%Y-%m-%d %H:%M:%S")).total_seconds()
        )
        self.assertLess(delta, 2.0)


class TestWait(unittest.TestCase):
    def test_wait_blocks_for_requested_time(self):
        t0 = time.time()
        verbs.wait(0.2)
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.0)

    def test_wait_zero_is_essentially_instant(self):
        t0 = time.time()
        verbs.wait(0)
        self.assertLess(time.time() - t0, 0.1)


class TestReadClipboard(unittest.TestCase):
    def test_returns_pyperclip_paste(self):
        with mock.patch.object(verbs, "pyperclip") as mp:
            mp.paste.return_value = "clipboard contents"
            self.assertEqual(verbs.read_clipboard(), "clipboard contents")
        mp.paste.assert_called_once_with()


class TestLog(unittest.TestCase):
    def test_log_forwards_to_db(self):
        with mock.patch.object(verbs.db, "log") as md:
            verbs.log("results", "alpha", 1, 2.5)
        md.assert_called_once_with("results", "alpha", 1, 2.5)


# --- log_csv ----------------------------------------------------------------


class TestLogCsv(unittest.TestCase):
    """log_csv exercises file creation, header-once behaviour, JSON encoding
    of nested cells, and clipboard auto-detection of TSV/CSV/SSV."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verbs_csv_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_rows(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def test_creates_file_with_header_on_first_call(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, [1, "alpha", 3.14], header=["i", "name", "x"])
        rows = self._read_rows(path)
        self.assertEqual(rows[0], ["i", "name", "x"])
        self.assertEqual(rows[1], ["1", "alpha", "3.14"])

    def test_subsequent_calls_skip_header(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, [1, "a"], header=["i", "name"])
        verbs.log_csv(path, [2, "b"], header=["i", "name"])
        verbs.log_csv(path, [3, "c"], header=["i", "name"])
        rows = self._read_rows(path)
        self.assertEqual(rows, [
            ["i", "name"],
            ["1", "a"],
            ["2", "b"],
            ["3", "c"],
        ])

    def test_multiple_rows_in_one_call(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, [1, "a"], [2, "b"], [3, "c"])
        rows = self._read_rows(path)
        self.assertEqual(rows[0], ["1", "a"])
        self.assertEqual(rows[1], ["2", "b"])
        self.assertEqual(rows[2], ["3", "c"])

    def test_creates_parent_directories(self):
        path = self.tmp / "deep" / "nested" / "out.csv"
        verbs.log_csv(path, [1])
        self.assertTrue(path.exists())

    def test_json_encodes_list_cell(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, ["id", [1, 2, 3]])
        rows = self._read_rows(path)
        self.assertEqual(rows[0][0], "id")
        self.assertEqual(rows[0][1], "[1, 2, 3]")

    def test_json_encodes_dict_cell(self):
        import json as _j
        path = self.tmp / "out.csv"
        verbs.log_csv(path, ["k", {"a": 1, "b": 2}])
        rows = self._read_rows(path)
        self.assertEqual(_j.loads(rows[0][1]), {"a": 1, "b": 2})

    def test_json_encodes_set_cell_sorted(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, ["k", {3, 1, 2}])
        rows = self._read_rows(path)
        self.assertEqual(rows[0][1], "[1, 2, 3]")

    def test_tuple_cell_becomes_json_list(self):
        path = self.tmp / "out.csv"
        verbs.log_csv(path, ["k", (10, 20, 30)])
        rows = self._read_rows(path)
        self.assertEqual(rows[0][1], "[10, 20, 30]")

    def test_clipboard_string_auto_detects_tsv(self):
        path = self.tmp / "out.csv"
        text = "a\tb\tc\n1\t2\t3\n"
        verbs.log_csv(path, text)
        rows = self._read_rows(path)
        self.assertEqual(rows[0], ["a", "b", "c"])
        self.assertEqual(rows[1], ["1", "2", "3"])

    def test_clipboard_string_auto_detects_semicolon(self):
        path = self.tmp / "out.csv"
        text = "a;b;c\n1;2;3\n"
        verbs.log_csv(path, text)
        rows = self._read_rows(path)
        self.assertEqual(rows[0], ["a", "b", "c"])

    def test_clipboard_string_falls_back_to_comma(self):
        path = self.tmp / "out.csv"
        text = "a,b,c\n1,2,3\n"
        verbs.log_csv(path, text)
        rows = self._read_rows(path)
        self.assertEqual(rows[0], ["a", "b", "c"])

    def test_custom_output_delimiter(self):
        path = self.tmp / "out.tsv"
        verbs.log_csv(path, [1, 2, 3], delimiter="\t")
        text = path.read_text(encoding="utf-8")
        self.assertIn("1\t2\t3", text)


# --- each() -----------------------------------------------------------------


class TestEach(unittest.TestCase):
    def test_applies_verb_to_each_id(self):
        win = _FakeWindow()
        results = verbs.each(
            lambda w, ctrl_id: f"{w.NativeWindowHandle}:{ctrl_id}",
            win,
            ["A", "B", "C"],
        )
        self.assertEqual(results, ["42:A", "42:B", "42:C"])

    def test_passes_kwargs_through(self):
        win = _FakeWindow()
        sentinel = object()
        captured = []

        def fake_verb(w, ctrl_id, *, timeout):
            captured.append((ctrl_id, timeout))
            return sentinel

        results = verbs.each(fake_verb, win, ["X", "Y"], timeout=3)
        self.assertEqual(results, [sentinel, sentinel])
        self.assertEqual(captured, [("X", 3), ("Y", 3)])

    def test_empty_list_returns_empty_list(self):
        results = verbs.each(lambda *a, **k: 1 / 0, _FakeWindow(), [])
        self.assertEqual(results, [])

    def test_real_each_with_is_visible(self):
        with mock.patch.object(verbs.actions, "is_present",
                               side_effect=[True, False, True]) as mip:
            results = verbs.each(verbs.is_visible, _FakeWindow(),
                                 ["A", "B", "C"])
        self.assertEqual(results, [True, False, True])
        self.assertEqual(mip.call_count, 3)


class TestSequence(unittest.TestCase):
    """`sequence` snapshots the HWND set at entry; if a new unexpected
    HWND appears between two ids (not after the last), it dismisses
    the popup and restarts the loop from id 0. Up to `attempts` tries."""

    def setUp(self):
        self._saved_expected = set(verbs._expected_hwnds)
        self._saved_trusted = set(verbs._trusted_pids)
        verbs._expected_hwnds.clear()
        verbs._trusted_pids.clear()

    def tearDown(self):
        verbs._expected_hwnds.clear()
        verbs._expected_hwnds.update(self._saved_expected)
        verbs._trusted_pids.clear()
        verbs._trusted_pids.update(self._saved_trusted)

    def test_no_popup_runs_through_once(self):
        calls = []
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[1, 2]), \
             mock.patch.object(verbs, "_dismiss_unexpected_popups"), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            verbs.sequence(lambda w, i: calls.append(i), _FakeWindow(),
                           ["A", "B", "C"])
        self.assertEqual(calls, ["A", "B", "C"])
        mdo.assert_not_called()

    def test_popup_between_ids_triggers_restart(self):
        # entry baseline=[1,2] → A → enum=[1,2,99] (popup!) → dismiss(99),
        # restart → A → enum=[1,2] → B → done (last id, no post-check).
        enum_returns = iter([
            [1, 2],          # initial baseline
            [1, 2, 99],      # post-A on attempt 1
            [1, 2],          # baseline refresh after dismiss
            [1, 2],          # post-A on attempt 2
        ])
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        side_effect=lambda: next(enum_returns)), \
             mock.patch.object(verbs, "_dismiss_unexpected_popups"), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            calls = []
            verbs.sequence(lambda w, i: calls.append(i), _FakeWindow(),
                           ["A", "B"])
        self.assertEqual(calls, ["A", "A", "B"])
        mdo.assert_called_once_with(99)

    def test_per_step_verbs(self):
        # verb=list of callables → verb[i] runs on ids[i].
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[1]), \
             mock.patch.object(verbs, "_dismiss_unexpected_popups"):
            calls = []
            verbs.sequence(
                [lambda w, i: calls.append(("a", i)),
                 lambda w, i: calls.append(("b", i)),
                 lambda w, i: calls.append(("c", i))],
                _FakeWindow(),
                ["X", "Y", "Z"],
            )
        self.assertEqual(calls, [("a", "X"), ("b", "Y"), ("c", "Z")])

    def test_verb_list_length_mismatch_raises(self):
        with self.assertRaisesRegex(ValueError, "verb list length"):
            verbs.sequence([lambda w, i: None, lambda w, i: None],
                           _FakeWindow(), ["A", "B", "C"])

    def test_attempts_kwarg_caps_retries(self):
        # Persistent popup → exhaust attempts and return whatever we have.
        enum_returns = iter([
            [1, 2],          # initial baseline
            [1, 2, 99],      # post-A attempt 1
            [1, 2],          # refresh
            [1, 2, 99],      # post-A attempt 2 (popup back again)
        ])
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        side_effect=lambda: next(enum_returns)), \
             mock.patch.object(verbs, "_dismiss_unexpected_popups"), \
             mock.patch.object(verbs, "_dismiss_one"):
            calls = []
            verbs.sequence(lambda w, i: calls.append(i), _FakeWindow(),
                           ["A", "B"], attempts=2)
        # Two attempts, both interrupted after A.
        self.assertEqual(calls, ["A", "A"])


# --- Click family wrappers --------------------------------------------------


class TestClickFamily(unittest.TestCase):
    def setUp(self):
        self.win = _FakeWindow()

    def test_click_delegates_to_press(self):
        with mock.patch.object(verbs.actions, "press",
                               return_value=True) as mp:
            self.assertTrue(verbs.click(self.win, "BTN_ID"))
        mp.assert_called_once_with(self.win, "BTN_ID")

    def test_double_click_delegates_to_double_press(self):
        with mock.patch.object(verbs.actions, "double_press",
                               return_value=True) as mp:
            self.assertTrue(verbs.double_click(self.win, "BTN_ID"))
        mp.assert_called_once_with(self.win, "BTN_ID")

    def test_right_click_delegates_to_right_press(self):
        with mock.patch.object(verbs.actions, "right_press",
                               return_value=True) as mp:
            self.assertTrue(verbs.right_click(self.win, "BTN_ID"))
        mp.assert_called_once_with(self.win, "BTN_ID")

    def test_click_when_enabled_default_timeout(self):
        with mock.patch.object(verbs.actions, "press_when_active",
                               return_value=True) as mp:
            verbs.click_when_enabled(self.win, "BTN_ID")
        mp.assert_called_once_with(self.win, "BTN_ID", timeout=30)

    def test_click_when_enabled_custom_timeout(self):
        with mock.patch.object(verbs.actions, "press_when_active",
                               return_value=True) as mp:
            verbs.click_when_enabled(self.win, "BTN_ID", timeout=5)
        mp.assert_called_once_with(self.win, "BTN_ID", timeout=5)

    def test_move_delegates_to_actions_move(self):
        with mock.patch.object(verbs.actions, "move",
                               return_value=True) as mm:
            self.assertTrue(verbs.move(self.win, "BTN_ID"))
        mm.assert_called_once_with(self.win, "BTN_ID")

    def test_hold_and_drag_delegates_to_actions_drag(self):
        with mock.patch.object(verbs.actions, "drag",
                               return_value=True) as md:
            self.assertTrue(
                verbs.hold_and_drag(self.win, "SRC_ID", "DST_ID"),
            )
        md.assert_called_once_with(self.win, "SRC_ID", "DST_ID")

    def test_click_at_delegates_to_cursor_click(self):
        with mock.patch.object(verbs.actions, "_cursor_click") as mc:
            verbs.click_at(123, 456)
        mc.assert_called_once_with(123, 456)

    def test_move_at_delegates_to_cursor_move(self):
        with mock.patch.object(verbs.actions, "_cursor_move") as mm:
            verbs.move_at(123, 456)
        mm.assert_called_once_with(123, 456)

    def test_hold_and_drag_at_delegates_to_cursor_drag(self):
        with mock.patch.object(verbs.actions, "_cursor_drag") as md:
            verbs.hold_and_drag_at(10, 20, 100, 200)
        md.assert_called_once_with(10, 20, 100, 200)

    def test_click_after_sleeps_then_presses(self):
        with mock.patch.object(verbs.actions, "press",
                               return_value=True) as mp:
            t0 = time.time()
            self.assertTrue(verbs.click_after(self.win, "BTN_ID", 0.05))
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.04)
        mp.assert_called_once_with(self.win, "BTN_ID")


class TestWebCoords(unittest.TestCase):
    """`web_coords` is a thin wrapper around Playwright's
    `page.evaluate` — verify it shapes the JS query and returns
    whatever evaluate produces. We don't mock the JS evaluation
    itself; that's Playwright's job."""

    def test_calls_page_evaluate_with_selector(self):
        page = mock.MagicMock()
        page.evaluate.return_value = [100, 200]
        result = verbs.web_coords(page, "button.save")
        self.assertEqual(result, [100, 200])
        # First arg is JS source; second is the selector forwarded as-is.
        args, _ = page.evaluate.call_args
        self.assertIn("getBoundingClientRect", args[0])
        self.assertEqual(args[1], "button.save")

    def test_returns_none_when_element_not_found(self):
        page = mock.MagicMock()
        page.evaluate.return_value = None
        self.assertIsNone(verbs.web_coords(page, "nope"))


class TestSystemWindowClassesProtectsBrowsers(unittest.TestCase):
    """Browser window classes must be in the system skip list so that
    coordinate-based clicks via Playwright don't let another verb's
    pre-dismiss accidentally close the host browser window."""

    def test_chrome_class_is_protected(self):
        self.assertIn("Chrome_WidgetWin_1", verbs._SYSTEM_WINDOW_CLASSES)

    def test_firefox_class_is_protected(self):
        self.assertIn("MozillaWindowClass", verbs._SYSTEM_WINDOW_CLASSES)


# --- Text input -------------------------------------------------------------


class TestFill(unittest.TestCase):
    def test_fill_delegates_to_write_text(self):
        win = _FakeWindow()
        with mock.patch.object(verbs.actions, "write_text",
                               return_value=True) as mw:
            self.assertTrue(verbs.fill(win, "EDIT_ID", "hello"))
        mw.assert_called_once_with(win, "EDIT_ID", "hello")


class TestType(unittest.TestCase):
    def test_type_writes_via_pyautogui(self):
        with mock.patch.object(verbs.pyautogui, "write") as mw:
            verbs.type("password123")
        mw.assert_called_once_with("password123", interval=0.02)

    def test_type_custom_interval(self):
        with mock.patch.object(verbs.pyautogui, "write") as mw:
            verbs.type("slow", interval=0.1)
        mw.assert_called_once_with("slow", interval=0.1)

    def test_type_signature_matches_docstring(self):
        # Documents the contract: `type` has signature (text, interval)
        # — NO window argument. It blasts keys at whatever currently has
        # keyboard focus.
        import inspect
        params = list(inspect.signature(verbs.type).parameters)
        self.assertEqual(params, ["text", "interval"])


class TestKey(unittest.TestCase):
    """`key` is the focus-targeted sibling of `hotkey` — no window
    arg, no auto-foreground, just sends the key/combo to whatever
    currently has keyboard focus."""

    def test_single_key_uses_press(self):
        with mock.patch.object(verbs.pyautogui, "press") as mp, \
             mock.patch.object(verbs.pyautogui, "hotkey") as mh:
            verbs.key("enter")
        mp.assert_called_once_with("enter")
        mh.assert_not_called()

    def test_combo_uses_hotkey(self):
        with mock.patch.object(verbs.pyautogui, "press") as mp, \
             mock.patch.object(verbs.pyautogui, "hotkey") as mh:
            verbs.key("ctrl", "c")
        mh.assert_called_once_with("ctrl", "c")
        mp.assert_not_called()

    def test_does_not_foreground_anything(self):
        # Critical contract: key() must NOT touch apps.bring_to_foreground
        # — that's the whole reason it exists (so a Save-dialog Enter
        # doesn't pull focus back to the parent app).
        with mock.patch.object(verbs.apps, "bring_to_foreground") as mfg, \
             mock.patch.object(verbs.pyautogui, "press"):
            verbs.key("enter")
        mfg.assert_not_called()


class TestHotkey(unittest.TestCase):
    def test_hotkey_brings_window_forward_then_sends_combo(self):
        win = _FakeWindow()
        order = []
        with mock.patch.object(verbs.apps, "bring_to_foreground",
                               side_effect=lambda w: order.append(("fg", w))), \
             mock.patch.object(verbs.pyautogui, "hotkey",
                               side_effect=lambda *combo: order.append(("hk", combo))):
            verbs.hotkey(win, "ctrl", "s")
        self.assertEqual(order, [("fg", win), ("hk", ("ctrl", "s"))])

    def test_hotkey_three_keys(self):
        with mock.patch.object(verbs.apps, "bring_to_foreground"), \
             mock.patch.object(verbs.pyautogui, "hotkey") as mh:
            verbs.hotkey(_FakeWindow(), "ctrl", "shift", "t")
        mh.assert_called_once_with("ctrl", "shift", "t")


# --- Checks / waits ---------------------------------------------------------


class TestVisibleEnabled(unittest.TestCase):
    def setUp(self):
        self.win = _FakeWindow()

    def test_is_visible_zero_timeout_default(self):
        with mock.patch.object(verbs.actions, "is_present",
                               return_value=True) as mp:
            self.assertTrue(verbs.is_visible(self.win, "X"))
        mp.assert_called_once_with(self.win, "X", timeout=0)

    def test_is_visible_explicit_timeout(self):
        with mock.patch.object(verbs.actions, "is_present",
                               return_value=False) as mp:
            self.assertFalse(verbs.is_visible(self.win, "X", timeout=2.5))
        mp.assert_called_once_with(self.win, "X", timeout=2.5)

    def test_is_enabled_zero_timeout_default(self):
        with mock.patch.object(verbs.actions, "check_active",
                               return_value=True) as mp:
            self.assertTrue(verbs.is_enabled(self.win, "X"))
        mp.assert_called_once_with(self.win, "X", timeout=0)

    def test_is_enabled_explicit_timeout(self):
        with mock.patch.object(verbs.actions, "check_active",
                               return_value=True) as mp:
            verbs.is_enabled(self.win, "X", timeout=3)
        mp.assert_called_once_with(self.win, "X", timeout=3)

    def test_wait_visible_default_timeout_is_ten(self):
        with mock.patch.object(verbs.actions, "is_present",
                               return_value=True) as mp:
            verbs.wait_visible(self.win, "X")
        mp.assert_called_once_with(self.win, "X", timeout=10)

    def test_wait_enabled_default_timeout_is_ten(self):
        with mock.patch.object(verbs.actions, "check_active",
                               return_value=True) as mp:
            verbs.wait_enabled(self.win, "X")
        mp.assert_called_once_with(self.win, "X", timeout=10)

    def test_wait_gone_default_timeout_is_ten(self):
        with mock.patch.object(verbs.actions, "wait_until_absent",
                               return_value=True) as mp:
            verbs.wait_gone(self.win, "X")
        mp.assert_called_once_with(self.win, "X", timeout=10)

    def test_wait_gone_returns_underlying_bool(self):
        with mock.patch.object(verbs.actions, "wait_until_absent",
                               return_value=False):
            self.assertFalse(verbs.wait_gone(self.win, "X", timeout=1))


# --- Color verbs ------------------------------------------------------------


class TestColorVerbs(unittest.TestCase):
    def setUp(self):
        self.win = _FakeWindow()

    def test_check_color_returns_actions_get_color(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(10, 20, 30)) as mg:
            self.assertEqual(verbs.check_color(self.win, "X"), (10, 20, 30))
        mg.assert_called_once_with(self.win, "X", x_offset=0, y_offset=0)

    def test_check_color_passes_offsets(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(0, 0, 0)) as mg:
            verbs.check_color(self.win, "X", dx=5, dy=-3)
        mg.assert_called_once_with(self.win, "X", x_offset=5, y_offset=-3)

    def test_is_color_exact_match_zero_tolerance(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(255, 100, 50)):
            self.assertTrue(
                verbs.is_color(self.win, "X", (255, 100, 50)),
            )
            self.assertFalse(
                verbs.is_color(self.win, "X", (254, 100, 50)),
            )

    def test_is_color_within_tolerance(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(248, 100, 50)):
            self.assertTrue(
                verbs.is_color(self.win, "X", (255, 100, 50), tolerance=10),
            )
            self.assertFalse(
                verbs.is_color(self.win, "X", (255, 100, 50), tolerance=5),
            )

    def test_is_color_outside_tolerance_in_one_channel(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(255, 100, 100)):
            self.assertFalse(
                verbs.is_color(self.win, "X", (255, 100, 50), tolerance=10),
            )

    def test_is_color_passes_offsets(self):
        with mock.patch.object(verbs.actions, "get_color",
                               return_value=(0, 0, 0)) as mg:
            verbs.is_color(self.win, "X", (0, 0, 0), dx=2, dy=4)
        mg.assert_called_once_with(self.win, "X", x_offset=2, y_offset=4)


# --- read_info --------------------------------------------------------------


class _FakeRect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class _FakeElement:
    def __init__(self, name="", value="", role="ButtonControl",
                 enabled=True, bbox=(10, 20, 110, 50), class_name="",
                 automation_id=""):
        self.Name = name
        self.ControlTypeName = role
        self.IsEnabled = enabled
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.BoundingRectangle = _FakeRect(*bbox)
        self._value = value

    def GetValuePattern(self):
        if self._value is None:
            raise RuntimeError("no value pattern")
        v = type("VP", (), {})()
        v.Value = self._value
        return v


class TestReadInfo(unittest.TestCase):
    def setUp(self):
        self.win = _FakeWindow()

    def test_returns_complete_dict(self):
        elem = _FakeElement(
            name="Save", value="", role="ButtonControl", enabled=True,
            bbox=(10, 20, 110, 50), class_name="Btn",
            automation_id="save_btn",
        )
        with mock.patch.object(
            verbs.actions, "_resolve",
            return_value=(elem, (60, 35)),
        ):
            info = verbs.read_info(self.win, "0.2.0")
        self.assertEqual(info["name"], "Save")
        self.assertEqual(info["role"], "ButtonControl")
        self.assertTrue(info["enabled"])
        self.assertTrue(info["visible"])
        self.assertEqual(info["bbox"], (10, 20, 110, 50))
        self.assertEqual(info["bbox_center"], (60, 35))
        self.assertEqual(info["class_name"], "Btn")
        self.assertEqual(info["automation_id"], "save_btn")
        self.assertEqual(info["struct_id"], "0.2.0")

    def test_zero_bbox_marks_invisible(self):
        elem = _FakeElement(bbox=(0, 0, 0, 0))
        with mock.patch.object(verbs.actions, "_resolve",
                               return_value=(elem, (0, 0))):
            info = verbs.read_info(self.win, "0.2.0")
        self.assertFalse(info["visible"])

    def test_value_pattern_present(self):
        elem = _FakeElement(name="Edit", value="typed", role="EditControl")
        with mock.patch.object(verbs.actions, "_resolve",
                               return_value=(elem, (0, 0))):
            info = verbs.read_info(self.win, "0.2.0")
        self.assertEqual(info["value"], "typed")

    def test_value_pattern_missing_returns_empty_string(self):
        elem = _FakeElement(name="Btn", value=None)  # GetValuePattern raises
        with mock.patch.object(verbs.actions, "_resolve",
                               return_value=(elem, (0, 0))):
            info = verbs.read_info(self.win, "0.2.0")
        self.assertEqual(info["value"], "")


# --- window.open/popup wiring ----------------------------------------------


class TestWindowOpen(unittest.TestCase):
    """`window.open` is the find-or-launch entry point — it looks up the
    name in the registry and delegates to `core.app.match`. Real
    fingerprint logic lives in test_app.py."""

    def setUp(self):
        from core import window
        window._reset()

    def tearDown(self):
        from core import window
        window._reset()

    def test_delegates_to_core_app_match(self):
        from core import window
        sentinel = mock.MagicMock()
        sentinel.NativeWindowHandle = 123
        window.register("notepad", "notepad.exe")
        with mock.patch("core.app.match", return_value=sentinel) as mm:
            result = window.open("notepad")
        self.assertIs(result, sentinel)
        mm.assert_called_once_with("notepad", launch="notepad.exe",
                                   timeout=45.0)
        # Cached on the namespace.
        self.assertIs(window.notepad, sentinel)

    def test_unregistered_name_raises(self):
        from core import window
        with self.assertRaises(KeyError):
            window.open("not_registered")

    def test_timeout_when_match_returns_none(self):
        from core import window
        window.register("foo", "foo.exe")
        with mock.patch("core.app.match", return_value=None):
            with self.assertRaises(TimeoutError):
                window.open("foo")


class TestPopupVerb(unittest.TestCase):
    """`popup(name, _trigger, timeout)` polls `core.app.match` with
    launch='popup' until a match appears or the timeout elapses."""

    def test_returns_first_match_immediately(self):
        sentinel = object()
        with mock.patch("core.app.match", return_value=sentinel) as mm, \
             mock.patch.object(verbs._time, "sleep") as msleep:
            result = verbs.popup("dlg", True, timeout=5.0)
        self.assertIs(result, sentinel)
        mm.assert_called_once_with("dlg", launch="popup",
                                   restrict_pid=None, parent=None)
        msleep.assert_not_called()

    def test_polls_until_match_appears(self):
        sentinel = object()
        # Three polls: None, None, hit.
        with mock.patch("core.app.match",
                        side_effect=[None, None, sentinel]) as mm, \
             mock.patch.object(verbs._time, "sleep"):
            result = verbs.popup("dlg")
        self.assertIs(result, sentinel)
        self.assertEqual(mm.call_count, 3)

    def test_returns_none_on_timeout(self):
        # Use real (small) timeout so the deadline elapses.
        with mock.patch("core.app.match", return_value=None), \
             mock.patch.object(verbs._time, "sleep"):
            result = verbs.popup("dlg", timeout=0.0)
        self.assertIsNone(result)

    def test_trigger_arg_is_consumed_and_ignored(self):
        # popup("name", click(...)) — the click's return value is the
        # trigger arg; popup must not propagate it back as the result.
        sentinel = object()
        with mock.patch("core.app.match", return_value=sentinel), \
             mock.patch.object(verbs._time, "sleep"):
            result = verbs.popup("dlg", "ignored_trigger_value")
        self.assertIs(result, sentinel)


# --- Synchronous popup dismiss ----------------------------------------------


class _DismissTestBase(unittest.TestCase):
    """Reset module-level dismiss state before every test in this group
    so ordering doesn't leak `_expected_hwnds` between cases."""

    def setUp(self):
        self._saved_expected = set(verbs._expected_hwnds)
        self._saved_trusted = set(verbs._trusted_pids)
        self._saved_baseline = set(verbs._hwnd_baseline_set)
        self._saved_depth = getattr(verbs._dismiss_paused, "depth", 0)
        verbs._expected_hwnds.clear()
        verbs._trusted_pids.clear()
        verbs._hwnd_baseline_set.clear()
        verbs._dismiss_paused.depth = 0

    def tearDown(self):
        verbs._expected_hwnds.clear()
        verbs._expected_hwnds.update(self._saved_expected)
        verbs._trusted_pids.clear()
        verbs._trusted_pids.update(self._saved_trusted)
        verbs._hwnd_baseline_set.clear()
        verbs._hwnd_baseline_set.update(self._saved_baseline)
        verbs._dismiss_paused.depth = self._saved_depth


class TestSynchronousPopupDismiss(_DismissTestBase):
    """`_dismiss_unexpected_popups` walks every visible HWND and
    dismisses any that aren't in `_expected_hwnds`. Expected HWNDs are
    left alone."""

    def test_unexpected_hwnd_is_dismissed(self):
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[111, 222]), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            # Neither hwnd is in expected → both should be dismissed.
            verbs._dismiss_unexpected_popups(window=None)
        self.assertEqual(mdo.call_count, 2)
        called_hwnds = sorted(c.args[0] for c in mdo.call_args_list)
        self.assertEqual(called_hwnds, [111, 222])

    def test_expected_hwnd_is_left_alone(self):
        verbs._expected_hwnds.update([111, 222])
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[111, 222, 333]), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            verbs._dismiss_unexpected_popups(window=None)
        # Only 333 (the unexpected one) gets dismissed.
        mdo.assert_called_once_with(333)

    def test_paused_skips_all_dismissal(self):
        verbs._dismiss_paused.depth = 1
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[111, 222]), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            verbs._dismiss_unexpected_popups(window=None)
        mdo.assert_not_called()

    def test_same_pid_popup_is_left_alone(self):
        """The app's own menus / dropdowns / dialogs share the registered
        window's PID. They must not be auto-dismissed (otherwise every
        `click(file_menu)` followed by `click(menu_item)` would have its
        menu killed in between)."""
        verbs._trusted_pids.add(5555)
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[111, 222]), \
             mock.patch.object(verbs, "_hwnd_pid",
                               side_effect=lambda h: 5555 if h == 111 else 9999), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            verbs._dismiss_unexpected_popups(window=None)
        # 111 (same PID as registered app) is kept. 222 (foreign) is dismissed.
        mdo.assert_called_once_with(222)

    def test_mark_hwnd_expected_also_trusts_pid(self):
        """`_mark_hwnd_expected` is called from `core.app.match` whenever
        a window is matched. Its side effect is to trust the owning PID
        so subsequent same-process popups are skipped by dismiss."""
        with mock.patch.object(verbs, "_hwnd_pid", return_value=7777):
            verbs._mark_hwnd_expected(123)
        self.assertIn(123, verbs._expected_hwnds)
        self.assertIn(7777, verbs._trusted_pids)

    def test_system_class_is_never_dismissed(self):
        """`_dismiss_one` hard-skips windows whose class is in the
        system list — regardless of whether the caller bypassed the
        expected/trusted checks. Defense in depth: even a buggy test
        setup can't kill the developer's terminal."""
        with mock.patch.object(verbs, "_hwnd_class",
                               return_value="ConsoleWindowClass"), \
             mock.patch.object(verbs, "_send_dismiss_key") as mkey, \
             mock.patch.object(verbs._user32, "PostMessageW") as mpm:
            result = verbs._dismiss_one(123)
        self.assertFalse(result, "system window dismiss must return False")
        mkey.assert_not_called()
        mpm.assert_not_called()

    def test_system_process_is_never_dismissed(self):
        """Process-name fallback: even if class isn't in the list,
        explorer.exe / dwm.exe / Code.exe etc. are protected."""
        fake_proc = mock.MagicMock()
        fake_proc.name.return_value = "explorer.exe"
        with mock.patch.object(verbs, "_hwnd_class",
                               return_value="SomeRandomClass"), \
             mock.patch.object(verbs, "_hwnd_pid", return_value=42), \
             mock.patch.object(verbs.psutil, "Process",
                               return_value=fake_proc), \
             mock.patch.object(verbs, "_send_dismiss_key") as mkey:
            result = verbs._dismiss_one(123)
        self.assertFalse(result)
        mkey.assert_not_called()

    def test_dismiss_unexpected_skips_system_windows(self):
        """The pre-dismiss loop must skip system windows even when
        they're not in `_expected_hwnds` and not owned by a trusted
        PID."""
        # Mock so HWND 999 looks like the developer's terminal.
        def fake_class(h):
            return "WindowsTerminal" if h == 999 else "PopupClass"
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[999, 111]), \
             mock.patch.object(verbs, "_hwnd_class",
                               side_effect=fake_class), \
             mock.patch.object(verbs, "_hwnd_pid", return_value=0), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            verbs._dismiss_unexpected_popups(window=None)
        # 111 (foreign popup) gets dismissed; 999 (terminal) does not.
        mdo.assert_called_once_with(111)

    def test_action_verb_runs_dismiss_before_inner_call(self):
        """The `_action_verb` decorator must call dismiss BEFORE the
        wrapped function (so popups don't intercept the click)."""
        order = []
        with mock.patch.object(verbs, "_dismiss_unexpected_popups",
                               side_effect=lambda w: order.append("dismiss")), \
             mock.patch.object(verbs.actions, "press",
                               side_effect=lambda *a, **k: order.append("press") or True):
            verbs.click(_FakeWindow(), "BTN")
        self.assertEqual(order, ["dismiss", "press"])


class TestNoDismissContext(_DismissTestBase):
    """`no_dismiss` context manager increments/decrements the pause
    counter; nesting works. While paused, `_dismiss_unexpected_popups`
    is a no-op."""

    def test_increment_on_enter_decrement_on_exit(self):
        self.assertEqual(getattr(verbs._dismiss_paused, "depth", 0), 0)
        with verbs.no_dismiss():
            self.assertEqual(verbs._dismiss_paused.depth, 1)
        self.assertEqual(verbs._dismiss_paused.depth, 0)

    def test_nested_contexts_stack(self):
        with verbs.no_dismiss():
            self.assertEqual(verbs._dismiss_paused.depth, 1)
            with verbs.no_dismiss():
                self.assertEqual(verbs._dismiss_paused.depth, 2)
            self.assertEqual(verbs._dismiss_paused.depth, 1)
        self.assertEqual(verbs._dismiss_paused.depth, 0)

    def test_dismiss_skipped_inside_block(self):
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[111]), \
             mock.patch.object(verbs, "_dismiss_one") as mdo:
            with verbs.no_dismiss():
                verbs._dismiss_unexpected_popups(window=None)
        mdo.assert_not_called()

    def test_exception_inside_block_still_decrements(self):
        try:
            with verbs.no_dismiss():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(verbs._dismiss_paused.depth, 0)


class TestHwndBaselineUpdates(_DismissTestBase):
    """Every action verb calls `_capture_hwnd_baseline` before
    delegating, so `match("popup")` always sees the freshest "what was
    visible right before this verb" set."""

    def test_capture_replaces_baseline_with_current_hwnds(self):
        verbs._hwnd_baseline_set.update([1, 2, 3])
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[10, 20]):
            verbs._capture_hwnd_baseline()
        self.assertEqual(verbs._hwnd_baseline_set, {10, 20})

    def test_action_verb_decorator_refreshes_baseline(self):
        # Run a click — afterwards the baseline should be whatever the
        # mocked enumerate returned at decorator-entry time.
        with mock.patch("core.app._enumerate_top_level_hwnds",
                        return_value=[55, 66, 77]), \
             mock.patch.object(verbs, "_dismiss_unexpected_popups"), \
             mock.patch.object(verbs.actions, "press", return_value=True):
            verbs.click(_FakeWindow(), "BTN")
        self.assertEqual(verbs._hwnd_baseline_set, {55, 66, 77})

    def test_baseline_snapshot_returns_frozenset(self):
        verbs._hwnd_baseline_set.update([7, 8, 9])
        snap = verbs._hwnd_baseline_snapshot()
        self.assertIsInstance(snap, frozenset)
        self.assertEqual(snap, frozenset({7, 8, 9}))

    def test_mark_hwnd_expected_adds_to_set(self):
        verbs._mark_hwnd_expected(999)
        self.assertIn(999, verbs._expected_hwnds)

    def test_mark_hwnd_expected_ignores_zero(self):
        verbs._mark_hwnd_expected(0)
        self.assertNotIn(0, verbs._expected_hwnds)


# --- screenshot, close ------------------------------------------------------


class TestScreenshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verbs_shot_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_screenshot_brings_window_forward_and_saves_png(self):
        win = _FakeWindow()
        win.BoundingRectangle = _FakeRect(10, 20, 110, 70)
        out = self.tmp / "deep" / "shot.png"

        fake_img = mock.MagicMock()
        with mock.patch.object(verbs.apps, "bring_to_foreground") as mfg, \
             mock.patch.object(verbs.pyautogui, "screenshot",
                               return_value=fake_img) as mss:
            verbs.screenshot(win, out)

        mfg.assert_called_once_with(win)
        mss.assert_called_once_with(region=(10, 20, 100, 50))
        fake_img.save.assert_called_once_with(out)
        self.assertTrue(out.parent.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
