"""
RRT* pick & place for YuMi (left arm). No collision checking yet (OBSTACLES is empty).

One command does everything:
  1. plans all jobs with RRT*
  2. shows a preview animation (nothing is sent)
  3. asks if you want to run it on the YuMi
  4. runs it, saves a log in runs/, and shows a replay of what the robot did

  python yumi_rrt_pick_place.py --job 260 115 390 295 --job 400 120 250 300

Replay a saved run later (no robot):
  python yumi_rrt_pick_place.py --replay runs/run_20260925_143000.json
"""
import argparse
import json
import math
import os
import random
import socket
import time

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

DEFAULT_HOST = "192.168.125.1"   # sim: 127.0.0.1
DEFAULT_PORT = 5001              # must match PORT in the RAPID module

# ================= WORKSPACE (mm, wobj0) =================
# Keep inside the RAPID safe zone (X 200-450, Y 50-350) and under the ceiling (423)
BOUNDS_LO = (200, 50, 60)
BOUNDS_HI = (450, 350, 400)
HOME = (325, 200, 250)           # start position above the work area
APPROACH = 100                   # must match APPROACH in RAPID
GRASP_Z = 40                     # grip height (cup mid-height)
CUP_R, CUP_H = 22, 80            # only used for drawing

DEFAULT_JOBS = [
    (260, 115, 390, 295),        # pick x, y  ->  place x, y
    (400, 120, 250, 300),
]

# ================= OBSTACLES (empty for now) =================
# Add later, e.g.  OBSTACLES = [Box([300, 180, 0], [340, 220, 200]), Cylinder(250, 215, 22, 80)]
CLEARANCE = 15
OBSTACLES = []


class Box:
    def __init__(self, lo, hi, name="box"):
        self.lo, self.hi, self.name = np.array(lo, float), np.array(hi, float), name

    def hits(self, pts, c):
        return bool(np.any(np.all((pts >= self.lo - c) & (pts <= self.hi + c), axis=1)))


class Cylinder:
    def __init__(self, x, y, r, h, name="cylinder"):
        self.x, self.y, self.r, self.h, self.name = x, y, r, h, name

    def hits(self, pts, c):
        d = np.hypot(pts[:, 0] - self.x, pts[:, 1] - self.y)
        return bool(np.any((d < self.r + c) & (pts[:, 2] < self.h + c)))


def segment_free(a, b, res=5.0):
    """Collision check for a straight TCP move. Always True while OBSTACLES is empty."""
    if not OBSTACLES:
        return True
    n = max(2, int(math.ceil(np.linalg.norm(b - a) / res)) + 1)
    pts = a + np.outer(np.linspace(0, 1, n), b - a)
    return not any(o.hits(pts, CLEARANCE) for o in OBSTACLES)


# ================= RRT* =================
class RRTStar:
    def __init__(self, start, goal, lo, hi, is_free, step=40.0, goal_bias=0.1,
                 goal_tol=40.0, max_iter=1500, gamma=400.0, r_max=120.0):
        self.start = np.array(start, float)
        self.goal = np.array(goal, float)
        self.lo = np.array(lo, float)
        self.hi = np.array(hi, float)
        self.is_free = is_free
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
        return bool(np.all(p >= self.lo) and np.all(p <= self.hi))

    def edge_free(self, a, b):
        return self.in_bounds(a) and self.in_bounds(b) and self.is_free(a, b)

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
        for name, p in (("Start", self.start), ("Goal", self.goal)):
            if not self.edge_free(p, p):
                print(f"  {name} {np.round(p, 1)} is outside the bounds or inside an obstacle.")
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
    return sum(np.linalg.norm(np.asarray(b) - np.asarray(a)) for a, b in zip(path[:-1], path[1:]))


# ================= PLANNING THE JOB QUEUE =================
def plan_path(a, b, iters, label, job):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if np.linalg.norm(b - a) < 1.0:
        return {"type": "path", "label": label, "job": job, "raw": [a], "points": [a], "planner": None}
    planner = RRTStar(a, b, BOUNDS_LO, BOUNDS_HI, segment_free, max_iter=iters)
    raw = planner.plan()
    if raw is None:
        print(f"  {label}: no path found. Try more --iters.")
        return None
    smooth = shortcut(raw, planner.edge_free)
    print(f"  {label}: RRT* {len(raw)} pts / {path_length(raw):.0f} mm, "
          f"shortcut {len(smooth)} pts / {path_length(smooth):.0f} mm")
    return {"type": "path", "label": label, "job": job, "raw": raw, "points": smooth, "planner": planner}


