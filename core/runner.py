import multiprocessing as mp
import time

import config
from core import db


def _child(target):
    target()


def _kill_orphans(parent_pid, names, elapsed):
    """After a watchdog kill, terminate any process whose name matches one
    of `names` (case-insensitive substring match, same rule as
    `apps.close_app`). Each kill writes a `closed_<name>` row into the
    watchdog table so the recovery path is auditable."""
    if not names:
        return
    # Lazy import — avoids a circular dep at module load.
    from core import apps
    for name in names:
        try:
            killed = apps.close_app(name)
        except Exception as e:
            db.log("watchdog", parent_pid, f"close_failed:{name}", -1, elapsed)
            continue
        db.log("watchdog", parent_pid, f"closed:{name}", killed, elapsed)


def _supervise(target, timeout_sec, kill_on_timeout=None):
    """Run `target()` in a child process, killing it if it exceeds
    `timeout_sec`. Returns ("exited", exitcode) on normal completion or
    ("killed_timeout", None) if the watchdog had to terminate it.

    `kill_on_timeout` is an optional iterable of process names that the
    watchdog will also terminate (via `apps.close_app`) after killing
    the python child — use it to clean up orphan GUI apps the child
    was driving (e.g. Notepad) so the next iteration starts clean.

    All watchdog log rows share the same shape so the schema-less db.log
    table holds: ts | pid | event | exitcode_or_count | elapsed_sec.
    """
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
    """Run `target()` once in a child process with a hard timeout. The child
    is killed if it doesn't return within `timeout_min` minutes
    (default: `config.LOOP_TIMEOUT_MIN`).  Use this when you want a single
    pass with a safety net — the parent exits after the child finishes
    or is killed.

    `kill_on_timeout`: list of process names to terminate after killing
    the child (only on timeout, not on clean exit). Useful for cleaning
    up GUI apps the child was driving."""
    if timeout_min is None:
        timeout_min = config.LOOP_TIMEOUT_MIN
    return _supervise(target, timeout_min * 60, kill_on_timeout=kill_on_timeout)


def run_with_watchdog(test_loop, kill_on_timeout=None):
    """Forever-loop variant: respawns `test_loop` after every exit (whether
    normal or timeout-killed).  Use for unattended monitoring runs that
    should self-recover from hangs.  For one-shot demos use
    `run_once_with_watchdog` instead.

    `kill_on_timeout`: list of process names to terminate between
    iterations whenever a child has to be killed for timeout — keeps
    the next iteration from inheriting a polluted GUI state."""
    timeout_sec = config.LOOP_TIMEOUT_MIN * 60
    while True:
        _supervise(test_loop, timeout_sec, kill_on_timeout=kill_on_timeout)
