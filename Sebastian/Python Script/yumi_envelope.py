"""
YuMi reach envelope.

  sweep : ask the robot (CHECK, no motion) about a 3D grid -> reach_map.csv
  build : turn reach_map.csv into envelope models         -> yumi_envelope.json
  show  : plot an existing envelope

In RRT* / simulation:
  from yumi_envelope import Envelope
  ENV = Envelope("yumi_envelope.json", model="box")   # "box", "sphere" or "grid"
  ENV.contains(p)  /  ENV.segment_ok(a, b)
"""
import argparse
import csv
import json
import math
import os
import socket
import time

import numpy as np

DEFAULT_HOST = "192.168.125.1"   # sim: 127.0.0.1
DEFAULT_PORT = 5001
PHYSICAL = {"OK", "BLOCKED"}     # BLOCKED = arm can reach it, only your limits say no


# ======================= robot communication =======================
def ask(host, port, payload, timeout=5):
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload.encode())
        return sock.recv(1024).decode().strip()


def read_ceiling(host, port):
    resp = ask(host, port, "LIMITS,0,0,0")
    if resp.startswith("ZMAX,"):
        return float(resp.split(",")[1])
    print(f"Could not read ceiling (robot answered '{resp}').")
    return None


# ======================= 1. SWEEP =======================
def frange(lo, hi, step):
    return np.round(np.arange(lo, hi + step / 2, step), 3)


def meta_path(csv_path):
    return os.path.splitext(csv_path)[0] + "_meta.json"


def sweep(args):
    ceiling = read_ceiling(args.host, args.port)
    xs, ys, zs = frange(*args.x, args.step), frange(*args.y, args.step), frange(*args.z, args.step)
    total = len(xs) * len(ys) * len(zs)
    print(f"Ceiling on robot: {ceiling} mm")
    print(f"Sweeping {total} points (step {args.step} mm). The robot does NOT move.")

    n = 0
    t0 = time.time()
    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x", "y", "z", "status"])
        for z in zs:
            for x in xs:
                for y in ys:
                    try:
                        status = ask(args.host, args.port, f"CHECK,{x:.1f},{y:.1f},{z:.1f}")
                    except OSError as e:
                        print(f"\nConnection problem at ({x}, {y}, {z}): {e}")
                        print("Is the RAPID program running and waiting for commands?")
                        return
                    if status.startswith("ERR"):
                        print(f"\nRobot answered '{status}'. Load the RAPID module that has CHECK.")
                        return
                    w.writerow([x, y, z, status])
                    n += 1
                    if n % 200 == 0:
                        rate = n / (time.time() - t0)
                        print(f"\r  {n}/{total}  (~{(total - n) / rate:.0f} s left)   ", end="")

    with open(meta_path(args.csv), "w") as f:
        json.dump({"ceiling_z": ceiling, "step": args.step,
                   "region": {"x": args.x, "y": args.y, "z": args.z},
                   "date": time.strftime("%Y-%m-%d %H:%M")}, f, indent=2)
    print(f"\nDone in {time.time() - t0:.0f} s. Saved {args.csv} and {meta_path(args.csv)}")


