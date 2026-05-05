"""Unit tests for inspector.py — naming, descendant detection, ancestor
lookup, path-to-chain walking, prompt-character handling, commit flow,
finalize flow, session-end clipboard dump, full-info dump.

These tests run on any platform: every UIA / clipboard / cursor / pyautogui
side effect is mocked. A FakeCtrl class stands in for `auto.Control` and
exposes only the attributes the inspector actually reads.
"""
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inspector  # noqa: E402


class FakeRect:
    def __init__(self, left=0, top=0, right=0, bottom=0):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeCtrl:
    """Minimal stand-in for uiautomation.Control. Exposes everything the
    inspector reads on a node: Name, AutomationId, ClassName,
    ControlTypeName, BoundingRectangle, GetChildren, plus ProcessId on
    window-level controls."""

    def __init__(
        self,
        name="",
        automation_id="",
        class_name="",
        control_type="WindowControl",
        bbox=(0, 0, 0, 0),
        children=(),
        process_id=None,
    ):
        self.Name = name
        self.AutomationId = automation_id
        self.ClassName = class_name
        self.ControlTypeName = control_type
        self.BoundingRectangle = FakeRect(*bbox)
        self.IsEnabled = True
        self._children = list(children)
        if process_id is not None:
            self.ProcessId = process_id

    def GetChildren(self):
        return self._children


def _reset_state():
    """Clear every module-level slot the inspector mutates so tests
    don't bleed state into each other."""
    inspector._last_committed = None
    inspector._pending_name = None
    inspector._captures.clear()
    inspector._used_names.clear()
    inspector._windows.clear()
    inspector._window_by_hwnd.clear()
    inspector._stems_seen.clear()
    inspector._skip_popup_hwnds.clear()
    inspector._color_sample_state = None
    inspector._step_counter = 0
    inspector._log_file = None
    inspector._snippets_file = None


# --- Pure helpers -----------------------------------------------------------


class TestSanitizeConst(unittest.TestCase):
    def test_simple_word(self):
        self.assertEqual(inspector._sanitize_const("Save"), "SAVE")

    def test_collapses_runs_of_punctuation(self):
        self.assertEqual(inspector._sanitize_const("Save As..."), "SAVE_AS")

    def test_strips_leading_and_trailing_underscore(self):
        self.assertEqual(inspector._sanitize_const("...hello.."), "HELLO")

    def test_returns_empty_for_punctuation_only(self):
        self.assertEqual(inspector._sanitize_const("!@#$"), "")

    def test_returns_empty_for_none(self):
        self.assertEqual(inspector._sanitize_const(None), "")

    def test_keeps_digits(self):
        self.assertEqual(inspector._sanitize_const("Tab 2"), "TAB_2")


class TestSegmentName(unittest.TestCase):
    def test_named_segment_returns_name(self):
        self.assertEqual(inspector._segment_name("Save:ButtonControl"), "Save")

    def test_indexed_segment_returns_index_token(self):
        # "#3:ButtonControl" — "#3" is the name token before the colon.
        self.assertEqual(inspector._segment_name("#3:ButtonControl"), "#3")

    def test_no_role_separator_returns_input(self):
        # No ":" in the segment → the whole thing is treated as the name.
        self.assertEqual(inspector._segment_name("Bare"), "Bare")

    def test_handles_colon_in_name(self):
        # "rpartition" splits on the LAST ":" so a name containing ":"
        # is preserved up to the role separator.
        self.assertEqual(
            inspector._segment_name("Foo:Bar:ButtonControl"),
            "Foo:Bar",
        )


class TestFormatColor(unittest.TestCase):
    def test_none_color_returns_unavailable(self):
        self.assertEqual(inspector._format_color(None), "(unavailable)")

    def test_rgb_tuple_renders_decimal_and_hex(self):
        self.assertEqual(
            inspector._format_color((255, 0, 16)),
            "(255, 0, 16)  #ff0010",
        )

    def test_zero_renders_as_padded_hex(self):
        self.assertEqual(
            inspector._format_color((0, 0, 0)),
            "(0, 0, 0)  #000000",
        )


class TestIsSameOrDescendant(unittest.TestCase):
    """`_is_same_or_descendant(info, last)` decides whether two presses
    are on the same UIA element (or the new one is geometrically inside
    the previous). Identity is via UIA `RuntimeId`; descendant is via
    bbox containment. struct_id deliberately is NOT consulted — paths
    are positional and a tree reshape (submenu open/close) can leave
    two different elements sharing the same struct_id."""

    def _info(self, *, runtime_id=(), bbox=None, window_name="app"):
        return {
            "runtime_id": tuple(runtime_id),
            "bbox": bbox,
            "window_name": window_name,
        }

    def test_same_runtime_id_is_same_element(self):
        a = self._info(runtime_id=(42, 100), bbox=(0, 0, 100, 50))
        b = self._info(runtime_id=(42, 100), bbox=(5, 5, 95, 45))
        self.assertTrue(inspector._is_same_or_descendant(a, b))

    def test_different_runtime_id_same_struct_id_is_NOT_same(self):
        # Regression for the bug: View>Zoom and Zoom>Zoom in both sit at
        # struct_id "0.0.0.0.0.0" at different times. RuntimeId distinguishes
        # them; struct_id can't.
        a = self._info(runtime_id=(1, 100), bbox=(0, 0, 100, 50))
        b = self._info(runtime_id=(1, 200), bbox=(0, 100, 100, 150))
        self.assertFalse(inspector._is_same_or_descendant(a, b))

    def test_geometric_descendant_matches(self):
        # Inner element (no RuntimeId) sits inside last bbox.
        outer = self._info(runtime_id=(), bbox=(0, 0, 100, 100))
        inner = self._info(runtime_id=(), bbox=(20, 20, 80, 80))
        self.assertTrue(inspector._is_same_or_descendant(inner, outer))

    def test_geometric_non_descendant_does_not_match(self):
        a = self._info(runtime_id=(), bbox=(200, 200, 300, 300))
        b = self._info(runtime_id=(), bbox=(0, 0, 100, 100))
        self.assertFalse(inspector._is_same_or_descendant(a, b))

    def test_partial_overlap_is_not_descendant(self):
        # New bbox extends beyond last bbox — not a descendant.
        a = self._info(runtime_id=(), bbox=(50, 50, 200, 200))
        b = self._info(runtime_id=(), bbox=(0, 0, 100, 100))
        self.assertFalse(inspector._is_same_or_descendant(a, b))

    def test_different_window_never_matches(self):
        a = self._info(runtime_id=(1,), bbox=(0, 0, 10, 10),
                       window_name="notepad")
        b = self._info(runtime_id=(1,), bbox=(0, 0, 10, 10),
                       window_name="calc")
        self.assertFalse(inspector._is_same_or_descendant(a, b))

    def test_empty_runtime_id_falls_through_to_bbox(self):
        # GetRuntimeId can fail — empty tuple. Match must still work
        # via bbox containment.
        a = self._info(runtime_id=(), bbox=(20, 20, 30, 30))
        b = self._info(runtime_id=(), bbox=(0, 0, 100, 100))
        self.assertTrue(inspector._is_same_or_descendant(a, b))

    def test_missing_bbox_with_no_runtime_match_returns_false(self):
        a = self._info(runtime_id=(), bbox=None)
        b = self._info(runtime_id=(), bbox=(0, 0, 100, 100))
        self.assertFalse(inspector._is_same_or_descendant(a, b))

    def test_runtime_match_wins_even_if_bbox_disagrees(self):
        # Same UIA element, but the user resized the window between
        # presses so bboxes are different. RuntimeId match short-circuits.
        a = self._info(runtime_id=(7,), bbox=(0, 0, 50, 50))
        b = self._info(runtime_id=(7,), bbox=(500, 500, 700, 700))
        self.assertTrue(inspector._is_same_or_descendant(a, b))


# --- Interactable ancestor --------------------------------------------------


