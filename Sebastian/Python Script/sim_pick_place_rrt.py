import argparse
import math
import random

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.patches import Patch
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

# ================= SCENE (mm, robot base frame, tray floor at z = 0) =================
# Tray inner area = camera field of view = where cups stand and where the TCP may go
TRAY_LO = (220, 70)          # inner x_min, y_min
TRAY_HI = (430, 330)         # inner x_max, y_max
RIM_H = 40
RIM_T = 8

CUP_R = 22                   # SmartGripper opens ~50 mm, so cups must be < ~45 mm wide
CUP_H = 80
BOTTLE_R = 30
BOTTLE_H = 220

HOME = (325, 200, 300)       # start position, above the tray
APPROACH = 100               # approach height above grasp point
Z_MIN, Z_MAX = 60, 320       # TCP height limits while planning
CAMERA_POS = (325, 200, 450)

# Collision model while moving between points
GRIPPER_R = 30               # horizontal radius of the open gripper
GRIPPER_BELOW = 10           # how far fingertips reach below the TCP (empty gripper)
CARRY_R = 30                 # horizontal radius of gripper + carried cup
CARRY_BELOW = CUP_H / 2      # carried cup hangs this far below the TCP
CLEARANCE = 15               # safety distance while travelling

# Straight up/down at pick and place: only the cup/fingers are near the tray
VERT_R = CUP_R
VERT_CLEARANCE = 5

DEFAULT_PICK = (260, 115)
DEFAULT_PLACE = (390, 295)

# Other objects on the tray: (x, y, radius, height, name)
OBJECTS = [
    (250, 215, CUP_R, CUP_H, "cup"),
    (340, 130, CUP_R, CUP_H, "cup"),
    (400, 180, CUP_R, CUP_H, "cup"),
    (300, 290, CUP_R, CUP_H, "cup"),
    (305, 185, BOTTLE_R, BOTTLE_H, "bottle"),
    (350, 225, BOTTLE_R, BOTTLE_H, "bottle"),
]


# ================= OBSTACLES =================
# A moving TCP is checked as a vertical "body": horizontal radius rc, reaching hc below
# the TCP. The gripper sits above the TCP, so checking the lowest point is enough.
class Cylinder:
    def __init__(self, x, y, r, h, name=""):
        self.x, self.y, self.r, self.h, self.name = x, y, r, h, name

    def describe(self):
        return f"{self.name} at ({self.x}, {self.y})"

    def collides(self, pts, rc, hc, c):
        dxy = np.hypot(pts[:, 0] - self.x, pts[:, 1] - self.y)
        return (dxy < self.r + rc + c) & (pts[:, 2] - hc < self.h + c)


class Box:
    def __init__(self, lo, hi, name=""):
        self.lo = np.array(lo, float)
        self.hi = np.array(hi, float)
        self.name = name

    def describe(self):
        return "tray rim"

    def collides(self, pts, rc, hc, c):
        m = rc + c
        return ((pts[:, 0] > self.lo[0] - m) & (pts[:, 0] < self.hi[0] + m) &
                (pts[:, 1] > self.lo[1] - m) & (pts[:, 1] < self.hi[1] + m) &
                (pts[:, 2] - hc < self.hi[2] + c))


def make_rim():
    x0, y0 = TRAY_LO
    x1, y1 = TRAY_HI
    t, h = RIM_T, RIM_H
    return [
        Box([x0 - t, y0 - t, 0], [x0, y1 + t, h], "rim"),
        Box([x1, y0 - t, 0], [x1 + t, y1 + t, h], "rim"),
        Box([x0, y0 - t, 0], [x1, y0, h], "rim"),
        Box([x0, y1, 0], [x1, y1 + t, h], "rim"),
    ]


def make_obstacles():
    return make_rim() + [Cylinder(*o) for o in OBJECTS]


def first_hit(a, b, obstacles, rc, hc, c, res=5.0):
    """Returns the first obstacle the segment a->b hits, or None if clear."""
    n = max(2, int(math.ceil(np.linalg.norm(b - a) / res)) + 1)
    pts = a + np.outer(np.linspace(0, 1, n), b - a)
    for ob in obstacles:
        if np.any(ob.collides(pts, rc, hc, c)):
            return ob
    return None


