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
FLANGE_MAX = (800, 500, 700)
Z_MIN = 199  # Minimum ordinary flange target, not a path or gripper-clearance guarantee.
HANDLE_MAX_DIST = 100
GRIP_MARGIN = 70
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


def _motion_flange_problem(position, *, pick=False):
    """Check endpoint limits, with the lower ordinary-motion flange bound."""
    problem = _flange_problem(position)
    if problem:
        return problem
    if position[2] < Z_MIN and not pick:
        return f"flange target is below the ordinary motion clearance of {Z_MIN} mm"
    return None


def position_problem(cup, tag_offset=(0, 0, 0)):
    try:
        normalize_offset(tag_offset)
    except SelectionError as error:
        return str(error)
    if not all(finite_number(cup.get(axis)) for axis in ("x", "y", "z")):
        return "missing or invalid depth/robot coordinates"
    if not X_MIN <= cup["x"] <= X_MAX or not Y_MIN <= cup["y"] <= Y_MAX:
        return "outside the configured XY workspace"
    return None


def _hover_target(cup, tag_offset):
    ox, oy, _ = normalize_offset(tag_offset)
    return {
        "tcp_mm": {"x": cup["x"] + ox, "y": cup["y"] + oy, "z": TRAVEL_Z},
        "flange_mm": {"x": cup["x"] + ox, "y": cup["y"] + oy, "z": TRAVEL_Z + GRIPPER_TCP_Z},
        "orientation_deg": dict(zip(("rx", "ry", "rz"), LEFT_DOWN)),
        "tag_offset_applied_axes": ["x", "y"],
    }


def set_grip_geometry(cups, objects):
    """Use V4's nearest-cup/nearest-handle rule within this observation.

    Input cups retain confidence order until geometry is assigned, so equal
    distances choose the same first candidate as V4. UI numbering happens later.
    """
    candidates = [cup for cup in cups if all(finite_number(cup.get(axis)) for axis in ("x", "y"))]
    closest = {}
    for obj in objects:
        if obj.get("class") != "CUP HANDLE" or not candidates:
            continue
        if not all(finite_number(obj.get("robot_" + axis + "_mm")) for axis in ("x", "y")):
            continue
        hx, hy = obj["robot_x_mm"], obj["robot_y_mm"]
        distance, cup = min(((math.hypot(hx - cup["x"], hy - cup["y"]), cup)
                             for cup in candidates), key=lambda item: item[0])
        if distance <= HANDLE_MAX_DIST and distance < closest.get(cup["id"], (math.inf,))[0]:
            closest[cup["id"]] = (distance, hx, hy)
    for cup in cups:
        cup.update(rz=LEFT_DOWN[2], has_handle=cup["id"] in closest, grip_geometry_error=None)
        if cup["has_handle"]:
            _, hx, hy = closest[cup["id"]]
            angle = math.degrees(math.atan2(hy - cup["y"], hx - cup["x"]))
            cup["rz"] = round(LEFT_DOWN[2] + angle % 180 - 90, 1)


def build_inventory(objects, *, capture_id, frame, calibration_id, gateway, images, model,
                    tag_offset=(0, 0, 0)):
    """One supported CUP BASE is one cup, as in V4's main picker.

    Parent CUPs and handles/height supply acceptance evidence, not a uniqueness
    requirement. IDs remain left-to-right for selecting cups in the UI.
    """
    offset = normalize_offset(tag_offset)
    whole = [o for o in objects if o["class"] == "CUP"]
    bases = [o for o in objects if o["class"] == "CUP BASE"]
    entries = []
    for base in sorted(bases, key=lambda point: measurement(point.get("confidence")) or 0, reverse=True):
        parent = next((cup for cup in whole if inside(base, cup)), None)
        supported = ((parent is not None and "in_cup" not in base)
                     or any(base.get(flag) is True for flag in ACCEPTANCE_FLAGS))
        if supported:
            entries.append((base, parent, None))
    cups = []
    for cup_id, (point, parent, reason) in enumerate(entries, 1):
        diameter = point.get("diameter_mm") if point["class"] == "CUP BASE" else None
        diameter = float(diameter) if finite_number(diameter) and diameter > 0 else None
        z_frames = point.get("robot_z_frames_mm", [])
        z_frames = ([measurement(value) for value in z_frames]
                    if isinstance(z_frames, (list, tuple)) else [])
        cup = {
            "id": cup_id,
            "confidence": measurement(point.get("confidence")),
            "x": measurement(point.get("robot_x_mm")),
            "y": measurement(point.get("robot_y_mm")),
            "z": measurement(point.get("robot_z_mm")),
            "z_frames": z_frames,
            "camera_mm": {axis: measurement(point.get(axis + "_mm")) for axis in ("x", "y", "z")},
            "pixel": {"u": measurement(point.get("x_px")), "v": measurement(point.get("y_px"))},
            "bbox_xyxy": (parent or point).get("bbox_xyxy"),
            "cup_detection_id": parent.get("detection_id") if parent else None,
            "base_detection_id": point.get("detection_id") if point["class"] == "CUP BASE" else None,
            "diameter": diameter,
            "aperture": round(diameter + GRIP_MARGIN, 1) if diameter is not None else None,
        }
        evidence = [flag for flag in ACCEPTANCE_FLAGS if point.get(flag) is True]
        if parent is not None and "in_cup" not in point:
            evidence.insert(0, "in_cup")  # Computed containment, not a truthy external flag.
        cup["acceptance_reasons"] = evidence if point["class"] == "CUP BASE" else []
        cup["robot_mm"] = {axis: cup[axis] for axis in ("x", "y", "z")}
        if point["class"] == "CUP BASE" and isinstance(point.get("depth_quality"), dict):
            cup["depth_quality"] = dict(point["depth_quality"])
        if not all(finite_number(cup["pixel"][axis]) for axis in ("u", "v")):
            reason = reason or "missing or invalid pixel coordinates"
        reason = reason or position_problem(cup, offset)
        cup["hover_target"] = _hover_target(cup, offset) if reason is None else None
        cup.update(selectable=reason is None, blocked_reason=reason,
                   status="available" if reason is None else "blocked")
        cups.append(cup)
    set_grip_geometry(cups, objects)
    cups.sort(key=lambda cup: tuple(cup["pixel"][axis] if finite_number(cup["pixel"][axis])
                                   else math.inf for axis in ("u", "v")))
    for cup_id, cup in enumerate(cups, 1):
        cup["id"] = cup_id
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
