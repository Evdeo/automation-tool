"""Unit tests for core/tree.py — snapshot save/load, find, compute_diff.

These tests exercise the pure logic of tree.py without driving any real UI:
walked data is synthesized, and a tiny FakeCtrl stands in for a uiautomation
control where snapshot_key/snapshot_path need to pull a Name/Role.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import tree  # noqa: E402


class FakeCtrl:
    """Minimal stand-in for a uiautomation control — enough for tree.py."""

    def __init__(self, name="", automation_id="", class_name="", role="WindowControl"):
        self.Name = name
        self.AutomationId = automation_id
        self.ClassName = class_name
        self.ControlTypeName = role


class TestNamingHelpers(unittest.TestCase):
    def test_safe_strips_separators(self):
        self.assertEqual(tree._safe("foo/bar:baz"), "foo_bar_baz")

    def test_safe_handles_none(self):
        self.assertEqual(tree._safe(None), "")

    def test_name_prefers_name_then_automation_id_then_class(self):
        self.assertEqual(tree._name(FakeCtrl(name="N", automation_id="A", class_name="C")), "N")
        self.assertEqual(tree._name(FakeCtrl(name="", automation_id="A", class_name="C")), "A")
        self.assertEqual(tree._name(FakeCtrl(name="", automation_id="", class_name="C")), "C")
        self.assertEqual(tree._name(FakeCtrl()), "")

    def test_segment_uses_index_when_unnamed(self):
        ctrl = FakeCtrl(role="ButtonControl")
        self.assertEqual(tree._segment(ctrl, 3), "#3:ButtonControl")

    def test_segment_uses_name_when_present(self):
        ctrl = FakeCtrl(name="Save", role="ButtonControl")
        self.assertEqual(tree._segment(ctrl, 0), "Save:ButtonControl")


class TestSnapshotKeyAndPath(unittest.TestCase):
    def test_snapshot_key_sanitises(self):
        win = FakeCtrl(name="My App: v1.0/foo", role="WindowControl")
        self.assertEqual(tree.snapshot_key(win), "My_App__v1.0_foo_WindowControl")

    def test_snapshot_path_under_configured_dir(self):
        win = FakeCtrl(name="Notepad", role="WindowControl")
        path = tree.snapshot_path(win)
        self.assertEqual(path.name, "Notepad_WindowControl.json")
        self.assertEqual(path.parent, Path(config.TREE_SNAPSHOT_DIR))


class TestSnapshotRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="snap_test_"))
        self._orig_dir = config.TREE_SNAPSHOT_DIR
        config.TREE_SNAPSHOT_DIR = self.tmp

    def tearDown(self):
        config.TREE_SNAPSHOT_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _walked(self):
        # Synthesised walk data: a window with two children.
        win = FakeCtrl(name="App", role="WindowControl")
        a = FakeCtrl(name="A", role="ButtonControl")
        b = FakeCtrl(name="B", role="ButtonControl")
        # Mimic the shape produced by _node — bbox is a list, ctrl is the live ref.
        return [
            {"tree_id": "App:WindowControl", "name": "App", "role": "WindowControl",
             "bbox": [0, 0, 100, 100], "enabled": True, "ctrl": win},
            {"tree_id": "App:WindowControl/A:ButtonControl", "name": "A",
             "role": "ButtonControl", "bbox": [10, 10, 30, 30], "enabled": True, "ctrl": a},
            {"tree_id": "App:WindowControl/B:ButtonControl", "name": "B",
             "role": "ButtonControl", "bbox": [40, 10, 60, 30], "enabled": True, "ctrl": b},
        ]

    def test_to_serializable_drops_ctrl(self):
        walked = self._walked()
        out = tree.to_serializable(walked)
        for n in out:
            self.assertNotIn("ctrl", n)
            self.assertIn("tree_id", n)

    def test_save_then_load_returns_equivalent_data(self):
        win = FakeCtrl(name="App", role="WindowControl")
        walked = self._walked()
        saved = tree.save_snapshot(win, walked=walked)
        path = tree.snapshot_path(win)
        self.assertTrue(path.exists())
        loaded = tree.load_snapshot(win)
        self.assertEqual(saved, loaded)
        # Round-trip should equal serialized walk
        self.assertEqual(loaded, tree.to_serializable(walked))

    def test_load_snapshot_returns_none_when_missing(self):
        win = FakeCtrl(name="Nope", role="WindowControl")
        self.assertIsNone(tree.load_snapshot(win))

    def test_ensure_snapshot_creates_when_missing(self):
        win = FakeCtrl(name="App", role="WindowControl")
        walked = self._walked()
        data, created = tree.ensure_snapshot(win, walked=walked)
        self.assertTrue(created)
        self.assertEqual(data, tree.to_serializable(walked))

    def test_ensure_snapshot_loads_when_present(self):
        win = FakeCtrl(name="App", role="WindowControl")
        walked = self._walked()
        tree.save_snapshot(win, walked=walked)
        data, created = tree.ensure_snapshot(win, walked=walked)
        self.assertFalse(created)

    def test_save_snapshot_writes_valid_json(self):
        win = FakeCtrl(name="App", role="WindowControl")
        tree.save_snapshot(win, walked=self._walked())
        path = tree.snapshot_path(win)
        # Should round-trip through json without error
        data = json.loads(path.read_text())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 3)


class TestComputeDiff(unittest.TestCase):
    def test_no_change(self):
        snap = [{"tree_id": "a"}, {"tree_id": "b"}]
        live = [{"tree_id": "a"}, {"tree_id": "b"}]
        diff = tree.compute_diff(snap, live)
        self.assertEqual(diff, {"added": [], "removed": []})

    def test_added_only(self):
        snap = [{"tree_id": "a"}]
        live = [{"tree_id": "a"}, {"tree_id": "b"}]
        diff = tree.compute_diff(snap, live)
        self.assertEqual(diff["added"], ["b"])
        self.assertEqual(diff["removed"], [])

    def test_removed_only(self):
        snap = [{"tree_id": "a"}, {"tree_id": "b"}]
        live = [{"tree_id": "a"}]
        diff = tree.compute_diff(snap, live)
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["removed"], ["b"])

    def test_added_and_removed_are_sorted(self):
        snap = [{"tree_id": "z"}, {"tree_id": "a"}]
        live = [{"tree_id": "z"}, {"tree_id": "m"}, {"tree_id": "c"}]
        diff = tree.compute_diff(snap, live)
        self.assertEqual(diff["added"], ["c", "m"])
        self.assertEqual(diff["removed"], ["a"])


class TestFind(unittest.TestCase):
    def setUp(self):
        self.win = FakeCtrl(name="App", role="WindowControl")
        self.btn = FakeCtrl(name="Save", role="ButtonControl")
        self.same_role_diff_name = FakeCtrl(name="Save as", role="ButtonControl")
        self.deep = FakeCtrl(name="Item", role="MenuItemControl")
        self.walked = [
            {"tree_id": "App:WindowControl", "name": "App", "role": "WindowControl",
             "ctrl": self.win, "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Save:ButtonControl", "name": "Save",
             "role": "ButtonControl", "ctrl": self.btn,
             "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Save as:ButtonControl", "name": "Save as",
             "role": "ButtonControl", "ctrl": self.same_role_diff_name,
             "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Menu:MenuControl/Item:MenuItemControl",
             "name": "Item", "role": "MenuItemControl", "ctrl": self.deep,
             "bbox": [0, 0, 1, 1], "enabled": True},
        ]

    def test_full_path_match_returns_ctrl(self):
        result = tree.find(self.walked, "App:WindowControl/Save:ButtonControl")
        self.assertIs(result, self.btn)

    def test_leaf_fallback_matches_name_and_role(self):
        # Pass only the leaf segment: full path won't match, but name+role will.
        result = tree.find(self.walked, "Save:ButtonControl")
        self.assertIs(result, self.btn)

    def test_leaf_fallback_distinguishes_similar_names(self):
        result = tree.find(self.walked, "Save as:ButtonControl")
        self.assertIs(result, self.same_role_diff_name)

    def test_leaf_fallback_finds_deep_element(self):
        result = tree.find(self.walked, "Item:MenuItemControl")
        self.assertIs(result, self.deep)

    def test_returns_none_when_missing(self):
        self.assertIsNone(tree.find(self.walked, "NoSuch:ButtonControl"))

    def test_does_not_use_leaf_fallback_for_index_segments(self):
        # `#3:Foo`-style segments are positional and should not match by name.
        walked = [
            {"tree_id": "Root:WindowControl/#3:ButtonControl", "name": "",
             "role": "ButtonControl", "ctrl": self.btn,
             "bbox": [0, 0, 1, 1], "enabled": True},
        ]
        # Passing the full path matches; passing just "#3:ButtonControl" does NOT.
        self.assertIs(tree.find(walked, "Root:WindowControl/#3:ButtonControl"), self.btn)
        self.assertIsNone(tree.find(walked, "#3:ButtonControl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
