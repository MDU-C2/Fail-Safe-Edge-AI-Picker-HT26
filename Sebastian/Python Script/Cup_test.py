"""
Cup handover, both arms at the same time. They only sync during the handover:

  left:  pick cup -> lift -> wait beside the handover -> slide in -> open -> slide out -> next cup
  right: wait below the handover -> grip the rim -> pull down -> turn over -> place in the grid -> back

Talks to CommServer over TCP, one connection per message. "@CMD,..." starts a move and returns
STARTED at once; L_STATUS / R_STATUS answer BUSY until the arm is done, then DONE or the error.

The fixed points and the 9 grid spots are reach-checked once and saved in reach_cache.json.
Each cup's pick point is checked just before the left arm goes there.

  python Cup_test.py --xy 400 80 --xy 450 150 --ip 127.0.0.1     cups at robot x, y (RobotStudio)
  python Cup_test.py detections.csv                              cups from the camera (needs CAM_R / CAM_T)
  python Cup_test.py --reset-grid ...                            start with an empty grid
  python Cup_test.py --recheck ...                               reach-check the fixed points and grid again
  python Cup_test.py ... --speed 250 --pick-speed 100            faster (default 100 / 50 mm/s)
"""
import argparse
import csv
import json
import os
import socket
import threading
import time

ROBOT_IP = "192.168.125.1"       # "127.0.0.1" for RobotStudio (same as SERVER_IP in CommServer)
PORT = 5001
REPLY_TIMEOUT = 100              # s, longer than ARM_TIMEOUT (90) in CommServer
POLL = 0.05                      # s between status polls
APPROACH = 100                   # same as APPROACH in SharedVars

# ---- cup (upside down on the table) ----
CUP_H = 80                       # height, mm
RIM_R = 35                       # outer radius of the rim, mm

# ---- left arm ----
GRASP_Z = 65                     # grip height, near the top of the cup
CARRY_Z = 250                    # straight up to this after the pick
HANDOVER = (420, 0, 390)         # left TCP holding the cup for the right arm. Max z 400 (zMax)
WAIT_PT = (420, 80, 390)         # left waits here (same height) and slides in. Keeps the cup's rim
                                 # above the right arm's open fingers, so the right arm can wait in advance

# ---- right arm (gripper straight up to take the rim, straight down to place) ----
UP, DOWN = (0, 0, 0), (180, 0, 0)
RIM_GRIP = 10                    # fingers pinch the wall this far above the rim edge
TAKE_APPROACH = 40               # same as in RightArm
R_DOWN = 60                      # straight down this far after the left opens
FLIP = (420, -115, 220)          # turn over here. Starts higher (pointing up), since then the arm hangs below the TCP
ABOVE_HEIGHTS = (220, 200, 185, 170)   # tried highest first for the move over a grid spot

# ---- grid of cup centers on the right side ----
GRID_CENTER = (420, -160)
GRID_SPACING = 100
GRID_N = 3                       # 3 x 3

# ---- camera -> robot (wobj0, mm): robot = CAM_R * camera + CAM_T. From the calibration ----
CAM_R = None                     # e.g. [[0, -1, 0], [-1, 0, 0], [0, 0, -1]]
CAM_T = None                     # e.g. [430, 200, 900]
CUP_CLASS = "CUP BASE"
VISION_WAIT = 60                 # s to wait for a new capture before the run ends

HERE = os.path.dirname(os.path.abspath(__file__))
GRID_FILE = os.path.join(HERE, "grid_state.json")
REACH_FILE = os.path.join(HERE, "reach_cache.json")


# ================= POINTS =================
def fmt(p, rot=None):
    return ",".join(f"{v:.1f}" for v in (*p, *(rot or ())))


def grid_spots():
    k = (GRID_N - 1) / 2
    return [(GRID_CENTER[0] + (r - k) * GRID_SPACING, GRID_CENTER[1] + (c - k) * GRID_SPACING)
            for r in range(GRID_N) for c in range(GRID_N)]


