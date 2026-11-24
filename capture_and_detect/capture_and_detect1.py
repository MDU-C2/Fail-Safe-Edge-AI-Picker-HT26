"""
Get a photo from the camera gateway, find the cups with the trained YOLO11-seg model,
and let both YuMi arms move one cup: the left arm picks it, hands it over to the right
arm, which turns it over and places it.

Every time you press Enter:
    1. get RGB + depth (aligned to RGB) from the Robot & Camera Gateway over HTTP
    2. YOLO11-seg -> one mask per CUP, CUP BASE and CUP HANDLE
    3. per object: mask centre (u, v) + median depth inside the mask
       -> camera X, Y, Z (mm) with the colour camera intrinsics
       -> robot X, Y, Z (mm) with ../Calibration/calibration.json
    4. save to Data/Detections/:
           rgb_<stamp>.jpg, depth_<stamp>.png (16-bit mm), annotated_<stamp>.jpg
           detections_<stamp>.json and latest.json   all objects of this capture
           detections.csv                            one row per object, all captures
           cups_<stamp>.json and cups.json           the cups and what happened to them
           grips.csv                                 per moved cup: base diameter and where
                                                     each gripper closed on it (grip level)
    5. every reachable cup is moved, most certain first, each to the next free spot in
       placing_grid.json; it stops at the first failure. The dual-arm movement uses
       handover_plan.handover_jobs, the same smooth buffered paths as the UI:
           left:  open, LEFT_READY, open, approach/down, grip, lift/handover, wait, release, up
           right: open, via/beside, wait, open, slide under/up, grip, wait, lower, turn/place
                  then release and up (both grippers stay open, ready for the next cup)
       The arms overlap independent moves and wait at the shared plan's handover barriers.
       Neither arm goes home between cups. When every cup is done (or the grid is full),
       both arms go home: the left first, then the right. After a failure they stay where
       they are.
       With --left-only the right arm stays still: the left arm picks the cup, lifts it to
       TRAVEL_Z, puts it back where it stood, lets go and stays above it.

A cup is a CUP BASE inside a detected CUP (the robot's joint was once seen as a CUP BASE).

The model is picked by the device; the weights sit next to this file:
    Westermo Lynx  -> best_ncnn_model/   (set PICKER_DEVICE=lynx in the Lynx container)
    anything else  -> best.pt            (Raspberry Pi, development laptop)

The robot is driven through the gateway's RWS API (POST /robot/rws/path|move|gripper, see
gateway/scripts/client_template.py), so the RAPID program in rapid/ must be running
(python rapid/rapidctl.py status). The camera gives each cup's x, y (gripper TCP, tGrip, as in
the calibration); the heights are fixed (PICK_Z, TRAVEL_Z), since the cups stand on the table.
The arm modules move tool0 (the flange): Arm.goto() and the shared planner add GRIPPER_TCP_Z,
and the fixed hand-over poses are flange positions. Dual-arm travel uses the shared
planner's 175 mm TCP height; --left-only keeps TRAVEL_Z (250 mm).

The gateway runs in strict mode and needs a token with robot.control (the operator token).
Set it once in the terminal (never put it in this file):
    PowerShell:  $env:GATEWAY_TOKEN = "<token>"
    Linux:       export GATEWAY_TOKEN="<token>"

Usage:
    py capture_and_detect.py
    py capture_and_detect.py --gateway http://169.254.125.202:8000 --conf 0.6
    py capture_and_detect.py --left-only

NEW: --standby (the fail-over tier, e.g. on the Raspberry Pi):
    python capture_and_detect.py --gateway http://192.168.125.20:8000 --standby
    Waits while another client holds the control lease and tries to take it every
    --poll seconds. When it gets control (the other client stopped, or its lease ran
    out 5 s after it died) it keeps the lease for the whole run and:
        1. waits until neither arm is busy (a job the other client started finishes)
        2. reads both grippers: neither open nor closed empty means it may hold a cup,
           then it waits for a person to check and press Enter
        3. homes both arms from where they really are (up first, as go_home does)
        4. captures, detects and moves cups without Enter; when no cup was moved it
           captures again after --idle seconds
    After a failed cup it waits for a person. If it loses control (a client with a
    higher priority took over) it goes back to waiting. Ctrl+C quits and releases control.
"""

import argparse
import csv
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from ultralytics import YOLO

import depth_average                               # depth of several frames, mask edge left out
from handover_plan import handover_jobs

# ================================================================
# Settings
# ================================================================

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE.parent / "Calibration/calibration.json"
KEEP_CLASSES = ["CUP", "CUP BASE", "CUP HANDLE"]
CUP_Z_MIN, CUP_Z_MAX = -30, 150  # robot Z (mm) of a CUP BASE on a cup standing on the table (was 10; a mug read -15)

CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px", "robot_z_frames_mm"]

GRIP_FIELDS = ["capture", "cup", "diameter_mm", "grip_left_mm", "grip_right_mm", "status"]

# ---------------- Robot moves ----------------
JOB_TIMEOUT = 90           # s, longer than the gateway's own 60 s job timeout
SPEED = 250               # TCP speed, mm/s (1..1500), sent with every move and home
ACCELERATION = 100         # legacy left-only moves; smooth paths use handover_plan's settings
RAMP = 100                 # legacy left-only ramp; smooth paths use handover_plan's settings
HOME_SPEED = 200          # mm/s for going home (grippers empty then), faster than SPEED
HOME_UP_Z = 600            # before going home each arm first goes straight up to this flange z
LEFT_HOME_Y = 400          # then the left arm goes sideways to the left (+y) up to here (gateway max 500)
FLIP_SPEED = 100           # mm/s: slower while the right arm turns the (heavy) cup over
PICK_SPEED = 100           # mm/s: left arm down to the cup, up with it, and to the hand-over

LEFT_DOWN = (180, 0, 180)  # rx, ry, rz (deg): left gripper straight down (as in the calibration)

