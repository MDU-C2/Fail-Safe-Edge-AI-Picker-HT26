"""Cup observations and selection rules. No camera, model, or robot side effects."""

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Preserve the existing Python client's XY limits. These are only a coarse filter;
# the deployed robot controller must still validate reachability and motion.
X_MIN, X_MAX = 350, 800
Y_MIN, Y_MAX = -230, 420
TRAVEL_Z = 250
GRIPPER_TCP_Z = 159
LEFT_READY = (400, 100, 450)
LEFT_DOWN = (180, 0, 180)
FLANGE_MIN = (180, -400, 50)
FLANGE_MAX = (800, 500, 600)
ACCEPTANCE_FLAGS = ("in_cup", "near_handle", "cup_height")


class SelectionError(ValueError):
    """A requested cup cannot currently be used for motion."""


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def measurement(value):
    return value if finite_number(value) else None


def inside(base, cup):
    box = cup.get("bbox_xyxy")
    return (isinstance(box, (list, tuple)) and len(box) == 4 and all(finite_number(v) for v in box)
            and finite_number(base.get("x_px"))
            and finite_number(base.get("y_px"))
            and box[0] <= base["x_px"] <= box[2]
            and box[1] <= base["y_px"] <= box[3])


def normalize_offset(tag_offset):
    """Validate all three calibration offsets, even though motion uses only XY."""
    try:
        values = tuple(tag_offset)
    except TypeError:
        values = ()
    if len(values) != 3 or not all(finite_number(value) for value in values):
        raise SelectionError("tag_offset must contain three finite numbers")
    return values


def _flange_problem(position):
    if not all(finite_number(value) for value in position):
        return "invalid corrected flange coordinates"
    if not all(low <= value <= high for value, low, high in zip(position, FLANGE_MIN, FLANGE_MAX)):
        return "corrected flange target is outside the gateway workspace"
    return None


def position_problem(cup, tag_offset=(0, 0, 0)):
    try:
        ox, oy, _ = normalize_offset(tag_offset)
    except SelectionError as error:
        return str(error)
    if not all(finite_number(cup.get(axis)) for axis in ("x", "y", "z")):
        return "missing or invalid depth/robot coordinates"
    if not X_MIN <= cup["x"] <= X_MAX or not Y_MIN <= cup["y"] <= Y_MAX:
        return "outside the configured XY workspace"
    if cup["z"] >= TRAVEL_Z:
        return "cup base is at or above the fixed hover TCP height"
    return _flange_problem((cup["x"] + ox, cup["y"] + oy, TRAVEL_Z + GRIPPER_TCP_Z))


def _hover_target(cup, tag_offset):
    ox, oy, _ = normalize_offset(tag_offset)
    return {
        "tcp_mm": {"x": cup["x"] + ox, "y": cup["y"] + oy, "z": TRAVEL_Z},
        "flange_mm": {"x": cup["x"] + ox, "y": cup["y"] + oy, "z": TRAVEL_Z + GRIPPER_TCP_Z},
        "orientation_deg": dict(zip(("rx", "ry", "rz"), LEFT_DOWN)),
        "tag_offset_applied_axes": ["x", "y"],
    }


def _duplicate_base(base, others):
    """Reject repeated detector IDs or coincident base centres, without merging cups."""
    for other in others:
        if base is other:
            continue
        same_id = base.get("detection_id") is not None and base.get("detection_id") == other.get("detection_id")
        same_point = (all(finite_number(base.get(axis)) and finite_number(other.get(axis))
                          for axis in ("x_px", "y_px"))
                      and all(base[axis] == other[axis] for axis in ("x_px", "y_px")))
        if same_id or same_point:
            return True
    # The exact same input object can also have been inserted more than once.
    return sum(other is base for other in others) > 1