hx, hy, hz = HANDOVER
TAKE = (hx, hy - RIM_R, hz - GRASP_Z + RIM_GRIP)     # right grips the wall on its own side (-y)
BELOW = (TAKE[0], TAKE[1], TAKE[2] - TAKE_APPROACH)  # where TAKE starts
AWAY = (TAKE[0], TAKE[1], TAKE[2] - R_DOWN)           # out of the left fingers
CARRY = (FLIP[0], FLIP[1], AWAY[2])                   # sideways at the same height, still pointing up


def place_pts(spot, above_z):
    """Right TCP over, at and above the place spot. Turned over, the cup center is -RIM_R in y from the TCP."""
    x, y = spot[0], spot[1] + RIM_R
    return (x, y, above_z), (x, y, CUP_H - RIM_GRIP)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ================= ROBOT =================
SOCKET_LOCK = threading.Lock()   # CommServer takes one connection at a time: the arm threads take turns


def send(cmd, ip):
    with SOCKET_LOCK:
        for _ in range(20):
            try:
                with socket.create_connection((ip, PORT), timeout=10) as s:
                    s.settimeout(REPLY_TIMEOUT)
                    s.sendall(cmd.encode())
                    return s.recv(1024).decode().strip()
            except ConnectionRefusedError as e:
                err = e
                time.sleep(0.05)            # CommServer busy for a moment, try again
            except OSError as e:
                return f"ERR connection: {e}"
        return f"ERR connection: {err}"


def check_reach(ip, recheck):
    """Fixed points and every grid spot, checked once and saved. Returns the height over each
    grid spot (None = can't be used), or None if a fixed point fails."""
    saved = load(REACH_FILE, {})
    key = json.dumps([HANDOVER, WAIT_PT, GRASP_Z, CUP_H, RIM_R, RIM_GRIP, TAKE_APPROACH, R_DOWN,
                      FLIP, ABOVE_HEIGHTS, APPROACH, GRID_CENTER, GRID_SPACING, GRID_N])
    if saved.get("key") == key and not recheck:
        print("Reach: already checked (reach_cache.json). --recheck to check again.")
        return saved["above_z"]

    print("Reach: checking the fixed points and grid spots once (no motion).")
    for name, cmd in (("left handover", f"CHECK,{fmt(HANDOVER)}"),
                      ("left waiting point", f"CHECK,{fmt(WAIT_PT)}"),
                      ("right below the cup", f"R_CHECK,{fmt(BELOW, UP)}"),
                      ("right take", f"R_CHECK,{fmt(TAKE, UP)}"),
                      ("right carry", f"R_CHECK,{fmt(CARRY, UP)}"),
                      ("right flip", f"R_CHECK,{fmt(FLIP, DOWN)}")):
        reply = send(cmd, ip)
        if reply != "OK":
            print(f"  The {name} point can't be used ({reply}). Change it at the top of the script.")
            return None

    above_z = []
    for k, spot in enumerate(grid_spots()):
        ok = lambda p: send(f"R_CHECK,{fmt(p, DOWN)}", ip) == "OK"
        z = next((z for z in ABOVE_HEIGHTS if ok(place_pts(spot, z)[0])), None)
        _, place = place_pts(spot, 0)
        if z is not None and not (ok(place) and ok((place[0], place[1], place[2] + APPROACH))):
            z = None
        above_z.append(z)
        print(f"  spot {k + 1} ({spot[0]:.0f}, {spot[1]:.0f}): "
              + (f"OK, over it at z {z}" if z else "can't be reached, the run stops there"))
    save(REACH_FILE, {"key": key, "above_z": above_z})
    return above_z


class Arm:
    def __init__(self, tag, ip, stop, lock):
        self.tag, self.ip, self.stop, self.lock = tag, ip, stop, lock
        self.idle = 0.0                  # s spent waiting for the other arm

    def log(self, text):
        with self.lock:
            print(f"  [{self.tag}] {text}")

    def fail(self, text):
        self.log(f"STOP: {text}")
        self.stop.set()
        return False

    def move(self, label, cmd):
        """Start the move and poll until done. True on DONE."""
        if self.stop.is_set():
            return False
        reply = send("@" + cmd, self.ip)
        if reply != "STARTED":
            return self.fail(f"{cmd} -> {reply}   ({label})")
        t0 = time.time()
        while (reply := send(f"{self.tag}_STATUS", self.ip)) == "BUSY":
            if time.time() - t0 > REPLY_TIMEOUT:
                return self.fail(f"{cmd} took longer than {REPLY_TIMEOUT} s   ({label})")
            time.sleep(POLL)
        if reply != "DONE":
            return self.fail(f"{cmd} -> {reply}   ({label})")
        self.log(f"{cmd:45s} -> DONE   {label}  ({time.time() - t0:.1f} s)")
        return True

    def wait(self, event):
        """Wait for the other arm. False if the run was stopped meanwhile."""
        t0 = time.time()
        while not event.wait(0.02):
            if self.stop.is_set():
                return False
        self.idle += time.time() - t0
        return True