def plan_jobs(home, jobs, iters):
    print("\nPlanning:")
    steps = []
    current = np.asarray(home, float)
    up = np.array([0, 0, APPROACH], float)
    for n, (px, py, qx, qy) in enumerate(jobs, 1):
        pick = np.array([px, py, GRASP_Z], float)
        place = np.array([qx, qy, GRASP_Z], float)

        leg = plan_path(current, pick + up, iters, f"Job {n}: to pick", n)
        if leg is None:
            return None
        steps += [leg, {"type": "pick", "job": n, "point": pick}]

        leg = plan_path(pick + up, place + up, iters, f"Job {n}: carry to place", n)
        if leg is None:
            return None
        steps += [leg, {"type": "place", "job": n, "point": place}]
        current = place + up
    return steps


def validate(home, jobs):
    lo, hi = np.array(BOUNDS_LO, float), np.array(BOUNDS_HI, float)
    problems = []
    if not (np.all(home >= lo) and np.all(home <= hi)):
        problems.append(f"Home {home.tolist()} is outside the bounds {BOUNDS_LO}..{BOUNDS_HI}.")
    for n, (px, py, qx, qy) in enumerate(jobs, 1):
        for name, (x, y) in (("pick", (px, py)), ("place", (qx, qy))):
            app = np.array([x, y, GRASP_Z + APPROACH])
            if not (np.all(app >= lo) and np.all(app <= hi)):
                problems.append(f"Job {n}: {name} ({x}, {y}) is outside the work area "
                                f"x {lo[0]:.0f}-{hi[0]:.0f}, y {lo[1]:.0f}-{hi[1]:.0f}.")
        if math.hypot(px - qx, py - qy) < 1:
            problems.append(f"Job {n}: pick and place are the same spot.")
    return problems


# ================= ROBOT =================
def fmt(p):
    return f"{p[0]:.1f},{p[1]:.1f},{p[2]:.1f}"


def commands_for(step):
    if step["type"] == "path":
        pts = step["sent"]
        return [("WAYEND" if i == len(pts) - 1 else "WAYPOINT", p) for i, p in enumerate(pts[1:], 1)]
    return [(step["type"].upper(), step["point"])]


def send_cmd(host, port, cmd, p):
    payload = f"{cmd},{fmt(p)}"
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(60)
            sock.sendall(payload.encode())
            return sock.recv(1024).decode().strip()
    except OSError as e:
        return f"ERR connection: {e}"


def run_on_robot(steps, home, host, port):
    events = []
    t0 = time.time()

    def send(cmd, p):
        resp = send_cmd(host, port, cmd, p)
        events.append({"t": round(time.time() - t0, 2), "cmd": cmd,
                       "point": [float(v) for v in p], "resp": resp})
        print(f"  {cmd:9s} {fmt(p):>22s}  ->  {resp}")
        return resp

    print("\nRunning on YuMi:")
    if send("WAYEND", home) != "DONE":
        print("Could not move to the start position. Nothing else was sent.")
        for s in steps:
            s["status"] = "not run"
        return events, False

    ok = True
    for s in steps:
        if not ok:
            s["status"] = "not run"
            continue
        s["confirmed"] = 0
        for cmd, p in commands_for(s):
            if send(cmd, p) != "DONE":
                ok = False
                s["status"] = "failed"
                break
            s["confirmed"] += 1
        else:
            s["status"] = "done"
    return events, ok


def to_jsonable(steps):
    out = []
    for s in steps:
        d = {"type": s["type"], "job": s["job"], "status": s.get("status", "planned"),
             "confirmed": s.get("confirmed", 0)}
        if s["type"] == "path":
            d["label"] = s["label"]
            d["raw"] = np.asarray(s["raw"]).tolist()
            d["points"] = np.asarray(s["points"]).tolist()
            d["sent"] = np.asarray(s["sent"]).tolist()
        else:
            d["point"] = np.asarray(s["point"]).tolist()
        out.append(d)
    return out


def save_log(log):
    os.makedirs("runs", exist_ok=True)
    path = os.path.join("runs", time.strftime("run_%Y%m%d_%H%M%S.json"))
    with open(path, "w") as f:
        json.dump(log, f, indent=2)
    return path


# ================= ANIMATION =================
def interp(a, b, res=5.0):
    n = max(2, int(math.ceil(np.linalg.norm(b - a) / res)) + 1)
    return [a + (b - a) * t for t in np.linspace(0, 1, n)]


