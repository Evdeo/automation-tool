"""Top-level entry point for the inspector.

Run from the project root:    python inspector.py

Click any UI element on screen and the matching tree_id (full path + leaf)
prints to the console — paste the leaf into actions.press / press_path
calls in your demo. The first click in each window auto-saves a baseline
snapshot under data/snapshots/."""

from core.inspector import run

if __name__ == "__main__":
    run()