# ---------------- Left arm: picking (gripper TCP, mm) ----------------
# The camera gives x, y; the heights are fixed because the cups stand on the table.
GRIPPER_TCP_Z = 159        # tGrip: the TCP is 159 mm past the flange
PICK_Z = 70             # TCP height when gripping a cup (was 80, then 70: gripped too high on the base; 115 with the tray)
# Test (--camera-z): grip height from each cup's own camera z (cups.json "z", CUP BASE) instead of PICK_Z.
# Picked cups read base z ~35, so z + 35 gives the tested 70 for a typical cup.
CAMERA_Z_OFFSET = 20       # TCP height above the cup's camera base z
CAMERA_Z_FLOOR = 40        # never lower than this (30 below the tested PICK_Z): limits a crash into the tray
USE_CAMERA_Z = False       # set by --camera-z
DEPTH_FRAMES = 5           # depth = pixel-by-pixel median of this many camera frames (depth_average.py)
TRAVEL_Z = 250             # left-only TCP travel height; dual-arm uses the shared plan's 175 mm
HANDLE_MAX_DIST = 100      # a CUP HANDLE further than this (mm) from a cup's base centre is not its handle
GRIP_MARGIN = 70           # left jaws open to the cup base diameter + this (mm)
LEFT_GAP_CLOSED = 75       # left jaws, measured: 75 mm at position 0, 95 at 10, 125 at 25 (2 mm per position)
Z_MIN = 199                # both arms: flange z stays within Z_MIN..GATEWAY_Z_MAX (tray top 170),
                           # except going down to grip (pick) or to put the cup down (place)
X_MIN, X_MAX = 350, 800    # cups outside this area are skipped
Y_MIN, Y_MAX = -230, 420

# ---------------- Left-only approach and placement storage ----------------
# Dual-arm poses, blends and acceleration settings live only in handover_plan.py.
LEFT_READY = (400, 100, 450)
GATEWAY_Z_MAX = 700        # flange z limit set on the Pi (GATEWAY_RWS_SAFE_ZONE_MAX)
PLACE_FILE = HERE / "placing_grid.json"   # where the cups go: id, x, y, z (flange), occupied

# ---------------- NEW: standby / takeover (--standby) ----------------
STANDBY_POLL = 1.0         # s between tries to take control while another client holds it (--poll)
AUTO_IDLE = 5.0            # s before capturing again when no cup was moved (--idle)
GRIP_EMPTY_MAX = 1.0       # gripper position (mm) at or below this: closed on nothing
#GRIP_OPEN_MIN = 24.0      # (right gripper reads 23.3 when fully open)
GRIP_OPEN_MIN = 23.0       # at or above this: fully open; anything in between may hold a cup
SAFE_ZONE_X_MIN = 180      # gateway safe zone starts at x 180 (GATEWAY_RWS_SAFE_ZONE_MIN); home is outside it,
                           # so an arm with x below this is at/near home: home it directly, no "up first"


def log(*parts):
    print(*parts, flush=True)


def load_token():
    """GATEWAY_TOKEN from the environment, or (Windows) where setx saved it."""
    token = os.environ.get("GATEWAY_TOKEN")
    if not token and os.name == "nt":
        # setx saves the token for new windows only; read it straight from where setx stores it
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token = winreg.QueryValueEx(key, "GATEWAY_TOKEN")[0]
        except OSError:
            pass
    if not token:
        raise SystemExit('No gateway token. Set it first, e.g. in PowerShell:  $env:GATEWAY_TOKEN = "<token>"')
    return token


# ================================================================
# Gateway client
# ================================================================

class Gateway:
    """HTTP client for the Robot & Camera Gateway (strict mode: bearer token)."""

    def __init__(self, url, token):
        self.api = url.rstrip("/") + "/api/v1"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.holding = False                       # NEW: standby holds the lease for the whole run
        self.lost = threading.Event()              # NEW: set when a renew is refused (control lost)
        self._renew_stop = None                    # NEW: stops the whole-run renew thread

    def get(self, path):
        """GET; stop with a clear message on auth/connection errors."""
        try:
            r = requests.get(self.api + path, headers=self.headers, timeout=15)
        except requests.RequestException as e:
            raise SystemExit(f"Cannot reach the gateway at {self.api}: {e}")
        if r.status_code in (401, 403):
            raise SystemExit(f"Gateway refused the token ({r.status_code}): {r.text}")
        r.raise_for_status()
        return r

    def post(self, path, timeout, body=None):
        return requests.post(self.api + path, headers=self.headers, json=body, timeout=timeout)

    def intrinsics(self):
        """Check the camera is connected; return the colour camera's {fx, fy, cx, cy}."""
        status = self.get("/camera/status").json()
        if not status.get("connected"):
            raise SystemExit(f"The gateway is running but its camera is not connected: {status}")
        K = self.get("/camera/intrinsics").json()
        return {name: K[name] for name in ("fx", "fy", "cx", "cy")}

    def frames(self, tries=5):
        """Latest RGB (BGR, HxWx3) + depth (uint16 mm, HxW) from the same camera frame."""
        for _ in range(tries):
            r_rgb, r_depth = self.get("/camera/rgb"), self.get("/camera/depth")
            if r_rgb.headers["X-Frame-Sequence"] == r_depth.headers["X-Frame-Sequence"]:
                rgb = frame_array(r_rgb, np.uint8, 3).transpose(1, 2, 0).copy()   # planar BGR
                return rgb, frame_array(r_depth, np.uint16)[0]
            # a new frame arrived in between - try again
        raise RuntimeError("Could not get a matching RGB + depth pair from the gateway")

    @contextmanager
    def control(self):
        """Hold robot control for the with-block: the lease lasts 5 s, so a thread
        renews it every 2 s; it is released at once at the end."""
        if self.holding:                           # NEW: standby already holds it for the whole run,
            yield                                  # so neither acquire again (409) nor release per cup
            return
        r = self.post("/control/acquire", timeout=10)
        if r.status_code != 200:
            raise SystemExit(f"Cannot take robot control ({r.status_code}): {r.text}")
        stop = threading.Event()

        def renew():
            while not stop.wait(2):
                try:
                    self.post("/control/renew", timeout=5).raise_for_status()
                except requests.RequestException as e:
                    log(f"  lease renew failed: {e}")

        threading.Thread(target=renew, daemon=True).start()
        try:
            yield
        finally:
            stop.set()
            try:
                self.post("/control/release", timeout=5)
            except requests.RequestException:
                pass                               # the lease runs out by itself after 5 s

    # ---------------- NEW: standby ----------------

    def try_acquire(self):
        """One try to take control. (True, lease) if we got it, (False, why) if not:
        another client holds it (409), or the gateway cannot be reached."""
        try:
            r = self.post("/control/acquire", timeout=10)
        except requests.RequestException as e:
            return False, f"gateway unreachable ({type(e).__name__})"
        if r.status_code in (401, 403):
            raise SystemExit(f"Gateway refused the token ({r.status_code}): {r.text}")
        if r.status_code == 200:
            return True, r.json().get("lease", {})
        try:
            return False, r.json().get("detail", r.text)
        except ValueError:
            return False, f"HTTP {r.status_code}"

    def hold(self):
        """Keep the lease we just acquired until drop(): renew every 2 s. A refused renew
        (another client took control) sets `lost`; a network error is retried."""
        self.lost.clear()
        self.holding = True
        stop = self._renew_stop = threading.Event()

        def renew():
            while not stop.wait(2):
                try:
                    r = self.post("/control/renew", timeout=5)
                except requests.RequestException as e:
                    log(f"  lease renew failed: {e}")
                    continue
                if r.status_code != 200:
                    log(f"  CONTROL LOST: renew refused ({r.status_code}): {r.text}")
                    self.lost.set()
                    return

        threading.Thread(target=renew, daemon=True).start()

    def drop(self):
        """Stop renewing and release the lease (if we still have it)."""
        if self._renew_stop:
            self._renew_stop.set()
        self.holding = False
        if not self.lost.is_set():
            try:
                self.post("/control/release", timeout=5)
            except requests.RequestException:
                pass                               # it runs out by itself after 5 s

    def arm_busy(self, arm):
        """True while the arm works on a job, also one another client started."""
        return self.get(f"/robot/rws/status?arm={arm}").json().get("message") == "BUSY"

    def arm_pose(self, arm):
        """tool0 in wobj0: ((x, y, z), (rx, ry, rz)), the same frame Arm.move uses."""
        p = self.get(f"/robot/arms/{arm}/pose?tool=tool0&wobj=wobj0").json()
        return (p["x"], p["y"], p["z"]), zyx_angles(p)

    def robot_job(self, arm, path, body):
        """POST one job to /robot/rws/<path> and wait until the arm has done it.
        True if the robot accepts the job and reports DONE; prints the reply either way."""
        try:
            r = self.post(f"/robot/rws/{path}", timeout=JOB_TIMEOUT, body={"arm": arm, **body})
        except requests.RequestException as e:
            log(f"    {arm}: {path} {body} -> {e}")
            return False
        # 200: {"accepted", "message"}; 409 busy / no lease / RAPID stopped, 503 unreachable, 504 timeout
        result = r.json() if r.status_code == 200 else {}
        reply = result.get("message") if r.status_code == 200 else f"HTTP {r.status_code}: {r.text}"
        log(f"    {arm}: {path} {body} -> {reply}")
        return result.get("accepted") is True and reply == "DONE"