class TestFindInteractableAncestor(unittest.TestCase):
    """Given the descent chain (window → ... → leaf), find the deepest
    ancestor that is interactable. Used to print the 'this is text,
    nearest button is …' note when a Text/Group/Pane/Image leaf was
    captured."""

    def _chain(self, *control_types):
        # Build a chain of (FakeCtrl, sibling_index) pairs from window
        # down to leaf. Sibling index is just position; the inspector's
        # logic only uses it to compute the struct_id.
        return [
            (FakeCtrl(control_type=ct, name=f"node_{i}"), i)
            for i, ct in enumerate(control_types)
        ]

    def test_returns_none_when_leaf_is_interactable(self):
        # ButtonControl is interactable — no need to walk up.
        chain = self._chain("WindowControl", "PaneControl", "ButtonControl")
        self.assertIsNone(inspector._find_interactable_ancestor(chain))

    def test_text_leaf_finds_button_ancestor(self):
        # Window → Pane → Button → Text  ⟹ note points to the Button.
        chain = self._chain(
            "WindowControl", "PaneControl", "ButtonControl", "TextControl"
        )
        ancestor = inspector._find_interactable_ancestor(chain)
        self.assertIsNotNone(ancestor)
        self.assertEqual(ancestor["control_type"], "ButtonControl")
        # struct_id is the dotted-index path down to the Button (3 levels).
        self.assertEqual(ancestor["struct_id"], "0.1.2")

    def test_image_leaf_finds_hyperlink_ancestor(self):
        chain = self._chain(
            "WindowControl", "HyperlinkControl", "ImageControl"
        )
        ancestor = inspector._find_interactable_ancestor(chain)
        self.assertIsNotNone(ancestor)
        self.assertEqual(ancestor["control_type"], "HyperlinkControl")

    def test_text_with_no_interactable_ancestor_returns_none(self):
        # Window → Pane → Group → Text — no interactable in the chain.
        chain = self._chain(
            "WindowControl", "PaneControl", "GroupControl", "TextControl"
        )
        # WindowControl is not in the interactable set, so even though
        # the chain has length, there's no ancestor to point at.
        self.assertIsNone(inspector._find_interactable_ancestor(chain))

    def test_picks_deepest_interactable_when_multiple(self):
        # Window → Button (outer) → Pane → Button (inner) → Text
        # Should point to the INNER button (deepest interactable
        # ancestor, not the outer).
        chain = self._chain(
            "WindowControl", "ButtonControl", "PaneControl",
            "ButtonControl", "TextControl",
        )
        ancestor = inspector._find_interactable_ancestor(chain)
        self.assertEqual(ancestor["struct_id"], "0.1.2.3")

    def test_empty_chain_returns_none(self):
        self.assertIsNone(inspector._find_interactable_ancestor([]))

    def test_singleton_chain_returns_none(self):
        # A chain of just the window doesn't have any ancestors to walk up.
        self.assertIsNone(
            inspector._find_interactable_ancestor(self._chain("WindowControl"))
        )


# --- Path walking -----------------------------------------------------------


class TestPathToChain(unittest.TestCase):
    """Walk a synthetic FakeCtrl tree top-down by bbox containment,
    same algorithm as the inspector uses against a real UIA tree."""

    def test_picks_smallest_containing_child(self):
        # Window (200x200) contains a Pane (100x100) which contains a
        # Button (20x20). A click at the button's center must end at
        # the button leaf — smaller area wins at each level.
        leaf = FakeCtrl(
            name="Btn", control_type="ButtonControl", bbox=(40, 40, 60, 60),
        )
        mid = FakeCtrl(
            name="Mid", control_type="PaneControl",
            bbox=(0, 0, 100, 100), children=[leaf],
        )
        win = FakeCtrl(
            name="Win", control_type="WindowControl",
            bbox=(0, 0, 200, 200), children=[mid],
        )
        cur, chain, name_path, struct_id = inspector._path_to_chain(
            win, 50, 50,
        )
        self.assertIs(cur, leaf)
        self.assertEqual(struct_id, "0.0.0")
        self.assertEqual(len(chain), 3)
        self.assertIn("Btn:ButtonControl", name_path)

    def test_returns_window_when_no_child_contains(self):
        # Click outside any child's bbox — descent stops at the window.
        win = FakeCtrl(
            name="Win", control_type="WindowControl",
            bbox=(0, 0, 200, 200),
            children=[FakeCtrl(name="C", bbox=(10, 10, 20, 20))],
        )
        cur, chain, name_path, struct_id = inspector._path_to_chain(
            win, 100, 100,
        )
        self.assertIs(cur, win)
        self.assertEqual(struct_id, "0")
        self.assertEqual(len(chain), 1)

    def test_promotes_text_leaf_to_interactable_ancestor(self):
        """Cursor over the inner TextControl of a MenuItemControl must
        capture the MenuItemControl, not the text label. Without this,
        two presses on the same Zoom in button can produce different
        captures (the Zoom in text, the Ctrl+Plus shortcut text, etc.)
        depending on cursor jitter."""
        text = FakeCtrl(
            name="Zoom in", control_type="TextControl",
            bbox=(40, 40, 60, 60),
        )
        item = FakeCtrl(
            name="Zoom in", control_type="MenuItemControl",
            bbox=(20, 30, 80, 70), children=[text],
        )
        win = FakeCtrl(
            name="Win", control_type="WindowControl",
            bbox=(0, 0, 200, 200), children=[item],
        )
        cur, chain, _, struct_id = inspector._path_to_chain(win, 50, 50)
        self.assertIs(cur, item,
                      "leaf should be promoted to MenuItemControl ancestor")
        self.assertEqual(cur.ControlTypeName, "MenuItemControl")
        self.assertEqual(struct_id, "0.0")
        self.assertEqual(len(chain), 2)

    def test_no_promotion_when_no_interactable_ancestor(self):
        """Standalone TextControl — no clickable parent — stays as-is.
        We don't promote arbitrarily; only when there's an interactable
        thing the user probably meant to click."""
        text = FakeCtrl(
            name="Label", control_type="TextControl",
            bbox=(40, 40, 60, 60),
        )
        pane = FakeCtrl(
            name="Container", control_type="PaneControl",
            bbox=(0, 0, 100, 100), children=[text],
        )
        win = FakeCtrl(
            name="Win", control_type="WindowControl",
            bbox=(0, 0, 200, 200), children=[pane],
        )
        cur, _, _, struct_id = inspector._path_to_chain(win, 50, 50)
        self.assertIs(cur, text,
                      "no interactable ancestor → leaf stays put")
        self.assertEqual(struct_id, "0.0.0")

    def test_promotes_to_deepest_interactable(self):
        """When multiple interactable ancestors exist (button inside a
        list-item), promote to the DEEPEST one — the most specific
        thing the user likely meant to click."""
        text = FakeCtrl(
            name="Save", control_type="TextControl",
            bbox=(40, 40, 60, 60),
        )
        button = FakeCtrl(
            name="Save", control_type="ButtonControl",
            bbox=(35, 35, 65, 65), children=[text],
        )
        list_item = FakeCtrl(
            name="Row", control_type="ListItemControl",
            bbox=(20, 20, 80, 80), children=[button],
        )
        win = FakeCtrl(
            name="Win", control_type="WindowControl",
            bbox=(0, 0, 200, 200), children=[list_item],
        )
        cur, chain, _, struct_id = inspector._path_to_chain(win, 50, 50)
        # Inner ButtonControl is the deepest interactable — pick it,
        # not the outer ListItemControl.
        self.assertIs(cur, button)
        self.assertEqual(cur.ControlTypeName, "ButtonControl")
        self.assertEqual(struct_id, "0.0.0")

    def test_picks_smallest_among_overlapping_siblings(self):
        # Two siblings overlap at the click point. The smaller one wins
        # so the inspector targets the most specific control under the
        # cursor.
        small = FakeCtrl(name="Small", bbox=(40, 40, 60, 60))
        big = FakeCtrl(name="Big", bbox=(0, 0, 200, 200))
        win = FakeCtrl(
            name="W", bbox=(0, 0, 300, 300), children=[big, small],
        )
        cur, chain, _, struct_id = inspector._path_to_chain(win, 50, 50)
        self.assertIs(cur, small)
        # Sibling index 1 = "small" (second child).
        self.assertEqual(struct_id, "0.1")


# --- Web selector extraction ------------------------------------------------