def build_frames(home, jobs, steps, hold=15):
    """(tcp, [cup centers], label) per frame. Stops at a failed or not-run step."""
    cup_offset = np.array([0, 0, CUP_H / 2 - GRASP_Z])
    cups = [np.array([j[0], j[1], CUP_H / 2], float) for j in jobs]
    state = {"tcp": np.asarray(home, float), "carry": None}
    frames = []

    def add(label):
        frames.append((state["tcp"].copy(), [c.copy() for c in cups], label))

    def move(a, b, label):
        for p in interp(np.asarray(a, float), np.asarray(b, float))[1:]:
            state["tcp"] = p
            if state["carry"] is not None:
                cups[state["carry"]] = p + cup_offset
            add(label)

    for _ in range(hold):
        add("Start position")

    for s in steps:
        status = s.get("status", "planned")
        if status == "not run":
            break
        if s["type"] == "path":
            pts = s["sent"] if "sent" in s else s["points"]
            if status == "failed":
                pts = pts[:1 + s.get("confirmed", 0)]
            for a, b in zip(pts[:-1], pts[1:]):
                move(a, b, s["label"])
        elif status != "failed":
            n = s["job"]
            p = np.asarray(s["point"], float)
            app = p + [0, 0, APPROACH]
            if s["type"] == "pick":
                move(app, p, f"Job {n}: down to cup")
                for _ in range(hold):
                    add(f"Job {n}: g_GripIn")
                state["carry"] = n - 1
                move(p, app, f"Job {n}: lift")
            else:
                move(app, p, f"Job {n}: lower")
                for _ in range(hold):
                    add(f"Job {n}: g_GripOut")
                state["carry"] = None
                move(p, app, f"Job {n}: retract")
        if status == "failed":
            for _ in range(hold * 3):
                add(f"STOPPED: {s.get('label', s['type'] + ' job ' + str(s['job']))}")
            return frames

    for _ in range(hold):
        add("All jobs done")
    return frames


def box_faces(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
         [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4],
             [2, 3, 7, 6], [1, 2, 6, 5], [0, 3, 7, 4]]
    return [[v[i] for i in f] for f in faces]


def draw_cylinder(ax, x, y, r, z0, z1, color, alpha=0.8, n=20):
    th = np.linspace(0, 2 * np.pi, n)
    TH, ZZ = np.meshgrid(th, [z0, z1])
    side = ax.plot_surface(x + r * np.cos(TH), y + r * np.sin(TH), ZZ,
                           color=color, alpha=alpha, linewidth=0, shade=True)
    top = Poly3DCollection([[(x + r * np.cos(t), y + r * np.sin(t), z1) for t in th]],
                           facecolor=color, alpha=alpha)
    ax.add_collection3d(top)
    return [side, top]


