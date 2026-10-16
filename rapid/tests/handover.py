"""
Left arm: picks the cup the camera found (capture_and_detect.py calls this with --x --y).
Right arm: takes the cup at the handover from below, turns it over, places it.
Both arms move at the same time and only wait for each other at the handover.

All points sent to the robot are tool0 (the flange) in mm, robot base frame.
Points marked PLACEHOLDER: jog the arm there with tool0 and read the FlexPendant.

Test one command at a time first:
    py handover.py --x 400 --y 80 --step
"""
import argparse
import os
import queue
import sys
import threading
import time

import requests

URL = "http://192.168.125.201:8000/api/v1"
ROBOT_TOKEN = os.environ.get("ROBOT_TOKEN", "BAD_TOKEN_OPERATOR")
STEP = False                      # True = ask before every command (--step)

# ---- Cup and grippers ----
TOOL_L = 159                      # left flange to fingertips (measured)
TOOL_R = 159                      # right flange to fingertips (MEASURE)
CUP_H = 80                        # cup height
GRIP_FROM_RIM = 80                # left grips 65 mm above the rim
RIM_GRIP = 10                     # right grips 10 mm above the rim
CUP_RADIUS = 35                   # right fingers sit on the rim wall, one radius from the centre (MEASURE)
DOWN = "180,0,0"                  # rx,ry,rz: gripper pointing down
UP = "0,0,0"                      # gripper pointing up

# ---- Left arm on the table ----
GRIP_Z = GRIP_FROM_RIM + TOOL_L   # flange height when gripping (rim on the table, z 0)
ABOVE_Z = 150 + TOOL_L            # approach height

# ---- Handover: choose ONE point, the rest follows from it ----
HANDOVER = (300, -200, 470)          # where the LEFT fingertips hold the cup (PLACEHOLDER)
TAKE_APPROACH = 60                # right comes the last 60 mm straight up

hx, hy, hz = HANDOVER
HANDOVER_L = (hx, hy, hz + TOOL_L)                           # left flange
HANDOVER_L_UP = (hx, hy, hz + TOOL_L + 50)                   # left after letting go
rim_z = hz - GRIP_FROM_RIM                                   # rim height in the air
R_TAKE = (hx, hy - CUP_RADIUS, rim_z + RIM_GRIP - TOOL_R)    # right flange, pointing up
R_BELOW = (R_TAKE[0], R_TAKE[1], R_TAKE[2] - TAKE_APPROACH)

# ---- Right arm, hardcoded (PLACEHOLDERS for x, y) ----
R_WAIT = (420, -150, 300)                                    # CHANGED: was z 200, too close to table/body
R_PLACE = (420, -200, CUP_H - RIM_GRIP + TOOL_R + 3)         # cup bottom 3 mm above the table
R_PLACE_ABOVE = (R_PLACE[0], R_PLACE[1], R_PLACE[2] + 80)    # turning to DOWN here flips the cup

# ---- Shared between the threads ----
LOCK = threading.Lock()           # robot commands one at a time (CommServer takes one connection)
STEP_Q = queue.Queue()            # step mode: arm threads hand their next command to the main thread
HEADERS = {}
failed = threading.Event()        # set if any arm fails, so the other one stops waiting
at_handover = threading.Event()   # left holds the cup at the handover
right_has_cup = threading.Event() # right closed on the rim
left_clear = threading.Event()    # left opened and moved up


# ================= GATEWAY =================
def acquire(token):
    """Take control (lease 5 s, renewed every 2 s). Falls back to ROBOT_TOKEN if refused."""
    for tok in dict.fromkeys([token, ROBOT_TOKEN]):
        if not tok:
            continue
        headers = {"Authorization": f"Bearer {tok}"}
        try:
            r = requests.post(f"{URL}/control/acquire", headers=headers, timeout=10)
        except requests.RequestException as e:
            sys.exit(f"Can't reach the gateway at {URL}. Is the PC on the Lynx network?\n({e})")
        if r.status_code in (401, 403):
            continue
        r.raise_for_status()
        HEADERS.update(headers)

        def renew():
            # CHANGED: no LOCK. Renew never reaches CommServer, so it must not wait
            # for a robot command, or the lease runs out (HTTP 409)
            while True:
                time.sleep(2)
                try:
                    requests.post(f"{URL}/control/renew", headers=HEADERS, timeout=5)
                except requests.RequestException as e:
                    print("lease renew failed:", e)

        threading.Thread(target=renew, daemon=True).start()
        return
    sys.exit('Gateway refused every token. Set $env:ROBOT_TOKEN = "<token>"')