def frame_array(r, dtype, planes=1):
    """Raw frame bytes -> (planes, H, W) array, sized from the X-Frame-* headers."""
    w, h = int(r.headers["X-Frame-Width"]), int(r.headers["X-Frame-Height"])
    return np.frombuffer(r.content, dtype).reshape(planes, h, w)


def zyx_angles(pose):
    """NEW (copied from gateway/scripts/rws_move.py, now gatewayctl.py): (rx, ry, rz) in degrees of the pose's
    quaternion, the inverse of RAPID's OrientZYX(rz, ry, rx)."""
    w, x, y, z = (pose[name] for name in ("q1", "q2", "q3", "q4"))
    rx = np.arctan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y))
    ry = np.arcsin(max(-1.0, min(1.0, 2 * (w * y - x * z))))
    rz = np.arctan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))
    return tuple(round(float(np.degrees(a)), 3) + 0.0 for a in (rx, ry, rz))


# ================================================================
# Robot
# ================================================================

class RobotError(RuntimeError):
    pass


class Arm:
    """One YuMi arm. Every method waits until the arm is done and raises RobotError
    if it fails, or if `abort` is set (the other arm failed)."""

    def __init__(self, gateway, name, abort, offset=(0, 0, 0)):
        self.gateway = gateway
        self.name = name
        self.abort = abort
        self.offset = offset
        self.last = None             # last move (flange, rotation), for going up before home

    def job(self, path, body):
        if self.abort.is_set():
            raise RobotError("the other arm failed")
        if not self.gateway.robot_job(self.name, path, body):
            raise RobotError(f"{self.name} {path} failed")

    def move(self, flange, rotation, speed=SPEED, acceleration=ACCELERATION, pick=False):
        """tool0 (the flange) to (x, y, z) mm with rotation (rx, ry, rz) degrees.
        Refused outside z Z_MIN..GATEWAY_Z_MAX; pick=True allows lower (gripping / putting down)."""
        (x, y, z), (rx, ry, rz) = flange, rotation
        if z > GATEWAY_Z_MAX or (z < Z_MIN and not pick):
            raise RobotError(f"{self.name} move to z {z} refused: outside z {Z_MIN}..{GATEWAY_Z_MAX}")
        self.job("move", {"x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz,
                          "speed": speed, "acceleration": acceleration, "ramp": RAMP})
        self.last = (flange, rotation)

    def goto(self, x, y, z, rz=LEFT_DOWN[2], speed=SPEED, pick=False):
        """Gripper TCP to x, y seen by the camera (plus the calibration's tag_offset in x, y)
        at height z (robot mm), gripper down and turned to rz degrees about the vertical:
        the flange is GRIPPER_TCP_Z above the TCP. pick=True: going down to grip (may go below Z_MIN)."""
        ox, oy, _ = self.offset
        rotation = (LEFT_DOWN[0], LEFT_DOWN[1], rz)
        self.move((x + ox, y + oy, z + GRIPPER_TCP_Z), rotation, speed, pick=pick or z == PICK_Z)

    def run_plan_job(self, job):
        """Dispatch one shared smooth-plan step, preserving its path and wait flag.

        As with the UI, only the final linear picking/placing target may be
        below the ordinary travel height. A failed path is never retried as moves.
        """
        body = dict(job["body"])
        if body.pop("arm") != self.name:
            raise RobotError(f"{self.name}: wrong arm for step {job['name']}")
        if job["endpoint"] == "/robot/rws/gripper":
            self.job("gripper", body)
            return
        if job["endpoint"] != "/robot/rws/path":
            raise RobotError(f"Unsupported smooth-plan endpoint: {job['endpoint']}")
        points = body["points"]
        for index, point in enumerate(points):
            pick = ((job["name"], self.name) in {
                        ("approach_cup", "left"), ("right_place", "right")}
                    and index == len(points) - 1 and point.get("linear") is True)
            z = point["z"]
            if z > GATEWAY_Z_MAX or (z < Z_MIN and not pick):
                raise RobotError(f"{self.name} path to z {z} refused: outside z {Z_MIN}..{GATEWAY_Z_MAX}")
        self.job("path", body)
        target = points[-1]
        self.last = (tuple(target[axis] for axis in ("x", "y", "z")),
                     tuple(target[axis] for axis in ("rx", "ry", "rz")))

    def gripper(self, action):
        """action: "open" (fully) or "grip" (close until it holds the cup, 20 N)."""
        self.job("gripper", {"action": action})

    def open_left_to(self, aperture):
        """Left jaws to a gap of aperture mm (limited to what they can do); None = fully open."""
        if aperture is None:
            self.gripper("open")
            return
        position = min(max((aperture - LEFT_GAP_CLOSED) / 2, 0), 25)
        self.job("gripper", {"action": "move", "position": round(position, 1)})

    def grip_level(self):
        """Where the fingers are now, mm on the SmartGripper's scale (0 closed .. 25 open);
        right after a grip that is how far it closed on the cup. None if the gateway
        cannot tell (no SmartGripper, older gateway); the job goes on either way."""
        try:
            r = requests.get(f"{self.gateway.api}/robot/arms/{self.name}/gripper",
                             headers=self.gateway.headers, timeout=10)
        except requests.RequestException as e:
            log(f"    {self.name}: grip level unknown ({e})")
            return None
        if r.status_code != 200:
            log(f"    {self.name}: grip level unknown (HTTP {r.status_code}: {r.text})")
            return None
        level = r.json()["position"]
        log(f"    {self.name}: grip level {level} mm")
        return level

    def home(self):
        """Straight up as high as the arm reaches there (HOME_UP_Z, else 50 mm lower each try;
        an unreachable target does not move the arm), the left arm then sideways to LEFT_HOME_Y,
        then home (the home move swings the whole arm)."""
        if self.last:
            (x, y, z), rotation = self.last
            for up in range(HOME_UP_Z, int(z), -50):
                try:
                    self.move((x, y, up), rotation)            # up
                    z = up
                    break
                except RobotError:
                    if self.abort.is_set():
                        raise
                    log(f"    {self.name}: z {up} out of reach here, trying lower")
            if self.name == "left" and y < LEFT_HOME_Y:
                self.move((x, LEFT_HOME_Y, z), rotation)       # then left, away from the right arm
        self.job("home", {"speed": HOME_SPEED, "acceleration": ACCELERATION, "ramp": RAMP})


