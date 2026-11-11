"""Pure, serial pick/handover/place plans using the project's tuned RWS poses.

Each job must report DONE before the next one starts. Workspace checks here are
only a coarse target filter; the controller must still check reachability.
"""

from cup_inventory import (
    GRIPPER_TCP_Z, LEFT_DOWN, LEFT_READY, TRAVEL_Z, SelectionError,
    _motion_flange_problem, finite_number, normalize_offset, position_problem,
)
from placing_grid import validate_place

# Preserve the table and handover geometry from the original detection program.
# Pick/travel heights refer to the left gripper TCP. All fixed poses below refer
# to tool0 (the flange), so calibration offsets must not be added to them.
CUP_Z_MIN, CUP_Z_MAX = -30, 150
PICK_Z = 70
LEFT_LIFT = 35
LEFT_TOP = 690
HANDOVER_X = 350
HANDOVER_L = (HANDOVER_X, -100, LEFT_TOP - LEFT_LIFT)
RIGHT_UP = (0, 0, 0)
RIGHT_DOWN = (180, 0, 0)
RIGHT_SIDE = (90, 0, 0)
R_VIA = (300, -300, 250)
R_TAKE = (HANDOVER_L[0] - 20, HANDOVER_L[1] - 15, HANDOVER_L[2] - 339)
R_BELOW = (R_TAKE[0], R_TAKE[1], R_TAKE[2] - 35)
R_BESIDE = (R_BELOW[0], R_BELOW[1] - 120, R_BELOW[2])
DEFAULT_SPEED = 250
HOME_SPEED = 200
PICK_SPEED = 100
FLIP_SPEED = 100
FLIP_ACCELERATION = 30
PLACE_ABOVE = 80
HANDOVER_OFFSET_LIMIT = 20
ALIGNMENT_SPEED = 25


def normalize_handover_offset(value):
    """Small operator-taught corrections in robot-base mm, never camera pixels.

    These bounds limit configuration mistakes; they do not prove path clearance.
    """
    if (not isinstance(value, (tuple, list)) or len(value) != 3
            or any(not finite_number(v) or abs(v) > HANDOVER_OFFSET_LIMIT for v in value)):
        raise SelectionError("Handover X/Y/Z offsets must be finite numbers from -20 to 20 mm.")
    return tuple(float(v) for v in value)


def handover_settings(offset=(0, 0, 0), speed=DEFAULT_SPEED):
    offset = normalize_handover_offset(offset)
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    adjusted = any(offset)
    return {"offset_mm": dict(zip("xyz", offset)),
            "receive_mm": dict(zip("xyz", (p + d for p, d in zip(R_TAKE, offset)))),
            "approach_speed": min(speed, ALIGNMENT_SPEED) if adjusted else speed,
            "adjusted": adjusted}


def pick_position_problem(cup, tag_offset=(0, 0, 0)):
    """A fixed-height pick additionally needs a plausible table cup-base height."""
    reason = position_problem(cup, tag_offset)
    if reason:
        return reason
    if not CUP_Z_MIN <= cup["z"] <= CUP_Z_MAX:
        return (f"cup base height must be {CUP_Z_MIN}..{CUP_Z_MAX} mm for the fixed-height pick "
                f"(detected {cup['z']:.1f} mm)")
    return None


