"""Which cup to pick next, and at which gripper angle, so the jaws do not hit what is around it.

Pick order (next_cup)
    Every cup base gets a circle of CUP_RADIUS_MM around its centre (x, y). Two cups are
    neighbours when their circles overlap, i.e. their centres are closer than 2 * CUP_RADIUS_MM.
    The next cup is the "waiting" cup with the fewest neighbours still on the table; on a tie
    the earlier one in the list wins (the most certain detection, see find_cups).

Orientation (grip_angle) and both together (next_cup_and_grip)
    The two jaws sit on opposite sides of the cup, (cup diameter + margin) / 2 from its centre,
    along the jaw axis. For every axis angle from -90 to +89 degrees (1 degree steps; the jaws are
    interchangeable, so this covers all 360) the clearance is the smallest gap between a jaw and
    an obstacle:
        other cup:  distance(jaw, other cup's base centre) - other cup's radius - jaw radius
        tray edge:  distance(jaw, edge line, on the cup's side) - jaw radius
    The angle with the largest clearance wins (on a tie: the one closest to the preferred angle,
    the handle angle or 0). A handle angle is kept when it is safe. When no angle is safe at the
    full opening, the opening is made smaller, down to MIN_MARGIN_MM. clearance < 0 at every
    angle and opening = no safe grip: pick another cup first.
    next_cup_and_grip: fewest neighbours first, then the larger clearance, then list order;
    cups with no safe grip are skipped.

Angles: axis angle 0 = jaws close along robot X (LEFT_DOWN, rz 180), counter-clockwise seen
from above, as in set_grip_rotation in capture_and_detect.py; rz = LEFT_DOWN_RZ + angle.

Not used by capture_and_detect.py yet (only next_cup is).
"""

import math

CUP_RADIUS_MM = 50.0              # 5 cm around each cup base (neighbour count)

LEFT_DOWN_RZ = 180.0              # rz of LEFT_DOWN in capture_and_detect.py: jaws along robot X
MAX_MARGIN_MM = 70.0              # full opening = diameter + this (GRIP_MARGIN in capture_and_detect.py)
MIN_MARGIN_MM = 10.0              # smallest opening tried in a tight spot
MARGIN_STEP_MM = 5.0
JAW_RADIUS_MM = 10.0              # a jaw seen from above, as a circle (finger about 20 mm wide) - MEASURE
TAPER_MM = 10.0                   # other cups are wider at jaw height than at the base - MEASURE
DEFAULT_DIAMETER_MM = 75.0        # when a cup has no measured diameter
ANGLE_STEP_DEG = 1
MIN_GAP_MM = 5.0                  # a grip is safe when every gap is at least this


# ==== pick order ====

def count_neighbours(cup, cups, radius_mm=CUP_RADIUS_MM):
    """Number of other cups still on the table (not placed) whose circle overlaps this one."""
    return sum(1 for other in cups
               if other is not cup and other["status"] != "placed"
               and math.hypot(cup["x"] - other["x"], cup["y"] - other["y"]) < 2 * radius_mm)


def next_cup(cups, radius_mm=CUP_RADIUS_MM):
    """The waiting cup with the fewest neighbours, or None if no cup is waiting."""
    waiting = [c for c in cups if c["status"] == "waiting"]
    if not waiting:
        return None
    return min(waiting, key=lambda c: count_neighbours(c, cups, radius_mm))   # min keeps list order on ties


GONE_STATUSES = ("placed", "replaced")             # web app: these cups are no longer on the table


def _on_table(cup):
    """A cup still standing in the source area, with a usable robot x and y."""
    x, y = cup.get("x"), cup.get("y")
    return (cup.get("status") not in GONE_STATUSES and not cup.get("placement_location_ids")
            and isinstance(x, (int, float)) and isinstance(y, (int, float))
            and math.isfinite(x) and math.isfinite(y))


def web_neighbours(cup, cups, radius_mm=CUP_RADIUS_MM):
    """count_neighbours for the web app's inventory cups (other statuses, x/y may be missing)."""
    if not _on_table(cup):
        return 0
    return sum(1 for other in cups
               if other is not cup and _on_table(other)
               and math.hypot(cup["x"] - other["x"], cup["y"] - other["y"]) < 2 * radius_mm)


def sort_fewest_neighbours(candidates, cups, radius_mm=CUP_RADIUS_MM):
    """Web app (cup_commands.py): the pickable candidates, fewest neighbours first, then the
    most certain (confidence), then the lowest id. Neighbours are counted among all cups
    still on the table (cups), not only the candidates. Sorts the list in place."""
    candidates.sort(key=lambda cup: (web_neighbours(cup, cups, radius_mm),
                                     -(cup.get("confidence") or 0), cup["id"]))
    return candidates


# ==== orientation ====

