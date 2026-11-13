"""Pure pick/handover/place plans using the project's tuned RWS poses.

Each arm advances only after its previous job reports DONE. Handover jobs add
explicit cross-arm dependencies for the original overlapping choreography.
Workspace checks are a coarse target filter; the controller checks reachability.
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
CAMERA_Z_OFFSET = 20
CAMERA_Z_FLOOR = 40
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
HOME_UP_Z = 600
LEFT_HOME_Y = 400
LEFT_GAP_CLOSED = 75
PICK_SPEED = 100
FLIP_SPEED = 100
FLIP_ACCELERATION = 30
PLACE_ABOVE = 80
HANDOVER_OFFSET_LIMIT = 20
ALIGNMENT_SPEED = 25


def motion_speeds(speed=None, home_speed=None):
    """Resolve defaults while preserving an explicitly selected speed cap.

    Unconfigured transfers use the upstream 250 mm/s moves and 200 mm/s home
    routines. An operator-selected speed caps home, including an explicitly
    supplied home setting restored with a pending plan.
    """
    selected_speed = speed
    speed = DEFAULT_SPEED if speed is None else speed
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    if home_speed is None:
        home_speed = HOME_SPEED if selected_speed is None else min(speed, HOME_SPEED)
    home_cap = min(speed, HOME_SPEED)
    if not finite_number(home_speed) or not 1 <= home_speed <= home_cap:
        raise SelectionError(f"home_speed must be a finite number from 1 to {home_cap} mm/s")
    return speed, home_speed


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
    if cup.get("grip_geometry_error"):
        return f"Grip geometry is unconfirmed: {cup['grip_geometry_error']}"
    if not CUP_Z_MIN <= cup["z"] <= CUP_Z_MAX:
        return (f"cup base height must be {CUP_Z_MIN}..{CUP_Z_MAX} mm for the fixed-height pick "
                f"(detected {cup['z']:.1f} mm)")
    return None


def pick_tcp_z(cup, *, camera_z=False):
    """Return the selected TCP height; camera Z is before the tag offset."""
    if type(camera_z) is not bool:
        raise SelectionError("camera_z must be a boolean")
    if not camera_z:
        return PICK_Z
    if not isinstance(cup, dict) or not finite_number(cup.get("z")):
        raise SelectionError("Camera-height picking requires a finite cup base height")
    return max(CAMERA_Z_FLOOR, round(cup["z"] + CAMERA_Z_OFFSET, 1))


def _pick_geometry(cup, tag_offset, *, camera_z=False):
    if not isinstance(cup, dict):
        raise SelectionError("Cup selection must be an object")
    reason = cup.get("blocked_reason") or pick_position_problem(cup, tag_offset)
    if cup.get("selectable") is False or reason:
        raise SelectionError(reason or "cup is not selectable")
    yaw = cup.get("rz", LEFT_DOWN[2])
    if not finite_number(yaw) or not 90 <= yaw <= 270:
        raise SelectionError("Cup grip yaw must be a finite angle from 90 to 270 degrees")
    aperture = cup.get("aperture")
    if aperture is not None and (not finite_number(aperture) or aperture <= 0):
        raise SelectionError("Cup aperture must be a finite positive width or unknown")
    ox, oy, _ = normalize_offset(tag_offset)
    x, y = cup["x"] + ox, cup["y"] + oy
    return ((x, y, TRAVEL_Z + GRIPPER_TCP_Z),
            (x, y, pick_tcp_z(cup, camera_z=camera_z) + GRIPPER_TCP_Z),
            (LEFT_DOWN[0], LEFT_DOWN[1], yaw), aperture)


class _Plan:
    """Build validated jobs only; no dispatch, inferred reachability, or retries."""

    def __init__(self, speed, home_speed, *, dependencies=False):
        self.speed, self.home_speed = motion_speeds(speed, home_speed)
        self.jobs = []
        self.dependencies = dependencies
        self.previous = {}

    def job(self, name, arm, endpoint, *, after=(), **body):
        job = {"name": name, "endpoint": "/robot/rws/" + endpoint,
               "body": {"arm": arm, "wait": True, **body}}
        if self.dependencies:
            previous = [self.previous[arm]] if arm in self.previous else []
            job["after"] = list(dict.fromkeys([*previous, *after]))
        self.jobs.append(job)
        self.previous[arm] = name

    def move(self, name, arm, position, rotation, *, move_speed=None, acceleration=100,
             ramp=100, pick=False, after=()):
        reason = _motion_flange_problem(position, pick=pick)
        if reason:
            raise SelectionError(f"{name}: {reason}")
        self.job(name, arm, "move", **dict(zip(("x", "y", "z"), position)),
                 **dict(zip(("rx", "ry", "rz"), rotation)),
                 speed=self.speed if move_speed is None else move_speed,
                 acceleration=acceleration, ramp=ramp, after=after)

    def home(self, name, arm):
        self.job(name, arm, "home", speed=self.home_speed, acceleration=100, ramp=100)

    def grip(self, name, arm, action, **options):
        self.job(name, arm, "gripper", action=action, **options)

    def pick(self, cup, tag_offset, *, after=(), camera_z=False):
        above, pick, rotation, aperture = _pick_geometry(cup, tag_offset, camera_z=camera_z)
        self.grip("left_close_travel", "left", "grip", after=after)
        self.move("left_ready", "left", LEFT_READY, LEFT_DOWN)
        self.move("approach_cup", "left", above, rotation)
        if aperture is None:
            self.grip("left_open", "left", "open")
        else:
            position = round(min(max((aperture - LEFT_GAP_CLOSED) / 2, 0), 25), 1)
            self.grip("left_open", "left", "move", position=position)
        self.move("descend_to_cup", "left", pick, rotation,
                  move_speed=min(self.speed, PICK_SPEED), pick=True)
        self.grip("grip_cup", "left", "grip")
        self.move("lift_cup", "left", above, rotation, move_speed=min(self.speed, PICK_SPEED))
        return above, pick, rotation

    def return_home(self, arm, last, rotation, *, after=()):
        """Return a known empty arm from its last confirmed planned target.

        The left arm uses explicit raising/lateral jobs and is never lowered to
        the nominal return height. The right arm homes from its confirmed raised
        placement position; an unverified 600 mm target is not a prerequisite.
        A failure stops the sequence; there is no automatic fallback or retry.
        """
        self.grip(f"{arm}_close_return", arm, "grip", after=after)
        x, y, z = last
        if arm == "left" and z < HOME_UP_Z:
            z = HOME_UP_Z
            self.move(f"{arm}_home_raise", arm, (x, y, z), rotation)
        if arm == "left" and y < LEFT_HOME_Y:
            self.move("left_home_lateral", arm, (x, LEFT_HOME_Y, z), rotation)
        self.home(f"{arm}_home", arm)


def handover_jobs(cup, *, place, tag_offset=(0, 0, 0), speed=None,
                  home_speed=None, handover_offset=(0, 0, 0), camera_z=False):
    """Build one guarded transfer with per-arm order and cross-arm dependencies.

    The accepted observation supplies the cup's yaw and aperture. Only cup
    approach/descent/lift use that yaw; the fixed handover keeps its tuned pose.
    Calibration XY is applied once. Travel uses TCP height 250 mm; picking uses
    70 mm, or optionally max(40, cup base Z + 20) mm, plus the 159 mm gripper
    length. Tag-offset Z is not added again. Placement is already a flange target.

    Defaults are 250 mm/s travel and 200 mm/s home. Picking, loaded lift and
    left handover are capped at 100; the two turnover moves also use 100 and
    30% acceleration. Selected lower speeds cap all those movements. Adjusted
    receiving poses retain the 25 mm/s, 20% acceleration, 10% ramp limits.

    Empty grippers travel closed. Opening happens above the cup or beside the
    handover. The left handover move overlaps the right via/beside approach;
    right opening waits for both. After the right retreats above placement, its
    placing/return sequence overlaps the left return. Left release still waits
    for right grip, and right lowering waits for left clearance. Every job's
    ``after`` names must complete before dispatch. The initial right home is
    retained as an additional startup guard before left picking begins.

    The right arm homes from its raised placement position without a mandatory
    600 mm move. There is no retry or fallback after any unconfirmed movement.
    """
    plan = _Plan(speed, home_speed, dependencies=True)
    alignment = handover_settings(handover_offset, plan.speed)
    take = tuple(alignment["receive_mm"][axis] for axis in "xyz")
    below = (take[0], take[1], take[2] - 35)
    beside = (below[0], below[1] - 120, below[2])
    approach = ({"move_speed": alignment["approach_speed"], "acceleration": 20, "ramp": 10}
                if alignment["adjusted"] else {})
    place = validate_place(place)
    spot = tuple(place[axis] for axis in ("x", "y", "z"))
    place_above = (spot[0], spot[1], spot[2] + PLACE_ABOVE)
    turn_half = tuple((a + b) / 2 for a, b in zip(below, place_above))

    plan.grip("right_close_travel", "right", "grip")
    plan.home("right_home_start", "right")
    plan.pick(cup, tag_offset, after=("right_home_start",), camera_z=camera_z)
    plan.move("left_handover", "left", HANDOVER_L, LEFT_DOWN,
              move_speed=min(plan.speed, PICK_SPEED))
    plan.move("right_via", "right", R_VIA, RIGHT_UP, after=("lift_cup",))
    plan.move("right_beside", "right", beside, RIGHT_UP, **approach)
    plan.grip("right_open", "right", "open", after=("left_handover",))
    plan.move("right_below", "right", below, RIGHT_UP, **approach)
    plan.move("right_take", "right", take, RIGHT_UP, **approach)
    plan.grip("right_grip", "right", "grip")
    plan.grip("left_release", "left", "open", after=("right_grip",))
    left_clear = (HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT)
    plan.move("left_clear", "left", left_clear, LEFT_DOWN)
    plan.move("right_lower", "right", below, RIGHT_UP, after=("left_clear",), **approach)
    plan.move("right_turn_half", "right", turn_half, RIGHT_SIDE,
              move_speed=min(plan.speed, FLIP_SPEED), acceleration=FLIP_ACCELERATION)
    plan.move("right_retreat", "right", place_above, RIGHT_DOWN,
              move_speed=min(plan.speed, FLIP_SPEED), acceleration=FLIP_ACCELERATION)
    plan.return_home("left", left_clear, LEFT_DOWN, after=("right_retreat",))
    plan.move("right_place", "right", spot, RIGHT_DOWN, pick=True)
    plan.grip("right_release", "right", "open")
    plan.move("right_clear", "right", place_above, RIGHT_DOWN)
    plan.return_home("right", place_above, RIGHT_DOWN)
    return plan.jobs


def left_only_jobs(cup, *, tag_offset=(0, 0, 0), speed=None, home_speed=None, camera_z=False):
    """Pick, lift and replace the verified cup, then return only the left arm."""
    plan = _Plan(speed, home_speed)
    above, pick, rotation = plan.pick(cup, tag_offset, camera_z=camera_z)
    plan.move("replace_cup", "left", pick, rotation,
              move_speed=min(plan.speed, PICK_SPEED), pick=True)
    plan.grip("left_release", "left", "open")
    plan.move("left_clear", "left", above, rotation)
    plan.return_home("left", above, rotation)
    return plan.jobs