class TestIsBrowserWindow(unittest.TestCase):
    """`_is_browser_window` decides whether to extract a CSS selector or
    fall back to struct_id. Browser detection is by Win32 class name —
    the same set already protected by the auto-dismiss skip list."""

    def test_chrome_class_is_browser(self):
        win = FakeCtrl(name="Tab", class_name="Chrome_WidgetWin_1")
        self.assertTrue(inspector._is_browser_window(win))

    def test_firefox_class_is_browser(self):
        win = FakeCtrl(name="Tab", class_name="MozillaWindowClass")
        self.assertTrue(inspector._is_browser_window(win))

    def test_notepad_class_is_not_browser(self):
        win = FakeCtrl(name="Notepad", class_name="Notepad")
        self.assertFalse(inspector._is_browser_window(win))

    def test_missing_class_name_is_not_browser(self):
        win = FakeCtrl(name="Empty")  # default class_name=""
        self.assertFalse(inspector._is_browser_window(win))


class TestExtractWebSelector(unittest.TestCase):
    """Priority order: AutomationId (DOM id) > unique aria-label >
    role+name composite > None (struct_id fallback)."""

    def _walked_node(self, ctrl):
        # Minimal walked-list entry; only `name` and `role` are
        # consulted by the uniqueness check.
        return {"name": ctrl.Name, "role": ctrl.ControlTypeName,
                "ctrl": ctrl}

    def test_dom_id_wins(self):
        """Element with AutomationId='login' (the DOM `id` attribute)
        always wins — it's the most stable web identifier."""
        leaf = FakeCtrl(
            name="Sign in", automation_id="login",
            control_type="ButtonControl",
        )
        sel = inspector._extract_web_selector(
            leaf, [self._walked_node(leaf)],
        )
        self.assertEqual(sel, "#login")

    def test_unique_name_emits_aria_label(self):
        """No AutomationId, but accessible name is unique within the
        page subtree → emit `[aria-label="..."]`. Robust against minor
        DOM reshuffles since it doesn't depend on position."""
        leaf = FakeCtrl(
            name="Sign in", control_type="ButtonControl",
        )
        sel = inspector._extract_web_selector(
            leaf, [self._walked_node(leaf)],
        )
        self.assertEqual(sel, '[aria-label="Sign in"]')

    def test_ambiguous_name_uses_role_composite(self):
        """Two elements share the same name; role-known fallback emits
        `button[name="Save"]`."""
        a = FakeCtrl(name="Save", control_type="ButtonControl")
        b = FakeCtrl(name="Save", control_type="TextControl")
        walked = [self._walked_node(a), self._walked_node(b)]
        sel = inspector._extract_web_selector(a, walked)
        self.assertEqual(sel, 'button[name="Save"]')

    def test_unknown_role_with_ambiguous_name_returns_none(self):
        """Two elements share name and we don't have a CSS-friendly
        ARIA role for the type → fall through to None (struct_id
        fallback at emit time)."""
        a = FakeCtrl(name="Caption", control_type="GroupControl")
        b = FakeCtrl(name="Caption", control_type="GroupControl")
        walked = [self._walked_node(a), self._walked_node(b)]
        self.assertIsNone(inspector._extract_web_selector(a, walked))

    def test_no_id_no_name_returns_none(self):
        leaf = FakeCtrl(control_type="ButtonControl")  # name=""
        self.assertIsNone(
            inspector._extract_web_selector(
                leaf, [self._walked_node(leaf)],
            ),
        )

    def test_dom_id_beats_name_uniqueness(self):
        """Even when name would also be unique, AutomationId wins
        because DOM `id` is the most stable selector."""
        leaf = FakeCtrl(
            name="Sign in", automation_id="login",
            control_type="ButtonControl",
        )
        sel = inspector._extract_web_selector(
            leaf, [self._walked_node(leaf)],
        )
        self.assertEqual(sel, "#login")

    def test_class_name_used_when_no_id_no_name(self):
        """No id, no accessible name — fall back to HTML class. Many
        SPA frameworks generate elements that have neither id nor
        accessible name, but always have CSS classes for styling."""
        leaf = FakeCtrl(
            class_name="btn-primary", control_type="GroupControl",
        )
        sel = inspector._extract_web_selector(
            leaf, [self._walked_node(leaf)],
        )
        self.assertEqual(sel, ".btn-primary")

    def test_multiple_classes_become_compound_selector(self):
        """Browsers expose multiple HTML classes as a single
        space-separated ClassName. CSS selector form is dot-joined:
        `class="btn btn-primary"` -> `.btn.btn-primary`."""
        leaf = FakeCtrl(
            class_name="btn btn-primary outline",
            control_type="GroupControl",
        )
        sel = inspector._extract_web_selector(
            leaf, [self._walked_node(leaf)],
        )
        self.assertEqual(sel, ".btn.btn-primary.outline")

    def test_class_used_when_name_present_but_ambiguous_and_no_role(self):
        """Name is non-unique AND role isn't in the ARIA-known set —
        the role-composite check fails, so we fall through to class."""
        a = FakeCtrl(
            name="Caption", control_type="GroupControl",
            class_name="caption-row",
        )
        b = FakeCtrl(name="Caption", control_type="GroupControl")
        walked = [self._walked_node(a), self._walked_node(b)]
        sel = inspector._extract_web_selector(a, walked)
        self.assertEqual(sel, ".caption-row")


# --- Suggested name disambiguation ------------------------------------------


