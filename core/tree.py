import json
import re
from pathlib import Path

import uiautomation as auto

import config


_SEP = "/"
_ROLE_SEP = ":"
_STRUCT_RE = re.compile(r"\d+(\.\d+)*$")


def _is_struct_id(s):
    """True iff `s` is a structural path: dotted 0-indexed integers,
    e.g. "0", "0.1", "0.1.2.1". The format dispatcher uses this to
    route find/find_or_heal between name-based and struct-based logic."""
    return bool(s) and bool(_STRUCT_RE.fullmatch(s))


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


def _node(ctrl, parent_path, parent_struct, sibling_index):
    seg = _segment(ctrl, sibling_index)
    path = f"{parent_path}{_SEP}{seg}" if parent_path else seg
    struct = (
        f"{parent_struct}.{sibling_index}" if parent_struct is not None else "0"
    )
    rect = ctrl.BoundingRectangle
    return {
        "tree_id": path,
        "struct_id": struct,
        "name": _name(ctrl),
        "role": _role(ctrl),
        "bbox": [rect.left, rect.top, rect.right, rect.bottom],
        "enabled": bool(ctrl.IsEnabled),
        "ctrl": ctrl,
    }


def _walk(ctrl, parent_path, parent_struct, out):
    for idx, child in enumerate(ctrl.GetChildren()):
        node = _node(child, parent_path, parent_struct, idx)
        out.append(node)
        _walk(child, node["tree_id"], node["struct_id"], out)


def walk_live(window):
    out = [_node(window, None, None, 0)]
    _walk(window, out[0]["tree_id"], out[0]["struct_id"], out)
    return out


def to_serializable(walked):
    return [{k: v for k, v in n.items() if k != "ctrl"} for n in walked]


def _process_name(pid):
    """Return the executable filename (e.g. "ValSuitePro.exe") for
    `pid`, or "" on any failure. Split out so tests can patch it
    without involving psutil."""
    try:
        import psutil
        return psutil.Process(pid).name() or ""
    except Exception:
        return ""


def _process_stem(window):
    """Return the executable stem for the process owning `window`
    (e.g., "ValSuitePro" for "ValSuitePro.exe"), or "" on failure.

    Process name is the most stable identifier for a window across
    runs — far more so than the live title (which may embed serial
    numbers, run counters, or document names) or the ClassName
    (which on WPF/WinForms is salted with a per-launch hash).
    """
    try:
        pid = window.ProcessId
    except AttributeError:
        return ""
    if not pid:
        return ""
    name = _process_name(pid)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return _safe(name)


def snapshot_key(window):
    """Auto-derived, stable filename stem for `window`'s snapshot.

    Keys off the owning process's executable name when available
    (e.g., a click anywhere in `ValSuitePro.exe` produces
    `ValSuitePro_WindowControl`), so snapshots survive volatile
    title fragments — instrument serials, run counters, document
    filenames — without any manual configuration.

    Falls back to the live `window.Name` only when the process
    can't be resolved (no ProcessId, psutil unavailable, etc.).
    """
    name = _process_stem(window) or _name(window)
    role = _role(window)
    base = name if name else "#0"
    seg = f"{base}{_ROLE_SEP}{role}"
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


def _derive_struct_ids(walked):
    """Mutate `walked` in place to add `struct_id` to every node that lacks
    one. The list is in walk order (depth-first), so we re-derive by
    counting siblings per parent tree_id as we iterate. Used by
    `load_snapshot` to upgrade snapshots saved before the struct_id
    addition."""
    counter_per_parent = {}
    struct_per_id = {}
    for n in walked:
        if "struct_id" in n and n["struct_id"]:
            struct_per_id[n["tree_id"]] = n["struct_id"]
            continue
        tid = n["tree_id"]
        sep_idx = tid.rfind(_SEP)
        if sep_idx == -1:
            n["struct_id"] = "0"
        else:
            parent_tid = tid[:sep_idx]
            idx = counter_per_parent.get(parent_tid, 0)
            counter_per_parent[parent_tid] = idx + 1
            parent_struct = struct_per_id.get(parent_tid, "0")
            n["struct_id"] = f"{parent_struct}.{idx}"
        struct_per_id[tid] = n["struct_id"]
    return walked


def load_snapshot(window):
    p = snapshot_path(window)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return _derive_struct_ids(data)


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


def _find_struct(walked, struct_id):
    """Exact match on `struct_id`. No fallback — healing is in
    `find_or_heal` so the two split mirrors the name-based pair."""
    for n in walked:
        if n.get("struct_id") == struct_id:
            return n["ctrl"]
    return None


