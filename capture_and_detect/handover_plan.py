"""Pure pick/handover/place plans using the project's tuned RWS poses.

Each arm advances only after its previous job reports DONE. Handover jobs add
explicit cross-arm dependencies for the original overlapping choreography.
Source XY and motion heights follow V4; the gateway and controller check targets.
"""

from cup_inventory import (
    GRIPPER_TCP_Z, LEFT_DOWN, LEFT_READY, TRAVEL_Z, SelectionError,
    finite_number, normalize_offset, position_problem,
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
HANDOVER_BLEND_MM = 10
DEFAULT_PHASE_SPEEDS = {"pick_lift": PICK_SPEED, "handover": None,
                        "flip": FLIP_SPEED, "placement": None}


def normalize_phase_speeds(value=None):
    """Requested phase limits; null travel/placement follows the overall cap.

    The numeric range is the existing controller command range, not a promise
    of collision clearance or cup retention at the selected speed.
    """
    if value is None:
        return dict(DEFAULT_PHASE_SPEEDS)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_PHASE_SPEEDS):
        raise SelectionError("Specify pick_lift, handover, flip and placement speeds together.")
    for key, speed in value.items():
        if speed is None and key in ("handover", "placement"):
            continue
        if not finite_number(speed) or not 1 <= speed <= 1500:
            raise SelectionError(f"{key} speed must be a finite number from 1 to 1500 mm/s.")
    return dict(value)


def effective_phase_speeds(speed=None, settings=None):
    speed, _ = motion_speeds(speed)
    return {key: speed if value is None else min(speed, value)
            for key, value in normalize_phase_speeds(settings).items()}


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
    """Read legacy settings without changing V4's fixed receiving poses."""
    if (not isinstance(value, (tuple, list)) or len(value) != 3
            or any(not finite_number(v) for v in value)):
        raise SelectionError("Legacy handover offsets must contain three finite numbers.")
    return (0.0, 0.0, 0.0)


def handover_settings(offset=(0, 0, 0), speed=DEFAULT_SPEED):
    normalize_handover_offset(offset)
    if not finite_number(speed) or not 1 <= speed <= 1500:
        raise SelectionError("speed must be a finite number from 1 to 1500 mm/s")
    return {"offset_mm": dict.fromkeys("xyz", 0.0),
            "receive_mm": dict(zip("xyz", R_TAKE)),
            "approach_speed": speed, "adjusted": False}


def pick_position_problem(cup, tag_offset=(0, 0, 0)):
    """Use V4's source-coordinate eligibility without extra grip/height gates."""
    return position_problem(cup, tag_offset)


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
    reason = pick_position_problem(cup, tag_offset)
    if reason:
        raise SelectionError(reason)
    yaw = cup.get("rz", LEFT_DOWN[2])
    if not finite_number(yaw):
        raise SelectionError("Cup grip yaw must be a finite angle")
    aperture = cup.get("aperture")
    if aperture is not None and not finite_number(aperture):
        raise SelectionError("Cup aperture must be a finite width or unknown")
    ox, oy, _ = normalize_offset(tag_offset)
    x, y = cup["x"] + ox, cup["y"] + oy
    return ((x, y, TRAVEL_Z + GRIPPER_TCP_Z),
            (x, y, pick_tcp_z(cup, camera_z=camera_z) + GRIPPER_TCP_Z),
            (LEFT_DOWN[0], LEFT_DOWN[1], yaw), aperture)


