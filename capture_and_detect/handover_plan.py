"""Pure pick/handover/place plans using the project's tuned RWS poses.

Each arm advances only after its previous job reports DONE. Handover jobs adapt
the smooth branch's normal choreography with explicit cross-arm dependencies.
Source detection follows V4; the gateway and controller check motion targets.
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
RIGHT_CAMERA_PARK = (200, -380, 250)
R_TAKE = (HANDOVER_L[0] - 20, HANDOVER_L[1] - 15, HANDOVER_L[2] - 339)
R_BELOW = (R_TAKE[0], R_TAKE[1], R_TAKE[2] - 35)
R_BESIDE = (R_BELOW[0], R_BELOW[1] - 120, R_BELOW[2])
DEFAULT_SPEED = 250
HOME_SPEED = 200
HOME_UP_Z = 600
LEFT_HOME_Y = 400
# Where the empty left arm waits after the handover, out of the camera's view, until
# the next cup: sideways to the left (+y) at the handover height (the same side move
# its home routine uses). Tune it so the arm is not in the camera image.
LEFT_PARK = (HANDOVER_X, LEFT_HOME_Y, LEFT_TOP)
LEFT_GAP_CLOSED = 75
PICK_SPEED = 100
FLIP_SPEED = 100
FLIP_ACCELERATION = 30
PLACE_ABOVE = 80
RIGHT_RELEASE_POS = 12 
HANDOVER_BLEND_MM = 10
SMOOTH_TRAVEL_Z = 300
SMOOTH_ACCELERATION = 60
SMOOTH_RAMP = 40
CARRY_MAX_ACC = 1.5
RECEIVING_BLEND_MM = 3
SLIDE_SPEED = 150
DEFAULT_PHASE_SPEEDS = {"pick_lift": PICK_SPEED, "handover": None,
                        "flip": FLIP_SPEED, "placement": None}


def right_via_pose():
    """The existing upright right-arm waiting target in tool0/wobj0.

    Camera parking can select this same target before capturing, so it cannot
    drift away from the normal next-cup preparation pose as the plan is tuned.
    This returns coordinates only and never dispatches motion.
    """
    return dict(zip(("x", "y", "z", "rx", "ry", "rz"), (*R_VIA, *RIGHT_UP)))


def right_camera_park_pose():
    """Empty right-arm waiting target behind the placement grid, tool0/wobj0.

    Keep this separate from R_VIA, the next cup's handover approach. The
    Mac, Pi and Lynx coordinated workflows use the same final parking target.
    """
    return dict(zip(("x", "y", "z", "rx", "ry", "rz"), (*RIGHT_CAMERA_PARK, *RIGHT_UP)))


def normalize_right_park_pose(value=None):
    """Copy an explicitly supplied flange pose, without offsets or inferred yaw.

    These are the existing ordinary Cartesian endpoint limits, not a guarantee
    of reachability or collision clearance. The controller checks the movement.
    None keeps the normal workflow ending above the placement destination.
    """
    if value is None:
        return None
    axes = ("x", "y", "z", "rx", "ry", "rz")
    if (not isinstance(value, dict) or set(value) != set(axes)
            or any(not finite_number(value[axis]) for axis in axes)):
        raise SelectionError("Right-arm parking pose must contain exactly six finite x/y/z/rx/ry/rz values.")
    problem = _motion_flange_problem(tuple(value[axis] for axis in ("x", "y", "z")))
    if problem:
        raise SelectionError(f"Right-arm parking pose: {problem}.")
    return {axis: value[axis] for axis in axes}


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


def _pick_geometry(cup, tag_offset, *, camera_z=False, travel_z=TRAVEL_Z):
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
    return ((x, y, travel_z + GRIPPER_TCP_Z),
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
             acceleration=SMOOTH_ACCELERATION, ramp=SMOOTH_RAMP,
             max_acceleration=None, after=()):
        """Blend transit points only; the final target remains a DONE barrier.

        Each waypoint selects a straight-line or joint move. Height is checked
        when dispatched; the gateway and controller check workspace/reachability.
        """
        points = []
        for index, (position, rotation, speed, linear) in enumerate(waypoints):
            radius = zone if index < len(waypoints) - 1 else 0
            points.append({**dict(zip(("x", "y", "z"), position)),
                           **dict(zip(("rx", "ry", "rz"), rotation)),
                           "linear": linear, "speed": speed, "zone": radius})
        options = {} if max_acceleration is None else {"max_acceleration": max_acceleration}
        self.job(name, arm, "path", points=points, speed=move_speed, zone=zone,
                 acceleration=acceleration, ramp=ramp, after=after, **options)

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
                  home_after_cup=False, right_park_pose=None):
    """Adapt the smooth branch's normal per-cup plan without automatic homing.

    Source XY, detected yaw/aperture, optional camera height and the selected
    placement remain UI inputs. Only cup approach/descent/lift use detected yaw;
    the fixed handover keeps its tuned pose. Calibration XY is applied once.
    Dual-arm travel uses TCP Z=250 mm; picking uses 70 mm or, with camera height,
    max(40, cup base Z + 20), plus the 159 mm gripper length. Left-only motion
    keeps its existing travel height. Placement is already a flange target.

    Motion stretches between gripper actions are buffered RAPID paths, with
    straight lines near cups, joint travel, 10 mm intermediate blends and exact
    final stops. The receiving corner uses 3 mm. Paths use 60% acceleration and
    40% ramp; carrying adds a 1.5 m/s2 acceleration limit, and turnover keeps
    30% acceleration. Existing UI phase speeds still apply, capped by the
    overall speed, with the slide under the cup additionally capped at 150.

    Left approach/grip overlaps right preparation at R_VIA. Both finish before
    left lift/handover overlaps the right move beside it. Right opening waits
    for both arrivals; left release waits for confirmed right grip. After the
    release the arms move straight apart: the left up out of the cup, the right
    down. The turnover waits until the left is up; only after the turnover does
    the left move on to LEFT_PARK. Placement completes before another cup starts.

    By default left parks open out of the camera's view (LEFT_PARK) and
    right stays open above placement; the grippers close only on a cup. No home command is issued,
    even for the last cup. Explicit ``home_after_cup=True`` retains the
    initial right home and return-home sequences. An explicit ``right_park_pose``
    adds one empty-arm joint path after the normal placement clearance lift;
    it cannot be combined with automatic homing. No pose is inferred or enabled
    by default. There is no retry or fallback after an unconfirmed movement.
    """
    if type(home_after_cup) is not bool:
        raise SelectionError("home_after_cup must be a boolean")
    right_park = normalize_right_park_pose(right_park_pose)
    if home_after_cup and right_park is not None:
        raise SelectionError("Right-arm parking cannot be combined with home_after_cup.")
    plan = _Plan(speed, home_speed, dependencies=True, phase_speeds=phase_speeds)
    normalize_handover_offset(handover_offset)
    take = R_TAKE
    below = (take[0], take[1], take[2] - 35)
    beside = (below[0], below[1] - 120, below[2])
    pick_speed = plan.phase_speeds["pick_lift"]
    receive_speed = plan.phase_speeds["handover"]
    flip_speed = plan.phase_speeds["flip"]
    place_speed = plan.phase_speeds["placement"]
    carrying = {"max_acceleration": CARRY_MAX_ACC}
    above, pick, rotation, aperture = _pick_geometry(
        cup, tag_offset, camera_z=camera_z, travel_z=SMOOTH_TRAVEL_Z)
    place = validate_place(place)
    spot = tuple(place[axis] for axis in ("x", "y", "z"))
    place_above = (spot[0], spot[1], spot[2] + PLACE_ABOVE)
    turn_half = tuple((a + b) / 2 for a, b in zip(below, place_above))

    # Grippers close only on a cup (grip_cup, right_grip); otherwise they stay
    # open, ready for the next cup. Both open first, in case they start closed.
    plan.grip("right_open_start", "right", "open")
    if home_after_cup:
        plan.home("right_home_start", "right")
    # The left opens to the cup's width where it waits (out of the camera's view),
    # then goes to the cup in one smooth motion without stopping: through LEFT_READY
    # and above the cup (both blended), then straight down to the grip height (stop).
    if aperture is None:
        plan.grip("left_open", "left", "open")
    else:
        position = round(min(max((aperture - LEFT_GAP_CLOSED) / 2, 0), 25), 1)
        plan.grip("left_open", "left", "move", position=position)
    plan.path("approach_cup", "left",
              [(LEFT_READY, LEFT_DOWN, plan.speed, False),
               (above, rotation, plan.speed, False),
               (pick, rotation, pick_speed, True)], move_speed=plan.speed,
              after=("right_home_start",) if home_after_cup else ())
    plan.grip("grip_cup", "left", "grip")
    plan.path("right_via", "right", [(R_VIA, RIGHT_UP, receive_speed, False)],
              move_speed=receive_speed, after=("left_open",))

    # This is the smooth source plan's pick-group barrier: neither arm begins
    # the handover approach until both picking and right preparation are DONE.
    plan.path("left_handover", "left",
              [(above, rotation, pick_speed, True),
               (HANDOVER_L, LEFT_DOWN, pick_speed, False)],
              move_speed=pick_speed, after=("right_via",), **carrying)
    plan.path("right_beside", "right",
              [(beside, RIGHT_UP, receive_speed, False)],
              move_speed=receive_speed, after=("grip_cup",))
    plan.grip("right_open", "right", "open", after=("left_handover",))
    plan.path("right_take", "right",
              [(below, RIGHT_UP, min(receive_speed, SLIDE_SPEED), True),
               (take, RIGHT_UP, receive_speed, True)],
              move_speed=receive_speed, zone=RECEIVING_BLEND_MM)
    plan.grip("right_grip", "right", "grip")
    plan.grip("left_release", "left", "open", after=("right_grip",))
    # 1. Apart: the left goes straight up out of the cup (slowly, its jaws are
    #    still around it) while the right takes the cup down out of the jaws.
    left_clear = (HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT)
    plan.path("left_clear", "left", [(left_clear, LEFT_DOWN, pick_speed, True)],
              move_speed=pick_speed)
    plan.path("right_lower", "right", [(below, RIGHT_UP, receive_speed, True)],
              move_speed=receive_speed, after=("left_release",), **carrying)
    # 2. The right turns the cup over only once the left is up.
    plan.path("right_retreat", "right",
              [(turn_half, RIGHT_SIDE, flip_speed, False),
               (place_above, RIGHT_DOWN, flip_speed, False)],
              move_speed=flip_speed, acceleration=FLIP_ACCELERATION,
              after=("left_clear",), **carrying)
    # 3. After the turnover the left moves on: to LEFT_PARK (out of the camera's
    #    view) between cups, or home.
    if home_after_cup:
        plan.return_home("left", left_clear, LEFT_DOWN, after=("right_retreat",))
    else:
        plan.path("left_park", "left", [(LEFT_PARK, LEFT_DOWN, plan.speed, False)],
                  move_speed=plan.speed, after=("right_retreat",))
    plan.path("right_place", "right", [(spot, RIGHT_DOWN, place_speed, True)],
              move_speed=place_speed, **carrying)
    # plan.grip("right_release", "right", "open")
    plan.grip("right_release", "right", "move", position = RIGHT_RELEASE_POS)
    plan.path("right_clear", "right", [(place_above, RIGHT_DOWN, place_speed, True)],
              move_speed=place_speed)
    plan.grip("right_open_after_clear", "right", "open")
    if home_after_cup:
        plan.home("right_home", "right")
    elif right_park is not None:
        position = tuple(right_park[axis] for axis in ("x", "y", "z"))
        rotation = tuple(right_park[axis] for axis in ("rx", "ry", "rz"))
        plan.path("right_park", "right", [(position, rotation, place_speed, False)],
                  move_speed=place_speed, zone=0)
    return plan.jobs


def right_camera_park_jobs(*, speed=None, phase_speeds=None):
    """Finish an inherited, fully placed cup at the same right camera target.

    The caller must confirm the previous queue completed placement and left
    parking. This is the normal right_clear-to-right_park transition, never Home.
    """
    plan = _Plan(speed, None, phase_speeds=phase_speeds)
    place_speed = plan.phase_speeds["placement"]
    pose = right_camera_park_pose()
    position = tuple(pose[axis] for axis in ("x", "y", "z"))
    rotation = tuple(pose[axis] for axis in ("rx", "ry", "rz"))
    plan.path("right_park", "right", [(position, rotation, place_speed, False)],
              move_speed=place_speed, zone=0)
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