class Handover:
    """Run the UI's shared smooth plan using the terminal's existing control lease."""

    def __init__(self, left, right, abort):
        self.left, self.right, self.abort = left, right, abort

    def run(self, cup, left_only=False, place=None):
        """True if the cup was placed. Ctrl+C stops both arms after their current job.
        left_only: the left arm picks the cup, lifts it and puts it back, no hand-over.
        place: the place spot from placing_grid.json (needed for the hand-over).
        Sets cup["grip_left_mm"] and cup["grip_right_mm"]: where each gripper closed
        on the cup (None if it did not get that far or the gateway cannot tell)."""
        self.abort.clear()
        cup["grip_left_mm"] = None
        self.grip_right_mm = None
        if left_only:
            jobs = [(self._pick_only_job, (cup,))]
        else:
            try:
                plan = handover_jobs(
                    cup, place=place, tag_offset=self.left.offset, speed=SPEED,
                    camera_z=USE_CAMERA_Z, home_after_cup=False,
                    phase_speeds={"pick_lift": PICK_SPEED, "handover": None,
                                  "flip": FLIP_SPEED, "placement": None})
                cup["pick_z"] = self.pick_z(cup)
            except Exception as error:
                log(f"  STOP: {error}")
                self.abort.set()
                cup["grip_right_mm"] = None
                return False
            log("  Smooth dual-arm handover (no automatic home).")
            completed = {job["name"]: threading.Event() for job in plan}
            jobs = [(self._plan_arm, (arm, plan, completed, cup))
                    for arm in (self.left, self.right)]
        finished = [threading.Event() for _ in jobs]
        arms = [threading.Thread(target=self._guarded, args=(job, *args),
                                 kwargs={"finished": done}, daemon=True)
                for (job, args), done in zip(jobs, finished)]
        for arm in arms:
            arm.start()
        try:
            self._wait_workers(finished)
        except KeyboardInterrupt:
            log("\nStopped (Ctrl+C). An arm that is moving finishes its current move.")
            self.abort.set()
            raise
        finally:
            # Keep the caller's control lease until in-flight jobs finish, even
            # on Ctrl+C. Waiting peers see abort and dispatch no further jobs.
            while not all(done.is_set() for done in finished):
                try:
                    self._wait_workers(finished)
                except KeyboardInterrupt:
                    self.abort.set()
            cup["grip_right_mm"] = self.grip_right_mm
        return not self.abort.is_set()

    @staticmethod
    def _wait_workers(finished):
        # Completion events describe actual job completion, even on Python
        # versions where Ctrl+C can interrupt Thread.join's bookkeeping.
        for done in finished:
            while not done.wait(0.1):
                pass

    def _plan_arm(self, arm, plan, completed, cup):
        """One worker per arm; cross-arm barriers come directly from the shared plan."""
        for job in plan:
            if job["body"]["arm"] != arm.name:
                continue
            for name in job["after"]:
                self._wait_for(completed[name])
            arm.run_plan_job(job)
            if job["name"] == "grip_cup":
                cup["grip_left_mm"] = arm.grip_level()
            elif job["name"] == "right_grip":
                self.grip_right_mm = arm.grip_level()
            completed[job["name"]].set()

    @staticmethod
    def pick_z(cup):
        """Grip height: PICK_Z, or with --camera-z the cup's camera z + CAMERA_Z_OFFSET (not below CAMERA_Z_FLOOR)."""
        if not USE_CAMERA_Z:
            return PICK_Z
        z = round(cup["z"] + CAMERA_Z_OFFSET, 1)
        log(f"    camera z: base {cup['z']} + {CAMERA_Z_OFFSET} = grip at {z} (fixed would be {PICK_Z})")
        if z < CAMERA_Z_FLOOR:
            log(f"    camera z {z} below the floor {CAMERA_Z_FLOOR}: using {CAMERA_Z_FLOOR}")
            z = CAMERA_Z_FLOOR
        return z

    def _pick(self, cup):
        x, y = cup["x"], cup["y"]                  # grip height: PICK_Z, or the cup's camera z (--camera-z)
        rz = cup.get("rz", LEFT_DOWN[2])           # jaws at 90 degrees to the handle (set_grip_rotation)
        z = self.pick_z(cup)
        cup["pick_z"] = z
        arm = self.left
        arm.gripper("grip")                        # closed while it travels
        arm.move(LEFT_READY, LEFT_DOWN)            # from home via above the work area
        arm.goto(x, y, TRAVEL_Z, rz)               # above the cup, jaws turned to the handle
        arm.open_left_to(cup.get("aperture"))      # only here, right above the cup
        arm.goto(x, y, z, rz, PICK_SPEED, pick=True)   # slow down to the cup
        arm.gripper("grip")
        cup["grip_left_mm"] = arm.grip_level()     # recorded with the base diameter (grips.csv)
        arm.goto(x, y, TRAVEL_Z, rz, PICK_SPEED)   # slow up with the cup

    def _pick_only_job(self, cup):
        arm = self.left
        self._pick(cup)
        arm.goto(cup["x"], cup["y"], cup.get("pick_z", PICK_Z), cup.get("rz", LEFT_DOWN[2]),
                 pick=True)                        # put it back where it stood
        arm.gripper("open")
        arm.goto(cup["x"], cup["y"], TRAVEL_Z, cup.get("rz", LEFT_DOWN[2]))
        #arm.gripper("grip")                        # closed on the way home
        #arm.home()                                 # up first, then home

    def _wait_for(self, event):
        """Wait for the other arm, but stop if it failed."""
        while not event.wait(0.1):
            if self.abort.is_set():
                raise RobotError("the other arm failed")

    def _guarded(self, job, *args, finished):
        try:
            job(*args)
        except Exception as e:
            log(f"  STOP: {e}")
            self.abort.set()
        finally:
            finished.set()


