"""Unit tests for showcase.py — state-machine wiring and key state
functions. End-to-end behaviour is verified manually by running
`python showcase.py` against real Notepad + Calculator.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import showcase  # noqa: E402
from core import window  # noqa: E402


def _make_data():
    """Empty scratch namespace — windows live on `core.window` now."""
    return SimpleNamespace()


class _WindowFixture(unittest.TestCase):
    """Base: install mock notepad + calc handles in `window._windows`."""

    def setUp(self):
        self.notepad = mock.MagicMock(name="notepad_window")
        self.calc = mock.MagicMock(name="calc_window")
        window._windows["notepad"] = self.notepad
        window._windows["calc"] = self.calc

    def tearDown(self):
        window._reset()


class TestStateMachineWiring(unittest.TestCase):
    def test_all_states_registered(self):
        expected = {"init", "audit", "compute", "swap_back",
                    "compose_report", "click_family_demo",
                    "visual_snapshot", "save", "close", "summary"}
        self.assertEqual(set(showcase.STATES), expected)

    def test_apps_dict_has_two_entries(self):
        self.assertEqual(set(showcase.APPS), {"notepad", "calc"})

    def test_calc_digit_map_complete(self):
        self.assertEqual(set(showcase.CALC_DIGITS),
                         {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"})


class TestStateInit(_WindowFixture):
    def test_routes_to_audit_on_success(self):
        data = _make_data()
        with mock.patch.object(showcase, "wait_visible",
                               return_value=True), \
             mock.patch.object(showcase, "log"):
            nxt, _ = showcase.state_init(data)
        self.assertEqual(nxt, "audit")

    def test_aborts_when_apps_unresponsive(self):
        data = _make_data()
        with mock.patch.object(showcase, "wait_visible",
                               return_value=False), \
             mock.patch.object(showcase, "log"):
            nxt, _ = showcase.state_init(data)
        self.assertIsNone(nxt)


class TestStateAudit(_WindowFixture):
    def test_each_run_for_visibility_and_enabled(self):
        data = _make_data()
        with mock.patch.object(showcase, "each",
                               side_effect=[[True] * 10, [True] * 10]) as me, \
             mock.patch.object(showcase, "check_color",
                               return_value=(255, 255, 255)), \
             mock.patch.object(showcase, "is_color", return_value=True), \
             mock.patch.object(showcase, "read_info", return_value={
                 "class_name": "Edit", "visible": True, "enabled": True,
             }), \
             mock.patch.object(showcase, "log_csv") as mlc:
            nxt, _ = showcase.state_audit(data)
        # `each` called twice: digits_visible + digits_enabled.
        self.assertEqual(me.call_count, 2)
        # log_csv called once with two data rows + a header.
        mlc.assert_called_once()
        self.assertEqual(nxt, "compute")


class TestStateCompute(_WindowFixture):
    def test_full_compute_pipeline(self):
        data = _make_data()
        with mock.patch.object(showcase, "click_when_enabled"), \
             mock.patch.object(showcase, "click"), \
             mock.patch.object(showcase, "each") as me, \
             mock.patch.object(showcase, "click_after"), \
             mock.patch.object(showcase, "wait_enabled"), \
             mock.patch.object(showcase, "read_info",
                               return_value={"class_name": "Button",
                                             "automation_id": "plusButton"}), \
             mock.patch.object(showcase, "hotkey"), \
             mock.patch.object(showcase, "read_clipboard",
                               return_value="79"), \
             mock.patch.object(showcase, "wait"), \
             mock.patch.object(showcase, "log"):
            nxt, out = showcase.state_compute(data)
        # Two each() calls — one for "47" (4 then 7) and one for "32".
        self.assertEqual(me.call_count, 2)
        # Result lifted from clipboard onto data namespace.
        self.assertEqual(out.calc_result, "79")
        self.assertEqual(nxt, "swap_back")


class TestStateClickFamilyDemo(_WindowFixture):
    def test_double_and_right_click_both_called(self):
        data = _make_data()
        with mock.patch.object(showcase, "double_click") as mdc, \
             mock.patch.object(showcase, "right_click") as mrc, \
             mock.patch.object(showcase, "hotkey"), \
             mock.patch.object(showcase, "wait"), \
             mock.patch.object(showcase, "log"):
            nxt, _ = showcase.state_click_family_demo(data)
        mdc.assert_called_once()
        mrc.assert_called_once()
        self.assertEqual(nxt, "visual_snapshot")

    def test_swallows_exceptions(self):
        # Notepad versions vary — context menus may fail to open. The
        # demo logs and continues rather than crashing the run.
        data = _make_data()
        with mock.patch.object(showcase, "double_click",
                               side_effect=RuntimeError("no menu")), \
             mock.patch.object(showcase, "log") as ml:
            nxt, _ = showcase.state_click_family_demo(data)
        self.assertEqual(nxt, "visual_snapshot")
        ml.assert_called_once()
        self.assertEqual(ml.call_args[0][:2], ("showcase", "click_family_warn"))


class TestStateSave(_WindowFixture):
    def test_save_via_hotkey_type_enter_inside_no_dismiss(self):
        """state_save wraps the Ctrl+S → type → Enter sequence in
        `no_dismiss()` so the auto-dismiss doesn't kill the Save dialog
        before we type into it."""
        from core import verbs
        data = _make_data()
        events = []  # (name, args, dismiss_depth_at_call)

        def _record(name):
            return lambda *a, **k: events.append(
                (name, a, getattr(verbs._dismiss_paused, "depth", 0))
            )

        with mock.patch.object(showcase, "hotkey", side_effect=_record("hk")), \
             mock.patch.object(showcase, "type", side_effect=_record("type")), \
             mock.patch.object(showcase, "key", side_effect=_record("key")), \
             mock.patch.object(showcase, "wait"), \
             mock.patch.object(showcase, "log"):
            nxt, out = showcase.state_save(data)
        # Sequence: hotkey(Ctrl+Shift+S), type(path), key("enter") —
        # focus-targeted so the confirm lands on the Save dialog, not
        # Notepad's editor.
        self.assertEqual([n for n, _, _ in events], ["hk", "type", "key"])
        self.assertEqual(events[0][1][1:], ("ctrl", "shift", "s"))
        self.assertEqual(events[2][1], ("enter",))
        # Every recorded call observed dismiss depth >= 1, proving the
        # whole sequence ran inside `no_dismiss()`.
        for name, _, depth in events:
            self.assertGreaterEqual(
                depth, 1,
                f"{name} ran with dismiss depth={depth}; expected >=1",
            )
        self.assertTrue(hasattr(out, "report_path"))
        self.assertEqual(nxt, "close")


class TestVerbsTouched(unittest.TestCase):
    """Sanity: every public verb listed in showcase.py's docstring is
    actually imported/used somewhere in the module. Catches accidental
    drift between documented and demonstrated capabilities."""

    def test_every_verb_imported(self):
        expected = {
            "click", "double_click", "right_click",
            "click_when_enabled", "click_after",
            "fill", "type", "key", "hotkey",
            "is_visible", "is_enabled", "is_color", "check_color",
            "wait_visible", "wait_enabled", "wait_gone",
            "read_info", "read_clipboard", "each", "no_dismiss",
            "screenshot",
            "log", "log_csv", "now", "wait",
        }
        for verb in expected:
            self.assertTrue(hasattr(showcase, verb),
                            f"showcase.py should reference {verb}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
