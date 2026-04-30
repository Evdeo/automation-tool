"""User-facing facade over a uiautomation WindowControl.

Every verb you need lives as a method on the window — `ctx.notepad.click(...)`,
`ctx.notepad.fill(...)`, `ctx.notepad.popup(...)`. IDE autocomplete on
`ctx.<app>.` lists the entire surface, so users don't have to remember
which framework module a function lives in.

Escape hatch: `ctx.notepad.ctrl` returns the raw uiautomation control
for anything not yet exposed as a method.
"""
import pyautogui

from core import actions, apps, dialogs


class Window:
    def __init__(self, ctrl):
        self.ctrl = ctrl

    @property
    def name(self):
        """Live window title."""
        return self.ctrl.Name

    @property
    def pid(self):
        """Owning process id."""
        return self.ctrl.ProcessId

    def focus(self):
        """Bring this window to the foreground."""
        apps.bring_to_foreground(self.ctrl)

    def click(self, control_id):
        """Click a single control."""
        return actions.press(self.ctrl, control_id)

    def menu(self, *items):
        """Walk a menu chain — each step's submenu only enters the live
        tree after the previous click opens it; the resolver bridges
        the gap. Use for File → Submenu → Item flows.
        """
        return actions.press_path(self.ctrl, *items)

    def double_click(self, control_id):
        """Double-click a control."""
        return actions.double_press(self.ctrl, control_id)

    def click_when_active(self, control_id, timeout=30):
        """Click as soon as the control becomes enabled — useful for
        buttons that are disabled while a background task runs."""
        return actions.press_when_active(self.ctrl, control_id, timeout=timeout)

    def type(self, text, interval=0.02):
        """Type `text` into whatever currently has keyboard focus."""
        return actions.type_text(text, interval=interval)

    def fill(self, control_id, text):
        """Click a text field and paste `text` into it (clipboard-based,
        works regardless of keyboard layout)."""
        return actions.write_text(self.ctrl, control_id, text)

    def hotkey(self, *keys):
        """Send a key combo (e.g. .hotkey('ctrl', 's')). Auto-focuses
        this window first."""
        self.focus()
        pyautogui.hotkey(*keys)

    def has(self, control_id, timeout=0, enabled=False):
        """Return True if the control is present (and enabled, if
        `enabled=True`) within `timeout` seconds. Non-throwing — use
        in `if` statements."""
        if enabled:
            return actions.check_active(self.ctrl, control_id, timeout=timeout)
        return actions.is_present(self.ctrl, control_id, timeout=timeout)

    def wait_gone(self, control_id, timeout=10):
        """Wait until the control is no longer visible. Returns True
        once it's gone, False on timeout."""
        return actions.wait_until_absent(self.ctrl, control_id, timeout=timeout)

    def popup(self, title, timeout=8):
        """Find a dialog / sub-window opened by this window. Returns a
        new Window with the same fluent interface, so the popup is
        driven the same way as the main app."""
        from core import app as app_mod
        return Window(app_mod.popup(self.ctrl, title, timeout=timeout))

    def dismiss_popups(self, max_passes=5):
        """Click any visible 'OK' button up to `max_passes` times.
        Useful for sweeping away leftover error dialogs from a previous
        run before the real test logic begins."""
        return dialogs.dismiss_ok_popups(self.ctrl, max_passes=max_passes)

    def save_as(self, path):
        """Drive this window as a Save As dialog: replace the file-name
        field with `path` and click Save. Call on the dialog returned
        by `.popup('save')`, not on the main app."""
        return dialogs.save_as(self.ctrl, path)

    def pixel(self, control_id, dx=0, dy=0):
        """Sample the pixel color at a control's center, optionally
        offset. Returns (r, g, b)."""
        return actions.get_color(self.ctrl, control_id, x_offset=dx, y_offset=dy)

    def __repr__(self):
        return f"Window({self.name!r})"
