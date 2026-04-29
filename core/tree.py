import json
import re
from pathlib import Path

import uiautomation as auto

import config


_SEP = "/"
_ROLE_SEP = ":"


def _safe(text):
    if text is None:
        return ""
    return str(text).replace(_SEP, "_").replace(_ROLE_SEP, "_").strip()


def _role(ctrl):
    return _safe(ctrl.ControlTypeName or "Unknown")


def _name(ctrl):
    n = ctrl.Name
    if n:
        return _safe(n)
    n = ctrl.AutomationId
    if n:
        return _safe(n)
    n = ctrl.ClassName
    if n:
        return _safe(n)
    return ""


def _segment(ctrl, sibling_index):
    name = _name(ctrl)
    role = _role(ctrl)
    base = name if name else f"#{sibling_index}"
    return f"{base}{_ROLE_SEP}{role}"


def _node(ctrl, parent_path, sibling_index):
    seg = _segment(ctrl, sibling_index)
    path = f"{parent_path}{_SEP}{seg}" if parent_path else seg
    rect = ctrl.BoundingRectangle
    return {
        "tree_id": path,
        "name": _name(ctrl),
        "role": _role(ctrl),
        "bbox": [rect.left, rect.top, rect.right, rect.bottom],
        "enabled": bool(ctrl.IsEnabled),
        "ctrl": ctrl,
    }


def _walk(ctrl, parent_path, out):
    for idx, child in enumerate(ctrl.GetChildren()):
        node = _node(child, parent_path, idx)
        out.append(node)
        _walk(child, node["tree_id"], out)


def walk_live(window):
    out = [_node(window, None, 0)]
    _walk(window, out[0]["tree_id"], out)
    return out


def to_serializable(walked):
    return [{k: v for k, v in n.items() if k != "ctrl"} for n in walked]


def snapshot_key(window):
    seg = _segment(window, 0)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", seg)


def snapshot_path(window):
    return Path(config.TREE_SNAPSHOT_DIR) / f"{snapshot_key(window)}.json"


def save_snapshot(window, walked=None):
    if walked is None:
        walked = walk_live(window)
    data = to_serializable(walked)
    p = snapshot_path(window)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return data


def load_snapshot(window):
    p = snapshot_path(window)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def ensure_snapshot(window, walked=None):
    snap = load_snapshot(window)
    if snap is not None:
        return snap, False
    return save_snapshot(window, walked), True


def compute_diff(snap, live):
    snap_ids = {n["tree_id"] for n in snap}
    live_ids = {n["tree_id"] for n in live}
    return {
        "added": sorted(live_ids - snap_ids),
        "removed": sorted(snap_ids - live_ids),
    }


def find(walked, tree_id):
    for n in walked:
        if n["tree_id"] == tree_id:
            return n["ctrl"]
    leaf = tree_id.split(_SEP)[-1]
    leaf_name, _, leaf_role = leaf.partition(_ROLE_SEP)
    if leaf_name and not leaf_name.startswith("#"):
        for n in walked:
            if n["name"] == leaf_name and n["role"] == leaf_role:
                return n["ctrl"]
    return None


if __name__ == "__main__":
    import sys
    from core import apps
    title = sys.argv[1] if len(sys.argv) > 1 else config.TARGET_WINDOW_TITLE
    win = apps.get_window(title)
    save_snapshot(win)
    print(f"Snapshot saved: {snapshot_path(win)}")
