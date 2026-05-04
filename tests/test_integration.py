"""Integration tests — drive a real Notepad and verify snapshot/drift/resolve work.

These tests open Notepad (Win11 modern), so they require a Windows machine
with notepad.exe and the harness's deps installed. Each test isolates state
under a fresh temp dir so it cannot pollute (or be polluted by) the demo run.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil
import uiautomation as auto

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import actions, apps, db, tree  # noqa: E402


def _kill_notepad():
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == "notepad.exe":
                p.kill()
        except Exception:
            pass
    time.sleep(2.0)


class WindowsUITestBase(unittest.TestCase):
    """Per-test isolation: redirect DB_PATH and snapshot dir to a temp area,
    so the production data/runs.db and data/snapshots/ are untouched."""

    @classmethod
    def setUpClass(cls):
        if os.name != "nt":
            raise unittest.SkipTest("Windows-only integration tests")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness_int_"))
        self._orig_db = config.DB_PATH
        self._orig_snap = config.TREE_SNAPSHOT_DIR
        config.DB_PATH = str(self.tmp / "runs.db")
        config.TREE_SNAPSHOT_DIR = self.tmp / "snapshots"
        # reset internal caches that would otherwise leak between tests
        actions._logged_diffs.clear()
        db._known_tables.clear()
        _kill_notepad()
        apps.open_app("notepad.exe")
        time.sleep(2.5)
        self.win = apps.get_window("Notepad")
        apps.bring_to_foreground(self.win)
        # dismiss any "cannot find file" startup dialog from prior sessions
        self._dismiss_ok_popups()

    def tearDown(self):
        config.DB_PATH = self._orig_db
        config.TREE_SNAPSHOT_DIR = self._orig_snap
        _kill_notepad()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dismiss_ok_popups(self, max_passes=4):
        import pyautogui
        for _ in range(max_passes):
            walked = tree.walk_live(self.win)
            ok = next(
                (n["ctrl"] for n in walked
                 if n["name"] == "OK" and n["role"] == "ButtonControl"),
                None,
            )
            if ok is None:
                return
            r = ok.BoundingRectangle
            if r.right - r.left <= 0:
                return
            pyautogui.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            time.sleep(0.5)

    def _drift_rows(self):
        if not Path(config.DB_PATH).exists():
            return []
        conn = sqlite3.connect(config.DB_PATH)
        try:
            return conn.execute("SELECT * FROM drift").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


class TestWalkLive(WindowsUITestBase):
    def test_walk_returns_window_plus_descendants(self):
        walked = tree.walk_live(self.win)
        self.assertGreater(len(walked), 5,
                           "Notepad should expose more than a handful of nodes")
        # First entry is the window itself
        self.assertTrue(walked[0]["tree_id"].endswith(":WindowControl"))
        # Every node carries the keys the rest of the harness depends on
        for n in walked[:10]:
            self.assertIn("tree_id", n)
            self.assertIn("name", n)
            self.assertIn("role", n)
            self.assertIn("bbox", n)
            self.assertIn("enabled", n)
            self.assertIn("ctrl", n)

    def test_find_locates_top_level_menu_buttons(self):
        walked = tree.walk_live(self.win)
        for menu in ("File", "Edit", "View"):
            ctrl = tree.find(walked, f"{menu}:MenuItemControl")
            self.assertIsNotNone(ctrl, f"{menu} menu must be findable via leaf fallback")


class TestSnapshotIntegration(WindowsUITestBase):
    def test_save_and_reload_match(self):
        path = tree.snapshot_path(self.win)
        self.assertFalse(path.exists())
        saved = tree.save_snapshot(self.win)
        self.assertTrue(path.exists())
        loaded = tree.load_snapshot(self.win)
        self.assertEqual(saved, loaded)

    def test_ensure_snapshot_first_call_creates_then_reuses(self):
        first, created1 = tree.ensure_snapshot(self.win)
        self.assertTrue(created1)
        second, created2 = tree.ensure_snapshot(self.win)
        self.assertFalse(created2)
        self.assertEqual(first, second)


class TestDriftDetection(WindowsUITestBase):
    """Saving a baseline, then expanding a menu must produce a drift entry
    whose `added` set includes the popup elements the click revealed."""

    def test_opening_file_menu_logs_drift_against_baseline(self):
        tree.save_snapshot(self.win)
        actions.press(self.win, "File:MenuItemControl")
        time.sleep(0.6)
        # press "New tab" so we re-walk and run drift detection at least once
        actions.press(self.win, "New tab:MenuItemControl")
        time.sleep(0.6)

        rows = self._drift_rows()
        self.assertGreater(len(rows), 0, "drift table should have entries")
        # rows: (ts, key TEXT, added_count INT, removed_count INT,
        #        added_sample JSON, removed_sample JSON)
        added_counts = [r[2] for r in rows]
        self.assertTrue(any(c > 0 for c in added_counts),
                        "at least one drift row should report added nodes")
        # Inspect the added sample — should include something menu-popup-shaped
        import json
        all_added_samples = []
        for r in rows:
            try:
                all_added_samples.extend(json.loads(r[4]))
            except Exception:
                pass
        joined = " ".join(all_added_samples)
        self.assertIn("MenuFlyout", joined,
                      "drift should reveal the MenuFlyout popup that File>... opens")

    def test_no_drift_against_fresh_snapshot(self):
        # Snapshot the live state, then immediately re-walk & compare:
        # added/removed should be empty (transient TeachingTips can flicker, so
        # we only require that nothing structurally substantial appears).
        snap = tree.save_snapshot(self.win)
        live = tree.to_serializable(tree.walk_live(self.win))
        diff = tree.compute_diff(snap, live)
        # Modern Notepad has a few zero-bbox TeachingTip helpers that wink in
        # and out — accept up to a small handful of phantom diffs.
        self.assertLessEqual(len(diff["added"]), 5)
        self.assertLessEqual(len(diff["removed"]), 5)


class TestResolveTimeout(WindowsUITestBase):
    """`_resolve` must raise TimeoutError after RESOLVE_TIMEOUT_SEC, not hang."""

    def test_resolve_raises_for_missing_element(self):
        original = config.RESOLVE_TIMEOUT_SEC
        config.RESOLVE_TIMEOUT_SEC = 1
        try:
            t0 = time.time()
            with self.assertRaises(TimeoutError):
                actions._resolve(self.win, "DefinitelyNotARealElement:ButtonControl")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 5.0, "should fail fast (within ~timeout)")
            self.assertGreaterEqual(elapsed, 0.9, "should honour the timeout")
        finally:
            config.RESOLVE_TIMEOUT_SEC = original


class TestClickActuallyFires(WindowsUITestBase):
    """Regression for the SendInput-vs-mouse_event WinUI bug.

    On Win11 Notepad, `pyautogui.click` (and any mouse_event-based click)
    silently no-ops on MenuFlyout items: the cursor moves, the press is
    logged, but the menu item never activates. The fix is `SendInput`-based
    clicks in actions._cursor_click. This test guards against regressing
    back to the silent-no-op behaviour: it asserts that a real menu-item
    press has the side effect the menu item promises (closes a tab)."""

    def _count_tabs(self):
        return sum(1 for n in tree.walk_live(self.win) if n["role"] == "TabItemControl")

    def test_close_tab_menu_item_actually_closes_a_tab(self):
        # add a known-extra tab
        actions.press(self.win, "File:MenuItemControl")
        actions.press(self.win, "New tab:MenuItemControl")
        time.sleep(0.8)
        apps.bring_to_foreground(self.win)
        n_before = self._count_tabs()

        # close it via File > Close tab — must actually close
        actions.press(self.win, "File:MenuItemControl")
        actions.press(self.win, "Close tab:MenuItemControl")
        time.sleep(1.5)

        n_after = self._count_tabs()
        self.assertLess(
            n_after, n_before,
            f"Close tab press must reduce tab count: before={n_before} after={n_after}. "
            "If this fails, actions._cursor_click probably regressed to mouse_event."
        )


class TestCheckActive(WindowsUITestBase):
    """check_active / is_present must return booleans (not raise) for both
    found and missing elements, and must reflect the live state."""

    def test_check_active_returns_true_for_visible_enabled_button(self):
        # The File menu top-level button is always visible and enabled.
        self.assertTrue(actions.check_active(self.win, "File:MenuItemControl"))

    def test_check_active_returns_false_for_nonexistent_id(self):
        # No element has this id — must return False quickly, not loop.
        t0 = time.time()
        result = actions.check_active(self.win, "NotARealThing:ButtonControl")
        elapsed = time.time() - t0
        self.assertFalse(result)
        self.assertLess(elapsed, 1.0,
                        "check_active(timeout=0) must fail fast for missing ids")

    def test_check_active_respects_timeout_for_late_arrivals(self):
        # Open the File menu, then check that "New tab" shows up — but only
        # if we give it a moment to render. With timeout=0 the very first
        # walk usually catches it; with a short timeout it always does.
        actions.press(self.win, "File:MenuItemControl")
        self.assertTrue(actions.check_active(self.win, "New tab:MenuItemControl",
                                             timeout=2.0))
        # close the menu so the next test starts clean
        import pyautogui as _p
        _p.press("escape")
        time.sleep(0.4)

    def test_is_present_true_for_visible(self):
        self.assertTrue(actions.is_present(self.win, "File:MenuItemControl"))

    def test_is_present_false_for_missing(self):
        self.assertFalse(actions.is_present(self.win, "NotARealThing:ButtonControl"))

    def test_check_active_in_if_statement(self):
        # Sanity: this is the user-facing pattern they asked about.
        if actions.check_active(self.win, "File:MenuItemControl"):
            actions.press(self.win, "File:MenuItemControl")
            time.sleep(0.4)
        if actions.is_present(self.win, "New tab:MenuItemControl"):
            self.assertTrue(actions.check_active(self.win, "New tab:MenuItemControl"))
        import pyautogui as _p
        _p.press("escape")
        time.sleep(0.4)


class TestPopupDismissDuringEach(WindowsUITestBase):
    """`each()` snapshots HWNDs at entry and pre-dismisses any
    unexpected top-level window. Spawn a foreign top-level via
    subprocess (tkinter Tk()), run each() against Notepad, and verify
    the popup gets auto-dismissed mid-loop."""

    def test_each_dismisses_unexpected_top_level_popup(self):
        import subprocess
        import sys as _sys
        from core import verbs

        # Mark everything currently visible as expected so only the
        # popup we're about to spawn counts as unexpected.
        verbs._seed_expected_from_current()

        # Spawn a foreign top-level window. Title 'AT_TEST_POPUP' is
        # unique so we can locate it. The `after(30s, destroy)` is a
        # backstop in case auto-dismiss fails — we never want a
        # zombie tk process to outlive the test.
        popup = subprocess.Popen([
            _sys.executable, "-c",
            "import tkinter\n"
            "r = tkinter.Tk()\n"
            "r.title('AT_TEST_POPUP')\n"
            "r.geometry('320x160')\n"
            "r.after(30000, r.destroy)\n"
            "r.mainloop()\n",
        ])
        try:
            # Wait for the window to render.
            time.sleep(2.5)
            self.assertIsNone(popup.poll(),
                              "popup subprocess should still be alive")

            # Sanity: the popup is in the live HWND enumeration.
            from core.app import _enumerate_top_level_hwnds
            from core.verbs import _hwnd_title
            popup_hwnds = [h for h in _enumerate_top_level_hwnds()
                           if _hwnd_title(h) == "AT_TEST_POPUP"]
            self.assertGreaterEqual(
                len(popup_hwnds), 1,
                "AT_TEST_POPUP must be visible at the start of the test",
            )

            # Run each() — its entry will pre-dismiss the foreign popup
            # because it's not in `_expected_hwnds`. The test verb is a
            # plain lambda so we measure dismiss behaviour, not click
            # mechanics.
            results = []
            verbs.each(
                lambda w, ctrl_id: results.append(ctrl_id),
                self.win,
                ["A", "B", "C"],
            )
            self.assertEqual(results, ["A", "B", "C"])

            # Give WM_CLOSE / mainloop a moment to actually tear down.
            for _ in range(30):
                if popup.poll() is not None:
                    break
                time.sleep(0.2)
            self.assertIsNotNone(
                popup.poll(),
                "popup subprocess should have exited after auto-dismiss",
            )
        finally:
            if popup.poll() is None:
                popup.kill()
                popup.wait(timeout=3)


class TestVerbPostConditions(WindowsUITestBase):
    """End-to-end verification that each verb produces the side effect
    its docstring promises — not just "didn't raise." Covers the five
    verbs that didn't have a strict integration test before:
    `right_click`, `double_click`, `click_after`, `key`, `is_color`.
    """

    def setUp(self):
        super().setUp()
        from core import verbs
        # Notepad was launched via `apps.open_app` (not `match()`), so
        # its HWND/PID isn't in the verb-level expected/trusted sets.
        # CRITICAL: seed `_expected_hwnds` from the *entire* current
        # top-level HWND list first — otherwise the action verb
        # decorator's pre-dismiss will WM_CLOSE the developer's
        # terminal, IDE, browser, etc. (anything not owned by Notepad)
        # the first time a verb runs in the test.
        verbs._seed_expected_from_current()
        verbs._mark_hwnd_expected(self.win.NativeWindowHandle)

    def _editor_text(self):
        """Read the editor's current text via UIA ValuePattern."""
        for n in tree.walk_live(self.win):
            if n["role"] == "DocumentControl":
                try:
                    return n["ctrl"].GetValuePattern().Value or ""
                except Exception:
                    return ""
        return ""

    def test_right_click_opens_context_menu(self):
        """`right_click(EDITOR)` must open a context menu. Win11
        Notepad's context menu lives in a separate `Pop-upHost`
        top-level HWND (NOT inside `self.win`'s tree), so the proof is
        a NEW top-level HWND that contains MenuItemControls."""
        from core import verbs
        from core.app import _enumerate_top_level_hwnds
        # Plant a marker so the editor isn't empty.
        actions.write_text(self.win, "Text editor:DocumentControl",
                           "right_click_target")
        time.sleep(0.3)

        before = set(_enumerate_top_level_hwnds())
        verbs.right_click(self.win, "Text editor:DocumentControl")
        time.sleep(0.7)
        new_hwnds = set(_enumerate_top_level_hwnds()) - before

        try:
            self.assertGreater(
                len(new_hwnds), 0,
                "right_click should spawn a context-menu HWND",
            )
            # The new HWND should contain at least one MenuItemControl
            # (proves it's actually a menu, not some unrelated popup).
            menu_item_total = 0
            for h in new_hwnds:
                try:
                    ctrl = auto.ControlFromHandle(h)
                except Exception:
                    continue
                if ctrl is None:
                    continue
                walked = tree.walk_live(ctrl)
                menu_item_total += sum(1 for n in walked
                                       if n["role"] == "MenuItemControl")
            self.assertGreater(
                menu_item_total, 0,
                f"right_click should produce a menu with MenuItemControls; "
                f"new HWNDs were {new_hwnds} but contained none",
            )
        finally:
            import pyautogui
            pyautogui.press("escape")
            time.sleep(0.3)

    def test_double_click_selects_word(self):
        """`double_click(EDITOR)` must select the word under the click
        point. Verified by Ctrl+C → `read_clipboard`: the clipboard
        should contain just that one word, not the whole document."""
        from core import verbs
        # Fill the editor with a sea of one repeated word so the click
        # center hits a "MARKER" no matter where it lands. Whitespace
        # is the word boundary, so double-click selects exactly one
        # MARKER (length 6), not the whole field.
        body = "MARKER " * 80
        actions.write_text(self.win, "Text editor:DocumentControl", body)
        time.sleep(0.3)
        # Clear clipboard so a stale value can't masquerade as success.
        import pyperclip
        pyperclip.copy("__pre_test_clipboard__")

        verbs.double_click(self.win, "Text editor:DocumentControl")
        time.sleep(0.3)
        # Use raw pyautogui so we don't drag focus around.
        import pyautogui
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)

        clipboard = pyperclip.paste().strip()
        self.assertEqual(
            clipboard, "MARKER",
            f"double_click should select exactly one word; got {clipboard!r}",
        )

    def test_move_positions_cursor_without_clicking(self):
        """`move(EDITOR)` must put the OS cursor at the editor center
        AND not actuate anything. Verified by reading the cursor
        position via Win32 GetCursorPos AND confirming the editor's
        text is unchanged (no click → no caret moved → no text
        inserted)."""
        from core import verbs
        import ctypes
        from ctypes import wintypes

        actions.write_text(self.win, "Text editor:DocumentControl",
                           "move_test_marker")
        time.sleep(0.3)
        text_before = self._editor_text() if hasattr(self, "_editor_text") else None

        verbs.move(self.win, "Text editor:DocumentControl")
        time.sleep(0.2)

        # Verify cursor is inside the editor's bbox.
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        # Find the editor in the live walk to get its bbox.
        editor = next(
            (n for n in tree.walk_live(self.win)
             if n["role"] == "DocumentControl"),
            None,
        )
        self.assertIsNotNone(editor, "editor should exist in live walk")
        l, t, r, b = editor["bbox"]
        self.assertTrue(
            l <= pt.x <= r and t <= pt.y <= b,
            f"cursor at ({pt.x}, {pt.y}) outside editor bbox {editor['bbox']}",
        )
        # Editor text unchanged — `move` didn't click, didn't reposition
        # caret, didn't insert anything.
        if text_before is not None:
            self.assertEqual(self._editor_text(), text_before,
                             "move() must not alter editor content")

    def test_click_after_honours_delay_and_clicks(self):
        """`click_after(id, delay)` sleeps for `delay`, then clicks.
        Verifies BOTH halves: the elapsed time matches the delay (within
        slack) AND the click actually landed (File menu opens)."""
        from core import verbs
        delay = 0.8
        t0 = time.time()
        verbs.click_after(self.win, "File:MenuItemControl", delay=delay)
        elapsed = time.time() - t0
        try:
            # Lower bound: delay actually happened (allow 50ms slack
            # for scheduler jitter).
            self.assertGreaterEqual(
                elapsed, delay - 0.05,
                f"click_after should sleep for at least {delay}s; "
                f"observed {elapsed:.3f}s",
            )
            # Upper bound: not absurdly slow (delay + click time + UIA
            # resolution; 5s is generous for CI).
            self.assertLess(
                elapsed, 5.0,
                f"click_after took {elapsed:.3f}s — slower than expected",
            )
            # Click landed: File menu opened, so New tab is now reachable.
            self.assertTrue(
                actions.is_present(self.win, "New tab:MenuItemControl",
                                   timeout=2),
                "click_after should have fired the File-menu click; "
                "menu didn't open",
            )
        finally:
            import pyautogui
            pyautogui.press("escape")
            time.sleep(0.3)

    def test_key_sends_combo_to_current_focus(self):
        """`key("ctrl", "a")` + `key("ctrl", "c")` must operate on
        whatever currently has keyboard focus — proven by typing
        text, selecting + copying via `key`, and round-tripping
        through the clipboard."""
        from core import verbs
        marker = "key_verb_marker_xyz123"
        actions.write_text(self.win, "Text editor:DocumentControl", marker)
        time.sleep(0.3)
        import pyperclip
        pyperclip.copy("__pre_key_test__")

        verbs.key("ctrl", "a")
        time.sleep(0.2)
        verbs.key("ctrl", "c")
        time.sleep(0.3)

        clipboard = pyperclip.paste().strip()
        self.assertIn(
            marker, clipboard,
            f"key('ctrl','a') + key('ctrl','c') should round-trip the "
            f"editor text; clipboard={clipboard!r}",
        )

    def test_is_color_distinguishes_match_from_mismatch(self):
        """`is_color` must return True for the actual sampled color
        AND False for a deliberately-wrong color. Self-match alone is
        a tautology — discriminative power is what matters."""
        from core import verbs
        actual = verbs.check_color(self.win, "File:MenuItemControl")
        # True path: exact match against the live sample.
        self.assertTrue(
            verbs.is_color(self.win, "File:MenuItemControl", actual,
                           tolerance=0),
            f"is_color should return True for the actual sampled "
            f"color {actual}",
        )
        # False path: a color obviously different from the actual one.
        # Pick the channel-flipped opposite so it can't accidentally
        # be within tolerance.
        wrong = tuple(255 - c for c in actual)
        # If the actual color is near-grey-50 the flip might still be
        # close; bias toward magenta to be safe.
        if max(abs(a - w) for a, w in zip(actual, wrong)) < 100:
            wrong = (255, 0, 255) if actual != (255, 0, 255) else (0, 255, 0)
        self.assertFalse(
            verbs.is_color(self.win, "File:MenuItemControl", wrong,
                           tolerance=0),
            f"is_color should return False for {wrong} when actual "
            f"is {actual}",
        )