# ================= RRT* =================
class RRTStar:
    def __init__(self, start, goal, lo, hi, obstacles, rc, hc, c,
                 step=40.0, goal_bias=0.1, goal_tol=40.0, max_iter=3000,
                 gamma=400.0, r_max=120.0):
        self.start = np.array(start, float)
        self.goal = np.array(goal, float)
        self.lo = np.array(lo, float)
        self.hi = np.array(hi, float)
        self.obstacles = obstacles
        self.rc, self.hc, self.c = rc, hc, c
        self.step = step
        self.goal_bias = goal_bias
        self.goal_tol = goal_tol
        self.max_iter = max_iter
        self.gamma = gamma
        self.r_max = r_max

        self.P = np.zeros((max_iter + 1, 3))
        self.P[0] = self.start
        self.n = 1
        self.parent = [-1]
        self.cost = [0.0]
        self.children = [[]]
        self.goal_candidates = []

    def in_bounds(self, p):
        return np.all(p >= self.lo) and np.all(p <= self.hi)

    def edge_free(self, a, b):
        return (self.in_bounds(a) and self.in_bounds(b) and
                first_hit(a, b, self.obstacles, self.rc, self.hc, self.c) is None)

    def check_endpoint(self, p, name):
        if not self.in_bounds(p):
            print(f"  {name} {np.round(p, 1)} is outside the planning bounds.")
            return False
        hit = first_hit(p, p, self.obstacles, self.rc, self.hc, self.c)
        if hit is not None:
            print(f"  {name} {np.round(p, 1)} is too close to the {hit.describe()}.")
            return False
        return True

    def sample(self):
        if random.random() < self.goal_bias:
            return self.goal.copy()
        return np.random.uniform(self.lo, self.hi)

    def steer(self, a, b):
        d = np.linalg.norm(b - a)
        if d <= self.step:
            return b.copy()
        return a + (b - a) / d * self.step

    def near(self, p):
        n = self.n
        r = self.gamma * (math.log(n + 1) / (n + 1)) ** (1 / 3)
        r = max(min(r, self.r_max), self.step)
        d = np.linalg.norm(self.P[:n] - p, axis=1)
        return np.where(d <= r)[0], d

    def add_node(self, p, parent, cost):
        i = self.n
        self.P[i] = p
        self.parent.append(parent)
        self.cost.append(cost)
        self.children.append([])
        self.children[parent].append(i)
        self.n += 1
        return i

    def propagate_cost(self, i):
        stack = [i]
        while stack:
            k = stack.pop()
            for c in self.children[k]:
                self.cost[c] = self.cost[k] + np.linalg.norm(self.P[c] - self.P[k])
                stack.append(c)

    def plan(self):
        ok_start = self.check_endpoint(self.start, "Start")
        ok_goal = self.check_endpoint(self.goal, "Goal")
        if not (ok_start and ok_goal):
            return None

        for _ in range(self.max_iter):
            q = self.sample()
            i_nearest = int(np.argmin(np.linalg.norm(self.P[:self.n] - q, axis=1)))
            p_new = self.steer(self.P[i_nearest], q)
            if not self.edge_free(self.P[i_nearest], p_new):
                continue

            idx, d = self.near(p_new)

            best_parent = i_nearest
            best_cost = self.cost[i_nearest] + np.linalg.norm(p_new - self.P[i_nearest])
            for j in idx:
                c = self.cost[j] + d[j]
                if c < best_cost and self.edge_free(self.P[j], p_new):
                    best_parent, best_cost = j, c

            new = self.add_node(p_new, best_parent, best_cost)

            for j in idx:
                if j == best_parent:
                    continue
                c = best_cost + d[j]
                if c < self.cost[j] and self.edge_free(p_new, self.P[j]):
                    self.children[self.parent[j]].remove(j)
                    self.parent[j] = new
                    self.children[new].append(j)
                    self.cost[j] = c
                    self.propagate_cost(j)

            if np.linalg.norm(self.goal - p_new) <= self.goal_tol and self.edge_free(p_new, self.goal):
                self.goal_candidates.append(new)

        if not self.goal_candidates:
            return None

        best = min(self.goal_candidates,
                   key=lambda i: self.cost[i] + np.linalg.norm(self.goal - self.P[i]))
        path = []
        k = best
        while k != -1:
            path.append(self.P[k].copy())
            k = self.parent[k]
        path.reverse()
        if np.linalg.norm(path[-1] - self.goal) > 1e-6:
            path.append(self.goal.copy())
        return path


