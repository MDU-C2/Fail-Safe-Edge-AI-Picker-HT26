"""Pure, serial pick/handover/place plans using the project's tuned RWS poses.

Each job must report DONE before the next one starts. Workspace checks here are
only a coarse target filter; the controller must still check reachability.
"""

from cup_inventory import (
    GRIPPER_TCP_Z, LEFT_DOWN, LEFT_READY, TRAVEL_Z, SelectionError,
    _flange_problem, finite_number, normalize_offset, position_problem,
)

# Preserve the table and handover geometry from the original detection program.
# Pick/travel heights refer to the left gripper TCP. All fixed poses below refer
# to tool0 (the flange), so calibration offsets must not be added to them.
CUP_Z_MIN, CUP_Z_MAX = 10, 150
PICK_Z = 75
HANDOVER_L = (400, -100, 549)
LEFT_LIFT = 35
RIGHT_UP = (0, 0, 0)
RIGHT_DOWN = (180, 0, 0)
R_VIA = (200, -250, 200)
R_TAKE = (380, -115, 210)
R_BELOW = (R_TAKE[0], R_TAKE[1], R_TAKE[2] - 35)
R_BESIDE = (R_BELOW[0], R_BELOW[1] - 120, R_BELOW[2])
R_PLACE = (420, -200, 230)
R_PLACE_ABOVE = (R_PLACE[0], R_PLACE[1], R_PLACE[2] + 80)


def pick_position_problem(cup, tag_offset=(0, 0, 0)):
    """A fixed-height pick additionally needs a plausible table cup-base height."""
    reason = position_problem(cup, tag_offset)
    if reason:
        return reason
    if not CUP_Z_MIN <= cup["z"] <= CUP_Z_MAX:
        return (f"cup base height must be {CUP_Z_MIN}..{CUP_Z_MAX} mm for the fixed-height pick "
                f"(detected {cup['z']:.1f} mm)")
    return None


def handover_jobs(cup, *, tag_offset=(0, 0, 0), speed=50):
    """Return ordered ``{name, endpoint, body}`` jobs for one selected cup.

    Apply the calibration's X/Y offset exactly once to cup-relative moves. The
    recorded Z and Z offset are not commanded: the table pick uses TCP Z=75 mm
    and travel uses TCP Z=250 mm, with the 159 mm gripper length added to each.
    Home jobs use the controller's existing home routines, not guessed poses.

    This only builds data. Execution must stop on any failure or unconfirmed job;
    in particular, the left hand may release only after the right grip is DONE.
    """
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    reason = cup.get("blocked_reason") or pick_position_problem(cup, tag_offset)
    if cup.get("selectable") is False or reason:
        raise SelectionError(reason or "cup is not selectable")
    ox, oy, _ = normalize_offset(tag_offset)
    x, y = cup["x"] + ox, cup["y"] + oy
    above = (x, y, TRAVEL_Z + GRIPPER_TCP_Z)
    pick = (x, y, PICK_Z + GRIPPER_TCP_Z)

    jobs = []

    def job(name, arm, endpoint, **body):
        jobs.append({"name": name, "endpoint": "/robot/rws/" + endpoint,
                     "body": {"arm": arm, "wait": True, **body}})

    def move(name, arm, position, rotation):
        reason = _flange_problem(position)
        if reason:
            raise SelectionError(f"{name}: {reason}")
        job(name, arm, "move", **dict(zip(("x", "y", "z"), position)),
            **dict(zip(("rx", "ry", "rz"), rotation)),
            speed=speed, acceleration=100, ramp=100)

    def home(name, arm):
        job(name, arm, "home", speed=speed, acceleration=100, ramp=100)

    def grip(name, arm, action):
        job(name, arm, "gripper", action=action)

    # Clear the right arm from the picking area before moving the left one.
    home("right_home_start", "right")
    move("left_ready", "left", LEFT_READY, LEFT_DOWN)
    grip("left_open", "left", "open")
    move("approach_cup", "left", above, LEFT_DOWN)
    move("descend_to_cup", "left", pick, LEFT_DOWN)
    grip("grip_cup", "left", "grip")
    move("lift_cup", "left", above, LEFT_DOWN)
    move("left_handover", "left", HANDOVER_L, LEFT_DOWN)

    # The left hand holds the cup until the right hand has finished gripping it.
    grip("right_open", "right", "open")
    move("right_via", "right", R_VIA, RIGHT_UP)
    move("right_beside", "right", R_BESIDE, RIGHT_UP)
    move("right_below", "right", R_BELOW, RIGHT_UP)
    move("right_take", "right", R_TAKE, RIGHT_UP)
    grip("right_grip", "right", "grip")
    grip("left_release", "left", "open")
    move("left_clear", "left", (HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT), LEFT_DOWN)

    # Clear the handover before the left arm's existing home routine runs.
    move("right_lower", "right", R_BELOW, RIGHT_UP)
    move("right_retreat", "right", R_PLACE_ABOVE, RIGHT_DOWN)
    home("left_home", "left")
    move("right_place", "right", R_PLACE, RIGHT_DOWN)
    grip("right_release", "right", "open")
    move("right_clear", "right", R_PLACE_ABOVE, RIGHT_DOWN)
    home("right_home", "right")
    return jobs