# ================================================================
# Detection
# ================================================================

def load_model():
    """NCNN on the Lynx (the Lynx container sets PICKER_DEVICE=lynx), best.pt elsewhere."""
    lynx = os.environ.get("PICKER_DEVICE", "").lower() == "lynx"
    path = HERE / ("best_ncnn_model" if lynx else "best.pt")
    if not path.exists():
        raise SystemExit(f"Model not found: {path}\n"
                         f"Put best.pt and best_ncnn_model/ in the same folder as capture_and_detect.py.")
    log(f"Loading {path.name} ...")
    return YOLO(str(path), task="segment")


class Calibration:
    """Camera mm -> robot mm:  robot = scale * R @ camera + t"""

    def __init__(self, path):
        with open(path) as f:
            cal = json.load(f)
        self.scale, self.R, self.t = cal["scale"], np.array(cal["R"]), np.array(cal["t"])
        # tag centre minus gripper TCP (robot mm); Arm.goto() adds its x, y to every pick move
        self.offset = tuple(cal.get("tag_offset", (0, 0, 0)))

    def to_robot(self, x, y, z):
        return tuple(self.scale * self.R @ np.array([x, y, z]) + self.t)


class Detector:
    """YOLO segmentation + aligned depth -> one dict per object (pixels, camera mm, robot mm)."""

    def __init__(self, model, calibration, K, conf):
        self.model, self.calibration, self.K, self.conf = model, calibration, K, conf
        self.classes = [i for i, n in model.names.items() if n in KEEP_CLASSES]
        log("Detecting:", [model.names[i] for i in self.classes])

    def analyse(self, rgb, depth, depths=()):
        """Run YOLO on one frame; return (list of objects, annotated image).
        depth: the averaged depth used; depths: the single frames it came from (Z per frame is saved too)."""
        result = self.model.predict(rgb, conf=self.conf, classes=self.classes,
                                    retina_masks=True, verbose=False)[0]
        objects, keep, boxes = [], [], []
        masks = [] if result.masks is None else result.masks.data
        for j, (box, mask) in enumerate(zip(result.boxes, masks)):
            mask = mask.cpu().numpy().astype(bool)
            if mask.any():
                keep.append(j)
                boxes.append(box.xyxy[0].tolist())
                objects.append(self.describe(self.model.names[int(box.cls)], float(box.conf), mask, depth, depths))
        mark_cup_bases(objects, boxes)

        annotated = result[keep].plot() if keep else rgb.copy()
        for o in objects:                          # mark the point we report
            cv2.drawMarker(annotated, (int(o["x_px"]), int(o["y_px"])), (0, 0, 255),
                           cv2.MARKER_CROSS, 25, 3)
        return objects, annotated

    def to_robot(self, u, v, z):
        """Pixel (u, v) at camera distance z -> (camera x, y) and robot (x, y, z), mm (pinhole model)."""
        x = (u - self.K["cx"]) * z / self.K["fx"]
        y = (v - self.K["cy"]) * z / self.K["fy"]
        return x, y, self.calibration.to_robot(x, y, z)

    def describe(self, name, confidence, mask, depth, depths=()):
        """Mask centre (u, v), median depth inside the mask -> camera and robot mm.
        depths: the single frames behind the averaged depth; their Z is saved per frame."""
        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())
        diameter_px = 2 * np.sqrt(mask.sum() / np.pi)   # of a circle with the mask's area
        z_frames, robot_z_frames = [], []          # the same object in each single frame
        for d in depths:
            zf = depth_average.mask_depth(d, mask)  # median inside the mask, its edge left out
            z_frames.append(mm(zf))
            robot_z_frames.append(mm(self.to_robot(u, v, zf)[2][2]) if zf else None)
        valid = [zf for zf in z_frames if zf]
        if valid:                                  # Z = mean of the frames (robot Z then = mean of robot_z_frames)
            z = sum(valid) / len(valid)
        else:                                      # no single frames given: the depth image as it is
            z = depth_average.mask_depth(depth, mask)

        x = y = None
        robot = (None, None, None)
        if z:
            x, y, robot = self.to_robot(u, v, z)
        diameter = diameter_px * z / self.K["fx"] if z else None   # pixels -> mm at that depth
        return {
            "class": name,
            "confidence": round(confidence, 3),
            "x_mm": mm(x),
            "y_mm": mm(y),
            "x_px": round(u, 1),
            "y_px": round(v, 1),
            "z_mm": mm(z),                         # distance from the camera
            "robot_x_mm": mm(robot[0]),
            "robot_y_mm": mm(robot[1]),
            "robot_z_mm": mm(robot[2]),
            "mask_area_px": int(mask.sum()),
            "diameter_mm": mm(diameter),
            "z_frames_mm": z_frames,               # camera distance in each single frame
            "robot_z_frames_mm": robot_z_frames,   # robot Z in each single frame (robot_z_mm = from the averaged depth)
        }