def shortcut(path, edge_free):
    out = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1 and not edge_free(path[i], path[j]):
            j -= 1
        out.append(path[j])
        i = j
    return out


def path_length(path):
    return sum(np.linalg.norm(b - a) for a, b in zip(path[:-1], path[1:]))


def plan_leg(name, start, goal, obstacles, rc, hc, iters):
    print(f"\nPlanning: {name}")
    lo = [TRAY_LO[0], TRAY_LO[1], Z_MIN]
    hi = [TRAY_HI[0], TRAY_HI[1], Z_MAX]
    planner = RRTStar(start, goal, lo, hi, obstacles, rc, hc, CLEARANCE, max_iter=iters)
    raw = planner.plan()
    if raw is None:
        print("  No path found. Try more --iters or other positions.")
        return None
    smooth = shortcut(raw, planner.edge_free)
    print(f"  Tree nodes: {planner.n}")
    print(f"  RRT* path: {len(raw)} points, {path_length(raw):.1f} mm")
    print(f"  Shortcut:  {len(smooth)} points, {path_length(smooth):.1f} mm")
    return planner, raw, smooth


# ================= VALIDATION =================
def validate(pick, place, obstacles):
    problems = []
    for name, p in (("Pick", pick), ("Place", place)):
        if not (TRAY_LO[0] + CUP_R <= p[0] <= TRAY_HI[0] - CUP_R and
                TRAY_LO[1] + CUP_R <= p[1] <= TRAY_HI[1] - CUP_R):
            problems.append(f"{name} spot is outside the tray / camera view.")
    for o in OBJECTS:
        if math.hypot(pick[0] - o[0], pick[1] - o[1]) < CUP_R + o[2]:
            problems.append(f"Pick spot overlaps a {o[4]} at ({o[0]}, {o[1]}).")
        if math.hypot(place[0] - o[0], place[1] - o[1]) < CUP_R + o[2]:
            problems.append(f"Place spot overlaps a {o[4]} at ({o[0]}, {o[1]}).")

    # Straight down/up at pick and place
    hit = first_hit(pick, pick + [0, 0, APPROACH], obstacles, VERT_R, CUP_H / 2, VERT_CLEARANCE)
    if hit is not None:
        problems.append(f"Straight lift at pick hits the {hit.describe()}.")
    hit = first_hit(place + [0, 0, APPROACH], place, obstacles, VERT_R, CUP_H / 2, VERT_CLEARANCE)
    if hit is not None:
        problems.append(f"Straight lowering at place hits the {hit.describe()}.")
    return problems


# ================= SEQUENCE =================
def interp(a, b, res=5.0):
    n = max(2, int(math.ceil(np.linalg.norm(b - a) / res)) + 1)
    return [a + (b - a) * t for t in np.linspace(0, 1, n)]


def build_sequence(home, pick, place, path_to_pick, path_to_place, hold=15):
    """List of (tcp_pos, cup_center, status) frames."""
    frames = []
    cup = pick.copy()
    pick_app = pick + [0, 0, APPROACH]
    place_app = place + [0, 0, APPROACH]

    def seg(a, b, carry, label):
        nonlocal cup
        for p in interp(a, b)[1:]:
            if carry:
                cup = p.copy()
            frames.append((p.copy(), cup.copy(), label))

    for _ in range(hold):
        frames.append((home.copy(), cup.copy(), "Start position"))
    for a, b in zip(path_to_pick[:-1], path_to_pick[1:]):
        seg(a, b, False, "RRT* to pick (empty)")
    seg(pick_app, pick, False, "MoveL down to cup")
    for _ in range(hold):
        frames.append((pick.copy(), cup.copy(), "g_GripIn"))
    seg(pick, pick_app, True, "MoveL up")
    for a, b in zip(path_to_place[:-1], path_to_place[1:]):
        seg(a, b, True, "RRT* to place (carrying)")
    seg(place_app, place, True, "MoveL down to place")
    for _ in range(hold):
        frames.append((place.copy(), cup.copy(), "g_GripOut"))
    seg(place, place_app, False, "MoveL up, done")
    return frames


