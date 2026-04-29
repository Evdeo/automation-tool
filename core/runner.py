import multiprocessing as mp
import time

import config
from core import db


def _child(target):
    target()


def _supervise(target, timeout_sec):
    """Run `target()` in a child process, killing it if it exceeds
    `timeout_sec`. Returns ("exited", exitcode) on normal completion or
    ("killed_timeout", None) if the watchdog had to terminate it.

    All three watchdog log rows share the same shape so the schema-less
    db.log table holds: ts | pid | event | exitcode | elapsed_sec.
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
        return ("killed_timeout", None)
    db.log("watchdog", proc.pid, "exited", proc.exitcode, elapsed)
    return ("exited", proc.exitcode)


def run_once_with_watchdog(target, timeout_min=None):
    """Run `target()` once in a child process with a hard timeout. The child
    is killed if it doesn't return within `timeout_min` minutes
    (default: `config.LOOP_TIMEOUT_MIN`).  Use this when you want a single
    pass with a safety net — the parent exits after the child finishes
    or is killed."""
    if timeout_min is None:
        timeout_min = config.LOOP_TIMEOUT_MIN
    return _supervise(target, timeout_min * 60)


def run_with_watchdog(test_loop):
    """Forever-loop variant: respawns `test_loop` after every exit (whether
    normal or timeout-killed).  Use for unattended monitoring runs that
    should self-recover from hangs.  For one-shot demos use
    `run_once_with_watchdog` instead."""
    timeout_sec = config.LOOP_TIMEOUT_MIN * 60
    while True:
        _supervise(test_loop, timeout_sec)
