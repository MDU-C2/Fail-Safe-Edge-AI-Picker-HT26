"""Offline height arithmetic for a measured cup in a dishwasher rack.

This reads archived observations and prints an UNVALIDATED report. It cannot
connect to a camera or robot, generate motion jobs, or change picker settings.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


GRIPPER_TCP_Z_MM = 159.0  # Existing left gripper model; not a measured finger envelope.


class PreviewError(ValueError):
    """The supplied measurements or archived observation cannot be compared."""


def _finite(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def _json(raw, label):
    def constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw, parse_constant=constant, object_pairs_hook=unique)
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise PreviewError(f"Invalid {label}: {error}") from error


def calculate_preview(inventory, calibration_bytes, *, cup_id, cup_top_mm,
                      cup_height_mm, cup_circumference_mm, open_gripper_width_mm,
                      highest_wire_mm, upright_wire_mm, clearance_mm,
                      grip_below_top_mm=20.0):
    """Calculate heights only, assuming a vertical cup and tabletop at robot Z=0.

    The saved CUP BASE observation is treated as the visible top of an inverted
    cup for comparison only. Neither that interpretation nor clearance is proved.
    """
    measurements = {
        "cup_top_mm": cup_top_mm, "cup_height_mm": cup_height_mm,
        "cup_circumference_mm": cup_circumference_mm,
        "open_gripper_outside_width_mm": open_gripper_width_mm,
        "highest_wire_mm": highest_wire_mm, "upright_wire_mm": upright_wire_mm,
        "clearance_assumption_mm": clearance_mm,
        "grip_below_top_assumption_mm": grip_below_top_mm,
    }
    for name, value in measurements.items():
        if not _finite(value) or value <= 0:
            raise PreviewError(f"{name} must be a positive finite number in millimetres.")
    if cup_top_mm < cup_height_mm:
        raise PreviewError("Cup top must not be lower than cup height above the tabletop.")
    if grip_below_top_mm >= cup_height_mm:
        raise PreviewError("Grip below top must be less than the cup height.")
    if highest_wire_mm < upright_wire_mm:
        raise PreviewError("Highest wire cannot be lower than the upright wire.")
    diameter = cup_circumference_mm / math.pi
    if open_gripper_width_mm <= diameter:
        raise PreviewError("Outside gripper width must exceed cup diameter; inside opening is still unmeasured.")

    if not isinstance(inventory, dict) or type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 2:
        raise PreviewError("An archived schema_version 2 cup inventory is required.")
    if inventory.get("units") != "mm" or not isinstance(inventory.get("capture"), str) or not inventory["capture"].strip():
        raise PreviewError("Inventory must identify its capture and use mm units.")
    if type(cup_id) is not int or cup_id < 1:
        raise PreviewError("Cup ID must be a positive integer from the specified capture.")
    cups = inventory.get("cups")
    if not isinstance(cups, list) or any(not isinstance(cup, dict) or type(cup.get("id")) is not int for cup in cups):
        raise PreviewError("Inventory cups must have integer IDs.")
    if len({cup["id"] for cup in cups}) != len(cups):
        raise PreviewError("Inventory contains duplicate cup IDs.")
    matches = [cup for cup in cups if cup["id"] == cup_id]
    if len(matches) != 1:
        raise PreviewError(f"Cup {cup_id} was not found in the specified capture.")
    cup = matches[0]
    if cup.get("selectable") is not True or cup.get("blocked_reason"):
        raise PreviewError(f"Cup {cup_id} was blocked in the archived scan: {cup.get('blocked_reason') or 'not selectable'}.")
    if cup.get("base_detection_id") is None or not all(_finite(cup.get(axis)) for axis in "xyz"):
        raise PreviewError("Selected cup needs a CUP BASE observation and finite saved XYZ.")

    if not isinstance(calibration_bytes, bytes):
        raise PreviewError("Calibration must be supplied as its original file bytes.")
    digest = hashlib.sha256(calibration_bytes).hexdigest()
    if inventory.get("calibration_sha256") != digest:
        raise PreviewError("Calibration hash does not match this archived capture; use its original calibration.")
    calibration = _json(calibration_bytes, "calibration JSON")
    offset = calibration.get("tag_offset") if isinstance(calibration, dict) else None
    saved_offset = inventory.get("tag_offset_mm")
    if (not isinstance(offset, list) or len(offset) != 3 or not all(_finite(v) for v in offset)
            or not isinstance(saved_offset, list) or len(saved_offset) != 3
            or not all(_finite(v) for v in saved_offset) or saved_offset != offset):
        raise PreviewError("Calibration and archived tag offsets must contain the same three finite values.")

    support = cup_top_mm - cup_height_mm
    grip = cup_top_mm - grip_below_top_mm
    grip_to_lowest_cup = grip - support
    # Above both the cup's current grip and the cup-only obstacle clearance.
    lift = max(grip, highest_wire_mm + grip_to_lowest_cup + clearance_mm)
    observed_top = cup["z"] + offset[2]
    values = (diameter, support, grip, grip_to_lowest_cup, lift,
              grip + GRIPPER_TCP_Z_MM, lift + GRIPPER_TCP_Z_MM,
              observed_top, observed_top - cup_top_mm)
    if not all(_finite(value) for value in values):
        raise PreviewError("Derived geometry is not finite; check measurement magnitudes.")

    return {
        "status": "UNVALIDATED_HEIGHT_PREVIEW",
        "units": "mm",
        "live_motion_enabled": False,
        "capture": inventory["capture"],
        "archived_saved_at": inventory.get("saved_at"),
        "cup_id": cup_id,
        "calibration_sha256": digest,
        "measurements_and_assumptions": measurements,
        "height_calculation": {
            "cup_diameter_assuming_circular_mm": round(diameter, 3),
            "nominal_support_if_vertical_mm": round(support, 3),
            "grip_tcp_height_mm": round(grip, 3),
            "grip_flange_height_mm": round(grip + GRIPPER_TCP_Z_MM, 3),
            "grip_to_lowest_cup_if_vertical_mm": round(grip_to_lowest_cup, 3),
            "cup_only_lift_tcp_height_mm": round(lift, 3),
            "cup_only_lift_flange_height_mm": round(lift + GRIPPER_TCP_Z_MM, 3),
            "cup_only_bottom_at_lift_mm": round(lift - grip_to_lowest_cup, 3),
        },
        "archived_depth_comparison": {
            "saved_cup_z_mm": cup["z"], "calibration_offset_z_mm": offset[2],
            "observed_top_robot_z_if_inverted_mm": round(observed_top, 3),
            "observed_minus_measured_top_mm": round(observed_top - cup_top_mm, 3),
            "note": "Comparison only. Resolve a height discrepancy before teaching a live pick; no automatic correction is applied.",
        },
        "wire_height_comparison": {
            "grip_tcp_minus_upright_wire_mm": round(grip - upright_wire_mm, 3),
            "grip_tcp_minus_highest_wire_mm": round(grip - highest_wire_mm, 3),
            "grip_at_or_below_upright_tips": grip <= upright_wire_mm,
            "note": "A grip at or below the wire tips requires a verified gap for the fingers. TCP height alone is not finger clearance.",
        },
        "blocking_notes": [
            "Historical scan only; freshness is not evaluated. The saved selectable flag does not validate the new tray geometry.",
            "All measured heights refer to the tabletop. Flange heights assume tabletop is robot-base Z=0, a vertical cup, downward gripper, and the existing 159 mm TCP offset.",
            "Support = cup top - cup height is only a vertical-cup estimate. Upright-wire height is not the support height.",
            "Lift is a CUP-ONLY height estimate. Unmeasured lower finger extensions and the arm may require more clearance.",
            "Outside gripper width is only one envelope dimension. Inside opening, finger length/depth, cup tilt, and full arm clearance remain unverified.",
            "RAPID currently uses MoveAbsJ; matching X/Y endpoints do not guarantee a vertical or straight Cartesian path.",
            "Rack position, internal wires and clear approach corridors have not been mapped in robot coordinates. No collision or reachability proof is provided.",
            "Placement slots must be checked against the tray footprint and gripper envelope. No destination is verified or chosen by this report.",
            "No robot settings, inventory or placement-grid files were changed. This report cannot be executed as a robot command.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path, help="Explicit archived cups_*.json file")
    parser.add_argument("--calibration", required=True, type=Path, help="Exact calibration used by that capture")
    parser.add_argument("--cup-id", required=True, type=int)
    for name in ("cup-top", "cup-height", "cup-circumference", "open-gripper-width",
                 "highest-wire", "upright-wire", "clearance"):
        parser.add_argument("--" + name + "-mm", required=True, type=float,
                            help="Millimetres; heights are above tabletop. Clearance is an unverified planning assumption.")
    parser.add_argument("--grip-below-top-mm", type=float, default=20.0,
                        help="Unverified grip-height assumption in mm (default 20)")
    args = parser.parse_args(argv)
    try:
        inventory = _json(args.inventory.read_bytes(), "inventory JSON")
        report = calculate_preview(inventory, args.calibration.read_bytes(), cup_id=args.cup_id,
                                   cup_top_mm=args.cup_top_mm, cup_height_mm=args.cup_height_mm,
                                   cup_circumference_mm=args.cup_circumference_mm,
                                   open_gripper_width_mm=args.open_gripper_width_mm,
                                   highest_wire_mm=args.highest_wire_mm,
                                   upright_wire_mm=args.upright_wire_mm,
                                   clearance_mm=args.clearance_mm,
                                   grip_below_top_mm=args.grip_below_top_mm)
    except (OSError, PreviewError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
