"""
Find how far the YuMi left arm reaches along x and y, using CHECK (no motion).
A spot counts as reachable only if grip, approach and carry heights all are.

  python reach_edges.py
"""
import argparse
import socket

DEFAULT_HOST = "192.168.125.1"
DEFAULT_PORT = 5001
HEIGHTS = (40, 140, 250)          # grip, approach, carry
CENTER = (420, 200)               # known-good spot to start from


def check(host, port, x, y, z):
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(10)
        sock.sendall(f"CHECK,{x:.1f},{y:.1f},{z:.1f}".encode())
        resp = sock.recv(1024).decode().strip()
    if resp.startswith("ERR"):
        raise SystemExit(f"Robot answered '{resp}'. Is the program running and the arm ready?")
    return resp


def reachable(args, x, y):
    # BLOCKED = reachable but outside your current safe zone, which is fine here
    return all(check(args.host, args.port, x, y, z) in ("OK", "BLOCKED") for z in HEIGHTS)


def walk(args, axis, fixed, start, step, limit):
    """Step from start until not reachable. Returns (last reachable value, hit scan limit?)."""
    last = None
    v = start
    while (step > 0 and v <= limit) or (step < 0 and v >= limit):
        x, y = (v, fixed) if axis == "x" else (fixed, v)
        if not reachable(args, x, y):
            return last, False
        last = v
        v += step
    return last, True


def show(label, value, open_end):
    if value is None:
        return f"{label}: start not reachable"
    return f"{label}: {value:.0f}" + ("  (scan limit, may reach further)" if open_end else "")


def main():
    ap = argparse.ArgumentParser(description="Measure YuMi reach edges with CHECK (no motion)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--step", type=float, default=10)
    args = ap.parse_args()
    cx, cy = CENTER

    print(f"Heights checked at every spot: {HEIGHTS}. The robot does NOT move.\n")

    print("Along X (distance from the robot):")
    for y in (100, 200, 300):
        far, o1 = walk(args, "x", y, cx, +args.step, 700)
        near, o2 = walk(args, "x", y, cx, -args.step, 150)
        print(f"  y = {y}:  {show('max x', far, o1)}   |   {show('min x', near, o2)}")

    print("\nAlong Y (sideways):")
    for x in (360, 420, 480):
        left, o1 = walk(args, "y", x, cy, +args.step, 600)
        right, o2 = walk(args, "y", x, cy, -args.step, -300)
        print(f"  x = {x}:  {show('max y', left, o1)}   |   {show('min y', right, o2)}")

    print("\nMin x is optimistic: CHECK doesn't know the robot's body. Keep X_MIN where it is.")
    print("For a safe work area, stay 30-50 mm inside the max values.")


if __name__ == "__main__":
    main()