class TestWaitUntilAbsent(WindowsUITestBase):
    def test_returns_true_after_menu_closed(self):
        # Open File menu, confirm New tab is in tree, press Esc, assert
        # wait_until_absent returns True quickly.
        actions.press(self.win, "File:MenuItemControl")
        self.assertTrue(actions.is_present(self.win,
                                           "New tab:MenuItemControl",
                                           timeout=2))
        import pyautogui
        pyautogui.press("escape")
        t0 = time.time()
        gone = actions.wait_until_absent(self.win,
                                         "New tab:MenuItemControl",
                                         timeout=3)
        elapsed = time.time() - t0
        self.assertTrue(gone, "wait_until_absent must detect menu close")
        self.assertLess(elapsed, 3.0)

    def test_returns_false_for_persistent_element(self):
        # File:MenuItemControl is the menu bar's File button — always present.
        t0 = time.time()
        gone = actions.wait_until_absent(self.win, "File:MenuItemControl",
                                         timeout=0.5)
        elapsed = time.time() - t0
        self.assertFalse(gone)
        self.assertGreaterEqual(elapsed, 0.4,
                                "should honour the timeout (not return early)")


class TestStructIdLive(WindowsUITestBase):
    """End-to-end: walk Notepad, look up a known control's struct_id from
    the live tree, then press the same control by struct_id. Confirms the
    new addressing scheme works on a real WinUI app."""

    def test_press_by_struct_id(self):
        # Find the File menu in the live walk and grab its struct_id.
        walked = tree.walk_live(self.win)
        file_node = next(
            (n for n in walked
             if n["name"] == "File" and n["role"] == "MenuItemControl"),
            None,
        )
        self.assertIsNotNone(file_node, "File menu must be in the walk")
        struct_id = file_node["struct_id"]
        # struct_id should be dotted digits (e.g. "0.2.0.0.0")
        self.assertTrue(tree._is_struct_id(struct_id),
                        f"unexpected struct_id format: {struct_id!r}")

        # Press by struct_id — auto-foreground + cursor click should
        # happen, click should land, press row should be in the DB.
        actions.press(self.win, struct_id)
        time.sleep(0.6)

        conn = sqlite3.connect(config.DB_PATH)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT c0 FROM press ORDER BY ts DESC LIMIT 1"
            )]
        finally:
            conn.close()
        self.assertEqual(ids, [struct_id])

        # Side effect check: clicking File opened the menu, so a popup
        # subtree should now be in the live walk.
        self.assertTrue(
            actions.is_present(self.win, "New tab:MenuItemControl",
                               timeout=2),
            "File menu should have opened from struct_id press",
        )
        # Close the menu so teardown is clean.
        import pyautogui
        pyautogui.press("escape")
        time.sleep(0.3)