class _Plan:
    """Build validated jobs only; no dispatch, inferred reachability, or retries."""

    def __init__(self, speed, home_speed, *, dependencies=False, phase_speeds=None):
        self.speed, self.home_speed = motion_speeds(speed, home_speed)
        self.phase_speeds = effective_phase_speeds(self.speed, phase_speeds)
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
        self.job(name, arm, "move", **dict(zip(("x", "y", "z"), position)),
                 **dict(zip(("rx", "ry", "rz"), rotation)),
                 speed=self.speed if move_speed is None else move_speed,
                 acceleration=acceleration, ramp=ramp, after=after)

    def home(self, name, arm):
        self.job(name, arm, "home", speed=self.home_speed, acceleration=100, ramp=100)

    def path(self, name, arm, waypoints, *, move_speed, zone=HANDOVER_BLEND_MM,
             acceleration=100, ramp=100, after=()):
        """Blend transit points only; the final target remains a DONE barrier.

        Keep V4's joint-space movement. Height is checked when dispatched;
        the gateway and controller check workspace and reachability.
        """
        points = []
        for index, (position, rotation, speed) in enumerate(waypoints):
            radius = zone if index < len(waypoints) - 1 else 0
            points.append({**dict(zip(("x", "y", "z"), position)),
                           **dict(zip(("rx", "ry", "rz"), rotation)),
                           "linear": False, "speed": speed, "zone": radius})
        self.job(name, arm, "path", points=points, speed=move_speed, zone=zone,
                 acceleration=acceleration, ramp=ramp, after=after)

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
                  move_speed=self.phase_speeds["pick_lift"], pick=True)
        self.grip("grip_cup", "left", "grip")
        self.move("lift_cup", "left", above, rotation, move_speed=self.phase_speeds["pick_lift"])
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
                  home_speed=None, handover_offset=(0, 0, 0), camera_z=False, phase_speeds=None,
                  home_after_cup=False):
    """Build one guarded transfer with per-arm order and cross-arm dependencies.

    The accepted observation supplies the cup's yaw and aperture. Only cup
    approach/descent/lift use that yaw; the fixed handover keeps its tuned pose.
    Calibration XY is applied once. Travel uses TCP height 250 mm; picking uses
    70 mm, or optionally max(40, cup base Z + 20) mm, plus the 159 mm gripper
    length. Tag-offset Z is not added again. Placement is already a flange target.

    Defaults are 250 mm/s travel and 200 mm/s home. Picking, loaded lift and
    left handover default to 100; the two turnover moves default to 100 and
    retain 30% acceleration. Explicit phase settings adjust future plans only,
    always capped by the overall speed. Receiving poses are fixed to V4.

    Empty grippers travel closed. Opening happens above the cup or beside the
    handover. The left handover move overlaps the right via/beside approach;
    right opening waits for both. Via/beside and turnover/retreat each use the
    loaded RAPID RunPath routine: a 10 mm intermediate blend and an exact final
    stop. Left release still waits
    for right grip, and right lowering waits for left clearance. Every job's
    ``after`` names must complete before dispatch. By default, the
    vision_detection4 workflow leaves both empty grippers open at their clear
    targets and starts the next cup through LEFT_READY without a home command.
    The right approach still waits for the left lift to finish. Explicit
    ``home_after_cup=True`` adds the initial right home before left picking and
    both return-home sequences; after the right retreats above placement, its
    placing/return sequence then overlaps the left return.

    The right arm homes from its raised placement position without a mandatory
    600 mm move. There is no retry or fallback after any unconfirmed movement.
    """
    if type(home_after_cup) is not bool:
        raise SelectionError("home_after_cup must be a boolean")
    plan = _Plan(speed, home_speed, dependencies=True, phase_speeds=phase_speeds)
    normalize_handover_offset(handover_offset)
    take = R_TAKE
    below = (take[0], take[1], take[2] - 35)
    beside = (below[0], below[1] - 120, below[2])
    approach = {"move_speed": plan.phase_speeds["handover"]}
    place = validate_place(place)
    spot = tuple(place[axis] for axis in ("x", "y", "z"))
    place_above = (spot[0], spot[1], spot[2] + PLACE_ABOVE)
    turn_half = tuple((a + b) / 2 for a, b in zip(below, place_above))

    plan.grip("right_close_travel", "right", "grip")
    if home_after_cup:
        plan.home("right_home_start", "right")
    plan.pick(cup, tag_offset, after=("right_home_start",) if home_after_cup else (),
              camera_z=camera_z)
    plan.move("left_handover", "left", HANDOVER_L, LEFT_DOWN,
              move_speed=plan.phase_speeds["pick_lift"])
    plan.path("right_beside", "right",
              [(R_VIA, RIGHT_UP, plan.phase_speeds["handover"]),
               (beside, RIGHT_UP, plan.phase_speeds["handover"])],
              after=("lift_cup",), zone=HANDOVER_BLEND_MM,
              **approach)
    plan.grip("right_open", "right", "open", after=("left_handover",))
    plan.move("right_below", "right", below, RIGHT_UP, **approach)
    plan.move("right_take", "right", take, RIGHT_UP, **approach)
    plan.grip("right_grip", "right", "grip")
    plan.grip("left_release", "left", "open", after=("right_grip",))
    left_clear = (HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT)
    plan.move("left_clear", "left", left_clear, LEFT_DOWN)
    plan.move("right_lower", "right", below, RIGHT_UP, after=("left_clear",), **approach)
    plan.path("right_retreat", "right",
              [(turn_half, RIGHT_SIDE, plan.phase_speeds["flip"]),
               (place_above, RIGHT_DOWN, plan.phase_speeds["flip"])],
              move_speed=plan.phase_speeds["flip"], acceleration=FLIP_ACCELERATION)
    if home_after_cup:
        plan.return_home("left", left_clear, LEFT_DOWN, after=("right_retreat",))
    plan.move("right_place", "right", spot, RIGHT_DOWN, pick=True, move_speed=plan.phase_speeds["placement"])
    plan.grip("right_release", "right", "open")
    plan.move("right_clear", "right", place_above, RIGHT_DOWN, move_speed=plan.phase_speeds["placement"])
    if home_after_cup:
        plan.return_home("right", place_above, RIGHT_DOWN)
    return plan.jobs


def left_only_jobs(cup, *, tag_offset=(0, 0, 0), speed=None, home_speed=None, camera_z=False,
                   phase_speeds=None, home_after_cup=False):
    """Pick, lift and replace, ending open above the cup unless home is requested."""
    if type(home_after_cup) is not bool:
        raise SelectionError("home_after_cup must be a boolean")
    plan = _Plan(speed, home_speed, phase_speeds=phase_speeds)
    above, pick, rotation = plan.pick(cup, tag_offset, camera_z=camera_z)
    plan.move("replace_cup", "left", pick, rotation,
              move_speed=min(plan.phase_speeds["pick_lift"], plan.phase_speeds["placement"]), pick=True)
    plan.grip("left_release", "left", "open")
    plan.move("left_clear", "left", above, rotation, move_speed=plan.phase_speeds["placement"])
    if home_after_cup:
        plan.return_home("left", above, rotation)
    return plan.jobs