# ================= DRAWING =================
def box_faces(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
         [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4],
             [2, 3, 7, 6], [1, 2, 6, 5], [0, 3, 7, 4]]
    return [[v[i] for i in f] for f in faces]


def draw_cylinder(ax, x, y, r, z0, z1, color, alpha=0.6, n=24):
    th = np.linspace(0, 2 * np.pi, n)
    TH, ZZ = np.meshgrid(th, [z0, z1])
    side = ax.plot_surface(x + r * np.cos(TH), y + r * np.sin(TH), ZZ,
                           color=color, alpha=alpha, linewidth=0, shade=True)
    top = Poly3DCollection([[(x + r * np.cos(t), y + r * np.sin(t), z1) for t in th]],
                           facecolor=color, alpha=alpha)
    ax.add_collection3d(top)
    return [side, top]


def draw_tree(ax, planner, color):
    segs = [[planner.P[planner.parent[i]], planner.P[i]] for i in range(1, planner.n)]
    ax.add_collection3d(Line3DCollection(segs, colors=color, linewidths=0.3, alpha=0.5))


def animate(legs, frames, home, pick, place, obstacles, save=None):
    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(projection="3d")

    # Tray floor + rim
    x0, y0 = TRAY_LO
    x1, y1 = TRAY_HI
    floor = [[(x0 - RIM_T, y0 - RIM_T, 0), (x1 + RIM_T, y0 - RIM_T, 0),
              (x1 + RIM_T, y1 + RIM_T, 0), (x0 - RIM_T, y1 + RIM_T, 0)]]
    ax.add_collection3d(Poly3DCollection(floor, facecolor="lightgray", alpha=0.4))
    for ob in obstacles:
        if isinstance(ob, Box):
            ax.add_collection3d(Poly3DCollection(box_faces(ob.lo, ob.hi),
                                                 facecolor="dimgray", alpha=0.5,
                                                 edgecolor="k", lw=0.3))

    # Other cups and bottles
    for x, y, r, h, name in OBJECTS:
        draw_cylinder(ax, x, y, r, 0, h, "lightskyblue" if name == "cup" else "seagreen", 0.55)

    # Camera and field of view (= tray)
    cx, cy, cz = CAMERA_POS
    ax.scatter(cx, cy, cz, color="k", marker="s", s=60)
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ax.plot([cx, px], [cy, py], [cz, RIM_H], "k:", lw=0.8)

    # Planner output for both legs
    (p1, raw1, sm1), (p2, raw2, sm2) = legs
    draw_tree(ax, p1, "lightsteelblue")
    draw_tree(ax, p2, "silver")
    r1, s1 = np.array(raw1), np.array(sm1)
    r2, s2 = np.array(raw2), np.array(sm2)
    ax.plot(r1[:, 0], r1[:, 1], r1[:, 2], color="tab:cyan", lw=1, alpha=0.6)
    ax.plot(s1[:, 0], s1[:, 1], s1[:, 2], "o--", color="tab:blue", lw=1.5, label="Path to pick (empty)")
    ax.plot(r2[:, 0], r2[:, 1], r2[:, 2], color="lightgreen", lw=1, alpha=0.6)
    ax.plot(s2[:, 0], s2[:, 1], s2[:, 2], "o--", color="tab:green", lw=1.5, label="Path to place (carrying)")
    ax.scatter(*home, color="tab:red", marker="^", s=80, label="Start position")
    ax.scatter(pick[0], pick[1], 1, color="k", marker="x", s=80)
    ax.scatter(place[0], place[1], 1, color="gold", marker="*", s=150)

    # Moving parts
    T = np.array([f[0] for f in frames])
    trail, = ax.plot([], [], [], color="tab:purple", lw=2, label="TCP trail")
    gripper, = ax.plot([], [], [], color="black", lw=6)
    status = ax.text2D(0.02, 0.95, "", transform=ax.transAxes, fontsize=12)
    cup_artists = []

    ax.set_xlim(x0 - 30, x1 + 30)
    ax.set_ylim(y0 - 30, y1 + 30)
    ax.set_zlim(0, CAMERA_POS[2] + 20)
    ax.set_box_aspect((x1 - x0 + 60, y1 - y0 + 60, CAMERA_POS[2] + 20))
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_zlabel("Z [mm]")

    handles, _ = ax.get_legend_handles_labels()
    handles += [Patch(color="orange", label="Target cup"),
                Patch(color="lightskyblue", label="Other cups"),
                Patch(color="seagreen", label="Bottles"),
                Patch(color="dimgray", label="Tray rim")]
    ax.legend(handles=handles, loc="upper right", fontsize=8)

    def update(i):
        nonlocal cup_artists
        tcp, cup, label = frames[i]
        for a in cup_artists:
            a.remove()
        cup_artists = draw_cylinder(ax, cup[0], cup[1], CUP_R,
                                    cup[2] - CUP_H / 2, cup[2] + CUP_H / 2, "orange", 0.9)
        trail.set_data_3d(T[:i + 1, 0], T[:i + 1, 1], T[:i + 1, 2])
        gripper.set_data_3d([tcp[0], tcp[0]], [tcp[1], tcp[1]], [tcp[2] + 10, tcp[2] + 90])
        status.set_text(label)
        return []

    anim = FuncAnimation(fig, update, frames=len(frames), interval=20, blit=False, repeat=False)
    if save:
        anim.save(save, writer="pillow", fps=30)
        print(f"Saved animation to {save}")
    plt.show()
    return anim