class TestAutoForeground(WindowsUITestBase):
    """Every action ensures its window is foreground before clicking —
    no need for the caller to call apps.bring_to_foreground."""

    def test_press_brings_minimized_window_forward(self):
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = self.win.NativeWindowHandle
        # Minimize Notepad (SW_MINIMIZE = 6).
        user32.ShowWindow(hwnd, 6)
        time.sleep(0.7)
        # Precondition: window IS minimized. Use IsIconic which directly
        # reports minimize state (more reliable than GetForegroundWindow,
        # which can race with the OS message queue).
        self.assertTrue(
            bool(user32.IsIconic(hwnd)),
            "precondition: notepad should be minimized at this point",
        )

        # press() should restore + foreground + click without any
        # explicit bring_to_foreground call from the test.
        actions.press(self.win, "File:MenuItemControl")
        time.sleep(0.7)

        # Postcondition 1: window is no longer minimized.
        self.assertFalse(
            bool(user32.IsIconic(hwnd)),
            "press() must restore the minimized window",
        )
        # Postcondition 2: window is foreground.
        self.assertEqual(
            user32.GetForegroundWindow(), hwnd,
            "press() must auto-foreground its window",
        )
        # Postcondition 3: the click landed — File menu opened.
        self.assertTrue(
            actions.is_present(self.win, "New tab:MenuItemControl",
                               timeout=2),
            "File menu should have opened",
        )
        import pyautogui
        pyautogui.press("escape")
        time.sleep(0.3)