def mm(value):
    """Round to 0.1 mm; None (no depth) stays None."""
    return None if value is None else round(float(value), 1)


def mark_cup_bases(objects, boxes):
    """Set "is_cup" on every CUP BASE. It is a cup if one of these is true (the robot's joint
    was once detected as CUP BASE, with no CUP around it and high above the table):
        in_cup       its centre lies inside a detected CUP
        near_handle  a CUP HANDLE is next to it (within half a base size)
        cup_height   its robot Z is that of a cup on the table (CUP_Z_MIN..CUP_Z_MAX); straight
                     from above, an upside-down mug often gives only a CUP BASE and no CUP"""
    cup_boxes = [b for o, b in zip(objects, boxes) if o["class"] == "CUP"]
    handle_boxes = [b for o, b in zip(objects, boxes) if o["class"] == "CUP HANDLE"]
    for o, (bx1, by1, bx2, by2) in zip(objects, boxes):
        if o["class"] != "CUP BASE":
            continue
        o["in_cup"] = any(x1 <= o["x_px"] <= x2 and y1 <= o["y_px"] <= y2
                          for x1, y1, x2, y2 in cup_boxes)
        mx, my = (bx2 - bx1) / 2, (by2 - by1) / 2
        o["near_handle"] = any(x1 <= bx2 + mx and x2 >= bx1 - mx and y1 <= by2 + my and y2 >= by1 - my
                               for x1, y1, x2, y2 in handle_boxes)
        o["cup_height"] = o["robot_z_mm"] is not None and CUP_Z_MIN <= o["robot_z_mm"] <= CUP_Z_MAX
        o["is_cup"] = o["in_cup"] or o["near_handle"] or o["cup_height"]


def set_grip_rotation(cups, objects):
    """Set each cup's "rz" so the jaws close at 90 degrees to the line cup centre -> handle.
    At LEFT_DOWN the jaws close along robot X. The jaws are interchangeable, so the turn stays
    within +-90 degrees of LEFT_DOWN. A cup with no handle of its own keeps LEFT_DOWN."""
    closest = {}                                   # cup id -> (distance, handle x, handle y)
    for o in objects:
        if o["class"] != "CUP HANDLE" or o["robot_x_mm"] is None or not cups:
            continue
        hx, hy = o["robot_x_mm"], o["robot_y_mm"]
        dist, cup = min(((np.hypot(hx - c["x"], hy - c["y"]), c) for c in cups), key=lambda d: d[0])
        if dist <= HANDLE_MAX_DIST and dist < closest.get(cup["id"], (np.inf,))[0]:
            closest[cup["id"]] = (dist, hx, hy)
    for cup in cups:
        cup["rz"] = LEFT_DOWN[2]
        cup["has_handle"] = cup["id"] in closest
        if cup["has_handle"]:
            _, hx, hy = closest[cup["id"]]
            angle = np.degrees(np.arctan2(hy - cup["y"], hx - cup["x"]))
            cup["rz"] = round(float(LEFT_DOWN[2] + angle % 180 - 90), 1)


def find_cups(objects):
    """Every CUP BASE that is a cup (see mark_cup_bases) = one cup, most certain first.
    Cups outside X_MIN..X_MAX, Y_MIN..Y_MAX get status "out of reach"."""
    cups = []
    for o in sorted(objects, key=lambda o: o["confidence"], reverse=True):
        if o["class"] != "CUP BASE" or o["robot_x_mm"] is None:
            continue
        if not o["is_cup"]:
            log(f"  CUP BASE at X={o['robot_x_mm']} Y={o['robot_y_mm']} Z={o['robot_z_mm']}: no CUP "
                f"around it, no handle next to it, not at cup height - ignored (false detection?)")
            continue
        x, y = o["robot_x_mm"], o["robot_y_mm"]
        reachable = X_MIN <= x <= X_MAX and Y_MIN <= y <= Y_MAX
        diameter = o["diameter_mm"]
        cups.append({"id": len(cups) + 1, "confidence": o["confidence"], "x": x, "y": y,
                     "z": o["robot_z_mm"], "z_frames": o.get("robot_z_frames_mm", []),
                     "diameter": diameter,
                     "aperture": None if diameter is None else round(diameter + GRIP_MARGIN, 1),
                     "status": "waiting" if reachable else "out of reach"})
    set_grip_rotation(cups, objects)
    return cups


def print_objects(objects):
    for o in objects:
        log(f"   {o['class']:<11} {o['confidence']:.2f}")
        if not o["z_mm"]:
            log("      no depth")
            continue
        log(f"      YOLO (camera):     X={o['x_mm']:7.1f}  Y={o['y_mm']:7.1f}  Z={o['z_mm']:7.1f} mm")
        log(f"      calibrated (robot): X={o['robot_x_mm']:7.1f}  Y={o['robot_y_mm']:7.1f}  Z={o['robot_z_mm']:7.1f} mm")
        if len(o.get("robot_z_frames_mm", [])) > 1:
            log(f"      robot Z per frame: {o['robot_z_frames_mm']} -> averaged {o['robot_z_mm']}")


# ================================================================
# Saving
# ================================================================

