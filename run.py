import time

from core import actions, apps, db, runner


APP_PATH = "notepad.exe"
APP_NAME = "notepad.exe"

BTN_FILE = "Notepad/MenuBar/File:MenuItem"
BTN_NEW = "Notepad/MenuBar/New:MenuItem"


def state_open(ctx):
    apps.open_app(APP_PATH)
    ctx["window"] = apps.get_window()
    return "click_file"


def state_click_file(ctx):
    actions.press(ctx["window"], BTN_FILE)
    actions.press_after_delay(ctx["window"], BTN_NEW, 0.3)
    db.log("results", "file_new_clicked", 1)
    return "close"


def state_close(ctx):
    apps.close_app(APP_NAME)
    return None


STATES = {
    "open": state_open,
    "click_file": state_click_file,
    "close": state_close,
}


def test_loop():
    while True:
        ctx = {}
        state = "open"
        while state is not None:
            state = STATES[state](ctx)
        time.sleep(2)


if __name__ == "__main__":
    runner.run_with_watchdog(test_loop)