class TestSuggestName(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def tearDown(self):
        _reset_state()

    def test_basic_name_uses_leaf(self):
        name = inspector._suggest_name(
            "App:WindowControl/Save:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "SAVE")

    def test_repeat_name_gets_suffix(self):
        # First commit reserves SAVE; second commit must get SAVE_2.
        inspector._used_names.add("SAVE")
        name = inspector._suggest_name(
            "App:WindowControl/Save:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "SAVE_2")

    def test_third_repeat_gets_suffix_3(self):
        inspector._used_names.update({"SAVE", "SAVE_2"})
        name = inspector._suggest_name(
            "App:WindowControl/Save:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "SAVE_3")

    def test_empty_name_falls_back_to_step(self):
        # Anonymous indexed leaf — _segment_name yields "#0", which
        # _sanitize_const produces "" for, triggering the STEP_n fallback.
        name = inspector._suggest_name(
            "App:WindowControl/#0:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "STEP_1")

    def test_step_fallback_uses_capture_count(self):
        # Two captures already, so the next anonymous leaf is STEP_3.
        inspector._captures.extend([{}, {}])
        name = inspector._suggest_name(
            "App:WindowControl/#0:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "STEP_3")

    def test_digit_first_name_falls_back_to_step(self):
        # "2nd:ButtonControl" → "2ND" — starts with a digit, not a valid
        # Python identifier. Must use STEP_n.
        name = inspector._suggest_name(
            "App:WindowControl/2nd:ButtonControl", "ButtonControl",
        )
        self.assertEqual(name, "STEP_1")


# --- Readable label ---------------------------------------------------------


class TestReadableLabel(unittest.TestCase):
    def test_named_leaf_returns_name_part(self):
        commit = {"name_path": "App:WindowControl/Save:ButtonControl",
                  "name": "Save"}
        self.assertEqual(inspector._readable_label(commit), "Save")

    def test_anonymous_leaf_falls_through_to_name_field(self):
        # Leaf is "#3:ButtonControl" — _segment_name returns "#3", which
        # is truthy, so the function returns "#3". This matches the
        # current implementation (the fallback only kicks in for empty
        # segments). Documents existing behaviour.
        commit = {"name_path": "App:WindowControl/#3:ButtonControl",
                  "name": "Some Name"}
        self.assertEqual(inspector._readable_label(commit), "#3")

    def test_question_mark_when_everything_empty(self):
        # name_path empty AND name empty.
        commit = {"name_path": "", "name": ""}
        self.assertEqual(inspector._readable_label(commit), "?")


# --- Prompt character handling ----------------------------------------------


class TestHandlePromptChar(unittest.TestCase):
    def setUp(self):
        _reset_state()
        # Set up a fake pending prompt so the handler has state to mutate.
        inspector._pending_name = {
            "buffer": "",
            "default": "DEFAULT_NAME",
            "commit": {
                "struct_id": "0.0",
                "name_path": "Win:WindowControl/Btn:ButtonControl",
                "name": "Btn",
                "control_type": "ButtonControl",
                "default_name": "DEFAULT_NAME",
                "final_name": None,
            },
        }
        inspector._used_names.add("DEFAULT_NAME")
        # Capture stdout so we don't pollute test output.
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def test_printable_char_appends_to_buffer(self):
        inspector._handle_prompt_char("H")
        inspector._handle_prompt_char("i")
        self.assertEqual(inspector._pending_name["buffer"], "Hi")

    def test_backspace_pops_last_char(self):
        inspector._pending_name["buffer"] = "Hel"
        inspector._handle_prompt_char("\b")
        self.assertEqual(inspector._pending_name["buffer"], "He")

    def test_backspace_on_empty_buffer_is_noop(self):
        inspector._handle_prompt_char("\b")
        self.assertEqual(inspector._pending_name["buffer"], "")

    def test_enter_finalizes_with_default_when_buffer_empty(self):
        inspector._handle_prompt_char("\r")
        self.assertIsNone(inspector._pending_name)
        # Per-step clipboard write was removed. Check the captures list
        # — that's what `_emit_session_end` reads at Ctrl+C.
        self.assertEqual(len(inspector._captures), 1)
        self.assertEqual(inspector._captures[0]["final_name"], "DEFAULT_NAME")

    def test_enter_finalizes_with_typed_buffer(self):
        inspector._pending_name["buffer"] = "my_button"
        inspector._handle_prompt_char("\r")
        self.assertIsNone(inspector._pending_name)
        # Typed name is sanitized to UPPER_SNAKE and used verbatim
        # (no window prefix prepended).
        self.assertEqual(inspector._captures[0]["final_name"], "MY_BUTTON")

    def test_ctrl_c_finalizes_and_signals_main_thread(self):
        inspector._pending_name["buffer"] = "x"
        with mock.patch.object(inspector, "pyperclip"), \
             mock.patch.object(inspector._thread, "interrupt_main") as mi:
            inspector._handle_prompt_char("\x03")
        mi.assert_called_once()
        self.assertIsNone(inspector._pending_name)

    def test_non_printable_unknown_char_ignored(self):
        # Tab, Esc, F-key returns from msvcrt — must not crash, must not
        # mutate the buffer.
        before = inspector._pending_name["buffer"]
        inspector._handle_prompt_char("\t")
        inspector._handle_prompt_char("\x1b")
        self.assertEqual(inspector._pending_name["buffer"], before)

    def test_no_op_when_no_pending_prompt(self):
        # If a stray char arrives outside a prompt, the handler bails
        # without touching anything.
        inspector._pending_name = None
        inspector._handle_prompt_char("Z")
        self.assertIsNone(inspector._pending_name)


# --- Finalize prompt --------------------------------------------------------


class TestFinalizePrompt(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.tmp = Path(tempfile.mkdtemp(prefix="inspector_finalize_"))
        inspector._snippets_file = self.tmp / "session.py"
        # Pre-create a minimal pending commit.
        self._set_pending(buffer="", default="MY_BTN")
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        _reset_state()

    def _set_pending(self, buffer, default, struct_id="0.2.0", label="Save"):
        commit = {
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/{label}:ButtonControl",
            "name": label,
            "control_type": "ButtonControl",
            "class_name": "",
            "automation_id": "",
            "bbox": (0, 0, 100, 30),
            "bbox_center": (50, 15),
            "color": (255, 0, 0),
            "win_stem": "app",
            "interactable_ancestor": None,
            "default_name": default,
            "final_name": None,
        }
        inspector._used_names.add(default)
        inspector._pending_name = {
            "buffer": buffer, "default": default, "commit": commit,
        }

    def test_default_used_when_buffer_empty(self):
        inspector._finalize_prompt()
        self.assertEqual(len(inspector._captures), 1)
        cap = inspector._captures[0]
        self.assertEqual(cap["final_name"], "MY_BTN")
        # Per-step clipboard copy was removed — full block goes to
        # clipboard once at session end. The sidecar file is the
        # per-step audit trail.
        contents = inspector._snippets_file.read_text(encoding="utf-8")
        self.assertIn('MY_BTN = "0.2.0"  # Save', contents)

    def test_typed_name_overrides_default(self):
        self._set_pending(buffer="really_save", default="SAVE")
        inspector._finalize_prompt()
        cap = inspector._captures[0]
        # Custom name used verbatim — no window prefix is prepended.
        self.assertEqual(cap["final_name"], "REALLY_SAVE")
        # Default is no longer reserved (user typed an override).
        self.assertNotIn("SAVE", inspector._used_names)
        contents = inspector._snippets_file.read_text(encoding="utf-8")
        self.assertIn("REALLY_SAVE", contents)

    def test_typed_name_disambiguates_against_used(self):
        # Pretend SUBMIT is already taken from a prior commit.
        inspector._used_names.add("SUBMIT")
        self._set_pending(buffer="submit", default="OK_BTN")
        inspector._finalize_prompt()
        self.assertEqual(inspector._captures[0]["final_name"], "SUBMIT_2")

    def test_typed_garbage_falls_back_to_default(self):
        # A buffer of pure punctuation sanitises to "" — falls back to
        # the suggested default rather than producing a malformed name.
        self._set_pending(buffer="!!!", default="OK_BTN")
        inspector._finalize_prompt()
        self.assertEqual(inspector._captures[0]["final_name"], "OK_BTN")

    def test_sidecar_file_appended(self):
        inspector._finalize_prompt()
        self.assertTrue(inspector._snippets_file.exists())
        contents = inspector._snippets_file.read_text(encoding="utf-8")
        self.assertIn('MY_BTN = "0.2.0"', contents)
        # Trailing newline so successive finalizes append cleanly.
        self.assertTrue(contents.endswith("\n"))

    def test_clears_pending_name(self):
        inspector._finalize_prompt()
        self.assertIsNone(inspector._pending_name)

    def test_no_op_when_no_prompt(self):
        # Calling finalize when nothing is pending must not crash. The
        # per-step clipboard touch was removed entirely — the only
        # clipboard write happens once at session end.
        inspector._pending_name = None
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._finalize_prompt()
        mp.copy.assert_not_called()

    def test_typed_name_does_not_get_window_prefix(self):
        """Regression: when the user types a custom name, that name is
        used verbatim — no window prefix is prepended even if there's
        a window context. Earlier behavior prepended `<WINDOW>_`."""
        commit = {
            "struct_id": "0.0.0.0.1.0.3",
            "name_path": "App:WindowControl/#3:WindowControl",
            "name": "",
            "control_type": "WindowControl",
            "class_name": "",
            "automation_id": "",
            "bbox": (0, 0, 100, 30),
            "bbox_center": (50, 15),
            "color": (0, 0, 0),
            "window_name": "riot_client",
            "interactable_ancestor": None,
            "default_name": "RIOT_CLIENT_STEP_1",
            "final_name": None,
        }
        inspector._used_names.add("RIOT_CLIENT_STEP_1")
        inspector._pending_name = {
            "buffer": "Hello", "default": "RIOT_CLIENT_STEP_1",
            "commit": commit,
        }
        inspector._finalize_prompt()
        self.assertEqual(inspector._captures[0]["final_name"], "HELLO")


# --- Press handling: routing decisions --------------------------------------


class TestHandlePressRouting(unittest.TestCase):
    """Verify the descendant/sibling logic that picks between 'commit'
    and 'full info dump' branches."""

    def setUp(self):
        _reset_state()
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def _info(self, struct_id, name="N", bbox=(0, 0, 10, 10),
              runtime_id=(), window_name="app"):
        # Minimal info dict that _handle_press / _commit accept.
        # `runtime_id` and `bbox` matter for the same-or-descendant
        # check; pass distinct values when constructing UNRELATED
        # elements so the new identity-based comparison sees them
        # as different.
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        return {
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/{name}:ButtonControl",
            "name": name,
            "control_type": "ButtonControl",
            "class_name": "",
            "automation_id": "",
            "bbox": bbox,
            "bbox_center": (cx, cy),
            "color": (1, 2, 3),
            "win_stem": "app",
            "window_name": window_name,
            "runtime_id": tuple(runtime_id),
            "interactable_ancestor": None,
        }

    def _patch_io(self):
        # Block side effects: cursor move, screenshot, clipboard, stdout.
        return [
            mock.patch.object(inspector, "_move_cursor"),
            mock.patch.object(inspector, "_save_step_screenshot"),
            mock.patch.object(inspector, "pyperclip"),
        ]

    def test_first_press_commits(self):
        with mock.patch.object(
            inspector, "_gather_element_info",
            return_value=self._info("0.2.0", "Save"),
        ):
            patches = self._patch_io()
            for p in patches:
                p.start()
            try:
                inspector._handle_press(100, 200)
            finally:
                for p in patches:
                    p.stop()
        # First press → commit was made and prompt is now open.
        self.assertIsNotNone(inspector._last_committed)
        self.assertEqual(inspector._last_committed["struct_id"], "0.2.0")
        self.assertIsNotNone(inspector._pending_name)

    def test_press_on_descendant_dumps_full_info_no_commit(self):
        # Pre-set a prior commit (outer button bbox 0..100). New press
        # lands on a descendant (inner element bbox 20..80, fully
        # inside the outer). Distinct runtime_ids so identity says
        # "different element"; bbox containment says "descendant".
        inspector._last_committed = self._info(
            "0.2.0", "Save",
            bbox=(0, 0, 100, 100), runtime_id=(1, 100),
        )
        descendant = self._info(
            "0.2.0.0", "Inner",
            bbox=(20, 20, 80, 80), runtime_id=(1, 200),
        )
        with mock.patch.object(
            inspector, "_gather_element_info", return_value=descendant,
        ), mock.patch.object(inspector, "_emit_full") as mef, \
             mock.patch.object(inspector, "_commit") as mc:
            inspector._handle_press(50, 50)
        mef.assert_called_once()
        mc.assert_not_called()
        # Last commit should still be the original — descendant didn't
        # replace it.
        self.assertEqual(
            inspector._last_committed["struct_id"], "0.2.0",
        )

    def test_press_on_same_element_dumps_full_info(self):
        # Same RuntimeId means same element; this is the canonical
        # "user pressed the same button twice" case.
        inspector._last_committed = self._info(
            "0.2.0", "Save", runtime_id=(7, 42),
        )
        with mock.patch.object(
            inspector, "_gather_element_info",
            return_value=self._info("0.2.0", "Save", runtime_id=(7, 42)),
        ), mock.patch.object(inspector, "_emit_full") as mef, \
             mock.patch.object(inspector, "_commit") as mc:
            inspector._handle_press(50, 50)
        mef.assert_called_once()
        mc.assert_not_called()

    def test_press_on_unrelated_element_finalizes_and_commits(self):
        # First commit. Open prompt. Press on a totally different element.
        # Distinct bboxes that don't contain each other AND distinct
        # runtime_ids — the new check correctly says "unrelated".
        inspector._last_committed = self._info(
            "0.2.0", "Save",
            bbox=(0, 0, 50, 50), runtime_id=(1, 100),
        )
        inspector._pending_name = {
            "buffer": "typed",
            "default": "SAVE",
            "commit": inspector._last_committed | {
                "default_name": "SAVE", "final_name": None,
            },
        }
        inspector._used_names.add("SAVE")
        unrelated = self._info(
            "0.5.1", "Cancel",
            bbox=(200, 200, 250, 250), runtime_id=(2, 300),
        )
        with mock.patch.object(
            inspector, "_gather_element_info", return_value=unrelated,
        ), mock.patch.object(inspector, "pyperclip"), \
             mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._handle_press(99, 99)
        # The previous prompt must have been finalized → 1 capture in
        # the list with the typed name applied.
        self.assertEqual(len(inspector._captures), 1)
        self.assertEqual(inspector._captures[0]["final_name"], "TYPED")
        # And a NEW commit is now pending under a fresh prompt.
        self.assertIsNotNone(inspector._pending_name)
        self.assertEqual(inspector._last_committed["struct_id"], "0.5.1")

    def test_press_on_same_struct_id_different_runtime_id_commits(self):
        """Regression for the Zoom/Zoom in bug. Two distinct elements
        share struct_id `0.0.0.0.0.0` at different times because the
        live UIA tree reshaped between presses. The new identity check
        uses RuntimeId — so this MUST commit Zoom in, not info-dump
        Zoom."""
        inspector._last_committed = self._info(
            "0.0.0.0.0.0", "Zoom",
            bbox=(-1803, 221, -1581, 249), runtime_id=(1, 100),
        )
        zoom_in = self._info(
            "0.0.0.0.0.0", "Zoom in",
            bbox=(-1576, 224, -1354, 253), runtime_id=(1, 200),
        )
        with mock.patch.object(
            inspector, "_gather_element_info", return_value=zoom_in,
        ), mock.patch.object(inspector, "_emit_full") as mef, \
             mock.patch.object(inspector, "_commit") as mc:
            inspector._handle_press(0, 0)
        mef.assert_not_called()
        mc.assert_called_once_with(zoom_in)

    def test_gather_returning_none_is_silent(self):
        # If UIA returned nothing (or scope filtered the press out),
        # _handle_press is a no-op — no commit, no error.
        with mock.patch.object(
            inspector, "_gather_element_info", return_value=None,
        ), mock.patch.object(inspector, "_commit") as mc, \
             mock.patch.object(inspector, "_emit_full") as mef:
            inspector._handle_press(0, 0)
        mc.assert_not_called()
        mef.assert_not_called()


# --- Commit flow ------------------------------------------------------------


class TestCommitFlow(unittest.TestCase):
    """The mechanical side of _commit: cursor jump, screenshot kickoff,
    minimal info print, prompt opening."""

    def setUp(self):
        _reset_state()
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def _info(self, struct_id="0.0", color=(10, 20, 30), ancestor=None):
        return {
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/Btn:ButtonControl",
            "name": "Btn",
            "control_type": "ButtonControl",
            "class_name": "ClassA",
            "automation_id": "btn1",
            "bbox": (0, 0, 100, 30),
            "bbox_center": (50, 15),
            "color": color,
            "win_stem": "app",
            "interactable_ancestor": ancestor,
        }

    def test_commit_jumps_cursor_to_center(self):
        with mock.patch.object(inspector, "_move_cursor") as mv, \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(self._info())
        mv.assert_called_once_with(50, 15)

    def test_commit_skips_cursor_jump_when_center_unknown(self):
        info = self._info()
        info["bbox_center"] = (None, None)
        with mock.patch.object(inspector, "_move_cursor") as mv, \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(info)
        mv.assert_not_called()

    def test_commit_kicks_off_screenshot(self):
        with mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot") as ms:
            inspector._commit(self._info())
        ms.assert_called_once()

    def test_commit_opens_prompt_and_records_last(self):
        with mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(self._info(struct_id="0.4.2"))
        self.assertIsNotNone(inspector._pending_name)
        self.assertEqual(inspector._pending_name["buffer"], "")
        self.assertEqual(inspector._pending_name["default"], "BTN")
        self.assertEqual(
            inspector._last_committed["struct_id"], "0.4.2",
        )

    def test_commit_reserves_default_name_in_used_set(self):
        with mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(self._info())
        self.assertIn("BTN", inspector._used_names)

    def test_minimal_info_printed_to_terminal(self):
        with mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(
                self._info(struct_id="0.0.7", color=(255, 0, 0)),
            )
        printed = inspector.sys.stdout.getvalue()
        self.assertIn("0.0.7", printed)
        self.assertIn("Btn", printed)
        self.assertIn("ButtonControl", printed)
        self.assertIn("ff0000", printed)  # hex color
        # Prompt line is present at the end (no newline — input is live).
        self.assertIn("name [BTN]:", printed)

    def test_minimal_info_includes_ancestor_note(self):
        ancestor = {
            "struct_id": "0.0",
            "control_type": "ButtonControl",
            "name": "Submit",
        }
        # Captured leaf is text — note line should explain the alternative.
        info = self._info(struct_id="0.0.0", ancestor=ancestor)
        info["control_type"] = "TextControl"
        with mock.patch.object(inspector, "_move_cursor"), \
             mock.patch.object(inspector, "_save_step_screenshot"):
            inspector._commit(info)
        printed = inspector.sys.stdout.getvalue()
        self.assertIn("note", printed.lower())
        self.assertIn("0.0", printed)
        self.assertIn("Submit", printed)


# --- Session-end clipboard --------------------------------------------------


class TestSessionEnd(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def _push(self, name, struct_id, label):
        inspector._captures.append({
            "final_name": name,
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/{label}:ButtonControl",
            "name": label,
        })

    def test_no_captures_announced(self):
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        mp.copy.assert_not_called()
        self.assertIn("No captures", inspector.sys.stdout.getvalue())

    def test_single_capture_clipboard_block(self):
        self._push("SAVE", "0.2.0", "Save")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertEqual(block, 'SAVE = "0.2.0"  # Save')

    def test_multi_capture_block_aligns_constants(self):
        self._push("SAVE", "0.2.0", "Save")
        self._push("CANCEL_BUTTON", "0.5.1", "Cancel")
        self._push("OK", "0.6.0", "OK")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        lines = block.split("\n")
        self.assertEqual(len(lines), 3)
        # All lines should pad the constant name to the longest width.
        widths = [line.index("=") for line in lines]
        self.assertEqual(len(set(widths)), 1)
        # Order matches commit order.
        self.assertTrue(lines[0].startswith("SAVE  "))
        self.assertTrue(lines[1].startswith("CANCEL_BUTTON "))
        self.assertTrue(lines[2].startswith("OK    "))

    def test_no_state_machine_skeletons_generated(self):
        # The user explicitly dropped state-machine generation. The
        # session-end block must contain ONLY constants — no `def
        # state_stepN` lines anywhere.
        self._push("SAVE", "0.2.0", "Save")
        self._push("CANCEL", "0.5.1", "Cancel")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertNotIn("def state_step", block)
        self.assertNotIn("def ", block)

    def test_finalizes_pending_prompt_first(self):
        # If Ctrl+C arrives mid-prompt, the pending capture must be
        # included — finalize is called before the clipboard build.
        commit = {
            "struct_id": "0.9", "name_path": "Win/Save:ButtonControl",
            "name": "Save", "control_type": "ButtonControl",
            "class_name": "", "automation_id": "",
            "bbox": (0, 0, 10, 10), "bbox_center": (5, 5),
            "color": None, "win_stem": "app",
            "interactable_ancestor": None,
            "default_name": "SAVE", "final_name": None,
        }
        inspector._used_names.add("SAVE")
        inspector._pending_name = {
            "buffer": "", "default": "SAVE", "commit": commit,
        }
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        # 1 capture finalized; clipboard contains its line.
        self.assertEqual(len(inspector._captures), 1)
        block = mp.copy.call_args[0][0]
        self.assertIn("SAVE", block)
        self.assertIn("0.9", block)


# --- Web selector emission in copy-paste block ------------------------------


class TestSessionEndEmitsWebSelector(unittest.TestCase):
    """`_render_group` (inside `_build_session_block`) prefers the web
    CSS selector over struct_id when the capture has one. Native
    captures (no `web_capture`) emit struct_id, exactly as today."""

    def setUp(self):
        _reset_state()
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def _push(self, name, struct_id, label, web_capture=False,
              web_selector=None):
        inspector._captures.append({
            "final_name": name,
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/{label}:ButtonControl",
            "name": label,
            "web_capture": web_capture,
            "web_selector": web_selector,
        })

    def test_native_capture_emits_struct_id(self):
        # Behaviour preserved for Windows apps: no web_capture flag,
        # no selector, struct_id wins.
        self._push("SAVE", "0.2.0", "Save")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertEqual(block, 'SAVE = "0.2.0"  # Save')

    def test_web_capture_with_selector_emits_selector(self):
        # Web capture with a usable CSS selector — selector wins.
        self._push("LOGIN", "0.5.3.2", "Sign in",
                   web_capture=True, web_selector="#login")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertEqual(block, 'LOGIN = "#login"  # Sign in')

    def test_web_capture_without_selector_emits_struct_id_with_warning(self):
        # Web capture but UIA exposed nothing usable — fall back to
        # struct_id with a warning so the user knows it's brittle.
        self._push("WIDGET", "0.5.3.2", "Widget", web_capture=True,
                   web_selector=None)
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertIn('WIDGET = "0.5.3.2"', block)
        self.assertIn("# Widget", block)
        self.assertIn("no stable web selector", block)

    def test_mixed_native_and_web_in_one_session(self):
        # Realistic multi-app session: Notepad (native) + a browser
        # tab (web). Each emits its right form.
        self._push("NOTEPAD_FILE", "0.2.0.0.0", "File")
        self._push("LOGIN_BTN", "0.5.3.2", "Sign in",
                   web_capture=True, web_selector="#login")
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        # Width is max(len("NOTEPAD_FILE"), len("LOGIN_BTN")) = 12, so
        # NOTEPAD_FILE has no extra padding, LOGIN_BTN gets 3 spaces.
        self.assertIn('NOTEPAD_FILE = "0.2.0.0.0"  # File', block)
        self.assertIn('LOGIN_BTN    = "#login"  # Sign in', block)


# --- Full info dump ---------------------------------------------------------


class TestEmitFullInfo(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        _reset_state()

    def _commit(self, **overrides):
        commit = {
            "struct_id": "0.2.0",
            "name_path": "App:WindowControl/Pane:PaneControl/Save:ButtonControl",
            "name": "Save",
            "control_type": "ButtonControl",
            "class_name": "AcmeButton",
            "automation_id": "save_btn",
            "bbox": (10, 20, 110, 50),
            "bbox_center": (60, 35),
            "color": (50, 200, 50),
            "win_stem": "app",
            "interactable_ancestor": None,
            "default_name": "SAVE",
            "final_name": None,
        }
        commit.update(overrides)
        return commit

    def test_all_fields_appear_in_block(self):
        inspector._emit_full(self._commit())
        printed = inspector.sys.stdout.getvalue()
        for token in (
            "0.2.0", "Save", "ButtonControl", "AcmeButton", "save_btn",
            "(10, 20)", "(110, 50)", "(60, 35)", "32c832",  # color hex
        ):
            self.assertIn(token, printed, f"missing {token!r} in full block")

    def test_ancestor_line_when_text_under_button(self):
        inspector._emit_full(self._commit(
            interactable_ancestor={
                "struct_id": "0.2", "control_type": "ButtonControl",
                "name": "Outer",
            },
        ))
        printed = inspector.sys.stdout.getvalue()
        self.assertIn("ancestor", printed.lower())
        self.assertIn("Outer", printed)

    def test_toggle_state_line_when_checkbox(self):
        inspector._emit_full(self._commit(toggle_state=True))
        printed = inspector.sys.stdout.getvalue()
        self.assertIn("checkbox", printed)
        self.assertIn("checked", printed)

    def test_no_toggle_line_when_not_toggleable(self):
        # Default fixture has no toggle_state key → no checkbox line.
        inspector._emit_full(self._commit())
        printed = inspector.sys.stdout.getvalue()
        self.assertNotIn("checkbox", printed)


class TestFormatToggle(unittest.TestCase):
    def test_true_renders_checked(self):
        self.assertEqual(inspector._format_toggle(True), "checked")

    def test_false_renders_unchecked(self):
        self.assertEqual(inspector._format_toggle(False), "unchecked")

    def test_indeterminate_string_passes_through(self):
        self.assertEqual(inspector._format_toggle("indeterminate"),
                         "indeterminate")


# --- Multi-app registration -------------------------------------------------


class _FakeWin:
    """Minimal stand-in for a top-level UIA window — exposes the
    attributes `_classify_window` reads."""

    def __init__(self, hwnd, pid, name=""):
        self.NativeWindowHandle = hwnd
        self.ProcessId = pid
        self.Name = name


class TestMultiAppRegistration(unittest.TestCase):
    """Inspector classifies each press's top-level window as an app
    (first HWND per exe), a popup (extra HWND in known process), or
    existing (HWND already classified). Drives the `_windows`/
    `_window_by_hwnd`/`_stems_seen` maps."""

    def setUp(self):
        _reset_state()

    def tearDown(self):
        _reset_state()

    def test_first_hwnd_per_stem_registers_as_app(self):
        win = _FakeWin(hwnd=100, pid=42, name="Notepad")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="notepad"):
            name, kind = inspector._classify_window(win)
        self.assertEqual(name, "notepad")
        self.assertEqual(kind, "app")
        self.assertIn("notepad", inspector._windows)
        self.assertTrue(inspector._windows["notepad"]["is_app"])
        self.assertEqual(inspector._windows["notepad"]["spec"], "notepad.exe")
        self.assertEqual(inspector._window_by_hwnd[100], "notepad")
        self.assertEqual(inspector._stems_seen["notepad"], "notepad")

    def test_app_spec_records_full_exe_path_when_available(self):
        """Apps not on PATH (Riot Client, Steam games, custom installs)
        need a full executable path so the runner can launch them.
        `_exe_path_for_pid` returns `psutil.Process(pid).exe()` which is
        the absolute path, not just the basename."""
        win = _FakeWin(hwnd=100, pid=42, name="Riot Client")
        full_path = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="riotclientservices"), \
             mock.patch.object(inspector, "_exe_path_for_pid",
                               return_value=full_path):
            inspector._classify_window(win)
        self.assertEqual(
            inspector._windows["riotclientservices"]["spec"], full_path,
            "spec must be the full exe path so subprocess.Popen can "
            "launch apps not on PATH",
        )

    def test_second_hwnd_same_stem_registers_as_popup(self):
        # First HWND establishes the app.
        win_app = _FakeWin(hwnd=100, pid=42, name="Notepad")
        win_popup = _FakeWin(hwnd=200, pid=42, name="Save As")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="notepad"), \
             mock.patch.object(inspector, "_prompt_save_popup",
                               return_value="save_as"):
            inspector._classify_window(win_app)
            name, kind = inspector._classify_window(win_popup)
        self.assertEqual(kind, "popup")
        self.assertEqual(name, "save_as")
        self.assertFalse(inspector._windows["save_as"]["is_app"])
        self.assertIsNone(inspector._windows["save_as"]["spec"])
        # `notepad` (the app) is unchanged.
        self.assertTrue(inspector._windows["notepad"]["is_app"])

    def test_popup_decline_skips_registration(self):
        # User says "n" at the prompt — the popup is added to the
        # skip set so subsequent presses inside it are ignored.
        win_app = _FakeWin(hwnd=100, pid=42, name="Notepad")
        win_popup = _FakeWin(hwnd=200, pid=42, name="Save As")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="notepad"), \
             mock.patch.object(inspector, "_prompt_save_popup",
                               return_value=None) as mp:
            inspector._classify_window(win_app)
            name, kind = inspector._classify_window(win_popup)
            # Second press in the same declined HWND must NOT re-prompt.
            inspector._classify_window(win_popup)
        self.assertIsNone(name)
        self.assertIsNone(kind)
        self.assertNotIn("save_as", inspector._windows)
        self.assertIn(200, inspector._skip_popup_hwnds)
        mp.assert_called_once()

    def test_known_hwnd_returns_existing(self):
        win = _FakeWin(hwnd=100, pid=42, name="Notepad")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="notepad"):
            inspector._classify_window(win)
            name, kind = inspector._classify_window(win)
        self.assertEqual(name, "notepad")
        self.assertEqual(kind, "existing")
        # Still only one app entry.
        self.assertEqual(len(inspector._windows), 1)

    def test_disambiguates_when_app_name_collides(self):
        # An (unusual) case: two different exe stems happen to
        # auto-name to the same string after sanitisation. The second
        # gets a "_2" suffix.
        win_a = _FakeWin(hwnd=100, pid=42, name="App A")
        win_b = _FakeWin(hwnd=200, pid=43, name="App B")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="myapp"), \
             mock.patch.object(inspector, "_exe_path_for_pid",
                               return_value="C:/Apps/myapp.exe"):
            # Force the second call to think it's a different stem
            # by clearing the _stems_seen entry between calls.
            name_a, _ = inspector._classify_window(win_a)
            inspector._stems_seen.clear()  # simulate "different stem"
            name_b, _ = inspector._classify_window(win_b)
        self.assertNotEqual(name_a, name_b)

    def test_popup_name_falls_back_when_title_empty(self):
        # Popup with no title → prompt's default is exe-stem-based, and
        # the user accepts that default ("" → fall back to default_base).
        win_app = _FakeWin(hwnd=100, pid=42, name="Notepad")
        win_popup = _FakeWin(hwnd=200, pid=42, name="")
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value="notepad"), \
             mock.patch.object(inspector, "_prompt_save_popup",
                               return_value="notepad_dlg"):
            inspector._classify_window(win_app)
            name, kind = inspector._classify_window(win_popup)
        self.assertEqual(kind, "popup")
        self.assertTrue(name.startswith("notepad_dlg") or name == "notepad")

    def test_classify_returns_none_when_pid_unresolvable(self):
        win = _FakeWin(hwnd=100, pid=42)
        with mock.patch.object(inspector, "_exe_stem_for_pid",
                               return_value=""):
            name, kind = inspector._classify_window(win)
        self.assertIsNone(name)
        self.assertIsNone(kind)
        self.assertEqual(inspector._windows, {})


# --- Session-end clipboard / deferred fingerprint writes --------------------


class TestSessionEndDeferredWrites(unittest.TestCase):
    """`_emit_session_end` is the only place fingerprints land on disk —
    a Ctrl+C-cancelled session leaves no sidecars behind. The clipboard
    block is structured: `APPS = {...}` + groups per window."""

    def setUp(self):
        _reset_state()
        self.tmp = Path(tempfile.mkdtemp(prefix="inspector_session_"))
        self._orig_dir = inspector.config.WINDOW_FINGERPRINT_DIR
        inspector.config.WINDOW_FINGERPRINT_DIR = self.tmp
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        inspector.config.WINDOW_FINGERPRINT_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        _reset_state()

    def _push(self, name, struct_id, label, window_name=""):
        inspector._captures.append({
            "final_name": name,
            "struct_id": struct_id,
            "name_path": f"App:WindowControl/{label}:ButtonControl",
            "name": label,
            "window_name": window_name,
        })

    def _register_app(self, name, fingerprint=None, idx=0):
        inspector._windows[name] = {
            "hwnd": 100 + idx,
            "is_app": True,
            "spec": f"{name}.exe",
            "title_hint": name.title(),
            "fingerprint": fingerprint,
            "first_seen_idx": idx,
        }

    def _register_popup(self, name, fingerprint=None, idx=0):
        inspector._windows[name] = {
            "hwnd": 200 + idx,
            "is_app": False,
            "spec": None,
            "title_hint": name.replace("_", " ").title(),
            "fingerprint": fingerprint,
            "first_seen_idx": idx,
        }

    def test_emits_apps_dict_line(self):
        self._register_app("notepad", idx=0)
        self._register_app("calc", idx=1)
        self._push("NOTEPAD_FILE", "0.0", "File", "notepad")
        self._push("CALC_BTN_5", "0.5", "Five", "calc")
        with mock.patch.object(inspector, "pyperclip") as mp, \
             mock.patch.object(inspector.tree, "save_fingerprint"):
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertIn('APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}', block)

    def test_constants_grouped_by_window(self):
        self._register_app("notepad", idx=0)
        self._register_popup("save_dialog", idx=1)
        self._push("NOTEPAD_FILE", "0.0", "File", "notepad")
        self._push("NOTEPAD_VIEW", "0.1", "View", "notepad")
        self._push("SAVE_DIALOG_OK", "0.0.0", "OK", "save_dialog")
        with mock.patch.object(inspector, "pyperclip") as mp, \
             mock.patch.object(inspector.tree, "save_fingerprint"):
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        # Headers are present and ordered by first-seen index.
        notepad_idx = block.index("# --- notepad ---")
        save_idx = block.index("# --- save_dialog ---")
        self.assertLess(notepad_idx, save_idx)
        # Notepad constants live in the notepad section.
        notepad_section = block[notepad_idx:save_idx]
        self.assertIn("NOTEPAD_FILE", notepad_section)
        self.assertIn("NOTEPAD_VIEW", notepad_section)
        # Save_dialog constants live in their own section.
        save_section = block[save_idx:]
        self.assertIn("SAVE_DIALOG_OK", save_section)

    def test_no_apps_line_when_only_popups(self):
        # Edge case — recovery mode could emit only popup data with no
        # registered apps. The block should still be valid Python.
        self._register_popup("dlg", idx=0)
        self._push("DLG_OK", "0.0", "OK", "dlg")
        with mock.patch.object(inspector, "pyperclip") as mp, \
             mock.patch.object(inspector.tree, "save_fingerprint"):
            inspector._emit_session_end()
        block = mp.copy.call_args[0][0]
        self.assertNotIn("APPS = ", block)
        self.assertIn("# --- dlg ---", block)
        self.assertIn("DLG_OK", block)

    def test_writes_fingerprint_sidecar_per_window(self):
        # Both registered windows have non-empty fingerprints → both
        # get save_fingerprint calls at session end.
        self._register_app("notepad",
                           fingerprint=[(0, "WindowControl")], idx=0)
        self._register_popup("save_dialog",
                             fingerprint=[(0, "WindowControl"),
                                          (1, "ButtonControl")], idx=1)
        self._push("X", "0.0", "x", "notepad")
        with mock.patch.object(inspector, "pyperclip"), \
             mock.patch.object(inspector.tree, "save_fingerprint") as msave:
            inspector._emit_session_end()
        names_saved = {call.args[0] for call in msave.call_args_list}
        self.assertEqual(names_saved, {"notepad", "save_dialog"})

    def test_no_sidecar_until_emit_session_end(self):
        # During the session we mutate `_windows[name]["fingerprint"]`
        # in memory. Confirm `tree.save_fingerprint` is NOT called by
        # `_capture_fingerprint` (deferred to session end).
        with mock.patch.object(inspector.tree, "fingerprint",
                               return_value=[(0, "WindowControl")]), \
             mock.patch.object(inspector.tree, "save_fingerprint") as msave:
            inspector._windows["notepad"] = {
                "hwnd": 100, "is_app": True, "spec": "notepad.exe",
                "title_hint": "Notepad", "fingerprint": None,
                "first_seen_idx": 0,
            }
            inspector._capture_fingerprint(_FakeWin(100, 42), "notepad")
        msave.assert_not_called()
        self.assertEqual(inspector._windows["notepad"]["fingerprint"],
                         [(0, "WindowControl")])

    def test_no_emit_when_no_captures(self):
        # Empty session — clipboard not touched, sidecars not written.
        with mock.patch.object(inspector, "pyperclip") as mp, \
             mock.patch.object(inspector.tree, "save_fingerprint") as msave:
            inspector._emit_session_end()
        mp.copy.assert_not_called()
        msave.assert_not_called()


# --- Recovery mode ----------------------------------------------------------


class TestRecoveryParseSession(unittest.TestCase):
    """`_parse_session_file` reads the session-py sidecar back into the
    structure recovery mode iterates over."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inspector_parse_"))
        self.path = self.tmp / "session.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parses_apps_dict(self):
        self.path.write_text(
            'APPS = {"notepad": "notepad.exe", "calc": "calc.exe"}\n',
            encoding="utf-8",
        )
        parsed = inspector._parse_session_file(self.path)
        self.assertEqual(parsed["apps"],
                         {"notepad": "notepad.exe", "calc": "calc.exe"})

    def test_parses_grouped_constants(self):
        self.path.write_text(
            'APPS = {"notepad": "notepad.exe"}\n'
            "\n"
            "# --- notepad ---\n"
            'NOTEPAD_FILE = "0.2.0"  # File\n'
            'NOTEPAD_VIEW = "0.2.1"  # View\n'
            "\n"
            "# --- save_dialog ---\n"
            'SAVE_OK = "0.0.0"  # OK\n',
            encoding="utf-8",
        )
        parsed = inspector._parse_session_file(self.path)
        self.assertEqual(len(parsed["windows"]["notepad"]), 2)
        self.assertEqual(parsed["windows"]["notepad"][0]["name"],
                         "NOTEPAD_FILE")
        self.assertEqual(parsed["windows"]["notepad"][0]["struct_id"],
                         "0.2.0")
        self.assertEqual(parsed["windows"]["save_dialog"][0]["name"],
                         "SAVE_OK")

    def test_returns_empty_when_file_missing(self):
        parsed = inspector._parse_session_file(self.tmp / "missing.py")
        self.assertEqual(parsed["apps"], {})
        self.assertEqual(parsed["windows"], {"": []})

    def test_skips_malformed_lines(self):
        self.path.write_text(
            "garbage that isn't a constant\n"
            'NOTEPAD_FILE = "0.2.0"  # File\n'
            "another junk line\n",
            encoding="utf-8",
        )
        parsed = inspector._parse_session_file(self.path)
        # The valid line is captured (under the unbound "" group).
        self.assertEqual(len(parsed["windows"][""]), 1)


class TestRecoveryFlow(unittest.TestCase):
    """High-level: recovery walks the parsed session, finds matching live
    windows, runs `find_or_heal` per element, and emits a refreshed
    paste-ready block. UI-less: every UIA call is mocked."""

    def setUp(self):
        _reset_state()
        self.tmp = Path(tempfile.mkdtemp(prefix="inspector_recover_"))
        self.snippets = self.tmp / "snippets"
        self.fps = self.tmp / "fps"
        self.snippets.mkdir()
        self.fps.mkdir()
        self._orig_snip = inspector._SNIPPETS_DIR
        self._orig_fp = inspector.config.WINDOW_FINGERPRINT_DIR
        inspector._SNIPPETS_DIR = self.snippets
        inspector.config.WINDOW_FINGERPRINT_DIR = self.fps
        self.stdout_patcher = mock.patch.object(
            inspector.sys, "stdout", new=io.StringIO(),
        )
        self.stdout_patcher.start()

    def tearDown(self):
        self.stdout_patcher.stop()
        inspector._SNIPPETS_DIR = self._orig_snip
        inspector.config.WINDOW_FINGERPRINT_DIR = self._orig_fp
        shutil.rmtree(self.tmp, ignore_errors=True)
        _reset_state()

    def _write_session(self, body):
        path = self.snippets / "session_2026-01-01_00-00-00.py"
        path.write_text(body, encoding="utf-8")

    def _save_fp(self, name, fp):
        from core import tree as t
        t.save_fingerprint(name, fp)

    def test_no_sidecar_announces_and_returns(self):
        # Empty snippets dir → recovery prints a hint and exits cleanly.
        inspector._recover()
        out = inspector.sys.stdout.getvalue()
        self.assertIn("no session sidecar", out.lower())

    def test_reuses_struct_id_when_window_not_matched(self):
        # Saved session refers to a window with no fingerprint sidecar →
        # recovery skips window-matching and keeps the original struct_id.
        self._write_session(
            'APPS = {"notepad": "notepad.exe"}\n'
            "\n"
            "# --- notepad ---\n"
            'NOTEPAD_FILE = "0.2.0"  # File\n'
        )
        # No fingerprint file written for "notepad" — window-match skipped.
        with mock.patch.object(inspector, "pyperclip") as mp:
            inspector._recover()
        block = mp.copy.call_args[0][0]
        # The original struct_id is preserved.
        self.assertIn('NOTEPAD_FILE = "0.2.0"', block)

    def test_silent_update_when_fingerprint_matches(self):
        # Saved fingerprint matches a live window above threshold →
        # recovery runs find_or_heal silently for each element.
        self._write_session(
            'APPS = {"notepad": "notepad.exe"}\n'
            "\n"
            "# --- notepad ---\n"
            'NOTEPAD_FILE = "0.2.0"  # File\n'
        )
        self._save_fp("notepad", [(0, "WindowControl"),
                                  (1, "MenuBarControl")])
        live = _FakeWin(hwnd=42, pid=1234, name="Notepad")
        # Pretend the live walk has the same struct_id (no drift); heal
        # returns it unchanged.
        fake_walked = [{"struct_id": "0.2.0", "ctrl": object(),
                        "tree_id": "App/File", "name": "File",
                        "role": "MenuItemControl",
                        "bbox": [0, 0, 1, 1], "enabled": True}]
        with mock.patch.object(inspector, "_find_live_window",
                               return_value=(live, 0.95)), \
             mock.patch.object(inspector.tree, "fingerprint",
                               return_value=[(0, "WindowControl"),
                                             (1, "MenuBarControl")]), \
             mock.patch.object(inspector.tree, "save_fingerprint"), \
             mock.patch.object(inspector.tree, "load_snapshot",
                               return_value=[]), \
             mock.patch.object(inspector.tree, "walk_live",
                               return_value=fake_walked), \
             mock.patch.object(inspector.tree, "find_or_heal",
                               return_value=(fake_walked[0]["ctrl"], False)), \
             mock.patch.object(inspector.psutil, "process_iter",
                               return_value=[]), \
             mock.patch.object(inspector, "pyperclip") as mp:
            inspector._recover()
        block = mp.copy.call_args[0][0]
        self.assertIn('NOTEPAD_FILE = "0.2.0"', block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
