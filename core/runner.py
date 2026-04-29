import multiprocessing as mp
import time

import config
from core import db


def _child(target):
    target()


def run_with_watchdog(test_loop):
    timeout_sec = config.LOOP_TIMEOUT_MIN * 60
    while True:
        proc = mp.Process(target=_child, args=(test_loop,), daemon=False)
        started = time.time()
        proc.start()
        db.log("watchdog", proc.pid, "started")
        proc.join(timeout=timeout_sec)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join()
            db.log("watchdog", proc.pid, "killed_timeout", round(time.time() - started, 1))
        else:
            db.log("watchdog", proc.pid, "exited", proc.exitcode, round(time.time() - started, 1))