def load_csv(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append((float(r["x"]), float(r["y"]), float(r["z"]), r["status"]))
    return rows


def build_grid(rows, min_x=None):
    xs = np.unique([r[0] for r in rows])
    ys = np.unique([r[1] for r in rows])
    zs = np.unique([r[2] for r in rows])
    ix = {v: i for i, v in enumerate(xs)}
    iy = {v: i for i, v in enumerate(ys)}
    iz = {v: i for i, v in enumerate(zs)}
    reach = np.zeros((len(xs), len(ys), len(zs)), bool)
    for x, y, z, s in rows:
        if s in PHYSICAL and (min_x is None or x >= min_x):
            reach[ix[x], iy[y], iz[z]] = True
    return xs, ys, zs, reach


# ======================= 2. ENVELOPE MODELS =======================
# Each model is a function (xs, ys, zs, reach) -> dict saved in the JSON.
# To add a new shape later: write a build_ function, add it to MODELS,
# and add a matching check in Envelope.contains().

def _max_rectangle(mask):
    nx, ny = mask.shape
    heights = np.zeros(ny, int)
    best = (0, 0, -1, 0, -1)
    for i in range(nx):
        heights = np.where(mask[i], heights + 1, 0)
        stack = []
        for j in range(ny + 1):
            h = heights[j] if j < ny else 0
            start = j
            while stack and stack[-1][1] >= h:
                s, sh = stack.pop()
                area = sh * (j - s)
                if area > best[0]:
                    best = (area, i - sh + 1, i, s, j - 1)
                start = s
            stack.append((start, h))
    return best


def build_box(xs, ys, zs, reach):
    """Largest axis-aligned box where EVERY grid point inside is reachable (conservative)."""
    nx, ny, nz = reach.shape
    best_vol, best = 0, None
    for z1 in range(nz):
        acc = np.ones((nx, ny), bool)
        for z2 in range(z1, nz):
            acc &= reach[:, :, z2]
            if not acc.any():
                break
            area, x1, x2, y1, y2 = _max_rectangle(acc)
            vol = area * (z2 - z1 + 1)
            if vol > best_vol:
                best_vol, best = vol, (x1, x2, y1, y2, z1, z2)
    if best is None:
        return None
    x1, x2, y1, y2, z1, z2 = best
    lo = [float(xs[x1]), float(ys[y1]), float(zs[z1])]
    hi = [float(xs[x2]), float(ys[y2]), float(zs[z2])]
    size = [h - l for l, h in zip(lo, hi)]
    return {"min": lo, "max": hi, "size_mm": size,
            "volume_litres": round(size[0] * size[1] * size[2] / 1e6, 2)}


def _fit_sphere(P):
    A = np.c_[2 * P, np.ones(len(P))]
    b = (P ** 2).sum(axis=1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    c = sol[:3]
    return c, math.sqrt(max(sol[3] + c @ c, 0.0))


def build_sphere(xs, ys, zs, reach, pct=95):
    """Sphere shell (center, inner and outer radius), like normal robot datasheets. Approximate."""
    idx = np.argwhere(reach)
    coords = np.c_[xs[idx[:, 0]], ys[idx[:, 1]], zs[idx[:, 2]]]
    centroid = coords.mean(axis=0)
    nx, ny, nz = reach.shape

    outer = []
    for (i, j, k), p in zip(idx, coords):
        for di, dj, dk in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            a, b, c = i + di, j + dj, k + dk
            if 0 <= a < nx and 0 <= b < ny and 0 <= c < nz and not reach[a, b, c]:
                q = np.array([xs[a], ys[b], zs[c]])
                if np.linalg.norm(q - centroid) > np.linalg.norm(p - centroid):
                    outer.append(p)
                    break
    center, r_fit = _fit_sphere(np.array(outer)) if len(outer) >= 10 else (centroid, float("nan"))

    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    G = np.c_[X.ravel(), Y.ravel(), Z.ravel()]
    R = reach.ravel()
    d = np.linalg.norm(G - center, axis=1)
    r_in = float(np.percentile(d[R], 100 - pct))
    r_out = float(np.percentile(d[R], pct))
    inside = (d >= r_in) & (d <= r_out)
    return {"center": [float(v) for v in center], "r_inner": r_in, "r_outer": r_out,
            "r_fit": float(r_fit),
            "precision": float(R[inside].mean()) if inside.any() else 0.0,
            "coverage": float(inside[R].mean())}


MODELS = {"box": build_box, "sphere": build_sphere}


def edge_warnings(reach):
    sides = {"-x": reach[0], "+x": reach[-1], "-y": reach[:, 0], "+y": reach[:, -1],
             "-z": reach[:, :, 0], "+z": reach[:, :, -1]}
    return [k for k, v in sides.items() if v.any()]


def build(args):
    rows = load_csv(args.csv)
    meta = {}
    if os.path.exists(meta_path(args.csv)):
        with open(meta_path(args.csv)) as f:
            meta = json.load(f)
    ceiling = args.ceiling if args.ceiling is not None else meta.get("ceiling_z")

    xs, ys, zs, reach = build_grid(rows, args.min_x)
    if not reach.any():
        print("No reachable points in the CSV.")
        return
    step = float(np.min(np.diff(xs))) if len(xs) > 1 else meta.get("step", 25)
    idx = np.argwhere(reach)
    P = np.c_[xs[idx[:, 0]], ys[idx[:, 1]], zs[idx[:, 2]]]

    models = {name: fn(xs, ys, zs, reach) for name, fn in MODELS.items()}
    edges = edge_warnings(reach)

    result = {
        "robot": "IRB 14000 YuMi, left arm",
        "orientation": "gripper pointing down (pRef at program start)",
        "tool": "tGrip",
        "frame": "wobj0 (robot base)",
        "source_csv": os.path.abspath(args.csv),
        "grid_step": step,
        "ceiling_z": ceiling,
        "min_x_filter": args.min_x,
        "reach_touches_sweep_edge": edges,
        "max_reach": {
            "min": P.min(axis=0).tolist(),
            "max": P.max(axis=0).tolist(),
            "points": int(len(P)),
        },
        "models": models,
    }
    with open(args.json, "w") as f:
        json.dump(result, f, indent=2)

    print("\n================ YuMi reach (left arm, gripper down) ================")
    print(f"Grid step {step:.0f} mm, {len(P)} reachable points")
    print(f"Max reach:  x {P[:, 0].min():.0f}..{P[:, 0].max():.0f}   "
          f"y {P[:, 1].min():.0f}..{P[:, 1].max():.0f}   z {P[:, 2].min():.0f}..{P[:, 2].max():.0f}")
    print(f"Ceiling:    {ceiling} mm")
    if edges:
        print(f"NOTE: reach touches the sweep edge on {', '.join(edges)} -> widen the sweep there.")
    b = models["box"]
    if b:
        print(f"\nBox (all reachable):  min {b['min']}  max {b['max']}  ({b['volume_litres']} L)")
    s = models["sphere"]
    print(f"Sphere shell:  center {np.round(s['center'], 1).tolist()}  "
          f"r {s['r_inner']:.0f}..{s['r_outer']:.0f} mm")
    print(f"               {s['precision'] * 100:.0f}% of the shell is reachable, "
          f"covers {s['coverage'] * 100:.0f}% of the reach")
    print(f"\nSaved {args.json}")

    if not args.no_plot:
        show(args.json)


# ======================= 3. PLOTS =======================
def _box_faces(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
         [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4],
             [2, 3, 7, 6], [1, 2, 6, 5], [0, 3, 7, 4]]
    return [[v[i] for i in f] for f in faces]


def show(json_path):
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    with open(json_path) as f:
        d = json.load(f)
    xs, ys, zs, reach = build_grid(load_csv(d["source_csv"]), d["min_x_filter"])
    box, sph, ceil = d["models"]["box"], d["models"]["sphere"], d["ceiling_z"]

    # 3D view
    idx = np.argwhere(reach)
    P = np.c_[xs[idx[:, 0]], ys[idx[:, 1]], zs[idx[:, 2]]]
    if len(P) > 4000:
        P = P[np.random.choice(len(P), 4000, replace=False)]
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(projection="3d")
    ax.scatter(P[:, 0], P[:, 1], P[:, 2], s=4, c="tab:green", alpha=0.3, label="Reachable")
    if box:
        ax.add_collection3d(Poly3DCollection(_box_faces(box["min"], box["max"]),
                                             facecolor="tab:blue", alpha=0.15, edgecolor="tab:blue"))
    c = sph["center"]
    u, v = np.mgrid[0:2 * np.pi:30j, 0:np.pi:15j]
    for r, col in ((sph["r_outer"], "tab:red"), (sph["r_inner"], "tab:orange")):
        ax.plot_wireframe(c[0] + r * np.cos(u) * np.sin(v), c[1] + r * np.sin(u) * np.sin(v),
                          c[2] + r * np.cos(v), color=col, alpha=0.15, lw=0.5)
    if ceil is not None:
        X, Y = np.meshgrid([xs[0], xs[-1]], [ys[0], ys[-1]])
        ax.plot_surface(X, Y, np.full_like(X, ceil, dtype=float), color="k", alpha=0.1)
    ax.scatter(0, 0, 0, color="k", s=80, marker="^", label="Robot base")
    ax.set_xlim(xs.min(), xs.max())
    ax.set_ylim(ys.min(), ys.max())
    ax.set_zlim(zs.min(), zs.max())
    ax.set_box_aspect((np.ptp(xs), np.ptp(ys), np.ptp(zs)))
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_zlabel("Z [mm]")
    ax.set_title("YuMi left arm reach\nblue = box, red/orange = sphere shell, gray = ceiling")
    ax.legend(fontsize=8)

    # Top-view slices
    levels = [k for k in range(len(zs)) if reach[:, :, k].any()]
    picks = [levels[int(i)] for i in np.linspace(0, len(levels) - 1, min(4, len(levels)))]
    fig2, axes = plt.subplots(1, len(picks), figsize=(4.5 * len(picks), 4.5), squeeze=False)
    for a, k in zip(axes[0], picks):
        z = zs[k]
        a.imshow(reach[:, :, k].T, origin="lower", cmap="Greens", alpha=0.7,
                 extent=[xs[0], xs[-1], ys[0], ys[-1]], aspect="equal")
        if box and box["min"][2] <= z <= box["max"][2]:
            a.add_patch(Rectangle((box["min"][0], box["min"][1]), box["size_mm"][0],
                                  box["size_mm"][1], fill=False, ec="tab:blue", lw=2))
        for r, col in ((sph["r_outer"], "tab:red"), (sph["r_inner"], "tab:orange")):
            h = r ** 2 - (z - c[2]) ** 2
            if h > 0:
                a.add_patch(Circle((c[0], c[1]), math.sqrt(h), fill=False, ec=col, ls="--"))
        title = f"z = {z:.0f} mm"
        if ceil is not None and z > ceil:
            title += "  (above ceiling)"
        a.set_title(title)
        a.set_xlabel("X [mm]")
        a.set_ylabel("Y [mm]")
    fig2.suptitle("Top views: green = reachable, blue = box, red/orange = shell")
    fig2.tight_layout()
    plt.show()


# ======================= 4. FOR RRT* / SIMULATION =======================
class Envelope:
    """
    ENV = Envelope("yumi_envelope.json", model="box")    # "box", "sphere" or "grid"
    ENV.contains(p)        -> True if p is reachable and under the ceiling
    ENV.segment_ok(a, b)   -> True if the whole straight line is
    ENV.ceiling            -> ceiling z from the robot (or None)
    """

    def __init__(self, json_path="yumi_envelope.json", model="box", use_ceiling=True):
        with open(json_path) as f:
            self.data = json.load(f)
        self.model = model
        self.ceiling = self.data["ceiling_z"] if use_ceiling else None
        m = self.data["models"]

        if model == "box":
            if not m["box"]:
                raise ValueError("No box in the envelope file.")
            self.lo = np.array(m["box"]["min"])
            self.hi = np.array(m["box"]["max"])
        elif model == "sphere":
            self.c = np.array(m["sphere"]["center"])
            self.r_in, self.r_out = m["sphere"]["r_inner"], m["sphere"]["r_outer"]
        elif model == "grid":
            rows = load_csv(self.data["source_csv"])
            self.step = self.data["grid_step"]
            self.origin = np.array([min(r[i] for r in rows) for i in range(3)])
            min_x = self.data["min_x_filter"]
            self.cells = {self._key(r[:3]) for r in rows
                          if r[3] in PHYSICAL and (min_x is None or r[0] >= min_x)}
        else:
            raise ValueError(f"Unknown model '{model}'")

    def _key(self, p):
        return tuple(np.round((np.asarray(p, float) - self.origin) / self.step).astype(int))

    def contains(self, p):
        p = np.asarray(p, float)
        if self.ceiling is not None and p[2] > self.ceiling:
            return False
        if self.model == "box":
            return bool(np.all(p >= self.lo) and np.all(p <= self.hi))
        if self.model == "sphere":
            return self.r_in <= np.linalg.norm(p - self.c) <= self.r_out
        return self._key(p) in self.cells

    def segment_ok(self, a, b, res=10.0):
        a, b = np.asarray(a, float), np.asarray(b, float)
        if self.model == "box":              # box + ceiling is convex: checking the ends is enough
            return self.contains(a) and self.contains(b)
        n = max(2, int(np.ceil(np.linalg.norm(b - a) / res)) + 1)
        return all(self.contains(a + (b - a) * t) for t in np.linspace(0, 1, n))


# ======================= MAIN =======================
def main():
    ap = argparse.ArgumentParser(description="YuMi reach envelope")
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("sweep", help="ask the robot (no motion) and save reach_map.csv")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--x", nargs=2, type=float, default=[-50, 600])
    s.add_argument("--y", nargs=2, type=float, default=[-150, 600])
    s.add_argument("--z", nargs=2, type=float, default=[0, 550])
    s.add_argument("--step", type=float, default=50, help="50 = quick, 25 = detailed")
    s.add_argument("--csv", default="reach_map.csv")

    b = sub.add_parser("build", help="make envelope models from reach_map.csv")
    b.add_argument("--csv", default="reach_map.csv")
    b.add_argument("--json", default="yumi_envelope.json")
    b.add_argument("--min-x", type=float, default=None,
                   help="drop points closer to the body than this x (IK doesn't know the torso)")
    b.add_argument("--ceiling", type=float, default=None, help="override the ceiling from the robot")
    b.add_argument("--no-plot", action="store_true")

    v = sub.add_parser("show", help="plot an existing envelope")
    v.add_argument("--json", default="yumi_envelope.json")

    args = ap.parse_args()
    if args.mode == "sweep":
        sweep(args)
    elif args.mode == "build":
        build(args)
    else:
        show(args.json)


if __name__ == "__main__":
    main()