class CaptureStore:
    """Everything a capture produces, written into one folder (Data/Detections/)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def save_capture(self, stamp, rgb, depth, annotated, objects, K):
        cv2.imwrite(str(self.folder / f"rgb_{stamp}.jpg"), rgb)
        cv2.imwrite(str(self.folder / f"depth_{stamp}.png"), depth)
        cv2.imwrite(str(self.folder / f"annotated_{stamp}.jpg"), annotated)
        record = {"capture": stamp, "units": "mm", "intrinsics": K, "objects": objects}
        self._write_json(record, f"detections_{stamp}.json", "latest.json")
        self._append_csv(objects, stamp)

    def save_cups(self, cups, stamp):
        self._write_json({"capture": stamp, "units": "mm", "cups": cups},
                         f"cups_{stamp}.json", "cups.json")

    def save_grip(self, cup, stamp):
        """One row per moved cup in grips.csv: its base diameter and where each gripper
        closed on it, to work out how far to open the grippers for a given cup."""
        path = self.folder / "grips.csv"
        new_file = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=GRIP_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow({"capture": stamp, "cup": cup["id"], "diameter_mm": cup["diameter"],
                             "grip_left_mm": cup.get("grip_left_mm"),
                             "grip_right_mm": cup.get("grip_right_mm"), "status": cup["status"]})

    def _write_json(self, data, *names):
        for name in names:
            with open(self.folder / name, "w") as f:
                json.dump(data, f, indent=2)

    def _append_csv(self, objects, stamp):
        """One row per object (an old file with other columns is renamed and kept).
        If detections.csv is open elsewhere (Excel locks it), the rows go to detections_<stamp>.csv
        instead: the CSV is only a log, it must not stop the run."""
        path = self.folder / "detections.csv"
        try:
            if path.exists():
                with open(path) as f:
                    header = f.readline().strip()
                if header != ",".join(CSV_FIELDS):     # renamed after closing it (Windows refuses an open file)
                    path.rename(self.folder / f"detections_old_{stamp}.csv")
            self._write_rows(path, objects, stamp)
        except PermissionError:
            spare = self.folder / f"detections_{stamp}.csv"
            log(f"  {path.name} is open in another program (Excel?) - rows saved to {spare.name}")
            self._write_rows(spare, objects, stamp)

    @staticmethod
    def _write_rows(path, objects, stamp):
        new_file = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerows({"capture": stamp, **o} for o in objects)


# ================================================================
# Main loop
# ================================================================

class Picker:
    """Capture, detect, save, then move all reachable cups one by one - once per Enter."""

    def __init__(self, gateway, detector, handover, store, left_only=False):
        self.gateway, self.detector, self.handover, self.store = gateway, detector, handover, store
        self.left_only = left_only
        self.captures = 0

    def run(self):
        log(f"Ready. Saving to: {self.store.folder}")
        try:
            while input("\nPress Enter to capture + detect (q + Enter to quit): ").strip().lower() != "q":
                self.capture_once()
        except (KeyboardInterrupt, EOFError):
            pass
        log(f"Done. {self.captures} captures saved.")

    def capture_once(self):
        rgb, depth, depths = depth_average.capture_averaged(self.gateway, DEPTH_FRAMES)
        log(f"Depth: mean of {len(depths)} frame(s)")
        t = time.perf_counter()
        objects, annotated = self.detector.analyse(rgb, depth, depths)
        ms = (time.perf_counter() - t) * 1000

        stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.captures:04d}"
        self.store.save_capture(stamp, rgb, depth, annotated, objects, self.detector.K)
        self.captures += 1
        log(f"[{stamp}] {len(objects)} objects, YOLO {ms:.0f} ms")
        print_objects(objects)

        cups = find_cups(objects)
        self.store.save_cups(cups, stamp)
        log(f"{len(cups)} cup(s) saved to cups.json")
        placed = 0
        while self.move_one(cups, stamp):          # all cups of this capture, most certain first
            placed += 1
        if placed and not any(c["status"] == "failed" for c in cups):
            self.go_home()                         # all done: home once, not between cups
        out_of_reach = sum(c["status"] == "out of reach" for c in cups)
        if out_of_reach:
            log(f"  {out_of_reach} cup(s) not moved: out of reach (X_MIN..X_MAX, Y_MIN..Y_MAX)")
        return cups                                # NEW: standby's auto loop looks at what happened

    # ---------------- NEW: standby (--standby) ----------------

    def standby(self, poll=STANDBY_POLL, idle=AUTO_IDLE):
        """Wait until control is free, take it, make the robot safe, then pick on our own."""
        log(f"Standby: waiting for control (trying every {poll:g} s, Ctrl+C to quit).")
        last_reason = None
        try:
            while True:
                ok, info = self.gateway.try_acquire()
                if not ok:
                    if info != last_reason:        # print only when the reason changes
                        log(f"  not yet: {info}")
                        last_reason = info
                    time.sleep(poll)
                    continue
                last_reason = None
                log(f"\nCONTROL TAKEN as {info.get('client_id')} (priority {info.get('priority')}): taking over.")
                self.gateway.hold()
                try:
                    if not self.takeover():
                        return                     # the person chose to quit
                    if not self.auto_run(idle):
                        return
                finally:
                    self.gateway.drop()
                log("\nStandby: control lost, waiting for it again.")
        except KeyboardInterrupt:
            log("\nStopped (Ctrl+C).")
        finally:
            if self.gateway.holding:
                self.gateway.drop()
            log(f"Done. {self.captures} captures saved.")

    def takeover(self):
        """Robot to a known state before our first move. False: the person chose to quit."""
        while True:
            log("  Takeover: waiting until both arms are idle ...")
            deadline = time.monotonic() + JOB_TIMEOUT
            while any(self.gateway.arm_busy(a) for a in ("left", "right")):
                if time.monotonic() > deadline:
                    log("  an arm is still busy after the job timeout")
                    break
                time.sleep(0.2)

            levels = {arm.name: arm.grip_level() for arm in (self.handover.left, self.handover.right)}
            unsure = [name for name, level in levels.items()
                      if level is None or GRIP_EMPTY_MAX < level < GRIP_OPEN_MIN]
            if unsure:
                log(f"  {', '.join(unsure)} gripper neither open nor closed empty {levels}: it may hold a cup.")
                if not self.ask_person("Remove any cup from the grippers"):
                    return False

            if self.gateway.lost.is_set():
                return True                        # auto_run sees it and we go back to waiting
            log("  Takeover: both arms home (up from where they are, then home).")
            self.handover.abort.clear()
            try:
                for arm in (self.handover.left, self.handover.right):
                    #arm.last = self.gateway.arm_pose(arm.name)   # real pose: up from here, not a guess
                    pose = self.gateway.arm_pose(arm.name)        # real pose: up from here, not a guess
                    if pose[0][0] < SAFE_ZONE_X_MIN:              # at/near home, outside the safe zone:
                        log(f"    {arm.name}: at x {pose[0][0]:.0f} (near home), homing directly")
                        arm.last = None                           # no "up first" (the gateway refuses it there)
                    else:
                        arm.last = pose
                    arm.home()
                return True
            except RobotError as e:
                log(f"  STOP: going home failed: {e}")
                if not self.ask_person("Move the arms clear (e.g. gatewayctl.py home)"):
                    return False

    def auto_run(self, idle):
        """Capture and pick without Enter while we hold control. False: the person chose to quit;
        True: control was lost (back to standby)."""
        while not self.gateway.lost.is_set():
            cups = self.capture_once()
            if self.gateway.lost.is_set():
                break
            if any(c["status"] == "failed" for c in cups):
                log("  A cup failed: the arms stay where they stopped.")
                if not self.ask_person("Check both arms and the cup"):
                    return False
                if not self.takeover():
                    return False
                continue
            if not any(c["status"] == "placed" for c in cups):
                log(f"  No cup moved, capturing again in {idle:g} s.")
                if self.gateway.lost.wait(idle):
                    break
        return True

    def ask_person(self, what):
        """Wait for Enter (keeping control meanwhile). False on q, Ctrl+D or no terminal."""
        try:
            answer = input(f"  >>> {what}, then press Enter to go on (q + Enter to quit): ")
        except EOFError:
            log("  no terminal to ask: quitting")
            return False
        return answer.strip().lower() != "q"

    def go_home(self):
        """Both arms home after the last cup: the left first (up, sideways, home), then the
        right (up, home). Not after a failure: then the arms stay where they stopped."""
        log("  All cups done: both arms home.")
        try:
            with self.gateway.control():
                self.handover.left.home()
                if not self.left_only:             # the right arm did not move with --left-only
                    self.handover.right.home()
        except RobotError as e:
            log(f"  STOP: going home failed: {e}")

    def move_one(self, cups, stamp):
        """Hand over the first cup that is still "waiting" (the most certain reachable one).
        True if it was placed, False if there was none left or it failed (then stop)."""
        cup = next((c for c in cups if c["status"] == "waiting"), None)
        if cup is None:
            return False
        places, place = None, None
        if not self.left_only:
            places = load_places()
            place = next((p for p in places["locations"] if not p["occupied"]), None)
            if place is None:
                log(f"  all place locations are occupied - empty them and set occupied to false in {PLACE_FILE.name}")
                return False
        what = "left arm only" if self.left_only else f"hand-over, place at location {place['id']}"
        log(f"  cup {cup['id']}: X={cup['x']} Y={cup['y']} Z={cup['z']} base Ø{cup['diameter']} mm -> {what}")
        cup["status"] = "moving"
        self.store.save_cups(cups, stamp)
        placed = False
        try:
            with self.gateway.control():           # keep robot control for the whole sequence
                placed = self.handover.run(cup, self.left_only, place)
        finally:
            cup["status"] = "placed" if placed else "failed"
            self.store.save_cups(cups, stamp)
            self.store.save_grip(cup, stamp)
        if placed and place is not None:
            place["occupied"] = True               # the next cup goes to the next free location
            save_places(places)
            log(f"  location {place['id']} is now occupied")
        log(f"  cup {cup['id']}: {cup['status']}")
        return placed


def load_places():
    with open(PLACE_FILE) as f:
        return json.load(f)


def save_places(places):
    """Same layout as the file: one location per line."""
    lines = ",\n".join("    " + json.dumps(p) for p in places["locations"])
    with open(PLACE_FILE, "w") as f:
        f.write(f'{{\n  "locations": [\n{lines}\n  ]\n}}\n')


def main():
    global USE_CAMERA_Z, DEPTH_FRAMES
    parser = argparse.ArgumentParser(description="Capture, detect cups and hand one over between the arms")
    parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
    parser.add_argument("--gateway", default="http://169.254.125.202:8000", help="Gateway address")
    parser.add_argument("--conf", type=float, default=0.5, help="YOLO confidence threshold")
    parser.add_argument("--left-only", action="store_true",
                        help="only the left arm: pick the cup, lift it and put it back (no hand-over)")
    parser.add_argument("--camera-z", action="store_true",
                        help=f"TEST: grip height from each cup's camera z + {CAMERA_Z_OFFSET} instead of PICK_Z {PICK_Z}")
    parser.add_argument("--depth-frames", type=int, default=DEPTH_FRAMES,
                        help=f"camera frames whose depth is averaged (default {DEPTH_FRAMES}, 1 = off)")
    parser.add_argument("--standby", action="store_true",            # NEW
                        help="wait until control is free, then take over: home both arms and pick without Enter")
    parser.add_argument("--poll", type=float, default=STANDBY_POLL,  # NEW
                        help=f"--standby: seconds between tries to take control (default {STANDBY_POLL:g})")
    parser.add_argument("--idle", type=float, default=AUTO_IDLE,     # NEW
                        help=f"--standby: seconds before capturing again when no cup was moved (default {AUTO_IDLE:g})")
    args = parser.parse_args()
    USE_CAMERA_Z = args.camera_z
    DEPTH_FRAMES = max(1, args.depth_frames)
    if USE_CAMERA_Z:
        log(f"--camera-z: grip height = cup camera z + {CAMERA_Z_OFFSET}, not below {CAMERA_Z_FLOOR}")

    gateway = Gateway(args.gateway, load_token())
    store = CaptureStore(args.output)
    calibration = Calibration(CALIBRATION_FILE)
    model = load_model()

    log(f"Gateway: {gateway.api}")
    K = gateway.intrinsics()
    log(f"Intrinsics: fx={K['fx']:.1f} fy={K['fy']:.1f} cx={K['cx']:.1f} cy={K['cy']:.1f}")

    abort = threading.Event()
    left = Arm(gateway, "left", abort, calibration.offset)
    right = Arm(gateway, "right", abort)
    picker = Picker(gateway, Detector(model, calibration, K, args.conf),
                    Handover(left, right, abort), store, args.left_only)
    #picker.run()
    if args.standby:                               # NEW: fail-over tier
        picker.standby(args.poll, args.idle)
    else:
        picker.run()                               # as before: one capture per Enter


if __name__ == "__main__":
    main()