def _diameter(cup):
    d = cup.get("diameter")
    return float(d) if d else DEFAULT_DIAMETER_MM


def _wall_lines(cup, walls):
    """Each wall ((x1, y1), (x2, y2)) as (a, b, c) with a*x + b*y + c = signed distance,
    positive on the cup's side."""
    lines = []
    for (x1, y1), (x2, y2) in walls:
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        a, b = -(y2 - y1) / length, (x2 - x1) / length
        c = -(a * x1 + b * y1)
        if a * cup["x"] + b * cup["y"] + c < 0:       # flip so the cup's side is positive
            a, b, c = -a, -b, -c
        lines.append((a, b, c))
    return lines


def clearance(cup, cups, angle_deg, margin_mm=MAX_MARGIN_MM, walls=()):
    """Smallest gap (mm) between either jaw and another cup still on the table or a tray edge,
    with the jaw axis at angle_deg and the jaws open to the cup's diameter + margin_mm."""
    reach = (_diameter(cup) + margin_mm) / 2 + JAW_RADIUS_MM      # cup centre -> jaw centre
    t = math.radians(angle_deg)
    others = [o for o in cups if o is not cup and o["status"] != "placed"]
    lines = _wall_lines(cup, walls)
    gap = math.inf
    for side in (1, -1):
        jx = cup["x"] + side * reach * math.cos(t)
        jy = cup["y"] + side * reach * math.sin(t)
        for o in others:
            gap = min(gap, math.hypot(jx - o["x"], jy - o["y"])
                      - (_diameter(o) + TAPER_MM) / 2 - JAW_RADIUS_MM)
        for a, b, c in lines:
            gap = min(gap, a * jx + b * jy + c - JAW_RADIUS_MM)
    return gap


def _angle_gap(a, b):
    """Difference between two jaw axis angles (degrees, the jaws are interchangeable): 0..90."""
    d = abs(a - b) % 180
    return min(d, 180 - d)


def grip_angle(cup, cups, walls=(), preferred_deg=None):
    """Best jaw axis angle for this cup.

    Returns {"angle", "rz", "clearance", "margin", "safe"}: angle in -90..89 degrees,
    rz = LEFT_DOWN_RZ + angle, clearance in mm at that margin. "safe" is False when no angle
    clears by MIN_GAP_MM even at MIN_MARGIN_MM; then the values are those of the best (still colliding) angle.
    preferred_deg: kept when safe (e.g. the handle angle), else the tie-breaker; default 0."""
    if preferred_deg is None:
        preferred_deg = 0.0
    preferred_deg = (preferred_deg + 90) % 180 - 90
    angles = range(-90, 90, ANGLE_STEP_DEG)
    best = None
    margin = MAX_MARGIN_MM
    while margin >= MIN_MARGIN_MM - 1e-9:
        if clearance(cup, cups, preferred_deg, margin, walls) >= MIN_GAP_MM:
            best = (preferred_deg, clearance(cup, cups, preferred_deg, margin, walls), margin)
            break
        gaps = [(clearance(cup, cups, a, margin, walls), a) for a in angles]
        top = max(g for g, _ in gaps)
        angle = min((a for g, a in gaps if g >= top - 1e-6), key=lambda a: _angle_gap(a, preferred_deg))
        if best is None or top > best[1]:
            best = (angle, top, margin)
        if top >= MIN_GAP_MM:
            best = (angle, top, margin)
            break
        margin -= MARGIN_STEP_MM
    angle, gap, margin = best
    gap = min(gap, 999.0)                          # nothing around it: no infinity in cups.json
    return {"angle": float(angle), "rz": round(LEFT_DOWN_RZ + angle, 1),
            "clearance": round(gap, 1), "margin": margin, "safe": gap >= MIN_GAP_MM}


def _preferred(cup):
    """Handle angle from set_grip_rotation (cup["rz"] when the cup has a handle), else None."""
    if cup.get("has_handle") and cup.get("rz") is not None:
        return cup["rz"] - LEFT_DOWN_RZ
    return None


# ==== both ====

def next_cup_and_grip(cups, walls=(), radius_mm=CUP_RADIUS_MM):
    """(cup, grip) for the next cup to pick, or (None, None).

    Fewest neighbours first, then the larger clearance, then list order (most certain);
    cups with no safe grip are skipped. grip is grip_angle()'s dict; the caller sets
    cup["rz"] = grip["rz"] and opens the jaws to diameter + grip["margin"]."""
    options = []
    for i, cup in enumerate(cups):
        if cup["status"] != "waiting":
            continue
        grip = grip_angle(cup, cups, walls, _preferred(cup))
        if grip["safe"]:
            options.append((count_neighbours(cup, cups, radius_mm), -grip["clearance"], i, cup, grip))
    if not options:
        return None, None
    _, _, _, cup, grip = min(options, key=lambda o: o[:3])
    return cup, grip
