"""Unit tests for the state functions in run.py.

Each state function takes `data` and returns `(next_state, data)`. The
tests mock every verb (already covered by `tests/test_verbs.py`) and
verify:

  * the right verbs are called in the right order with the right args,
  * the returned `next_state` matches the documented control flow,
  * fail-soft branches log + degrade gracefully instead of raising.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402


def _make_data():
    """Stand-in for the runner-built `data` SimpleNamespace. The window
    object itself is opaque — verbs are mocked so they never inspect it."""
    return SimpleNamespace(notepad=mock.MagicMock(name="notepad_window"))


class TestStateInit(unittest.TestCase):
    def test_routes_to_new_tab_when_file_menu_visible(self):
        # Stale popups are auto-dismissed by every action verb's
        # pre-call check — there's no explicit dismiss_popups call to
        # mock here.
        data = _make_data()
        with mock.patch.object(run, "wait_visible", return_value=True) as mwv:
            nxt, out = run.state_init(data)
        mwv.assert_called_once_with(data.notepad, run.FILE_MENU, timeout=10)
        self.assertEqual(nxt, "new_tab")
        self.assertIs(out, data)

    def test_stops_when_file_menu_never_visible(self):
        # If wait_visible returns False, the state machine should end —
        # not crash.
        data = _make_data()
        with mock.patch.object(run, "wait_visible", return_value=False), \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_init(data)
        self.assertIsNone(nxt)
        ml.assert_called_once()
        # The log row tags the failure for later debugging.
        self.assertEqual(ml.call_args[0][:2], ("results", "init_failed"))


class TestStateNewTab(unittest.TestCase):
    def test_happy_path_opens_menu_and_clicks_new_tab(self):
        data = _make_data()
        with mock.patch.object(run, "click_when_enabled") as mce, \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "wait_visible", return_value=True) as mwv, \
             mock.patch.object(run, "wait_gone") as mwg:
            nxt, _ = run.state_new_tab(data)
        mce.assert_called_once_with(data.notepad, run.FILE_MENU)
        mwv.assert_called_once_with(data.notepad, run.NEW_TAB, timeout=5)
        mc.assert_called_once_with(data.notepad, run.NEW_TAB)
        mwg.assert_called_once_with(data.notepad, run.NEW_TAB, timeout=3)
        self.assertEqual(nxt, "zoom_in")

    def test_logs_and_routes_to_close_when_new_tab_invisible(self):
        # If the New tab menu item never appears, log the failure and
        # short-circuit to the close state — don't try to click a ghost.
        data = _make_data()
        with mock.patch.object(run, "click_when_enabled"), \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "wait_visible", return_value=False), \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_new_tab(data)
        self.assertEqual(nxt, "close")
        mc.assert_not_called()
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2], ("results", "new_tab_failed"))


class TestStateZoomIn(unittest.TestCase):
    def test_walks_view_menu_chain(self):
        # Chain: View > Zoom > Zoom In. Every step waits for the next
        # item to render before clicking.
        data = _make_data()
        with mock.patch.object(run, "click_when_enabled") as mce, \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "wait_visible", return_value=True):
            nxt, _ = run.state_zoom_in(data)
        # Two click_when_enabled calls (VIEW_MENU and ZOOM), one final
        # plain click (ZOOM_IN — already enabled by the time we reach it).
        self.assertEqual(mce.call_count, 2)
        mce.assert_any_call(data.notepad, run.VIEW_MENU)
        mce.assert_any_call(data.notepad, run.ZOOM)
        mc.assert_called_once_with(data.notepad, run.ZOOM_IN)
        self.assertEqual(nxt, "zoom_out")


class TestStateZoomOut(unittest.TestCase):
    def test_routes_to_type_time(self):
        data = _make_data()
        with mock.patch.object(run, "click_when_enabled"), \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "wait_visible", return_value=True):
            nxt, _ = run.state_zoom_out(data)
        mc.assert_called_once_with(data.notepad, run.ZOOM_OUT)
        self.assertEqual(nxt, "type_time")


class TestStateTypeTime(unittest.TestCase):
    def test_fill_called_with_now_string(self):
        data = _make_data()
        with mock.patch.object(run, "fill") as mfill, \
             mock.patch.object(run, "now",
                               return_value="2026-01-01 12:00:00") as mn:
            nxt, _ = run.state_type_time(data)
        mfill.assert_called_once_with(data.notepad, run.EDITOR,
                                      "2026-01-01 12:00:00")
        mn.assert_called_once()
        self.assertEqual(nxt, "verify")


class TestStateVerify(unittest.TestCase):
    def test_logs_csv_with_visibility_and_color(self):
        data = _make_data()
        info_dict = {
            "class_name": "EditControl", "name": "Editor",
            "value": "", "role": "EditControl", "enabled": True,
            "visible": True, "bbox": (0, 0, 100, 100),
            "bbox_center": (50, 50), "automation_id": "edit",
            "struct_id": run.EDITOR,
        }
        with mock.patch.object(run, "each",
                               side_effect=[[True, True, True],
                                            [True, True, True]]), \
             mock.patch.object(run, "read_info",
                               return_value=info_dict), \
             mock.patch.object(run, "check_color",
                               return_value=(255, 100, 50)), \
             mock.patch.object(run, "log_csv") as mlc, \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_verify(data)
        mlc.assert_called_once()
        # Header includes nine columns (ts + four kind/value pairs).
        self.assertEqual(len(mlc.call_args.kwargs["header"]), 9)
        # No warning logged when everything is visible+enabled.
        ml.assert_not_called()
        self.assertEqual(nxt, "snapshot")

    def test_warns_when_control_invisible_or_disabled(self):
        data = _make_data()
        with mock.patch.object(run, "each",
                               side_effect=[[True, False, True],
                                            [True, True, True]]), \
             mock.patch.object(run, "read_info",
                               return_value={"class_name": "X"}), \
             mock.patch.object(run, "check_color",
                               return_value=(0, 0, 0)), \
             mock.patch.object(run, "log_csv"), \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_verify(data)
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2],
                         ("results", "verify_warning"))
        self.assertEqual(nxt, "snapshot")  # still proceeds — fail-soft


class TestStateSnapshot(unittest.TestCase):
    def test_calls_screenshot_and_logs_path(self):
        data = _make_data()
        with mock.patch.object(run, "screenshot") as mss, \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_snapshot(data)
        mss.assert_called_once()
        # Path argument is data.RESULTS_DIR / before_save.png.
        path_arg = mss.call_args[0][1]
        self.assertEqual(path_arg.name, "before_save.png")
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2], ("results", "screenshot"))
        self.assertEqual(nxt, "save")


class TestStateSave(unittest.TestCase):
    def test_hotkey_type_enter_sequence(self):
        """state_save uses raw Ctrl+S → type(path) → Enter instead of
        the removed `save_as` verb. Verify the call sequence and that
        the path used is `config.SAVE_PATH`."""
        data = _make_data()
        order = []
        with mock.patch.object(run, "hotkey",
                               side_effect=lambda *a, **k: order.append(("hk", a))), \
             mock.patch.object(run, "type",
                               side_effect=lambda *a, **k: order.append(("type", a))), \
             mock.patch.object(run, "wait"), \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_save(data)
        # Sequence: hotkey(notepad, "ctrl", "s"), type(path), hotkey(notepad, "enter")
        self.assertEqual([k for k, _ in order], ["hk", "type", "hk"])
        # First hotkey is Ctrl+S, second is Enter.
        self.assertEqual(order[0][1][1:], ("ctrl", "s"))
        self.assertEqual(order[2][1][1:], ("enter",))
        # type() received the resolved SAVE_PATH as a string.
        import config as _cfg
        self.assertEqual(order[1][1][0], str(_cfg.SAVE_PATH))
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2], ("results", "saved"))
        self.assertEqual(nxt, "close")


class TestStateClose(unittest.TestCase):
    def test_clicks_close_tab_when_menu_visible(self):
        data = _make_data()
        with mock.patch.object(run, "is_visible", return_value=True), \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "wait_visible", return_value=True):
            nxt, _ = run.state_close(data)
        # Two clicks: FILE_MENU then CLOSE_TAB.
        self.assertEqual(mc.call_count, 2)
        mc.assert_any_call(data.notepad, run.FILE_MENU)
        mc.assert_any_call(data.notepad, run.CLOSE_TAB)
        self.assertIsNone(nxt)

    def test_skips_when_menu_invisible(self):
        # Defensive branch: if FILE_MENU isn't there, don't try to drive
        # a non-existent menu — log + end run.
        data = _make_data()
        with mock.patch.object(run, "is_visible", return_value=False), \
             mock.patch.object(run, "click") as mc, \
             mock.patch.object(run, "log") as ml:
            nxt, _ = run.state_close(data)
        mc.assert_not_called()
        self.assertIsNone(nxt)
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2],
                         ("results", "close_skipped"))


class TestStateMachineWiring(unittest.TestCase):
    """Top-level checks on the STATES dict — every state function in the
    module is registered, and the start_state in __main__ is one of them."""

    def test_all_states_registered(self):
        expected = {"init", "new_tab", "zoom_in", "zoom_out",
                    "type_time", "verify", "snapshot", "save", "close"}
        self.assertEqual(set(run.STATES), expected)

    def test_every_registered_state_is_callable(self):
        for name, fn in run.STATES.items():
            self.assertTrue(callable(fn), f"{name} must be callable")

    def test_apps_dict_well_formed(self):
        self.assertIsInstance(run.APPS, dict)
        self.assertGreaterEqual(len(run.APPS), 1)
        for k, v in run.APPS.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)
            self.assertTrue(v.endswith(".exe") or "/" in v or "\\" in v,
                            f"APPS[{k!r}] should look like an exe path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