class TestAppsIsRunning(WindowsUITestBase):
    def test_is_running_finds_notepad(self):
        self.assertTrue(apps.is_running("notepad.exe"))

    def test_is_running_partial_match(self):
        self.assertTrue(apps.is_running("notepad"))

    def test_is_running_false_for_nonexistent(self):
        self.assertFalse(apps.is_running("definitelynotanapp.exe"))


class TestMultiAppMatchAndFingerprint(WindowsUITestBase):
    """End-to-end stress: multi-app, fingerprint capture, `match()`,
    tree-structure changes, close-and-reopen, window swap.

    This class drives both Notepad and Calculator so the multi-app
    code paths (`_classify_window`, the runtime `match`, and the
    `data.<name>` runner attribute logic) exercise real UIA shapes.
    """

    @classmethod
    def setUpClass(cls):
        if os.name != "nt":
            raise unittest.SkipTest("Windows-only integration tests")
        # Skip if Calculator isn't available (sandbox / Windows Server).
        if shutil_which("calc.exe") is None and shutil_which("calc") is None:
            raise unittest.SkipTest("calc.exe not on PATH")

    def setUp(self):
        super().setUp()
        # Override fingerprint dir so we don't pollute any pre-existing
        # data/window_fingerprints/ on this machine.
        self._orig_fp = config.WINDOW_FINGERPRINT_DIR
        config.WINDOW_FINGERPRINT_DIR = self.tmp / "fingerprints"
        config.WINDOW_FINGERPRINT_DIR.mkdir(parents=True, exist_ok=True)
        _kill_calc()

    def tearDown(self):
        config.WINDOW_FINGERPRINT_DIR = self._orig_fp
        _kill_calc()
        super().tearDown()

    def _open_calc(self):
        apps.open_app("calc.exe")
        # Calculator on Win11 is a UWP app — the launcher exits and
        # the actual window is owned by a different process. Poll.
        deadline = time.time() + 10
        win = None
        while time.time() < deadline:
            for w in auto.GetRootControl().GetChildren():
                try:
                    if (w.ClassName or "").lower().startswith("application"):
                        if "calc" in (w.Name or "").lower():
                            win = w
                            break
                except Exception:
                    continue
            if win is not None:
                break
            time.sleep(0.5)
        if win is None:
            self.skipTest("Calculator window did not appear within 10s")
        return win

    def test_fingerprint_captures_and_persists(self):
        """Compute a fingerprint of live Notepad and round-trip it
        through the sidecar. Loaded fingerprint matches the live one
        within similarity tolerance."""
        fp = tree.fingerprint(self.win)
        self.assertGreater(len(fp), 5,
                           "Notepad's depth-4 walk should have >5 nodes")
        # All entries must be (depth, role) tuples.
        for entry in fp:
            self.assertEqual(len(entry), 2)
            self.assertIsInstance(entry[0], int)
            self.assertIsInstance(entry[1], str)
        path = tree.save_fingerprint("notepad_test", fp)
        self.assertTrue(path.exists())
        loaded = tree.load_fingerprint("notepad_test")
        self.assertEqual(loaded, fp)
        # Re-walking should produce a near-identical fingerprint.
        fp2 = tree.fingerprint(self.win)
        self.assertGreaterEqual(tree.similarity(fp, fp2), 0.95,
                                "two consecutive walks of the same window "
                                "should be ~identical in shape")

    def test_match_finds_app_by_fingerprint(self):
        """Save a fingerprint for Notepad, then call match() with the
        already-open fast path. Returns the live Notepad control (same
        HWND as `self.win`)."""
        from core import app
        tree.save_fingerprint("notepad_test", tree.fingerprint(self.win))
        result = app.match("notepad_test", launch="notepad.exe")
        self.assertIsNotNone(result, "match should find Notepad")
        self.assertEqual(result.NativeWindowHandle,
                         self.win.NativeWindowHandle)

    def test_match_returns_none_when_no_sidecar(self):
        # Same silent-fail contract for both modes.
        from core import app
        self.assertIsNone(
            app.match("never_inspected_window", launch="notepad.exe"),
        )
        self.assertIsNone(
            app.match("never_inspected_window", launch="popup"),
        )

    def test_match_distinguishes_notepad_from_calc(self):
        """Both apps open simultaneously. match() must pick the right
        one from the fingerprint sidecar (fast path — both already open
        means no Popen fires)."""
        from core import app
        calc_win = self._open_calc()
        try:
            tree.save_fingerprint("notepad_test", tree.fingerprint(self.win))
            tree.save_fingerprint("calc_test", tree.fingerprint(calc_win))

            np_match = app.match("notepad_test", launch="notepad.exe")
            calc_match = app.match("calc_test", launch="calc.exe")
            self.assertEqual(np_match.NativeWindowHandle,
                             self.win.NativeWindowHandle,
                             "match('notepad') must pick the Notepad HWND")
            self.assertEqual(calc_match.NativeWindowHandle,
                             calc_win.NativeWindowHandle,
                             "match('calc') must pick the Calc HWND, "
                             "not Notepad — fingerprint shapes differ enough")
        finally:
            _kill_calc()

    def test_match_relocates_after_close_and_reopen(self):
        """Save fingerprint → close Notepad → match with launch
        relaunches and finds the new instance with a fresh HWND."""
        from core import app
        tree.save_fingerprint("notepad_test", tree.fingerprint(self.win))
        old_hwnd = self.win.NativeWindowHandle

        _kill_notepad()
        # match with launch=exe re-opens it.
        result = app.match("notepad_test", launch="notepad.exe", timeout=15)
        self.assertIsNotNone(result, "match with launch should relaunch + find")
        self.assertNotEqual(result.NativeWindowHandle, old_hwnd,
                            "relaunched Notepad has a fresh HWND")
        # Re-establish self.win for tearDown / subsequent tests.
        self.win = result

    def test_fingerprint_tolerates_menu_open(self):
        """Open the File menu (which adds a popup subtree to the live
        UIA tree) and confirm the depth-limited fingerprint of the
        WINDOW (not the popup) still matches the saved baseline. Depth
        limit is the key — popups live deeper than 4."""
        baseline = tree.fingerprint(self.win)
        actions.press(self.win, "File:MenuItemControl")
        try:
            time.sleep(0.6)
            opened = tree.fingerprint(self.win)
            score = tree.similarity(baseline, opened)
            self.assertGreaterEqual(
                score, config.FINGERPRINT_THRESHOLD,
                f"opening File menu should not break window fingerprint "
                f"(score={score:.2f})",
            )
        finally:
            import pyautogui
            pyautogui.press("escape")
            time.sleep(0.4)

    def test_swap_between_apps_and_back(self):
        """Drive Notepad, swap to Calculator, do something there, swap
        back to Notepad and confirm it's still functional. Verifies the
        cross-process bring-to-foreground + verb dispatch doesn't get
        confused about which window is "current"."""
        from core import verbs
        calc_win = self._open_calc()
        try:
            # Step 1: Notepad — write a marker.
            apps.bring_to_foreground(self.win)
            self.assertTrue(verbs.is_visible(self.win, "File:MenuItemControl"),
                            "Notepad's File menu should be visible")

            # Step 2: Calculator — bring it forward and confirm UIA sees it.
            apps.bring_to_foreground(calc_win)
            time.sleep(0.5)
            calc_walked = tree.walk_live(calc_win)
            self.assertGreater(len(calc_walked), 5,
                               "Calculator walk should have >5 nodes")

            # Step 3: Hop back to Notepad. Verbs must still resolve
            # against self.win — the runner-style "window.notepad"
            # pattern works because the window control object is stable.
            apps.bring_to_foreground(self.win)
            time.sleep(0.5)
            self.assertTrue(verbs.is_visible(self.win, "File:MenuItemControl"),
                            "after swap-back, File menu must still be visible")
        finally:
            _kill_calc()