def find(walked, tree_id):
    """Resolve `tree_id` to a live control in `walked`. Dispatches by
    format:

    * **Structural id** (dotted 0-indexed digits, e.g. ``"0.1.2.1"``) —
      exact `struct_id` match. Names are not consulted; this is the
      mode for apps whose controls have no useful Name / AutomationId.
    * **Name-based path** (everything else, e.g.
      ``"Save:ButtonControl"`` or ``"Window:.../Save:ButtonControl"``) —
      tries three tiers in order:

         1. Exact full-path match — what the inspector emits.
         2. Suffix match — the supplied id is the tail of a longer path.
            Useful when controls have no unique name and you have to
            include parent context to disambiguate, e.g.
            "Toolbar:ToolBarControl/#3:ButtonControl".
         3. Leaf name+role fallback — `Save:ButtonControl` matches the
            first node where name=="Save" and role=="ButtonControl".
            Disabled for `#idx:Role` leaves (those are positional and
            every Nth child would otherwise match).

    Self-healing across drift is in `find_or_heal`, not `find`.
    """
    if _is_struct_id(tree_id):
        return _find_struct(walked, tree_id)
    # 1. exact
    for n in walked:
        if n["tree_id"] == tree_id:
            return n["ctrl"]
    # 2. suffix (only meaningful if the input has a separator — otherwise
    # this is the same as the leaf strategy below)
    if _SEP in tree_id:
        for n in walked:
            tid = n["tree_id"]
            if tid.endswith(tree_id) and (
                len(tid) == len(tree_id) or tid[-len(tree_id) - 1] == _SEP
            ):
                return n["ctrl"]
    # 3. leaf name+role
    leaf = tree_id.split(_SEP)[-1]
    leaf_name, _, leaf_role = leaf.partition(_ROLE_SEP)
    if leaf_name and not leaf_name.startswith("#"):
        for n in walked:
            if n["name"] == leaf_name and n["role"] == leaf_role:
                return n["ctrl"]
    return None


def _bbox_shape(bbox):
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def _shape_distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _live_children_of_struct(walked, parent_struct):
    prefix = parent_struct + "."
    out = []
    for n in walked:
        sid = n.get("struct_id", "")
        if not sid.startswith(prefix):
            continue
        # Only DIRECT children (one more dot than parent).
        if sid.count(".") != parent_struct.count(".") + 1:
            continue
        out.append(n)
    return out


def _snap_lookup_struct(snap, struct_id):
    return next((n for n in snap if n.get("struct_id") == struct_id), None)


def _heal_struct(walked, target_struct, snap):
    """Re-anchor a missing structural path by walking up the snapshot
    until an ancestor still resolves in the live tree, then descending
    by role + bbox-shape correlation. Returns (ctrl, True) on success,
    (None, False) on total miss.
    """
    target = _snap_lookup_struct(snap, target_struct)
    if target is None:
        return None, False
    target_role = target["role"]
    target_shape = _bbox_shape(target["bbox"])

    parts = target_struct.split(".")
    # Try the deepest still-anchored ancestor first; loop back to
    # shallower anchors if descent fails at any level.
    for anchor_depth in range(len(parts) - 1, 0, -1):
        anchor_struct = ".".join(parts[:anchor_depth])
        snap_anchor = _snap_lookup_struct(snap, anchor_struct)
        if snap_anchor is None:
            continue
        live_anchor = next(
            (n for n in walked
             if n.get("struct_id") == anchor_struct
             and n["role"] == snap_anchor["role"]),
            None,
        )
        if live_anchor is None:
            continue

        # Descend the snap path one segment at a time. At each step,
        # find live children whose role matches the snap's expected
        # child role; tie-break by bbox shape, then sibling index.
        current_live_struct = anchor_struct
        descent_failed = False
        for d in range(anchor_depth, len(parts)):
            child_snap_struct = ".".join(parts[: d + 1])
            child_snap = _snap_lookup_struct(snap, child_snap_struct)
            if child_snap is None:
                descent_failed = True
                break
            expected_role = child_snap["role"]
            expected_shape = _bbox_shape(child_snap["bbox"])
            expected_idx = int(parts[d])

            candidates = [
                n for n in _live_children_of_struct(walked, current_live_struct)
                if n["role"] == expected_role
            ]
            if not candidates:
                descent_failed = True
                break

            # Tie-break: shape-distance ascending, then |index - expected|.
            scored = [
                (_shape_distance(_bbox_shape(c["bbox"]), expected_shape),
                 abs(int(c["struct_id"].rsplit(".", 1)[-1]) - expected_idx),
                 c)
                for c in candidates
            ]
            scored.sort(key=lambda t: (t[0], t[1]))
            chosen = scored[0][2]
            current_live_struct = chosen["struct_id"]

        if descent_failed:
            continue
        # Found a leaf. Cross-check role matches the target's role.
        leaf_node = next(
            (n for n in walked if n.get("struct_id") == current_live_struct),
            None,
        )
        if leaf_node is None or leaf_node["role"] != target_role:
            continue
        return leaf_node["ctrl"], True
    return None, False


