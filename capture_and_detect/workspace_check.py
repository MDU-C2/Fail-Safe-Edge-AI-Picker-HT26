"""Pure comparison and refresh of source cups between camera observations.

No robot movement, grid lookup, or reservation changes happen here. The strict
compatibility check permits only reviewed sources. The dynamic refresh rebuilds
current candidates while retaining IDs only for unambiguous nearby matches.
"""

from copy import deepcopy
import math

from cup_inventory import SelectionError, finite_number, normalize_offset, position_problem


def _changed(reason):
    return SelectionError(f"Workspace changed: {reason}. Run scan again.")


def _completed(cup):
    execution = cup.get("execution")
    return (cup.get("status") in ("placed", "replaced")
            or isinstance(execution, dict) and execution.get("state") == "completed"
            and execution.get("action") == "goto")


def _sources(inventory):
    cups = inventory.get("cups")
    if not isinstance(cups, list) or any(not isinstance(cup, dict) for cup in cups):
        raise _changed("source-cup observations are invalid")
    # Capture has already classified the source area. Do not consult grid
    # geometry or occupancy, even if placement bookkeeping is present/invalid.
    return [cup for cup in cups if not cup.get("placement_location_ids")]


def _ids(cups):
    identifiers = [cup.get("id") for cup in cups]
    if (any(type(identifier) is not int or identifier < 1 for identifier in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise _changed("source-cup identities are ambiguous")
    return {cup["id"]: cup for cup in cups}


def _pixel(cup):
    value = cup.get("pixel")
    if isinstance(value, dict) and all(finite_number(value.get(axis)) for axis in ("u", "v")):
        return tuple(value[axis] for axis in ("u", "v"))
    return None


def _xyz(cup):
    if all(finite_number(cup.get(axis)) for axis in ("x", "y", "z")):
        return tuple(cup[axis] for axis in ("x", "y", "z"))
    return None


def _eligible(cup, offset):
    return (cup.get("selectable") is True and not cup.get("blocked_reason")
            and not position_problem(cup, offset) and _pixel(cup) is not None)


def _near(original, observed, match_mm, match_px):
    """Include partial observations as ambiguity/blocking evidence.

    Missing depth must not make a second nearby blocked detection disappear
    from association while a neighboring valid detection silently substitutes.
    An observation still needs at least one usable coordinate system.
    """
    distances = []
    for read, tolerance in ((_xyz, match_mm), (_pixel, match_px)):
        before, after = read(original), read(observed)
        if before is not None and after is not None:
            distances.append(math.dist(before, after) <= tolerance)
    return bool(distances) and all(distances)


def _metadata(inventory, calibration_id, offset):
    if not isinstance(inventory, dict):
        raise _changed("a camera observation is missing")
    if inventory.get("calibration_sha256") != calibration_id:
        raise _changed("camera calibration no longer matches this session")
    try:
        observed_offset = normalize_offset(inventory.get("tag_offset_mm", (0, 0, 0)))
    except SelectionError as error:
        raise _changed("camera calibration offsets are invalid") from error
    if observed_offset != offset:
        raise _changed("camera calibration offsets no longer match this session")
    capture = inventory.get("capture")
    if not isinstance(capture, str) or not capture.strip():
        raise _changed("the camera capture identity is missing")
    frame = inventory.get("frame")
    if (not isinstance(frame, dict) or type(frame.get("sequence")) is not int
            or frame["sequence"] < 0):
        raise _changed("the camera frame identity is invalid")
    gateway = inventory.get("gateway")
    if not isinstance(gateway, str) or not gateway.strip():
        raise _changed("the camera gateway identity is missing")
    return gateway.rstrip("/")


def check_workspace(previous_inventory, fresh_inventory, *, calibration_id,
                    tag_offset=(0, 0, 0), match_mm=20, match_px=40, baseline_inventory=None):
    """Match remaining approved sources or raise a descriptive SelectionError.

    ``previous_inventory`` supplies current completion state and the latest
    successful frame identity. ``baseline_inventory``, when supplied, must be
    the immutable operator-reviewed scan. Every match must stay within both
    the latest observation and that baseline, preventing gradual drift from
    silently relocating an approved cup. Without it, previous is the baseline.

    Fresh frame sequences must strictly advance; a camera restart requires a
    new reviewed scan. Gateway timestamps are not compared to the host clock.
    Completed cups may disappear. Placement-tagged, initially blocked, and
    out-of-workspace cups are not required to persist. Any additional eligible
    source (including a formerly blocked cup) requires a new scan.

    Neither input is modified. ``matches`` maps stable original IDs to copied
    fresh observations, whose own ``id`` remains the fresh detection ID. The
    caller may update display/pick geometry while retaining stable IDs and the
    separate immutable baseline. No new cup is authorized by this result.
    """
    for name, value in (("match_mm", match_mm), ("match_px", match_px)):
        if not finite_number(value) or value <= 0:
            raise SelectionError(f"{name} must be a positive finite number")
    offset = normalize_offset(tag_offset)
    if not isinstance(calibration_id, str) or not calibration_id:
        raise _changed("the session camera calibration identity is missing")
    baseline = previous_inventory if baseline_inventory is None else baseline_inventory
    gateways = [_metadata(value, calibration_id, offset)
                for value in (previous_inventory, fresh_inventory, baseline)]
    if len(set(gateways)) != 1:
        raise _changed("the camera gateway no longer matches the reviewed scan")
    if fresh_inventory["capture"] in (previous_inventory["capture"], baseline["capture"]):
        raise _changed("the camera repeated a previous capture")
    if fresh_inventory["frame"]["sequence"] <= previous_inventory["frame"]["sequence"]:
        raise _changed("the camera frame did not advance after the previous observation")

    current = _ids(_sources(previous_inventory))
    original_sources = _ids(_sources(baseline))
    fresh_sources = _sources(fresh_inventory)
    _ids(fresh_sources)
    approved = {cup_id: cup for cup_id, cup in original_sources.items()
                if _eligible(cup, offset) and not _completed(cup)}
    remaining = {}
    for cup_id, original in approved.items():
        latest = current.get(cup_id)
        if latest is None:
            raise _changed(f"reviewed source cup {cup_id} is missing from the active scan")
        if _completed(latest):
            continue
        if not _eligible(latest, offset):
            raise _changed(f"source cup {cup_id} is blocked in the active scan")
        remaining[cup_id] = original
    if any(_eligible(cup, offset) and not _completed(cup) and cup_id not in approved
           for cup_id, cup in current.items()):
        raise _changed("the active scan contains an unreviewed source cup")

    matches = {}
    used_fresh = set()
    latest_remaining = [current[cup_id] for cup_id in remaining]
    for cup_id, original in remaining.items():
        candidates = [cup for cup in fresh_sources if _near(original, cup, match_mm, match_px)]
        if not candidates:
            raise _changed(f"source cup {cup_id} moved, disappeared, or became blocked")
        if len(candidates) != 1:
            raise _changed(f"source cup {cup_id} has an ambiguous match")
        matched = candidates[0]
        if not _eligible(matched, offset):
            raise _changed(f"source cup {cup_id} became blocked or lost usable coordinates")
        if not _near(current[cup_id], matched, match_mm, match_px):
            raise _changed(f"source cup {cup_id} moved since the previous observation")
        reverse_original = [cup for cup in remaining.values() if _near(cup, matched, match_mm, match_px)]
        reverse_latest = [cup for cup in latest_remaining if _near(cup, matched, match_mm, match_px)]
        if (len(reverse_original) != 1 or len(reverse_latest) != 1
                or matched["id"] in used_fresh):
            raise _changed(f"source cup {cup_id} has an ambiguous match")
        used_fresh.add(matched["id"])
        matches[cup_id] = deepcopy(matched)

    if any(_eligible(cup, offset) and cup["id"] not in used_fresh for cup in fresh_sources):
        raise _changed("an additional source cup is visible")
    return {"matches": matches, "capture": fresh_inventory["capture"],
            "frame": deepcopy(fresh_inventory["frame"]),
            "matched_cup_ids": list(matches), "matched_count": len(matches)}


def _overlapping_boxes(first, second):
    """True for substantially shared cup regions, not merely touching boxes."""
    boxes = [cup.get("bbox_xyxy") for cup in (first, second)]
    if any(not isinstance(box, (list, tuple)) or len(box) != 4
           or not all(finite_number(value) for value in box)
           or box[2] <= box[0] or box[3] <= box[1] for box in boxes):
        return False
    a, b = boxes
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return overlap >= 0.65 * min((a[2] - a[0]) * (a[3] - a[1]),
                                (b[2] - b[0]) * (b[3] - b[1]))


def _fresh_conflicts(cups, match_mm, match_px):
    """Return detection indexes whose independent pick targets are ambiguous."""
    conflicts = set()
    for index, cup in enumerate(cups):
        identifier = cup.get("id")
        if type(identifier) is not int or identifier < 1:
            conflicts.add(index)
        for other_index in range(index):
            other = cups[other_index]
            same_id = identifier == other.get("id")
            parent = cup.get("cup_detection_id")
            shared_parent = parent is not None and parent == other.get("cup_detection_id")
            if (same_id or shared_parent or _near(cup, other, match_mm, match_px)
                    or _overlapping_boxes(cup, other)):
                conflicts.update((index, other_index))
    return conflicts


def refresh_workspace(previous_inventory, fresh_inventory, *, calibration_id,
                      tag_offset=(0, 0, 0), match_mm=20, match_px=40):
    """Rebuild current source candidates without assuming a stationary scene.

    Moved, new and removed sources update the inventory instead of failing the
    check. A stable ID survives only a unique two-way nearby match between
    eligible old and fresh sources. Ambiguous history associations receive new
    IDs; they do not imply ambiguous fresh geometry. Conflicting fresh targets
    (nearby coordinate candidates, shared CUP detections, substantially shared
    boxes, or duplicate observation IDs) are blocked individually. Other fresh
    eligible sources remain available for the caller's autonomous batch.

    Completed history remains unselectable without boxes/pixels from an old
    image. It is never matched to a new source. Unmatched unfinished history is
    removed. Placement-tagged detections are excluded completely. The caller
    decides whether to rebuild a batch; a single pick must remain a single pick.

    Persist returned ``next_cup_id`` on the active inventory and provide it on
    the next call. This watermark prevents reuse of IDs even after the highest
    unfinished ID disappears. Previous capture/frame must identify the latest
    photo (the caller may keep a separate original action-lineage capture).
    Metadata/freshness failures still raise SelectionError. Inputs are copied;
    this function performs no robot, camera, grid or persistence operation.
    """
    for name, value in (("match_mm", match_mm), ("match_px", match_px)):
        if not finite_number(value) or value <= 0:
            raise SelectionError(f"{name} must be a positive finite number")
    offset = normalize_offset(tag_offset)
    if not isinstance(calibration_id, str) or not calibration_id:
        raise _changed("the session camera calibration identity is missing")
    gateways = [_metadata(value, calibration_id, offset)
                for value in (previous_inventory, fresh_inventory)]
    if gateways[0] != gateways[1]:
        raise _changed("the camera gateway no longer matches the previous observation")
    if previous_inventory["capture"] == fresh_inventory["capture"]:
        raise _changed("the camera repeated a previous capture")
    if fresh_inventory["frame"]["sequence"] <= previous_inventory["frame"]["sequence"]:
        raise _changed("the camera frame did not advance after the previous observation")

    # Validate old stable IDs across the full history, including old grid labels
    # that must not later be accidentally reused as a new source identity.
    _sources(previous_inventory)
    history = _ids(previous_inventory["cups"])
    watermark = previous_inventory.get("next_cup_id", 1)
    if type(watermark) is not int or watermark < 1:
        raise _changed("the source identity watermark is invalid")
    next_id = max(watermark, max(history, default=0) + 1)
    fresh_sources = _sources(fresh_inventory)
    conflicts = _fresh_conflicts(fresh_sources, match_mm, match_px)
    remaining = {cup["id"]: cup for cup in _sources(previous_inventory) if not _completed(cup)}
    old_eligible = {identifier: cup for identifier, cup in remaining.items() if _eligible(cup, offset)}
    fresh_eligible = {index for index, cup in enumerate(fresh_sources) if _eligible(cup, offset)}
    forward = {identifier: [index for index in fresh_eligible
                            if _near(cup, fresh_sources[index], match_mm, match_px)]
               for identifier, cup in old_eligible.items()}
    reverse = {index: [identifier for identifier, indexes in forward.items() if index in indexes]
               for index in fresh_eligible}
    stable = {}
    for identifier, indexes in forward.items():
        if len(indexes) == 1 and len(reverse[indexes[0]]) == 1 and indexes[0] not in conflicts:
            stable[indexes[0]] = identifier

    cups = []
    for original in history.values():
        if _completed(original):
            completed = deepcopy(original)
            completed.update(selectable=False, blocked_reason="Cup was already processed from this scan.",
                             cup_detection_id=None, base_detection_id=None,
                             bbox_xyxy=None, pixel={"u": None, "v": None})
            if completed.get("status") not in ("placed", "replaced"):
                completed["status"] = "placed"
            cups.append(completed)
    matched, added, blocked, ambiguous_identity = [], [], [], []
    for index, observed in enumerate(fresh_sources):
        identifier = stable.get(index)
        if identifier is None:
            identifier, next_id = next_id, next_id + 1
            added.append(identifier)
            links = reverse.get(index, [])
            if len(links) > 1 or any(len(forward[old]) > 1 for old in links):
                ambiguous_identity.append(identifier)
            current = {}
        else:
            matched.append(identifier)
            current = deepcopy(remaining[identifier])
        sensor_record = deepcopy(observed)
        # New observations never carry execution facts for an unrelated source.
        for key in ("execution", "last_verification", "last_action", "grip_left_mm", "grip_right_mm"):
            sensor_record.pop(key, None)
        current.update(sensor_record, id=identifier, observed_cup_id=observed.get("id"))
        eligible = index in fresh_eligible and index not in conflicts
        if eligible:
            current.update(selectable=True, status="available", blocked_reason=None)
        else:
            reason = ("Fresh cup detections overlap or have ambiguous coordinates; this cup was skipped."
                      if index in conflicts else observed.get("blocked_reason")
                      or position_problem(observed, offset)
                      or ("Missing or invalid image coordinates." if _pixel(observed) is None
                          else "This source observation is not selectable."))
            current.update(selectable=False, status="blocked", blocked_reason=reason)
            blocked.append(identifier)
        cups.append(current)
    removed = [identifier for identifier in remaining if identifier not in matched]
    return {"cups": sorted(cups, key=lambda cup: cup["id"]),
            "capture": fresh_inventory["capture"], "frame": deepcopy(fresh_inventory["frame"]),
            "next_cup_id": next_id, "matched_cup_ids": matched, "matched_count": len(matched),
            "added_cup_ids": added, "removed_cup_ids": removed, "blocked_cup_ids": blocked,
            "ambiguous_identity_cup_ids": ambiguous_identity,
            "state": "refreshed" if added or removed else "unchanged"}
