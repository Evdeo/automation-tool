import argparse
import functools
import multiprocessing as mp
import re
import time
from types import SimpleNamespace

import config
from core import db


def _child(target):
    target()


def _kill_orphans(parent_pid, names, elapsed):
    """After a watchdog kill, terminate any process whose name matches one
    of `names` (case-insensitive substring match, same rule as
    `apps.close_app`)."""
    if not names:
        return
    from core import apps
    for name in names:
        try:
            killed = apps.close_app(name)
        except Exception:
            db.log("watchdog", parent_pid, f"close_failed:{name}", -1, elapsed)
            continue
        db.log("watchdog", parent_pid, f"closed:{name}", killed, elapsed)


def _supervise(target, timeout_sec, kill_on_timeout=None):
    proc = mp.Process(target=_child, args=(target,), daemon=False)
    started = time.time()
    proc.start()
    db.log("watchdog", proc.pid, "started", 0, 0.0)
    proc.join(timeout=timeout_sec)
    elapsed = round(time.time() - started, 1)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.kill()
            proc.join()
        db.log("watchdog", proc.pid, "killed_timeout", -1, elapsed)
        _kill_orphans(proc.pid, kill_on_timeout, elapsed)
        return ("killed_timeout", None)
    db.log("watchdog", proc.pid, "exited", proc.exitcode, elapsed)
    return ("exited", proc.exitcode)


def run_once_with_watchdog(target, timeout_min=None, kill_on_timeout=None):
    if timeout_min is None:
        timeout_min = config.LOOP_TIMEOUT_MIN
    return _supervise(target, timeout_min * 60, kill_on_timeout=kill_on_timeout)


def run_with_watchdog(test_loop, error_loop=None, kill_on_timeout=None):
    """Forever-loop the watchdog. After a clean exit the next iteration
    runs `test_loop`; after a watchdog kill or non-zero exit it runs
    `error_loop` (defaults to `test_loop`)."""
    if error_loop is None:
        error_loop = test_loop
    timeout_sec = config.LOOP_TIMEOUT_MIN * 60
    target = test_loop
    while True:
        outcome, exitcode = _supervise(target, timeout_sec,
                                       kill_on_timeout=kill_on_timeout)
        had_error = (outcome == "killed_timeout"
                     or (exitcode is not None and exitcode != 0))
        target = error_loop if had_error else test_loop


def _run_states(states, start_state, data):
    """State-machine driver with automatic transition logging.

    Each state function takes `data` (scratch carried between states —
    counters, intermediate values, results) and returns
    `(next_state, data)`. Live windows are NOT in `data`; access them
    via `from core import window` then `window.<name>`. Returning
    `(None, data)` ends the run. Entry and exit (with duration) are
    auto-logged to the `states` table. Payload-bearing logs still
    belong in the state function via `core.log`.
    """
    state = start_state
    while state is not None:
        fn = states[state]
        t0 = time.time()
        db.log("states", state, "entered", 0.0, "")
        nxt, data = fn(data)
        dur = round(time.time() - t0, 3)
        db.log("states", state, "exited", dur, nxt or "")
        state = nxt
    return data


def _normalize_apps(apps, app_mod):
    """Return [(name, exe_path), ...] from APPS as a dict {name: path}
    or a list of paths (auto-name = lowercased exe stem).

    Strings only — `app.spec(...)` was removed; fingerprint matching
    in `match()` makes the title-hint obsolete.
    """
    pairs = []
    if isinstance(apps, dict):
        for name, path in apps.items():
            pairs.append((name, str(path)))
    else:
        for path in apps:
            stem = app_mod._exe_stem(str(path)).lower()
            stem = re.sub(r"[^a-z0-9_]", "_", stem) or "app"
            pairs.append((stem, str(path)))
    return pairs


def start(states, apps, start_state, error_state=None, prelaunch=True):
    """Entry point for user scripts.

    - Parses `--loop`.
    - Verifies every launch path in `apps` is reachable.
    - Registers each app in `core.window` so state functions can call
      `window.open(name)`, `window.close(name)`, `window.get(name)` —
      and access live handles as `window.<name>`. Auto-name = lowercased
      exe stem; pass `apps={"my_name": "path.exe", ...}` to override.
    - With `prelaunch=True` (default), every registered app is opened
      before the first state runs. Set `prelaunch=False` for apps that
      can't coexist — open/close them yourself inside states.
    - Drives the state machine starting at `start_state`. Each state
      function takes `data` (scratch carried between states) and
      returns `(next_state, data)`. Returning `(None, data)` ends the
      run. Entry/exit transitions are auto-logged with timing.
    - On `--loop`, respawns a fresh child process per iteration; the
      watchdog kills any iteration that exceeds `LOOP_TIMEOUT_MIN`.
      After a kill or crash the next iteration starts at `error_state`
      (defaults to `start_state`) — point it at a recovery state if
      you want a different entry on failure. `kill_on_timeout` is
      auto-derived from `apps` so a hung GUI won't pollute next iter.
    """
    if error_state is None:
        error_state = start_state
    if start_state not in states:
        raise ValueError(
            f"start_state={start_state!r} not in STATES; "
            f"valid: {list(states)}"
        )
    if error_state not in states:
        raise ValueError(
            f"error_state={error_state!r} not in STATES; "
            f"valid: {list(states)}"
        )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously: each iteration is a fresh child process "
             "supervised by the watchdog. Stop with Ctrl+C.",
    )
    args = parser.parse_args()

    from core import app as app_mod
    from core import apps as apps_mod

    pairs = _normalize_apps(apps, app_mod)
    apps_mod.verify_installed([p for _, p in pairs])
    kill_names = [app_mod._exe_stem(p) + ".exe" for _, p in pairs]

    # Bundle args via functools.partial — module-level _driver_entry
    # plus picklable args (states is a dict of module-level functions;
    # pairs is a list of (str, frozen Spec)). A nested closure here
    # would fail to pickle on Windows under spawn-based multiprocessing.
    normal_target = functools.partial(_driver_entry, states, start_state,
                                      pairs, prelaunch)
    error_target = functools.partial(_driver_entry, states, error_state,
                                     pairs, prelaunch)

    if args.loop:
        run_with_watchdog(normal_target, error_loop=error_target,
                          kill_on_timeout=kill_names)
    else:
        run_once_with_watchdog(normal_target, kill_on_timeout=kill_names)


def _driver_entry(states, start_state, pairs, prelaunch):
    """Runs in the watchdog's child process. Registers every app in
    `core.window` and (by default) opens each one before the state
    machine starts.

    Seeds the popup-dismiss "expected" set from the current top-level
    HWNDs (anything already open at runner start is treated as wanted).
    """
    from core import verbs as verbs_mod
    from core import window
    verbs_mod._seed_expected_from_current()
    for name, path in pairs:
        window.register(name, path)
    if prelaunch:
        for name, _ in pairs:
            window.open(name)
    _run_states(states, start_state, SimpleNamespace())
