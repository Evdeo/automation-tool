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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import actions, apps, db, inspector, tree  # noqa: E402


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


class TestPressPath(WindowsUITestBase):
    """The new press_path helper must drive a menu cascade end-to-end."""

    def test_press_path_opens_view_zoom_submenu(self):
        # Cascade File>New tab to get a clean tab for measurement
        actions.press_path(self.win, "File:MenuItemControl", "New tab:MenuItemControl")
        time.sleep(0.6)
        apps.bring_to_foreground(self.win)

        # Now cascade View > Zoom > Zoom in.  If any step fails to open the
        # next, _resolve will raise TimeoutError and this test fails loudly.
        actions.press_path(
            self.win,
            "View:MenuItemControl",
            "Zoom:MenuItemControl",
            "Zoom in:MenuItemControl",
        )
        # Verify the action logged: the press table should now contain the
        # three tree_ids in the order they were pressed.
        conn = sqlite3.connect(config.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT c0 FROM press ORDER BY ts"
            ).fetchall()
        finally:
            conn.close()
        ids_pressed = [r[0] for r in rows]
        # Only assert about the three latest cascade steps — earlier File>New tab
        # presses are also logged.
        self.assertIn("View:MenuItemControl", ids_pressed)
        self.assertIn("Zoom:MenuItemControl", ids_pressed)
        self.assertIn("Zoom in:MenuItemControl", ids_pressed)
        # And they must be in this order (View < Zoom < Zoom in)
        self.assertLess(ids_pressed.index("View:MenuItemControl"),
                        ids_pressed.index("Zoom:MenuItemControl"))
        self.assertLess(ids_pressed.index("Zoom:MenuItemControl"),
                        ids_pressed.index("Zoom in:MenuItemControl"))


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
        actions.press_path(self.win, "File:MenuItemControl", "New tab:MenuItemControl")
        time.sleep(0.8)
        apps.bring_to_foreground(self.win)
        n_before = self._count_tabs()

        # close it via File > Close tab — must actually close
        actions.press_path(self.win, "File:MenuItemControl", "Close tab:MenuItemControl")
        time.sleep(1.5)

        n_after = self._count_tabs()
        self.assertLess(
            n_after, n_before,
            f"Close tab press must reduce tab count: before={n_before} after={n_after}. "
            "If this fails, actions._cursor_click probably regressed to mouse_event."
        )


class TestInspectorRoundtrip(WindowsUITestBase):
    """The inspector's job is to hand a human a tree_id they can paste into a
    press / press_path call.  This test simulates the inspector for each step
    of a View → Zoom → Zoom in cascade by:
      1. opening the menu/submenu so the target element exists in the tree,
      2. asking inspector._path_to() for the full path of that element,
      3. extracting the leaf segment (name:role),
      4. feeding the leaves into press_path() and verifying the cascade fires.

    If this test passes, then a real human using the inspector by clicking
    each menu item in turn gets paths that are usable with press_path."""

    def _find_node(self, name, role):
        walked = tree.walk_live(self.win)
        return next(
            (n["ctrl"] for n in walked if n["name"] == name and n["role"] == role),
            None,
        )

    def _leaf(self, full_path):
        # mirror the same leaf extraction tree.find uses on the fallback path
        seg = full_path.split("/")[-1]
        name, _, role = seg.partition(":")
        return f"{name}:{role}"

    def test_inspector_paths_drive_press_path(self):
        # 1. Click View to open the top-level menu — Zoom MenuItem only exists
        #    in the live tree once the View flyout is open.
        actions.press(self.win, "View:MenuItemControl")
        time.sleep(0.5)
        zoom_ctrl = self._find_node("Zoom", "MenuItemControl")
        self.assertIsNotNone(zoom_ctrl, "Zoom item must be in tree once View is open")
        zoom_full = inspector._path_to(zoom_ctrl)
        zoom_leaf = self._leaf(zoom_full)
        self.assertEqual(zoom_leaf, "Zoom:MenuItemControl",
                         f"inspector leaf should be Zoom:MenuItemControl, got {zoom_leaf!r}")

        # 2. Click Zoom to expand the submenu — Zoom in only enters the tree now.
        actions.press(self.win, "Zoom:MenuItemControl")
        time.sleep(0.5)
        zoomin_ctrl = self._find_node("Zoom in", "MenuItemControl")
        self.assertIsNotNone(zoomin_ctrl, "Zoom in must be in tree once Zoom is expanded")
        zoomin_full = inspector._path_to(zoomin_ctrl)
        zoomin_leaf = self._leaf(zoomin_full)
        self.assertEqual(zoomin_leaf, "Zoom in:MenuItemControl",
                         f"inspector leaf should be 'Zoom in:MenuItemControl', got {zoomin_leaf!r}")

        # 3. Both inspector paths should round-trip through tree.find — both as
        #    full path and as leaf — when the relevant submenu is open.
        walked_now = tree.walk_live(self.win)
        self.assertIsNotNone(tree.find(walked_now, zoomin_full),
                             "full inspector path should resolve")
        self.assertIsNotNone(tree.find(walked_now, zoomin_leaf),
                             "leaf form should resolve via name+role fallback")

        # Close the open menu so the next test starts clean.
        import pyautogui
        pyautogui.press("escape")
        pyautogui.press("escape")
        time.sleep(0.5)

        # 4. Now feed those leaves into press_path from a closed-menu state.
        #    If the inspector is producing usable IDs, this cascade fires.
        actions.press_path(
            self.win,
            "View:MenuItemControl",
            zoom_leaf,
            zoomin_leaf,
        )
        time.sleep(0.4)

        # Verify all three presses landed in the press log in order.
        conn = sqlite3.connect(config.DB_PATH)
        try:
            ids = [r[0] for r in conn.execute("SELECT c0 FROM press ORDER BY ts")]
        finally:
            conn.close()
        self.assertIn("View:MenuItemControl", ids)
        self.assertIn(zoom_leaf, ids)
        self.assertIn(zoomin_leaf, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
