"""Pick/handover/place plans using the project's tuned RWS poses (classic: serial; smooth: path jobs, some together).

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


# ---------------- Smooth version: path jobs ----------------
# Uses the poses, heights and speeds of the real runs (capture_and_detect.py),
# not the older constants above (which the classic plan keeps). Every stretch
# between two gripper actions is one /robot/rws/path job: the arm blends through
# the middle points and only stops where it must (a grip/release, a home, and
# LEFT_READY, where the cup is observed again before the approach). Straight
# lines (MoveL) near the cup, joint moves for travel.
#
# The arms work at the same time where they cannot meet (jobs with the same
# "group" run together, one lane per arm, see RobotClient.execute_jobs), as the
# real-run program already did with its two threads:
#   - while the left approaches and grips, the right goes to R_VIA (x 300,
#     outside the cup area); not before: the recheck at LEFT_READY needs the
#     right arm out of the camera view
#   - while the left brings the cup to the handover, the right waits beside it
#     (R_BESIDE), away from the left arm
#   - once the left holds still, the right opens, slides in under the cup and
#     goes straight up to the rim in one motion, and grips; the left lets go;
#     together the right pulls the cup down and the left goes up to its top
#     (70 mm apart, as in the real runs)
#   - the right turns the cup over (two steps, slowly) while the left waits at
#     its top: its way out could cross the right arm during the turn (the real
#     runs waited too); then the right places while the left goes on
# Grippers are closed while travelling, as in the real runs.
# With more_cups the left arm does not go home after the handover: it stays
# lifted above the handover (its tested top), out of the camera view.

# Poses from the real runs: capture_and_detect.py (tool0 = flange, mm).
RUN_PICK_Z = 70            # TCP height when gripping a cup
RUN_TRAVEL_Z = 175         # TCP height above the cups
RUN_Z_MAX = 700            # flange z limit set on the Pi (GATEWAY_RWS_SAFE_ZONE_MAX)
RUN_LEFT_TOP = 690         # highest the left reaches over the handover (tested)
RUN_HANDOVER_L = (350, -100, RUN_LEFT_TOP - LEFT_LIFT)
RUN_LEFT_CLEAR = (RUN_HANDOVER_L[0], RUN_HANDOVER_L[1], RUN_LEFT_TOP)
RUN_R_VIA = (300, -300, 250)   # (200, -250, 200) hit the tray with the elbow
RUN_R_TAKE = (RUN_HANDOVER_L[0] - 20, RUN_HANDOVER_L[1] - 15, RUN_HANDOVER_L[2] - 339)
RUN_R_BELOW = (RUN_R_TAKE[0], RUN_R_TAKE[1], RUN_R_TAKE[2] - 35)
RUN_R_BESIDE = (RUN_R_BELOW[0], RUN_R_BELOW[1] - 120, RUN_R_BELOW[2])
RUN_PLACE = (350, -200, 230)   # spot 1 of placing_grid.json
PLACE_ABOVE = 80           # mm above the place spot: the cup is turned over here
RIGHT_SIDE = (90, 0, 0)    # right gripper sideways: halfway through turning the cup over
FLIP_SPEED = 100           # mm/s, turning the (heavy) cup over
FLIP_ACCELERATION = 30     # %
RUN_FLANGE_MIN = (180, -400, 50)
RUN_FLANGE_MAX = (800, 500, RUN_Z_MAX)

TRAVEL_SPEED = 250         # mm/s, empty arm (the real runs' SPEED)
CARRY_SPEED = 100          # mm/s, holding the cup (the real runs' PICK_SPEED)
SMOOTH_ACCELERATION = 60   # % (AccSet), softer starts and stops than 100
SMOOTH_RAMP = 40           # % (AccSet ramp), less jerk
CARRY_MAX_ACC = 1.5        # m/s2 (PathAccLim) while holding the cup
TRAVEL_ZONE = 20           # mm blend radius on free travel
NEAR_ZONE = 10             # mm blend radius where the next stretch is close to something
TIGHT_ZONE = 3             # mm, the corner right under the cup (slide in, then up)


def _speed_problem(name, value):
    if not finite_number(value) or not 1 <= value <= 1500:
        return f"{name} must be a finite number from 1 to 1500 mm/s"
    return None


def _run_flange_problem(position):
    if not all(finite_number(value) for value in position):
        return "invalid flange coordinates"
    if not all(low <= value <= high for value, low, high in zip(position, RUN_FLANGE_MIN, RUN_FLANGE_MAX)):
        return "flange target is outside the workspace of the real runs"
    return None


class _SmoothPlan:
    """Builds smooth jobs; every step name gets ``suffix`` (e.g. "#2" in a batch)."""

    def __init__(self, *, speed, travel_speed, carry_speed, suffix=""):
        for name, value in (("speed", speed), ("travel_speed", travel_speed),
                            ("carry_speed", carry_speed)):
            reason = _speed_problem(name, value)
            if reason:
                raise SelectionError(reason)
        self.speed = speed
        self.travel_speed = travel_speed
        self.carry_speed = carry_speed
        self.suffix = suffix
        self.jobs = []
        self.motion = {"acceleration": SMOOTH_ACCELERATION, "ramp": SMOOTH_RAMP}

    def point(self, name, position, rotation, *, linear=False, speed=None, zone=None):
        reason = _run_flange_problem(position)
        if reason:
            raise SelectionError(f"{name}: {reason}")
        p = {**dict(zip(("x", "y", "z"), position)), **dict(zip(("rx", "ry", "rz"), rotation)),
             "linear": linear}
        if speed is not None:
            p["speed"] = speed
        if zone is not None:
            p["zone"] = zone
        return p

    def add(self, name, endpoint, body, group):
        job = {"name": name + self.suffix, "endpoint": endpoint, "body": body}
        if group is not None:
            job["group"] = group
        self.jobs.append(job)

    def path(self, name, arm, *points, carrying=False, group=None, speed=None, acceleration=None):
        body = {"arm": arm, "wait": True, "points": list(points), "zone": NEAR_ZONE,
                "speed": speed or (self.carry_speed if carrying else self.travel_speed), **self.motion}
        if acceleration is not None:
            body["acceleration"] = acceleration
        if carrying:
            body["max_acceleration"] = CARRY_MAX_ACC
        self.add(name, "/robot/rws/path", body, group)

    def home(self, name, arm, group=None):
        self.add(name, "/robot/rws/home",
                 {"arm": arm, "wait": True, "speed": self.travel_speed, **self.motion}, group)

    def grip(self, name, arm, action, group=None):
        self.add(name, "/robot/rws/gripper", {"arm": arm, "wait": True, "action": action}, group)

    # ---- steps ----
    def left_ready(self, group=None):
        # Stops here on purpose: the cup is observed again before the approach.
        self.path("left_ready", "left", self.point("left_ready", LEFT_READY, LEFT_DOWN), group=group)
        self.grip("left_open", "left", "open", group=group)

    def approach(self, above, pick, group=None):
        self.path("approach_cup", "left",
                  self.point("above_cup", above, LEFT_DOWN, zone=NEAR_ZONE),
                  self.point("descend_to_cup", pick, LEFT_DOWN, linear=True, speed=self.speed),
                  group=group)
        self.grip("grip_cup", "left", "grip", group=group)

    def right_to_via(self, group=None):
        self.path("right_to_via", "right", self.point("right_via", RUN_R_VIA, RIGHT_UP), group=group)

    def lift(self, above, group=None):
        # Up with the cup, then to the handover, as in the real runs.
        self.path("lift_to_handover", "left",
                  self.point("lift_cup", above, LEFT_DOWN, linear=True, zone=NEAR_ZONE),
                  self.point("left_handover", RUN_HANDOVER_L, LEFT_DOWN),
                  carrying=True, group=group)

    def right_to_beside(self, group=None, *, via=False):
        # Waits beside the handover, away from the left arm, until the left holds still.
        points = [self.point("right_via", RUN_R_VIA, RIGHT_UP, zone=TRAVEL_ZONE)] if via else []
        points.append(self.point("right_beside", RUN_R_BESIDE, RIGHT_UP))
        self.path("right_to_beside", "right", *points, group=group)

    def handover(self, place):
        # Only once the left holds still at the handover: the right opens, slides
        # in under the cup and goes straight up to the rim, in one motion.
        # The left hand holds the cup until the right hand has finished gripping it.
        self.grip("right_open", "right", "open")
        self.path("right_take", "right",
                  self.point("right_below", RUN_R_BELOW, RIGHT_UP, linear=True, speed=self.speed,
                             zone=TIGHT_ZONE),
                  self.point("right_take", RUN_R_TAKE, RIGHT_UP, linear=True, speed=self.speed))
        self.grip("right_grip", "right", "grip")
        self.grip("left_release", "left", "open")
        # Together, straight apart: the right pulls the cup down, the left goes up
        # to its top (the real runs' LEFT_LIFT); 70 mm between hand and cup, as in
        # the real runs, before the cup is turned.
        release = "release" + self.suffix
        self.path("right_lower", "right",
                  self.point("right_lower", RUN_R_BELOW, RIGHT_UP, linear=True, speed=self.speed),
                  carrying=True, group=release)
        self.left_clear(group=release)
        # The left waits at its top while the right turns the cup over: its way out
        # could cross the right arm during the turn (the real runs waited too).
        self.right_retreat(place)

    def left_clear(self, group=None):
        self.path("left_clear", "left",
                  self.point("left_clear", RUN_LEFT_CLEAR, LEFT_DOWN, linear=True, speed=self.speed),
                  group=group)

    def right_retreat(self, place, group=None):
        # Turn the cup over slowly and in two steps (the cup is heavy for the right arm).
        above = (place[0], place[1], place[2] + PLACE_ABOVE)
        half = tuple((a + b) / 2 for a, b in zip(RUN_R_BELOW, above))
        self.path("right_retreat", "right",
                  self.point("right_turn_half", half, RIGHT_SIDE, zone=NEAR_ZONE),
                  self.point("right_retreat", above, RIGHT_DOWN),
                  carrying=True, group=group, speed=FLIP_SPEED, acceleration=FLIP_ACCELERATION)

    def right_place(self, place, group=None, *, home=True):
        above = (place[0], place[1], place[2] + PLACE_ABOVE)
        self.path("right_place", "right",
                  self.point("right_place", tuple(place), RIGHT_DOWN, linear=True, speed=self.speed),
                  carrying=True, group=group)
        self.grip("right_release", "right", "open", group=group)
        self.path("right_clear", "right",
                  self.point("right_clear", above, RIGHT_DOWN, linear=True, speed=self.speed),
                  group=group)
        self.grip("right_close", "right", "grip", group=group)   # closed on the way home
        if home:
            self.home("right_home", "right", group=group)

    def left_home(self, group=None):
        self.grip("left_close", "left", "grip", group=group)     # closed on the way home
        self.home("left_home", "left", group=group)

    def finish(self, place, *, more_cups):
        """After the turn: the right places the cup; the left waits or goes home."""
        if more_cups:
            # The left stays at its top, above the handover (out of the camera view).
            self.right_place(place)
        else:
            self.left_home(group="home" + self.suffix)
            self.right_place(place, group="home" + self.suffix, home=False)
            self.home("right_home", "right")


def _cup_targets(cup, tag_offset):
    reason = cup.get("blocked_reason") or pick_position_problem(cup, tag_offset)
    if cup.get("selectable") is False or reason:
        raise SelectionError(reason or "cup is not selectable")
    ox, oy, _ = normalize_offset(tag_offset)
    x, y = cup["x"] + ox, cup["y"] + oy
    return (x, y, RUN_TRAVEL_Z + GRIPPER_TCP_Z), (x, y, RUN_PICK_Z + GRIPPER_TCP_Z)


def _place_problem(place):
    try:
        place = tuple(place)
    except TypeError:
        return "a place spot is x, y, z"
    if len(place) != 3:
        return "a place spot is x, y, z"
    above = (place[0], place[1], place[2] + PLACE_ABOVE) if all(finite_number(v) for v in place) else place
    return _run_flange_problem(place) or _run_flange_problem(above)


def handover_paths(cup, *, tag_offset=(0, 0, 0), speed=50, travel_speed=TRAVEL_SPEED,
                   carry_speed=CARRY_SPEED, more_cups=False, place=RUN_PLACE):
    """Smooth ``{name, endpoint, body[, group]}`` jobs for one selected cup.

    ``speed`` is used for the last stretch to every grasp and release (as in
    handover_jobs), ``travel_speed`` for empty travel and homes,
    ``carry_speed`` for travel with the cup, which also has a TCP acceleration
    limit. ``more_cups``: leave the left arm lifted above the handover instead
    of sending it home. ``place``: flange x, y, z of the place spot. Needs the
    path variables in rapid/SharedVars.mod and the gateway's z limit at 700.
    """
    reason = _place_problem(place)
    if reason:
        raise SelectionError(f"place: {reason}")
    plan = _SmoothPlan(speed=speed, travel_speed=travel_speed, carry_speed=carry_speed)
    above, pick = _cup_targets(cup, tag_offset)
    plan.home("right_home_start", "right")
    plan.left_ready()
    # Together: the left approaches and grips, the right goes to R_VIA.
    plan.approach(above, pick, group="pick")
    plan.right_to_via(group="pick")
    # Together: the left brings the cup to the handover, the right waits beside it.
    plan.lift(above, group="lift")
    plan.right_to_beside(group="lift")
    plan.handover(tuple(place))
    plan.finish(tuple(place), more_cups=more_cups)
    return plan.jobs


# ---------------- Several cups in one go (simulation for now) ----------------
# Once the right has turned cup N over, the right places it while the left goes
# from its top to ready and picks cup N+1; the right then waits beside the
# handover for it instead of going home (only after the last cup it homes). The place spots are outside the camera view, so the recheck
# before the approach can run while the right arm is placing.
# Only for cups on the left half of the table: a cup with y below
# OVERLAP_MIN_Y is near the place spots, so for it the left arm only goes to
# ready and waits there until the right arm is home (R_BESIDE is next to
# those cups, so the right does not wait there). Tune in RobotStudio.
OVERLAP_MIN_Y = 0          # mm, robot y; cups at or above it are picked while the right places


def overlaps_placing(cup, tag_offset=(0, 0, 0)):
    """True if the left arm may pick this cup while the right arm is placing."""
    _, oy, _ = normalize_offset(tag_offset)
    return cup["y"] + oy >= OVERLAP_MIN_Y


def handover_batch(cups, *, tag_offset=(0, 0, 0), speed=50, travel_speed=TRAVEL_SPEED,
                   carry_speed=CARRY_SPEED, places=None):
    """One smooth plan for several cups, picked in the given order.

    ``places``: one place spot per cup (default RUN_PLACE for all). Step names
    end in "#<cup number>". Same steps and interlocks per cup as
    handover_paths; the left arm homes only after the last cup.
    """
    if not isinstance(cups, list) or not cups:
        raise SelectionError("Give at least one cup.")
    places = [RUN_PLACE] * len(cups) if places is None else list(places)
    if len(places) != len(cups):
        raise SelectionError("Give one place spot per cup.")
    for place in places:
        reason = _place_problem(place)
        if reason:
            raise SelectionError(f"place: {reason}")
    places = [tuple(place) for place in places]
    jobs = []
    plans = []
    for number, cup in enumerate(cups, 1):
        plan = _SmoothPlan(speed=speed, travel_speed=travel_speed, carry_speed=carry_speed,
                           suffix=f"#{number}")
        plan.jobs = jobs        # one job list, step names per cup
        plans.append(plan)
        above, pick = _cup_targets(cup, tag_offset)
        if number == 1:
            plan.home("right_home_start", "right")
            plan.left_ready()
            plan.approach(above, pick, group=f"pick#{number}")
            plan.right_to_via(group=f"pick#{number}")
            plan.lift(above, group=f"lift#{number}")
            plan.right_to_beside(group=f"lift#{number}")
        else:
            previous = plans[-2]
            together = f"next#{number}"
            if overlaps_placing(cup, tag_offset):
                # Right lane: the previous cup (already turned over) placed, then
                # not home but straight on to wait beside the handover for this cup.
                previous.right_place(places[number - 2], group=together, home=False)
                plan.right_to_beside(group=together)
                # Left lane: from its top to ready, the approach and the grip.
                plan.left_ready(group=together)
                plan.approach(above, pick, group=together)
                plan.lift(above)
            else:
                # The cup is near the place spots and R_BESIDE: the right goes home
                # and the left only goes to ready until the right is out of the way.
                previous.right_place(places[number - 2], group=together)
                plan.left_ready(group=together)
                plan.approach(above, pick, group=f"pick#{number}")
                plan.right_to_via(group=f"pick#{number}")
                plan.lift(above, group=f"lift#{number}")
                plan.right_to_beside(group=f"lift#{number}")
        plan.handover(places[number - 1])
    plans[-1].finish(places[-1], more_cups=False)
    return jobs