def build_inventory(objects, *, capture_id, frame, calibration_id, gateway, images, model,
                    tag_offset=(0, 0, 0)):
    """Keep CUP observations and supported orphan bases, including blocked entries.

    A CUP needs one unambiguous base. An orphan base may use a literal True
    in_cup/near_handle/cup_height flag computed by mark_cup_bases. Legacy is_cup
    alone is not evidence. Duplicate detections are never selectable.
    IDs are left-to-right in this snapshot, never persistent physical identities.
    """
    offset = normalize_offset(tag_offset)
    whole = [o for o in objects if o["class"] == "CUP"]
    bases = [o for o in objects if o["class"] == "CUP BASE"]
    entries = []
    for parent in whole:
        matches = [base for base in bases if inside(base, parent)]
        reason = None
        if len(matches) == 0:
            reason = "no CUP BASE detected for this cup"
        elif len(matches) > 1:
            reason = "multiple CUP BASE detections in this cup"
        elif sum(inside(matches[0], other) for other in whole) != 1:
            reason = "CUP BASE belongs to overlapping CUP detections"
        entries.append((matches[0] if len(matches) == 1 else parent, parent, reason))
    for base in bases:
        if not any(inside(base, parent) for parent in whole):
            supported = any(base.get(flag) is True for flag in ACCEPTANCE_FLAGS)
            entries.append((base, None, None if supported else
                            "CUP BASE has no surrounding CUP detection or supported handle/height evidence"))

    entries.sort(key=lambda item: tuple(item[0].get(axis) if finite_number(item[0].get(axis))
                                       else math.inf for axis in ("x_px", "y_px")))
    cups = []
    for cup_id, (point, parent, reason) in enumerate(entries, 1):
        cup = {
            "id": cup_id,
            "confidence": measurement(point.get("confidence")),
            "x": measurement(point.get("robot_x_mm")),
            "y": measurement(point.get("robot_y_mm")),
            "z": measurement(point.get("robot_z_mm")),
            "camera_mm": {axis: measurement(point.get(axis + "_mm")) for axis in ("x", "y", "z")},
            "pixel": {"u": measurement(point.get("x_px")), "v": measurement(point.get("y_px"))},
            "bbox_xyxy": (parent or point).get("bbox_xyxy"),
            "cup_detection_id": parent.get("detection_id") if parent else None,
            "base_detection_id": point.get("detection_id") if point["class"] == "CUP BASE" else None,
        }
        evidence = [flag for flag in ACCEPTANCE_FLAGS if point.get(flag) is True]
        if parent is not None and point["class"] == "CUP BASE" and "in_cup" not in evidence:
            evidence.insert(0, "in_cup")  # Computed containment, not a truthy external flag.
        cup["acceptance_reasons"] = evidence if point["class"] == "CUP BASE" else []
        cup["robot_mm"] = {axis: cup[axis] for axis in ("x", "y", "z")}
        if point["class"] == "CUP BASE" and _duplicate_base(point, bases):
            reason = reason or "duplicate CUP BASE detection"
        if not all(finite_number(cup["pixel"][axis]) for axis in ("u", "v")):
            reason = reason or "missing or invalid pixel coordinates"
        reason = reason or position_problem(cup, offset)
        cup["hover_target"] = _hover_target(cup, offset) if reason is None else None
        cup.update(selectable=reason is None, blocked_reason=reason,
                   status="available" if reason is None else "blocked")
        cups.append(cup)
    return {
        "schema_version": 2,
        "capture": capture_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "units": "mm",
        "id_scope": "this capture only; numbered left to right in the camera image",
        "gateway": gateway,
        "calibration_sha256": calibration_id,
        "tag_offset_mm": list(offset),
        "hover_motion": {"endpoint": "/robot/rws/move", "arm": "left",
                         "ready_flange_mm": dict(zip(("x", "y", "z"), LEFT_READY)),
                         "travel_tcp_z_mm": TRAVEL_Z, "gripper_tcp_z_mm": GRIPPER_TCP_Z,
                         "tag_offset_z_applied": False},
        "frame": {key: value for key, value in frame.items() if not key.endswith("monotonic")},
        "model": model,
        "images": images,
        "cups": cups,
    }


def atomic_json(path, value):
    """Readers see either the previous complete document or the next one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def save_inventory(directory, inventory, *, latest=True):
    directory = Path(directory)
    atomic_json(directory / f"cups_{inventory['capture']}.json", inventory)
    if latest:
        atomic_json(directory / "cups.json", inventory)


def selected_cup(inventory, capture_id, cup_id):
    if inventory is None or inventory["capture"] != capture_id:
        raise SelectionError("Capture ID is not the active scan. Run scan and list again.")
    if type(cup_id) is not int or cup_id < 1:
        raise SelectionError("Cup ID must be a positive integer, e.g. goto 2.")
    matches = [cup for cup in inventory["cups"] if cup["id"] == cup_id]
    if len(matches) != 1:
        raise SelectionError(f"Cup {cup_id} is not in this scan. Use list.")
    cup = matches[0]
    reason = cup.get("blocked_reason") or position_problem(cup, inventory.get("tag_offset_mm", (0, 0, 0)))
    if not cup.get("selectable") or reason:
        raise SelectionError(f"Cup {cup_id} is blocked: {reason or 'not selectable'}.")
    return cup


def match_observation(original, fresh_cups, max_distance_mm, max_distance_px, *, tag_offset=(0, 0, 0)):
    """Conservative spatial association for a stationary scene, not object tracking."""
    candidates = []
    for cup in fresh_cups:
        if (not all(finite_number(cup.get(axis)) for axis in ("x", "y", "z"))
                or not all(finite_number(cup.get("pixel", {}).get(axis)) for axis in ("u", "v"))):
            continue
        distance = math.dist([original[a] for a in ("x", "y", "z")],
                             [cup[a] for a in ("x", "y", "z")])
        pixels = math.dist([original["pixel"][a] for a in ("u", "v")],
                           [cup["pixel"][a] for a in ("u", "v")])
        if distance <= max_distance_mm and pixels <= max_distance_px:
            candidates.append(cup)
    if len(candidates) != 1:
        raise SelectionError("Selected cup moved, disappeared, or has an ambiguous match. Run scan again.")
    cup = candidates[0]
    reason = cup.get("blocked_reason") or position_problem(cup, tag_offset)
    if not cup["selectable"] or reason:
        raise SelectionError(f"Fresh observation is blocked: {reason}.")
    return cup


def hover_moves(cup, *, tag_offset=(0, 0, 0), speed=50):
    """Two POST /robot/rws/move bodies: LEFT_READY, then above the selected cup.

    The saved camera-to-robot coordinates are unchanged. As in Arm.goto, only
    tag_offset X/Y are applied. TCP Z is fixed at 250 mm, flange Z at 409 mm;
    the recorded tag_offset Z deliberately has no effect on these moves.
    This plan neither opens/closes the gripper nor descends to pick the cup.
    """
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    reason = position_problem(cup, tag_offset) or _flange_problem(LEFT_READY)
    if reason:
        raise SelectionError(reason)
    hover = _hover_target(cup, tag_offset)["flange_mm"]
    targets = [dict(zip(("x", "y", "z"), LEFT_READY)), hover]
    return [{**target, **dict(zip(("rx", "ry", "rz"), LEFT_DOWN)),
             "arm": "left", "speed": speed, "acceleration": 100, "ramp": 100,
             "wait": True} for target in targets]