def find_or_heal(walked, tree_id, snap):
    """Resolve `tree_id` with snapshot-driven self-healing.

    Dispatches by format:

    * **Structural id** (``"0.1.2.1"``): exact `struct_id` first; on
      miss, hands off to `_heal_struct` which walks up the snapshot
      path to find a still-anchored ancestor and descends correlating
      by role + bbox shape. Names are never consulted — works for
      apps whose controls have no useful Name.
    * **Name-based path**: existing logic — `find` first, then re-anchor
      by walking up the snapshot's path until a live ancestor is
      found by name, then searching its descendants for a name+role
      match.

    Returns `(ctrl, healed)`:
      * `(ctrl, False)` when `find` matched directly,
      * `(ctrl, True)` when the heal path produced the result,
      * `(None, False)` on total miss (or when `snap` is empty /
        target metadata is missing).
    """
    if _is_struct_id(tree_id):
        direct = _find_struct(walked, tree_id)
        if direct is not None:
            # Trust an exact struct_id match only if the live node's role
            # matches what the snapshot recorded at this path. Otherwise
            # the tree has drifted (sibling inserted, parent reorganised)
            # and we must heal.
            if snap:
                snap_target = _snap_lookup_struct(snap, tree_id)
                live_node = next(
                    (n for n in walked if n.get("struct_id") == tree_id),
                    None,
                )
                if (snap_target is not None and live_node is not None
                        and live_node["role"] != snap_target["role"]):
                    # role mismatch → fall through to heal
                    pass
                else:
                    return direct, False
            else:
                return direct, False
        if not snap:
            return None, False
        return _heal_struct(walked, tree_id, snap)

    direct = find(walked, tree_id)
    if direct is not None:
        return direct, False
    if not snap:
        return None, False

    # Locate the target in the snapshot — by exact path first, then
    # by leaf name+role if the user passed a leaf-only id.
    target = next((n for n in snap if n["tree_id"] == tree_id), None)
    if target is None:
        leaf = tree_id.split(_SEP)[-1]
        leaf_name, _, leaf_role = leaf.partition(_ROLE_SEP)
        if leaf_name and not leaf_name.startswith("#"):
            target = next(
                (n for n in snap
                 if n["name"] == leaf_name and n["role"] == leaf_role),
                None,
            )
    if target is None:
        return None, False

    target_name = target["name"]
    target_role = target["role"]
    if not target_name or target_name.startswith("#"):
        # Anonymous — no name to anchor on. Could heal by sibling
        # position, but that's another flaky tier; skip for now.
        return None, False

    # Walk the snapshot path upward, looking for the deepest still-live
    # ancestor. For each candidate, search its live descendants for a
    # name+role match against the target.
    parts = target["tree_id"].split(_SEP)
    for i in range(len(parts) - 1, 0, -1):
        ancestor_path = _SEP.join(parts[:i])
        ancestor_ctrl = find(walked, ancestor_path)
        if ancestor_ctrl is None:
            continue
        # Locate the live tree_id corresponding to that ctrl (the path
        # may differ from `ancestor_path` if find() suffix-matched).
        anchor_node = next(
            (n for n in walked if n["ctrl"] is ancestor_ctrl),
            None,
        )
        if anchor_node is None:
            continue
        anchor_path = anchor_node["tree_id"]
        prefix = anchor_path + _SEP
        for n in walked:
            tid = n["tree_id"]
            if tid == anchor_path or not tid.startswith(prefix):
                continue
            if n["name"] == target_name and n["role"] == target_role:
                return n["ctrl"], True
    return None, False


if __name__ == "__main__":
    import sys
    from core import apps
    title = sys.argv[1] if len(sys.argv) > 1 else config.TARGET_WINDOW_TITLE
    win = apps.get_window(title)
    save_snapshot(win)
    print(f"Snapshot saved: {snapshot_path(win)}")