def animate(home, jobs, steps, trees=(), title="", save=None):
    frames = build_frames(home, jobs, steps)
    cmap = plt.get_cmap("tab10")

    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(projection="3d")
    lo, hi = BOUNDS_LO, BOUNDS_HI

    # Work area, floor and obstacles
    ax.add_collection3d(Poly3DCollection(box_faces(lo, hi), facecolor=(0, 0, 0, 0),
                                         edgecolor="lightgray", lw=0.6))
    floor = [[(lo[0], lo[1], 0), (hi[0], lo[1], 0), (hi[0], hi[1], 0), (lo[0], hi[1], 0)]]
    ax.add_collection3d(Poly3DCollection(floor, facecolor="whitesmoke", alpha=0.5))
    for o in OBSTACLES:
        if isinstance(o, Box):
            ax.add_collection3d(Poly3DCollection(box_faces(o.lo, o.hi), facecolor="tab:red", alpha=0.3))
        else:
            draw_cylinder(ax, o.x, o.y, o.r, 0, o.h, "tab:red", 0.3)

    # RRT* trees (only when planned in this session)
    for t in trees:
        if t is not None and t.n > 1:
            segs = [[t.P[t.parent[i]], t.P[i]] for i in range(1, t.n)]
            ax.add_collection3d(Line3DCollection(segs, colors="silver", linewidths=0.3, alpha=0.4))

    # Planned / sent paths, one color per job
    for s in steps:
        if s["type"] != "path" or len(s["points"]) < 2:
            continue
        col = cmap((s["job"] - 1) % 10)
        r = np.asarray(s["raw"])
        sent = np.asarray(s["sent"] if "sent" in s else s["points"])
        ax.plot(r[:, 0], r[:, 1], r[:, 2], color=col, lw=0.8, alpha=0.4)
        ax.plot(sent[:, 0], sent[:, 1], sent[:, 2], "o--", color=col, lw=1.5, ms=3)

    # Pick / place markers
    for n, (px, py, qx, qy) in enumerate(jobs, 1):
        col = cmap((n - 1) % 10)
        ax.scatter(px, py, 0, color=col, marker="x", s=60)
        ax.scatter(qx, qy, 0, color=col, marker="*", s=120)
        ax.text(px, py, 5, f" P{n}", color=col, fontsize=9)
        ax.text(qx, qy, 5, f" D{n}", color=col, fontsize=9)
    ax.scatter(*home, color="k", marker="^", s=70)
    ax.text(*home, "  start", fontsize=9)

    # Moving parts
    T = np.array([f[0] for f in frames])
    trail, = ax.plot([], [], [], color="tab:purple", lw=2)
    gripper, = ax.plot([], [], [], color="black", lw=6)
    status = ax.text2D(0.02, 0.95, "", transform=ax.transAxes, fontsize=12)
    drawn = [c.copy() for c in frames[0][1]]
    cup_art = [draw_cylinder(ax, c[0], c[1], CUP_R, c[2] - CUP_H / 2, c[2] + CUP_H / 2,
                             cmap(k % 10)) for k, c in enumerate(drawn)]

    ax.set_xlim(lo[0] - 50, hi[0] + 50)
    ax.set_ylim(lo[1] - 50, hi[1] + 50)
    ax.set_zlim(0, hi[2] + 50)
    ax.set_box_aspect((hi[0] - lo[0] + 100, hi[1] - lo[1] + 100, hi[2] + 50))
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_zlabel("Z [mm]")
    ax.set_title(title + "\nx = pick, * = place, dashed = path sent to robot")

    def update(i):
        tcp, cups, label = frames[i]
        for k, c in enumerate(cups):
            if np.any(np.abs(c - drawn[k]) > 1e-6):
                for a in cup_art[k]:
                    a.remove()
                cup_art[k] = draw_cylinder(ax, c[0], c[1], CUP_R, c[2] - CUP_H / 2,
                                           c[2] + CUP_H / 2, cmap(k % 10))
                drawn[k] = c.copy()
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
    ap = argparse.ArgumentParser(description="RRT* pick & place queue for YuMi")
    ap.add_argument("--job", nargs=4, type=float, action="append",
                    metavar=("PICK_X", "PICK_Y", "PLACE_X", "PLACE_Y"),
                    help="add one pick & place job (repeat for more)")
    ap.add_argument("--home", nargs=3, type=float, default=list(HOME), metavar=("X", "Y", "Z"))
    ap.add_argument("--iters", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--raw", action="store_true", help="send the raw RRT* path instead of the shortcut")
    ap.add_argument("--no-preview", action="store_true", help="skip the preview animation")
    ap.add_argument("--replay", default=None, help="replay a saved run (no robot)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--save", default=None, help="save the replay animation as GIF")
    args = ap.parse_args()

    # ---------- replay an old run ----------
    if args.replay:
        with open(args.replay) as f:
            log = json.load(f)
        print(f"Replaying {args.replay}  (success: {log['success']})")
        animate(log["home"], log["jobs"], log["steps"],
                title=f"Replay: {os.path.basename(args.replay)}", save=args.save)
        return

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    jobs = [tuple(j) for j in args.job] if args.job else DEFAULT_JOBS
    home = np.array(args.home, float)

    problems = validate(home, jobs)
    if problems:
        print("Problems:")
        for p in problems:
            print("  -", p)
        return

    # ---------- 1. plan ----------
    steps = plan_jobs(home, jobs, args.iters)
    if steps is None:
        return
    for s in steps:
        if s["type"] == "path":
            s["sent"] = s["raw"] if args.raw else s["points"]
    trees = [s["planner"] for s in steps if s["type"] == "path"]

    n_cmds = 1 + sum(len(commands_for(s)) for s in steps)
    print(f"\nCommands ({n_cmds}):")
    print(f"  WAYEND,{fmt(home)}   <- move to start")
    for s in steps:
        for cmd, p in commands_for(s):
            print(f"  {cmd},{fmt(p)}")

    # ---------- 2. preview ----------
    if not args.no_preview:
        print("\nShowing preview. Close the window when you're done looking.")
        animate(home, jobs, steps, trees, "Preview (nothing sent to the robot)")

    # ---------- 3. ask ----------
    ans = input(f"\nRun this on the YuMi ({args.host}:{args.port})? (y/n): ")
    if ans.strip().lower() != "y":
        print("Not sent. Nothing moved.")
        return

    # ---------- 4. run + save + replay ----------
    events, ok = run_on_robot(steps, home, args.host, args.port)
    log = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": args.host,
        "port": args.port,
        "home": home.tolist(),
        "jobs": [list(j) for j in jobs],
        "sent_raw_path": args.raw,
        "success": ok,
        "steps": to_jsonable(steps),
        "events": events,
    }
    path = save_log(log)
    print(f"\n{'All jobs done.' if ok else 'STOPPED early, see above.'}  Log saved to {path}")

    animate(log["home"], log["jobs"], log["steps"], trees,
            f"Executed on YuMi ({'OK' if ok else 'STOPPED'})", args.save)


if __name__ == "__main__":
    main()