def handover_jobs(cup, *, place, tag_offset=(0, 0, 0), speed=DEFAULT_SPEED,
                  handover_offset=(0, 0, 0)):
    """Return ordered ``{name, endpoint, body}`` jobs for one selected cup.

    Apply the calibration's X/Y offset exactly once to cup-relative moves. The
    recorded Z and Z offset are not commanded: the table pick uses TCP Z=70 mm
    and travel uses TCP Z=175 mm, with the 159 mm gripper length added to each.
    Home jobs use the controller's existing home routines, not guessed poses.
    The explicit placing-grid location is a flange target, without calibration
    offsets. Normal moves use the original program's 250 mm/s default. Homes
    use at most 200 mm/s; picking, lifting and carrying to the handover use at
    most 100 mm/s. Rotation moves use at most 100 mm/s and 30% acceleration.
    A lower selected speed caps all of those moves.

    An explicit receiving offset translates the right hand's take/below/beside
    points together and recomputes its turning midpoint. Near the adjusted grip,
    right-arm moves are capped at 25 mm/s, 20% acceleration and 10% ramp.
    This does not change finger speed/force or make MoveAbsJ a linear path.

    This only builds data. Execution must stop on any failure or unconfirmed job;
    in particular, the left hand may release only after the right grip is DONE.
    """
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    alignment = handover_settings(handover_offset, speed)
    take = tuple(alignment["receive_mm"][axis] for axis in "xyz")
    below = (take[0], take[1], take[2] - 35)
    beside = (below[0], below[1] - 120, below[2])
    approach = ({"move_speed": alignment["approach_speed"], "acceleration": 20, "ramp": 10}
                if alignment["adjusted"] else {})
    place = validate_place(place)
    reason = cup.get("blocked_reason") or pick_position_problem(cup, tag_offset)
    if cup.get("selectable") is False or reason:
        raise SelectionError(reason or "cup is not selectable")
    ox, oy, _ = normalize_offset(tag_offset)
    x, y = cup["x"] + ox, cup["y"] + oy
    above = (x, y, TRAVEL_Z + GRIPPER_TCP_Z)
    pick = (x, y, PICK_Z + GRIPPER_TCP_Z)
    spot = tuple(place[axis] for axis in ("x", "y", "z"))
    place_above = (spot[0], spot[1], spot[2] + PLACE_ABOVE)
    turn_half = tuple((a + b) / 2 for a, b in zip(below, place_above))

    jobs = []

    def job(name, arm, endpoint, **body):
        jobs.append({"name": name, "endpoint": "/robot/rws/" + endpoint,
                     "body": {"arm": arm, "wait": True, **body}})

    def move(name, arm, position, rotation, *, move_speed=None, acceleration=100, ramp=100, pick=False):
        reason = _motion_flange_problem(position, pick=pick)
        if reason:
            raise SelectionError(f"{name}: {reason}")
        job(name, arm, "move", **dict(zip(("x", "y", "z"), position)),
            **dict(zip(("rx", "ry", "rz"), rotation)),
            speed=speed if move_speed is None else move_speed, acceleration=acceleration, ramp=ramp)

    def home(name, arm):
        job(name, arm, "home", speed=min(speed, HOME_SPEED), acceleration=100, ramp=100)

    def grip(name, arm, action):
        job(name, arm, "gripper", action=action)

    # Clear the right arm from the picking area before moving the left one.
    home("right_home_start", "right")
    grip("left_open", "left", "open")
    move("left_ready", "left", LEFT_READY, LEFT_DOWN)
    move("approach_cup", "left", above, LEFT_DOWN)
    move("descend_to_cup", "left", pick, LEFT_DOWN, move_speed=min(speed, PICK_SPEED), pick=True)
    grip("grip_cup", "left", "grip")
    move("lift_cup", "left", above, LEFT_DOWN, move_speed=min(speed, PICK_SPEED))
    move("left_handover", "left", HANDOVER_L, LEFT_DOWN, move_speed=min(speed, PICK_SPEED))

    # The left hand holds the cup until the right hand has finished gripping it.
    grip("right_open", "right", "open")
    move("right_via", "right", R_VIA, RIGHT_UP)
    move("right_beside", "right", beside, RIGHT_UP, **approach)
    move("right_below", "right", below, RIGHT_UP, **approach)
    move("right_take", "right", take, RIGHT_UP, **approach)
    grip("right_grip", "right", "grip")
    grip("left_release", "left", "open")
    move("left_clear", "left", (HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT), LEFT_DOWN)

    # Clear the handover before the left arm's existing home routine runs.
    move("right_lower", "right", below, RIGHT_UP, **approach)
    move("right_turn_half", "right", turn_half, RIGHT_SIDE,
         move_speed=min(speed, FLIP_SPEED), acceleration=FLIP_ACCELERATION)
    move("right_retreat", "right", place_above, RIGHT_DOWN,
         move_speed=min(speed, FLIP_SPEED), acceleration=FLIP_ACCELERATION)
    home("left_home", "left")
    move("right_place", "right", spot, RIGHT_DOWN, pick=True)
    grip("right_release", "right", "open")
    move("right_clear", "right", place_above, RIGHT_DOWN)
    home("right_home", "right")
    return jobs
