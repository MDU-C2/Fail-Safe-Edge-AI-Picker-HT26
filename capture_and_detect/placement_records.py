"""Shared placement evidence bookkeeping for Python and native perception."""
import copy
from cup_inventory import SelectionError, finite_number

def combine(snapshot, observations, sequences):
    count = len(observations)
    if (count not in (1, 3) or len(sequences) != count
            or any(type(s) is not int or s < 0 for s in sequences)
            or any(a >= b for a, b in zip(sequences, sequences[1:]))):
        raise SelectionError("Placement check needs one or three distinct advancing camera observations. Scan again.")
    # Validate evidence before the single-view fast path. Aggregating three
    # views used to access these fields implicitly; one view must not bypass
    # the same contract and turn a malformed clear result into grid clearance.
    for observation in observations:
        for slot in observation:
            if (not isinstance(slot, dict)
                    or not finite_number(slot.get("valid_fraction"))
                    or not 0 <= slot["valid_fraction"] <= 1):
                raise SelectionError("Placement evidence has an invalid valid_fraction. Scan again.")
            if "evidence" in slot and (not isinstance(slot["evidence"], list)
                    or any(not isinstance(kind, str) for kind in slot["evidence"])):
                raise SelectionError("Placement evidence must be a list of strings. Scan again.")
    expected_ids = [location["id"] for location in snapshot["locations"]]
    if (any([slot["id"] for slot in observation] != expected_ids for observation in observations)
            or any([slot["polygon_px"] for slot in observation] !=
                   [slot["polygon_px"] for slot in observations[0]] for observation in observations)):
        raise SelectionError("Placement geometry changed during the scan.")
    slots = copy.deepcopy(observations[-1])
    for index, slot in enumerate(slots):
        if count == 1:
            # Preserve the native reason and evidence. Five raw samples for
            # object depth are still only one placement observation.
            continue
        checks = [observation[index] for observation in observations]
        if any(check["id"] != slot["id"] for check in checks):
            raise SelectionError("Placement geometry changed during the scan.")
        if all("evidence" in check for check in checks):
            slot["evidence"] = sorted({kind for check in checks for kind in check["evidence"]})
        if any(check["state"] == "occupied" for check in checks):
            evidence = slot.get("evidence", [])
            reason = ("Cup assigned to this cell during the scan." if "cup" in evidence
                      else "Detected object or unassigned cup seen during this scan." if "detected_object" in evidence
                      else "Raised depth without an assigned cup; check for an object or inaccurate depth." if "raised_depth" in evidence
                      else "Cup or raised object seen during this scan.")
            slot.update(state="occupied", reason=reason,
                        reason_code="cup" if "cup" in evidence else "detected_object"
                        if "detected_object" in evidence else "raised_depth")
        elif not all(check["state"] == "clear" for check in checks):
            slot.update(state="unknown", reason="Three clear depth observations were not obtained.",
                        reason_code="inconsistent_frames")
        slot["valid_fraction"] = min(check["valid_fraction"] for check in checks)
    result = {"frame_count": count, "frame_sequences": list(sequences), "slots": slots,
              "grid_snapshot": copy.deepcopy(snapshot), "applied": False, "released_ids": [],
              "message": ("Placement area checked against one fresh depth observation."
                          if count == 1 else
                          "Placement area checked against three fresh depth observations.")}
    # Report one count per physical cup group in the latest frame, never a
    # sum across frames or a quota of blocked cells. Two cups may share one
    # cell. Legacy observations without assignments have no inferred count.
    if all("assigned_cups" in slot for frame in observations for slot in frame):
        counts = [[len(slot["assigned_cups"]) for slot in frame] for frame in observations]
        totals = list(map(sum, counts))
        result["cup_summary"] = {
            "assigned_count": totals[-1],
            "location_ids": [slot["id"] for slot, count in zip(slots, counts[-1]) if count],
            "min_assigned_count": min(totals), "max_assigned_count": max(totals),
            "stable_cell_counts": all(frame == counts[-1] for frame in counts),
        }
    return result


def exclude_placed_cups(cups, locations, cell_size, offset):
    half = cell_size / 2
    for cup in cups:
        if not all(finite_number(cup.get(axis)) for axis in ("x", "y")):
            continue
        x, y = cup["x"] + offset[0], cup["y"] + offset[1]
        ids = [place["id"] for place in locations
               if abs(place["x"] - x) <= half and abs(place["y"] - y) <= half]
        if ids:
            cup.update(selectable=False, status="blocked", placement_location_ids=ids,
                       blocked_reason="Cup is already in the placement area; remove it by hand before reusing the location.")
