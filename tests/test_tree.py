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
    def setUp(self):
        self._orig_title = config.TARGET_WINDOW_TITLE

    def tearDown(self):
        config.TARGET_WINDOW_TITLE = self._orig_title

    def test_snapshot_key_uses_configured_title(self):
        # The live window's Name has volatile content (a version,
        # path), but TARGET_WINDOW_TITLE is the stable identifier;
        # snapshot_key keys off the latter so the file is the same
        # across runs.
        config.TARGET_WINDOW_TITLE = "My App"
        win = FakeCtrl(name="My App - v3.7 [run 12345]", role="WindowControl")
        self.assertEqual(tree.snapshot_key(win), "My_App_WindowControl")

    def test_snapshot_key_sanitises_configured_title(self):
        config.TARGET_WINDOW_TITLE = "My App: v1.0/foo"
        win = FakeCtrl(name="something else", role="WindowControl")
        self.assertEqual(tree.snapshot_key(win), "My_App__v1.0_foo_WindowControl")

    def test_snapshot_key_falls_back_to_window_name(self):
        # When TARGET_WINDOW_TITLE is unset, fall back to live Name.
        config.TARGET_WINDOW_TITLE = ""
        win = FakeCtrl(name="LiveName", role="WindowControl")
        self.assertEqual(tree.snapshot_key(win), "LiveName_WindowControl")

    def test_snapshot_path_under_configured_dir(self):
        config.TARGET_WINDOW_TITLE = "Notepad"
        win = FakeCtrl(name="Notepad - Untitled", role="WindowControl")
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
        # Synthesised walk data: a window with two children. struct_id
        # mirrors what walk_live would produce: root="0", children "0.0"
        # and "0.1".
        win = FakeCtrl(name="App", role="WindowControl")
        a = FakeCtrl(name="A", role="ButtonControl")
        b = FakeCtrl(name="B", role="ButtonControl")
        return [
            {"tree_id": "App:WindowControl", "struct_id": "0",
             "name": "App", "role": "WindowControl",
             "bbox": [0, 0, 100, 100], "enabled": True, "ctrl": win},
            {"tree_id": "App:WindowControl/A:ButtonControl", "struct_id": "0.0",
             "name": "A", "role": "ButtonControl",
             "bbox": [10, 10, 30, 30], "enabled": True, "ctrl": a},
            {"tree_id": "App:WindowControl/B:ButtonControl", "struct_id": "0.1",
             "name": "B", "role": "ButtonControl",
             "bbox": [40, 10, 60, 30], "enabled": True, "ctrl": b},
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


class TestSuffixMatching(unittest.TestCase):
    """The middle tier between full-path and leaf-only — match a tail of the
    path. Designed for controls that have no unique name and need parent
    context to disambiguate."""

    def setUp(self):
        # Two anonymous Save buttons in different toolbars + a named one.
        c1 = type("C", (), {})()
        c2 = type("C", (), {})()
        c3 = type("C", (), {})()
        self.unique_save = c1
        self.toolbar_save = c2
        self.dialog_save = c3
        self.walked = [
            {"tree_id": "App:WindowControl/Toolbar:ToolBarControl/#1:ButtonControl",
             "name": "", "role": "ButtonControl", "ctrl": self.toolbar_save,
             "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Dialog:WindowControl/#1:ButtonControl",
             "name": "", "role": "ButtonControl", "ctrl": self.dialog_save,
             "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Save Settings:ButtonControl",
             "name": "Save Settings", "role": "ButtonControl",
             "ctrl": self.unique_save,
             "bbox": [0, 0, 1, 1], "enabled": True},
        ]

    def test_suffix_with_index_segment_resolves(self):
        # Just `#1:ButtonControl` would be ambiguous (both anonymous saves
        # are #1 of their parent). Add one parent segment of context:
        result = tree.find(self.walked,
                           "Toolbar:ToolBarControl/#1:ButtonControl")
        self.assertIs(result, self.toolbar_save)
        result = tree.find(self.walked,
                           "Dialog:WindowControl/#1:ButtonControl")
        self.assertIs(result, self.dialog_save)

    def test_suffix_match_requires_segment_boundary(self):
        # "ButtonControl" alone is a substring of every "...:ButtonControl"
        # tree_id but isn't a valid suffix at a segment boundary — must NOT
        # match. (We need a `/` before the suffix or an exact length match.)
        result = tree.find(self.walked, "ButtonControl")
        self.assertIsNone(result)

    def test_suffix_falls_through_to_leaf_match_when_no_separator(self):
        # No `/` in the input → suffix tier is skipped, leaf tier handles it.
        # "Save Settings:ButtonControl" matches the named one via leaf fallback.
        result = tree.find(self.walked, "Save Settings:ButtonControl")
        self.assertIs(result, self.unique_save)

    def test_suffix_returns_first_match_when_ambiguous(self):
        # Just "#1:ButtonControl" with no preceding segment — falls through
        # to leaf tier, which is disabled for index segments → None.
        # This documents that the user must add enough context to make the
        # suffix unique; the system won't guess.
        self.assertIsNone(tree.find(self.walked, "#1:ButtonControl"))


class TestStructWalk(unittest.TestCase):
    """Verify walk_live populates struct_id on every node."""

    def test_walk_live_populates_struct_id(self):
        # FakeCtrl has to expose ControlTypeName, Name, AutomationId,
        # ClassName, BoundingRectangle, IsEnabled, GetChildren.
        class FakeRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b

        class WalkCtrl:
            def __init__(self, name="", role="WindowControl",
                         bbox=(0, 0, 0, 0), children=()):
                self.Name = name
                self.AutomationId = ""
                self.ClassName = ""
                self.ControlTypeName = role
                self.BoundingRectangle = FakeRect(*bbox)
                self.IsEnabled = True
                self._children = list(children)

            def GetChildren(self):
                return self._children

        leaf = WalkCtrl(name="Leaf", role="ButtonControl", bbox=(20, 20, 30, 30))
        mid = WalkCtrl(name="Mid", role="PaneControl",
                       bbox=(10, 10, 100, 100), children=[leaf])
        root = WalkCtrl(name="Root", role="WindowControl",
                        bbox=(0, 0, 200, 200), children=[mid])

        walked = tree.walk_live(root)
        self.assertEqual(walked[0]["struct_id"], "0")
        self.assertEqual(walked[1]["struct_id"], "0.0")
        self.assertEqual(walked[2]["struct_id"], "0.0.0")


class TestFindDispatch(unittest.TestCase):
    """find() routes to struct logic for dotted-int inputs, name logic
    for everything else."""

    def setUp(self):
        ctrl = type("C", (), {})()
        self.target = ctrl
        self.walked = [
            {"tree_id": "App:WindowControl", "struct_id": "0",
             "name": "App", "role": "WindowControl",
             "ctrl": object(), "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/Btn:ButtonControl", "struct_id": "0.0",
             "name": "Btn", "role": "ButtonControl",
             "ctrl": ctrl, "bbox": [0, 0, 1, 1], "enabled": True},
        ]

    def test_struct_format_routes_to_struct_lookup(self):
        # "0.0" is a struct_id — find should match by struct_id, not name.
        self.assertIs(tree.find(self.walked, "0.0"), self.target)

    def test_name_format_routes_to_name_lookup(self):
        # "Btn:ButtonControl" goes through the name-based tiers.
        self.assertIs(tree.find(self.walked, "Btn:ButtonControl"), self.target)

    def test_struct_miss_returns_none(self):
        self.assertIsNone(tree.find(self.walked, "0.99"))

    def test_is_struct_id_helper(self):
        self.assertTrue(tree._is_struct_id("0"))
        self.assertTrue(tree._is_struct_id("0.1"))
        self.assertTrue(tree._is_struct_id("12.345.6.7"))
        self.assertFalse(tree._is_struct_id(""))
        self.assertFalse(tree._is_struct_id("0."))
        self.assertFalse(tree._is_struct_id(".0"))
        self.assertFalse(tree._is_struct_id("Save:ButtonControl"))
        self.assertFalse(tree._is_struct_id("0.1a"))


class TestStructHeal(unittest.TestCase):
    """Self-healing for struct_ids — the core feature for apps whose
    controls have no useful Name. Names are never consulted in heal;
    correlation is by role + bbox shape + sibling position."""

    def _node(self, struct_id, role, bbox, name="", ctrl=None):
        return {
            "tree_id": f"#{struct_id}:{role}",
            "struct_id": struct_id,
            "name": name,
            "role": role,
            "bbox": list(bbox),
            "enabled": True,
            "ctrl": ctrl if ctrl is not None else object(),
        }

    def test_struct_heal_inserted_sibling(self):
        # Snapshot: target at 0.1.2 (a Button), siblings 0.1.0/0.1.1 are
        # smaller buttons. Live: a Separator was inserted at index 2, so
        # the original target moved to 0.1.3. Heal triggers because the
        # node now sitting at 0.1.2 has the wrong role.
        snap = [
            self._node("0", "WindowControl", (0, 0, 1000, 800)),
            self._node("0.0", "PaneControl", (0, 0, 1000, 50)),
            self._node("0.1", "PaneControl", (0, 50, 1000, 800)),
            self._node("0.1.0", "ButtonControl", (10, 60, 40, 90)),   # 30x30
            self._node("0.1.1", "ButtonControl", (50, 60, 80, 90)),   # 30x30
            self._node("0.1.2", "ButtonControl", (90, 60, 180, 90)),  # 90x30 TARGET
        ]
        target_ctrl = object()
        live = [
            self._node("0", "WindowControl", (0, 0, 1000, 800)),
            self._node("0.0", "PaneControl", (0, 0, 1000, 50)),
            self._node("0.1", "PaneControl", (0, 50, 1000, 800)),
            self._node("0.1.0", "ButtonControl", (10, 60, 40, 90)),
            self._node("0.1.1", "ButtonControl", (50, 60, 80, 90)),
            # NEW: a separator inserted at the original index
            self._node("0.1.2", "SeparatorControl", (88, 60, 92, 90)),
            self._node("0.1.3", "ButtonControl", (100, 60, 190, 90),
                       ctrl=target_ctrl),  # original target shifted right
        ]
        result, healed = tree.find_or_heal(live, "0.1.2", snap)
        self.assertTrue(healed)
        self.assertIs(result, target_ctrl)

    def test_struct_heal_anonymous_throughout(self):
        # Same drift scenario but every node has empty name. Confirms
        # heal does NOT consult names — fingerprint is role+shape+position.
        snap = [
            self._node("0", "WindowControl", (0, 0, 800, 600)),
            self._node("0.0", "ToolBarControl", (0, 0, 800, 30)),
            self._node("0.0.0", "ButtonControl", (0, 0, 30, 30)),
            self._node("0.0.1", "ButtonControl", (32, 0, 92, 30)),  # 60x30 TARGET
        ]
        target_ctrl = object()
        live = [
            self._node("0", "WindowControl", (0, 0, 800, 600)),
            self._node("0.0", "ToolBarControl", (0, 0, 800, 30)),
            self._node("0.0.0", "ButtonControl", (0, 0, 30, 30)),
            # New separator inserted, pushing target to index 2
            self._node("0.0.1", "SeparatorControl", (32, 0, 36, 30)),
            self._node("0.0.2", "ButtonControl", (40, 0, 100, 30),
                       ctrl=target_ctrl),
        ]
        for n in snap + live:
            self.assertEqual(n["name"], "")
        result, healed = tree.find_or_heal(live, "0.0.1", snap)
        self.assertTrue(healed)
        self.assertIs(result, target_ctrl)

    def test_struct_heal_role_disambiguation_by_shape(self):
        # When the live tree has multiple same-role children at the
        # anchor's child level, heal tie-breaks by bbox shape. Set up
        # the scenario where the target struct_id no longer exists
        # (shifted by an insertion at the parent level).
        snap = [
            self._node("0", "WindowControl", (0, 0, 800, 600)),
            self._node("0.0", "PaneControl", (0, 0, 800, 600)),
            self._node("0.0.0", "ButtonControl", (0, 0, 100, 30)),  # 100x30 TARGET
        ]
        target_ctrl = object()
        # Live: parent reorganised so 0.0 is now a different role
        # (forces heal). Target moved to under a new pane at 0.1.
        live = [
            self._node("0", "WindowControl", (0, 0, 800, 600)),
            self._node("0.0", "ToolBarControl", (0, 0, 800, 30)),  # role changed
            self._node("0.1", "PaneControl", (0, 30, 800, 600)),   # was 0.0
            # A small narrow button + the wide one matching target shape
            self._node("0.1.0", "ButtonControl", (0, 30, 30, 60)),  # 30x30
            self._node("0.1.1", "ButtonControl", (40, 30, 140, 60),
                       ctrl=target_ctrl),  # 100x30
        ]
        result, healed = tree.find_or_heal(live, "0.0.0", snap)
        self.assertTrue(healed)
        # Both live buttons have the right role; the wide one (100x30)
        # is closer to the snap target's shape than the narrow one (30x30).
        self.assertIs(result, target_ctrl)

    def test_struct_heal_total_miss(self):
        snap = [
            self._node("0", "WindowControl", (0, 0, 100, 100)),
            self._node("0.0", "ButtonControl", (0, 0, 50, 30)),
        ]
        # Live has no buttons under the root at all — heal must give up.
        live = [
            self._node("0", "WindowControl", (0, 0, 100, 100)),
            self._node("0.0", "PaneControl", (0, 0, 50, 30)),
        ]
        result, healed = tree.find_or_heal(live, "0.0", snap)
        self.assertFalse(healed)
        self.assertIsNone(result)

    def test_struct_exact_match_no_heal(self):
        # When the live tree still has the exact struct_id with matching
        # role, heal should NOT be invoked (healed=False).
        target = object()
        snap = [
            self._node("0", "WindowControl", (0, 0, 100, 100)),
            self._node("0.0", "ButtonControl", (0, 0, 50, 30)),
        ]
        live = [
            self._node("0", "WindowControl", (0, 0, 100, 100)),
            self._node("0.0", "ButtonControl", (0, 0, 50, 30), ctrl=target),
        ]
        result, healed = tree.find_or_heal(live, "0.0", snap)
        self.assertFalse(healed)
        self.assertIs(result, target)


class TestSnapshotBackcompat(unittest.TestCase):
    """Snapshots saved before struct_id was added must still work — the
    field is derived on load."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="snap_legacy_"))
        self._orig_dir = config.TREE_SNAPSHOT_DIR
        config.TREE_SNAPSHOT_DIR = self.tmp

    def tearDown(self):
        config.TREE_SNAPSHOT_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_derives_struct_id_when_absent(self):
        # Hand-write a legacy snapshot file with no struct_id field.
        legacy = [
            {"tree_id": "App:WindowControl", "name": "App",
             "role": "WindowControl", "bbox": [0, 0, 100, 100], "enabled": True},
            {"tree_id": "App:WindowControl/A:ButtonControl", "name": "A",
             "role": "ButtonControl", "bbox": [0, 0, 1, 1], "enabled": True},
            {"tree_id": "App:WindowControl/B:ButtonControl", "name": "B",
             "role": "ButtonControl", "bbox": [0, 0, 1, 1], "enabled": True},
        ]
        win = FakeCtrl(name="App", role="WindowControl")
        path = tree.snapshot_path(win)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy))

        loaded = tree.load_snapshot(win)
        self.assertEqual(loaded[0]["struct_id"], "0")
        self.assertEqual(loaded[1]["struct_id"], "0.0")
        self.assertEqual(loaded[2]["struct_id"], "0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
