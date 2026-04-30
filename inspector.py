"""Click-to-capture inspector.

    python inspector.py                 # locks on first click
    python inspector.py notepad.exe     # pre-binds to a process

Clicks outside the locked process are silently ignored. Consecutive
clicks on the same control are deduped. Output is also written to
data/inspector.txt.
"""
import argparse

from core.inspector import run


def _parse():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("process", nargs="?", help="Optional exe name to pre-bind")
    return p.parse_args()


if __name__ == "__main__":
    run(scope=_parse().process)
