import argparse
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


def run_with_watchdog(test_loop, kill_on_timeout=None):
    timeout_sec = config.LOOP_TIMEOUT_MIN * 60
    while True:
        _supervise(test_loop, timeout_sec, kill_on_timeout=kill_on_timeout)


def _run_states(states, start_state, ctx):
    """State-machine driver with automatic transition logging.

    Each state function returns the next state name (or None to end).
    Entry and exit (with duration) are auto-logged to the `states`
    table. Payload-bearing logs (saved path, written value, etc.)
    still belong in the state function.
    """
    state = start_state
    while state is not None:
        fn = states[state]
        t0 = time.time()
        db.log("states", state, "entered", 0.0)
        nxt = fn(ctx)
        dur = round(time.time() - t0, 3)
        db.log("states", state, "exited", dur, nxt or "")
        state = nxt
    return ctx


def _normalize_apps(apps, app_mod):
    """Return [(name, Spec), ...] from APPS in either form:

    * list of strings/Specs — auto-name = lowercased exe stem
      ("notepad.exe" → "notepad", "ValSuitePro.exe" → "valsuitepro")
    * dict {name: path-or-Spec} — explicit names, useful when the
      auto-name collides or you want a clearer attribute on ctx
    """
    pairs = []
    if isinstance(apps, dict):
        for name, item in apps.items():
            pairs.append((name, app_mod._coerce(item)))
    else:
        for item in apps:
            spec = app_mod._coerce(item)
            stem = app_mod._exe_stem(spec.path).lower()
            stem = re.sub(r"[^a-z0-9_]", "_", stem) or "app"
            pairs.append((stem, spec))
    return pairs


def start(states, apps, start_state):
    """Entry point for user scripts.

    - Parses `--loop`.
    - Verifies every launch path in `apps` is reachable.
    - Pre-launches every app and exposes each one as `ctx.<name>` to
      every state function (auto-name = lowercased exe stem; pass
      `apps={"my_name": "path.exe", ...}` to override).
    - Drives the state machine starting at `start_state`. Each state
      function takes `ctx` and returns the next state name (or None
      to end). Entry/exit transitions are auto-logged with timing.
    - On `--loop`, respawns a fresh child process per iteration; the
      watchdog kills any iteration that exceeds `LOOP_TIMEOUT_MIN`.
      `kill_on_timeout` is auto-derived from `apps` so a hung GUI
      won't pollute the next iteration.
    """
    if start_state not in states:
        raise ValueError(
            f"start_state={start_state!r} not in STATES; "
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
    apps_mod.verify_installed([s.path for _, s in pairs])
    kill_names = [app_mod._exe_stem(s.path) + ".exe" for _, s in pairs]

    def driver():
        from core.window import Window
        ctx = SimpleNamespace()
        for name, spec in pairs:
            setattr(ctx, name, Window(app_mod.launch(spec)))
        _run_states(states, start_state, ctx)

    if args.loop:
        run_with_watchdog(driver, kill_on_timeout=kill_names)
    else:
        run_once_with_watchdog(driver, kill_on_timeout=kill_names)