# ================= CUPS =================
def csv_cups(path):
    """Yields the best CUP BASE (robot x, y) from each new capture in the vision CSV.
    Ends if no new capture comes within VISION_WAIT s."""
    used = None
    while True:
        t0 = time.time()
        while True:
            try:
                with open(path, newline="") as f:
                    rows = list(csv.DictReader(f))
                latest = rows[-1]["capture"] if rows else None
                cups = [r for r in rows if r["capture"] == latest and r["class"].strip() == CUP_CLASS
                        and float(r["z_mm"]) > 0]
            except (OSError, KeyError, ValueError):
                cups = []
            if cups and latest != used:
                break
            if time.time() - t0 > VISION_WAIT:
                return
            time.sleep(0.2)
        used = latest
        best = max(cups, key=lambda r: float(r["confidence"]))
        cam = [float(best[k]) for k in ("x_mm", "y_mm", "z_mm")]
        yield tuple(sum(CAM_R[i][j] * cam[j] for j in range(3)) + CAM_T[i] for i in range(2))


# ================= RUN =================
def run(cups, args):
    spots = grid_spots()
    filled0 = load(GRID_FILE, {}).get("filled", 0)
    above_z = check_reach(args.ip, args.recheck)
    if above_z is None:
        return

    stop, log_lock, ev_lock = threading.Event(), threading.Lock(), threading.Lock()
    no_more = threading.Event()
    events = {}

    def ev(name, i):                      # one event per step and cup, made when first used
        with ev_lock:
            return events.setdefault((name, i), threading.Event())

    L, R = Arm("L", args.ip, stop, log_lock), Arm("R", args.ip, stop, log_lock)
    syncs, placed = [], []

    def left():
        for i in range(len(spots) - filled0):
            cup = next(cups, None)        # camera: the newest detection, now that the arm is free
            if cup is None or stop.is_set():
                break
            pick = (*cup, GRASP_Z)
            if (reply := send(f"CHECK,{fmt(pick)}", L.ip)) != "OK":
                L.fail(f"cup at ({cup[0]:.0f}, {cup[1]:.0f}) can't be picked ({reply})")
                break
            L.log(f"--- cup {i + 1}: pick ({cup[0]:.0f}, {cup[1]:.0f})")
            ev("coming", i).set()
            if not (L.move("pick", f"PICK,{fmt(pick)}")
                    and L.move("lift", f"WAYEND,{fmt((*cup, CARRY_Z))}")
                    and L.move("to the waiting point", f"WAYEND,{fmt(WAIT_PT)}")
                    and L.wait(ev("r_ready", i))):
                break
            t0 = time.time()              # ---- handover ----
            if not L.move("slide in", f"WAYEND,{fmt(HANDOVER)}"):
                break
            ev("at_handover", i).set()
            if not (L.wait(ev("r_has_cup", i)) and L.move("open", "RELEASE")):
                break
            ev("l_released", i).set()
            if not L.wait(ev("r_down", i)):
                break
            syncs.append(time.time() - t0)    # right arm free from here
            L.log(f"--- cup {i + 1} handed over, sync {syncs[-1]:.1f} s")
            if not L.move("slide out", f"WAYEND,{fmt(WAIT_PT)}"):
                break
            ev("l_left", i).set()
        no_more.set()

    def right():
        for i, spot in enumerate(spots[filled0:]):
            if i > 0 and not R.wait(ev("l_left", i - 1)):
                return
            while not ev("coming", i).wait(0.02):     # another cup on its way?
                if no_more.is_set() or stop.is_set():
                    return
            if above_z[filled0 + i] is None:
                R.fail(f"grid spot {filled0 + i + 1} can't be reached")
                return
            above, place = place_pts(spot, above_z[filled0 + i])
            if not R.move("below the handover", f"R_GOTO,{fmt(BELOW, UP)}"):
                return
            ev("r_ready", i).set()
            if not (R.wait(ev("at_handover", i)) and R.move("grip the rim", f"R_TAKE,{fmt(TAKE)}")):
                return
            ev("r_has_cup", i).set()
            if not (R.wait(ev("l_released", i)) and R.move("pull down", f"R_WAYEND,{fmt(AWAY)}")):
                return
            ev("r_down", i).set()
            # Joint moves to and from the grid: a straight move would twist wrist joint 6 past its limit
            if not (R.move("sideways", f"R_WAYEND,{fmt(CARRY)}")
                    and R.move("turn over", f"R_GOTO,{fmt(FLIP, DOWN)}")
                    and R.wait(ev("l_left", i))          # left out of the middle first
                    and R.move("over the grid spot", f"R_GOTO,{fmt(above, DOWN)}")
                    and R.move("place", f"R_PLACE,{fmt(place)}")):
                return
            placed.append(time.time())
            save(GRID_FILE, {"filled": filled0 + i + 1})
            R.log(f"--- cup {i + 1} placed, grid {filled0 + i + 1} of {len(spots)}")
            if not (R.move("back", f"R_GOTO,{fmt(FLIP, DOWN)}")
                    and R.move("turn back", f"R_GOTO,{fmt(CARRY, UP)}")):
                return

    print("\nRunning:")
    t0 = time.time()
    threads = [threading.Thread(target=left), threading.Thread(target=right)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    n = len(placed)
    took = (placed[-1] if placed else time.time()) - t0     # not counting a last wait for the camera
    if stop.is_set():
        print(f"\nStopped after {n} cup(s), {took:.0f} s. Check the arms: one may still hold a cup.")
    elif n == 0:
        print("\nNo cups placed." + (" The grid is full, use --reset-grid." if filled0 >= len(spots) else ""))
    else:
        print(f"\nDone: {n} cup(s) in {took:.0f} s ({took / n:.0f} s per cup), "
              f"handover sync {sum(syncs) / len(syncs):.1f} s per cup.")
        print(f"Waited for the other arm: left {L.idle:.0f} s, right {R.idle:.0f} s.")


def main():
    ap = argparse.ArgumentParser(description="Cup handover, both arms in parallel")
    ap.add_argument("csv", nargs="?", help="vision CSV file")
    ap.add_argument("--xy", nargs=2, type=float, action="append", metavar=("X", "Y"),
                    help="robot x y of a cup instead of the camera. Repeat for more cups")
    ap.add_argument("--reset-grid", action="store_true", help="start with an empty grid")
    ap.add_argument("--recheck", action="store_true", help="reach-check the fixed points and grid again")
    ap.add_argument("--speed", type=float, default=100, help="travel speed, mm/s (10-1000)")
    ap.add_argument("--pick-speed", type=float, default=50, help="speed near the cup, mm/s (5-300)")
    ap.add_argument("--yes", action="store_true", help="don't ask before starting")
    ap.add_argument("--ip", default=ROBOT_IP)
    args = ap.parse_args()

    if args.reset_grid:
        save(GRID_FILE, {"filled": 0})
        print("Grid reset.")
    if args.xy:
        cups = iter([tuple(c) for c in args.xy])
        what = f"{len(args.xy)} cup(s) from --xy"
    elif args.csv:
        if CAM_R is None or CAM_T is None:
            print("CAM_R / CAM_T not set, so camera coordinates can't be turned into robot coordinates yet.")
            return
        cups = csv_cups(args.csv)
        what = f"cups from {args.csv}, a new capture for each"
    else:
        if not args.reset_grid:
            print("Give --xy X Y or a vision CSV file.")
        return

    if not args.yes and input(f"Pick {what}? (y/n): ").strip().lower() != "y":
        print("Nothing moved.")
        return
    if (reply := send(f"SPEED,{args.speed:.0f},{args.pick_speed:.0f},0", args.ip)) != "DONE":
        print(f"Speed not accepted ({reply}). Stopping.")
        return
    run(cups, args)


if __name__ == "__main__":
    main()