"""Window registry + lifecycle.

`from core import window` and access live windows by name:

    window.notepad           # cached Control for the "notepad" app
    window.open("notepad")   # launch (or re-bind to) the registered app
    window.close("notepad")  # terminate the process, drop the handle
    window.get("calc")       # match an already-open window, no launch
    window.popup("save_dlg") # match a popup that just appeared

Names map to executable paths via `register(name, path)`. The runner
calls `register` for every entry in the `apps={...}` dict at start, so
user code only ever passes the name.

Reserved attribute names: `open`, `close`, `get`, `popup`, `register`,
`registry`. Don't name an app one of these.
"""
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import uiautomation as auto


_windows: dict = {}   # name -> Control (live window handle)
_registry: dict = {}  # name -> exe path


def __getattr__(name: str):
    """Module-level dynamic attribute access (PEP 562). `window.notepad`
    returns the live Control if `open` or `get` has registered one."""
    try:
        return _windows[name]
    except KeyError:
        registered = ", ".join(sorted(_windows)) or "<none>"
        raise AttributeError(
            f"no live window named {name!r}. Open one with "
            f"window.open({name!r}) first. Currently open: {registered}."
        )


def register(name: str, path: str) -> None:
    """Bind `name` to an executable path. Called once per app at runner
    start; user code rarely needs to call this directly."""
    _registry[name] = path


def registry() -> dict:
    """Snapshot of registered name -> exe path."""
    return dict(_registry)


def open(name: str, timeout: float = 45.0) -> "auto.Control":
    """Find or launch the app `name`. Caches the resulting window so
    `window.<name>` returns it for the rest of the run.

    Timeout default 45s — UWP apps (Calculator on Win11) can take 20-30s
    between Popen returning and the window actually rendering.

    Raises `KeyError` if `name` isn't registered, `TimeoutError` if no
    matching window appears within `timeout`.
    """
    if name not in _registry:
        registered = ", ".join(sorted(_registry)) or "<none>"
        raise KeyError(
            f"app {name!r} not registered. Add it to apps={{...}} in "
            f"runner.start(). Registered: {registered}."
        )
    from core import app as app_mod
    win = app_mod.match(name, launch=_registry[name], timeout=timeout)
    if win is None:
        raise TimeoutError(
            f"could not match or launch app {name!r} from "
            f"{_registry[name]!r}. Run `python inspector.py`, capture "
            f"this app, and try again."
        )
    _windows[name] = win
    return win


def get(name: str, timeout: float = 0.0) -> Optional["auto.Control"]:
    """Match an already-open window by saved fingerprint. Does NOT
    launch — returns `None` if no live window matches. Caches the hit
    so `window.<name>` works afterwards.

    `timeout > 0` polls until a match appears or the timeout elapses.
    """
    import time
    from core import app as app_mod
    deadline = time.time() + timeout
    while True:
        hit = app_mod.find(name)
        if hit is not None:
            _windows[name] = hit
            return hit
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def popup(name: str, restrict_pid=None,
          parent=None) -> Optional["auto.Control"]:
    """Match a popup that appeared since the last action verb. Use
    inside `no_dismiss()` so auto-dismiss doesn't kill the popup before
    it's captured:

        with no_dismiss():
            hotkey(window.notepad, "ctrl", "s")
            dlg = window.popup("save_dialog")
            # ...drive the dialog via dlg...

    Returns `Control | None` — `None` if no new top-level window
    matches the saved fingerprint. Popups are ephemeral, so the result
    is NOT cached on `window.<name>`; hold on to the return value.
    """
    from core import app as app_mod
    return app_mod.match(name, launch="popup",
                         restrict_pid=restrict_pid, parent=parent)


def close(name: str) -> None:
    """Terminate the process owning `window.<name>` and drop the handle.
    Silent no-op if `name` isn't currently open."""
    win = _windows.pop(name, None)
    if win is None:
        return
    import psutil
    try:
        psutil.Process(win.ProcessId).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _reset() -> None:
    """Test helper: drop all cached handles and the registry. Each
    --loop iteration runs in a fresh child process so production code
    never needs this."""
    _windows.clear()
    _registry.clear()