# ================= MAIN =================
def main():
    ap = argparse.ArgumentParser(description="Python-only sim: start -> pick -> place with RRT* on a tray")
    ap.add_argument("--home", nargs=3, type=float, default=list(HOME), metavar=("X", "Y", "Z"))
    ap.add_argument("--pick", nargs=2, type=float, default=list(DEFAULT_PICK), metavar=("X", "Y"))
    ap.add_argument("--place", nargs=2, type=float, default=list(DEFAULT_PLACE), metavar=("X", "Y"))
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--save", default=None, help="Save animation as GIF, e.g. --save run.gif")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    obstacles = make_obstacles()
    home = np.array(args.home, float)
    pick = np.array([args.pick[0], args.pick[1], CUP_H / 2])     # grip at mid-height
    place = np.array([args.place[0], args.place[1], CUP_H / 2])
    pick_app = pick + [0, 0, APPROACH]
    place_app = place + [0, 0, APPROACH]

    problems = validate(pick, place, obstacles)
    if problems:
        print("Scene problems:")
        for p in problems:
            print("  -", p)
        return

    leg1 = plan_leg("start -> above pick (empty gripper)", home, pick_app,
                    obstacles, GRIPPER_R, GRIPPER_BELOW, args.iters)
    if leg1 is None:
        return
    leg2 = plan_leg("above pick -> above place (carrying cup)", pick_app, place_app,
                    obstacles, CARRY_R, CARRY_BELOW, args.iters)
    if leg2 is None:
        return

    print("\nMessages the robot would get:")
    for leg in (leg1, leg2):
        smooth = leg[2]
        for i, p in enumerate(smooth[1:], start=1):
            cmd = "WAYEND" if i == len(smooth) - 1 else "WAYPOINT"
            print(f"  {cmd},{p[0]:.1f},{p[1]:.1f},{p[2]:.1f}")
        if leg is leg1:
            print(f"  PICK,{pick[0]:.1f},{pick[1]:.1f},{pick[2]:.1f}")
    print(f"  PLACE,{place[0]:.1f},{place[1]:.1f},{place[2]:.1f}")

    frames = build_sequence(home, pick, place, leg1[2], leg2[2])
    animate([leg1, leg2], frames, home, pick, place, obstacles, save=args.save)


if __name__ == "__main__":
    main()