class TestTwoSameAppInstances(WindowsUITestBase):
    """Two Notepad windows open simultaneously: drive both, swap focus,
    write distinct text in each, verify each retained its own content.

    Documents the *expected* shape of this workflow (user obtains
    individual `Control` references via top-level window enumeration —
    `match()` with fingerprint returns the *first* identical window and
    can't disambiguate two structurally identical instances)."""

    def setUp(self):
        super().setUp()
        # Open a SECOND notepad on top of the one setUp launched.
        apps.open_app("notepad.exe")
        time.sleep(2.5)
        self.windows = self._all_notepad_windows()
        if len(self.windows) < 2:
            self.skipTest(
                "could not open two simultaneous Notepad top-level windows "
                "on this machine (some Win11 builds tab everything into one)"
            )

    def _all_notepad_windows(self):
        out = []
        for w in auto.GetRootControl().GetChildren():
            try:
                pid = w.ProcessId
            except Exception:
                continue
            try:
                stem = (psutil.Process(pid).name() or "").lower().rsplit(".", 1)[0]
            except Exception:
                continue
            if stem != "notepad":
                continue
            if not w.Name:
                continue
            out.append(w)
        return out

    def _editor_text(self, win):
        """Read the current document text via UIA ValuePattern."""
        for n in tree.walk_live(win):
            if n["role"] == "DocumentControl":
                try:
                    return n["ctrl"].GetValuePattern().Value or ""
                except Exception:
                    return ""
        return ""

    def test_can_drive_two_notepads_independently(self):
        """Each Notepad instance should accept independent fill+verify
        cycles. Confirms `apps.bring_to_foreground` correctly targets a
        specific HWND and the verbs follow the foreground window."""
        win_a, win_b = self.windows[0], self.windows[1]
        self.assertNotEqual(win_a.NativeWindowHandle,
                            win_b.NativeWindowHandle,
                            "two top-level Notepad windows have different HWNDs")

        # Round 1: write distinct markers.
        text_a = "alpha_run_marker_2026"
        text_b = "beta_run_marker_2026"
        actions.write_text(win_a, "Text editor:DocumentControl", text_a)
        time.sleep(0.4)
        actions.write_text(win_b, "Text editor:DocumentControl", text_b)
        time.sleep(0.4)

        self.assertIn(text_a, self._editor_text(win_a),
                      "Notepad A should hold its marker")
        self.assertIn(text_b, self._editor_text(win_b),
                      "Notepad B should hold its marker")
        # And critically — they should NOT have each other's text.
        self.assertNotIn(text_b, self._editor_text(win_a),
                         "Notepad A must not pick up B's text")
        self.assertNotIn(text_a, self._editor_text(win_b),
                         "Notepad B must not pick up A's text")

    def test_swap_back_and_forth_preserves_state(self):
        """Hop A → B → A and confirm A still has its text after the
        round-trip. Catches regressions where bringing one window
        forward might deselect / overwrite another's content."""
        win_a, win_b = self.windows[0], self.windows[1]
        actions.write_text(win_a, "Text editor:DocumentControl",
                           "first_pass_a")
        actions.write_text(win_b, "Text editor:DocumentControl",
                           "first_pass_b")
        # Swap back to A and append more — `write_text` clicks first
        # (so it brings A forward, focuses the editor, paste-overwrites).
        actions.write_text(win_a, "Text editor:DocumentControl",
                           "second_pass_a")
        time.sleep(0.4)
        # Hop to B for a peek; B should still have its earlier text.
        apps.bring_to_foreground(win_b)
        time.sleep(0.4)
        self.assertIn("first_pass_b", self._editor_text(win_b),
                      "Notepad B's content should survive the swap")
        # Hop back to A; both pastes are present (write_text inserts at
        # cursor, so the second call appends to the first).
        apps.bring_to_foreground(win_a)
        time.sleep(0.4)
        text_a = self._editor_text(win_a)
        self.assertIn("first_pass_a", text_a,
                      "first paste must persist after swapping windows")
        self.assertIn("second_pass_a", text_a,
                      "second paste must also be present (append semantics)")

    def test_match_documents_first_wins_limitation(self):
        """When two structurally identical windows are open, `match()`
        with a saved fingerprint returns the FIRST scorer. This test
        documents that limitation so future regressions are visible.
        Workaround: enumerate top-level HWNDs manually (as this test
        class does)."""
        from core import app as app_mod
        win_a, _ = self.windows[0], self.windows[1]
        tree.save_fingerprint("notepad_test", tree.fingerprint(win_a))
        result = app_mod.match("notepad_test", launch="notepad.exe")
        self.assertIsNotNone(result, "match should find at least one window")
        # We don't assert WHICH window wins — only that match returned
        # one of them. The point is to document non-determinism here.
        hwnds = {w.NativeWindowHandle for w in self.windows}
        self.assertIn(result.NativeWindowHandle, hwnds,
                      "match must return one of the two open Notepad HWNDs")


def _kill_calc():
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info.get("name") or "").lower()
            if n in ("calculator.exe", "calc.exe", "calculatorapp.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(0.5)


def shutil_which(name):
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