def send(text):
    """One message to CommServer through the gateway, returns its answer."""
    with LOCK:
        r = requests.post(f"{URL}/robot/command", headers=HEADERS,
                          json={"command": text, "parameters": {}}, timeout=10)
    if not r.ok:
        return f"HTTP {r.status_code}"
    return str(r.json().get("message", r.json())).strip()


def do(arm, cmd):
    """Start one command on one arm, wait until THAT arm is done. The other arm keeps going."""
    if failed.is_set():
        raise RuntimeError("other arm failed")
    if STEP:
        go = threading.Event()
        STEP_Q.put((arm, cmd, go))       # the main thread asks you, then sets go
        wait_for(go)
    prefix = "R," if arm == "R" else ""
    reply = send(prefix + cmd)
    if reply != "STARTED":
        raise RuntimeError(f"{arm} {cmd} -> {reply}")
    while (status := send(prefix + "STATUS")) == "BUSY":
        time.sleep(0.1)
    print(f"  {arm}: {cmd:32s} {status}")
    if status != "DONE":
        raise RuntimeError(f"{arm} {cmd} -> {status}")


def pt(p, rot=""):
    """(400, 80, 300) -> "400.0,80.0,300.0", plus ",180,0,0" if a rotation is given."""
    s = ",".join(f"{v:.1f}" for v in p)
    return f"{s},{rot}" if rot else s


def wait_for(event):
    """Wait for the other arm (or for you in step mode), but stop if something failed."""
    while not event.wait(0.1):
        if failed.is_set():
            raise RuntimeError("other arm failed")


# ================= THE TWO ARMS =================
def left_job(x, y):
    do("L", "OPEN")
    do("L", pt((x, y, ABOVE_Z), DOWN))   # always send rotation on the first move after HOME
    do("L", pt((x, y, GRIP_Z)))
    do("L", "GRIP")
    do("L", pt((x, y, ABOVE_Z)))
    do("L", pt(HANDOVER_L))
    at_handover.set()                    # -> right: come and take it
    wait_for(right_has_cup)
    do("L", "OPEN")
    do("L", pt(HANDOVER_L_UP))
    left_clear.set()                     # -> right: you can pull it down
    do("L", "HOME")                      # out of the camera view for the next capture


def right_job():
    do("R", "OPEN")
    do("R", pt(R_WAIT, UP))              # moves WHILE the left arm picks (no move if already there)
    wait_for(at_handover)
    do("R", pt(R_BELOW))                 # under the cup first, so the last bit is straight up
    do("R", pt(R_TAKE))
    do("R", "GRIP")
    right_has_cup.set()                  # -> left: let go
    wait_for(left_clear)
    do("R", pt(R_BELOW))
    do("R", pt(R_PLACE_ABOVE, DOWN))     # turn over: cup now upright
    do("R", pt(R_PLACE))
    do("R", "OPEN")
    do("R", pt(R_PLACE_ABOVE))
    do("R", pt(R_WAIT, UP))              # ready for the next cup instead of HOME


def guarded(name, job, *args):
    try:
        job(*args)
    except Exception as e:
        print(f"STOP {name}: {type(e).__name__}: {e}")
        failed.set()


def main():
    global URL, STEP
    ap = argparse.ArgumentParser(description="Left picks the camera's cup, right takes it and places it")
    ap.add_argument("--x", type=float, required=True, help="robot X of the cup, mm")
    ap.add_argument("--y", type=float, required=True, help="robot Y of the cup, mm")
    ap.add_argument("--z", type=float, default=120, help="not used, grip height is fixed")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--token", default=None)
    ap.add_argument("--step", action="store_true", help="ask before every command")
    args = ap.parse_args()

    URL = args.url.rstrip("/")
    STEP = args.step
    print(f"Left flange at handover {HANDOVER_L}, right takes at {R_TAKE}, from below {R_BELOW}")
    print(f"Right waits at {R_WAIT}, places at {R_PLACE}, from {R_PLACE_ABOVE}")
    acquire(args.token)

    left = threading.Thread(target=guarded, args=("L", left_job, args.x, args.y), daemon=True)
    right = threading.Thread(target=guarded, args=("R", right_job), daemon=True)
    left.start()
    right.start()

    # The main thread does all the asking in step mode, and keeps Ctrl+C working
    try:
        while left.is_alive() or right.is_alive():
            try:
                arm, cmd, go = STEP_Q.get(timeout=0.2)
            except queue.Empty:
                continue
            input(f"  next {arm}: {cmd}  (Enter) ")
            go.set()
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C). An arm that is moving finishes its current move.")
        failed.set()

    print("Failed." if failed.is_set() else "Done.")
    sys.exit(1 if failed.is_set() else 0)


if __name__ == "__main__":
    main()