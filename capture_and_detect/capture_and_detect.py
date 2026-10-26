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
    5. the most certain reachable cup is moved (the hand-over of rapid/tests/handover.py):
           left:  open, LEFT_READY, above the cup, down, grip, up, to HANDOVER_L ... wait, open, up, home
           right: open ... wait, beside the hand-over ... wait, slide in under the cup, up, grip
                  ... wait, down, turn over, place, open, up, home
       Both arms move at the same time and only wait for each other (the "wait"s). The right arm
       stays home while the left one picks, so they never share the picking area.
       With --left-only the right arm stays home: the left arm picks the cup, lifts it to
       TRAVEL_Z, puts it back where it stood, lets go and goes home.

A cup is a CUP BASE inside a detected CUP (the robot's joint was once seen as a CUP BASE).

The model is picked by the device; the weights sit next to this file:
    Westermo Lynx  -> best_ncnn_model/   (set PICKER_DEVICE=lynx in the Lynx container)
    anything else  -> best.pt            (Raspberry Pi, development laptop)

The robot is driven through the gateway's RWS API (POST /robot/rws/move|home|gripper, see
gateway/scripts/client_template.py), so the RAPID program in rapid/ must be running
(python rapid/rapidctl.py status). The camera gives each cup's x, y (gripper TCP, tGrip, as in
the calibration); the heights are fixed (PICK_Z, TRAVEL_Z), since the cups stand on the table.
The arm modules move tool0 (the flange): Arm.goto() adds GRIPPER_TCP_Z, and the fixed
hand-over poses are flange positions.

The gateway runs in strict mode and needs a token with robot.control (the operator token).
Set it once in the terminal (never put it in this file):
    PowerShell:  $env:GATEWAY_TOKEN = "<token>"
    Linux:       export GATEWAY_TOKEN="<token>"

Usage:
    py capture_and_detect.py
    py capture_and_detect.py --gateway http://169.254.125.202:8000 --conf 0.6
    py capture_and_detect.py --left-only
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

# ================================================================
# Settings
# ================================================================

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE.parent / "Calibration/calibration.json"
KEEP_CLASSES = ["CUP", "CUP BASE", "CUP HANDLE"]
CUP_Z_MIN, CUP_Z_MAX = 10, 150   # robot Z (mm) of a CUP BASE on a cup standing on the table

CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px"]

# ---------------- Robot moves ----------------
JOB_TIMEOUT = 90           # s, longer than the gateway's own 60 s job timeout
SPEED = 1200               # TCP speed, mm/s (1..1500), sent with every move and home
ACCELERATION = 100         # % of max acceleration (20..100)
RAMP = 100                 # % how fast the acceleration builds up (10..100)

LEFT_DOWN = (180, 0, 180)  # rx, ry, rz (deg): left gripper straight down (as in the calibration)
RIGHT_DOWN = (180, 0, 0)   # right gripper straight down (as in handover.py)
RIGHT_UP = (0, 0, 0)       # right gripper pointing up

# ---------------- Left arm: picking (gripper TCP, mm) ----------------
# The camera gives x, y; the heights are fixed because the cups stand on the table.
GRIPPER_TCP_Z = 159        # tGrip: the TCP is 159 mm past the flange
PICK_Z = 75                # TCP height when gripping a cup
TRAVEL_Z = 250             # TCP height above the cups, on the way to and from them
X_MIN, X_MAX = 350, 800    # cups outside this area are skipped
Y_MIN, Y_MAX = -230, 420

# ---------------- Hand-over and placing (tool0 = flange positions, mm, tuned in RobotStudio) ----
# The left arm comes from home via LEFT_READY (straight from home, the joint move to a far cup
# swings the hand into the table), picks the cup and holds it at HANDOVER_L. The right arm
# waits at home until then, comes via R_VIA to R_BESIDE, slides in under the cup, goes up to
# R_TAKE and grips the rim. The left lets go and lifts LEFT_LIFT (HANDOVER_L z + LEFT_LIFT
# must stay within the gateway's z <= 600), the right goes down again, turns the cup over
# above R_PLACE and puts it down there.
LEFT_READY = (400, 100, 450)
HANDOVER_L = (400, -100, 549)
LEFT_LIFT = 35             # straight up after letting go (then it waits until the right arm is away)
R_VIA = (200, -250, 200)
R_TAKE = (380, -115, 210)
R_BELOW = (R_TAKE[0], R_TAKE[1], R_TAKE[2] - 35)            # straight under R_TAKE
R_BESIDE = (R_BELOW[0], R_BELOW[1] - 120, R_BELOW[2])       # beside it, away from the left arm
R_PLACE = (420, -200, 230)
R_PLACE_ABOVE = (R_PLACE[0], R_PLACE[1], R_PLACE[2] + 80)   # the cup is turned over here


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

    def robot_job(self, arm, path, body):
        """POST one job to /robot/rws/<path> and wait until the arm has done it.
        True if the robot reports DONE; prints the reply either way."""
        try:
            r = self.post(f"/robot/rws/{path}", timeout=JOB_TIMEOUT, body={"arm": arm, **body})
        except requests.RequestException as e:
            log(f"    {arm}: {path} {body} -> {e}")
            return False
        # 200: {"accepted", "message"}; 409 busy / no lease / RAPID stopped, 503 unreachable, 504 timeout
        reply = r.json()["message"] if r.status_code == 200 else f"HTTP {r.status_code}: {r.text}"
        log(f"    {arm}: {path} {body} -> {reply}")
        return reply == "DONE"


def frame_array(r, dtype, planes=1):
    """Raw frame bytes -> (planes, H, W) array, sized from the X-Frame-* headers."""
    w, h = int(r.headers["X-Frame-Width"]), int(r.headers["X-Frame-Height"])
    return np.frombuffer(r.content, dtype).reshape(planes, h, w)


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

    def job(self, path, body):
        if self.abort.is_set():
            raise RobotError("the other arm failed")
        if not self.gateway.robot_job(self.name, path, body):
            raise RobotError(f"{self.name} {path} failed")

    def move(self, flange, rotation):
        """tool0 (the flange) to (x, y, z) mm with rotation (rx, ry, rz) degrees."""
        (x, y, z), (rx, ry, rz) = flange, rotation
        self.job("move", {"x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz,
                          "speed": SPEED, "acceleration": ACCELERATION, "ramp": RAMP})

    def goto(self, x, y, z):
        """Gripper TCP to x, y seen by the camera (plus the calibration's tag_offset in x, y)
        at height z (robot mm), gripper down: the flange is GRIPPER_TCP_Z above the TCP."""
        ox, oy, _ = self.offset
        self.move((x + ox, y + oy, z + GRIPPER_TCP_Z), LEFT_DOWN)

    def gripper(self, action):
        """action: "open" or "grip"."""
        self.job("gripper", {"action": action})

    def home(self):
        self.job("home", {"speed": SPEED, "acceleration": ACCELERATION, "ramp": RAMP})


class Handover:
    """Moves one cup with both arms at once; they only wait for each other at the hand-over."""

    def __init__(self, left, right, abort):
        self.left, self.right, self.abort = left, right, abort
        self.left_has_cup = threading.Event()      # left gripped the cup and left the picking area
        self.at_handover = threading.Event()       # left holds the cup at HANDOVER_L
        self.right_has_cup = threading.Event()     # right closed on the rim
        self.left_clear = threading.Event()        # left let go and moved up
        self.right_away = threading.Event()        # right took the cup away from the hand-over
        self.events = (abort, self.left_has_cup, self.at_handover, self.right_has_cup,
                       self.left_clear, self.right_away)

    def run(self, cup, left_only=False):
        """True if the cup was placed. Ctrl+C stops both arms after their current job.
        left_only: the left arm picks the cup, lifts it and puts it back, no hand-over."""
        for event in self.events:
            event.clear()
        jobs = [(self._pick_only_job, (cup,))] if left_only else \
               [(self._left_job, (cup,)), (self._right_job, ())]
        arms = [threading.Thread(target=self._guarded, args=(job, *args), daemon=True)
                for job, args in jobs]
        for arm in arms:
            arm.start()
        try:
            while any(arm.is_alive() for arm in arms):
                arms[0].join(0.2)                  # join with a timeout keeps Ctrl+C working
        except KeyboardInterrupt:
            log("\nStopped (Ctrl+C). An arm that is moving finishes its current move.")
            self.abort.set()
            raise
        return not self.abort.is_set()

    def _pick(self, cup):
        x, y = cup["x"], cup["y"]                  # the heights are fixed: PICK_Z, TRAVEL_Z
        arm = self.left
        arm.gripper("open")
        arm.move(LEFT_READY, LEFT_DOWN)            # from home via above the work area
        arm.goto(x, y, TRAVEL_Z)
        arm.goto(x, y, PICK_Z)
        arm.gripper("grip")
        arm.goto(x, y, TRAVEL_Z)

    def _pick_only_job(self, cup):
        arm = self.left
        self._pick(cup)
        arm.goto(cup["x"], cup["y"], PICK_Z)       # put it back where it stood
        arm.gripper("open")
        arm.goto(cup["x"], cup["y"], TRAVEL_Z)
        arm.home()

    def _left_job(self, cup):
        arm = self.left
        self._pick(cup)
        self.left_has_cup.set()                    # -> right: come to the hand-over
        arm.move(HANDOVER_L, LEFT_DOWN)
        self.at_handover.set()                     # -> right: come and take it
        self._wait_for(self.right_has_cup)
        arm.gripper("open")
        arm.move((HANDOVER_L[0], HANDOVER_L[1], HANDOVER_L[2] + LEFT_LIFT), LEFT_DOWN)  # straight up
        self.left_clear.set()                      # -> right: you can pull it down
        self._wait_for(self.right_away)            # the way home could cross the right arm
        arm.home()                                 # out of the camera view for the next capture

    def _right_job(self):
        arm = self.right
        arm.gripper("open")
        self._wait_for(self.left_has_cup)          # stay home, out of the picking area
        arm.move(R_VIA, RIGHT_UP)
        arm.move(R_BESIDE, RIGHT_UP)               # beside the hand-over, just under the rim height
        self._wait_for(self.at_handover)
        arm.move(R_BELOW, RIGHT_UP)                # slide in under the cup
        arm.move(R_TAKE, RIGHT_UP)                 # the last bit straight up
        arm.gripper("grip")
        self.right_has_cup.set()                   # -> left: let go
        self._wait_for(self.left_clear)
        arm.move(R_BELOW, RIGHT_UP)
        arm.move(R_PLACE_ABOVE, RIGHT_DOWN)        # turn over: cup now upright
        self.right_away.set()                      # -> left: you can go home
        arm.move(R_PLACE, RIGHT_DOWN)
        arm.gripper("open")
        arm.move(R_PLACE_ABOVE, RIGHT_DOWN)
        arm.home()                                 # out of the picking area for the next cup

    def _wait_for(self, event):
        """Wait for the other arm, but stop if it failed."""
        while not event.wait(0.1):
            if self.abort.is_set():
                raise RobotError("the other arm failed")

    def _guarded(self, job, *args):
        try:
            job(*args)
        except Exception as e:
            log(f"  STOP: {e}")
            self.abort.set()


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

    def analyse(self, rgb, depth):
        """Run YOLO on one frame; return (list of objects, annotated image)."""
        result = self.model.predict(rgb, conf=self.conf, classes=self.classes,
                                    retina_masks=True, verbose=False)[0]
        objects, keep, boxes = [], [], []
        masks = [] if result.masks is None else result.masks.data
        for j, (box, mask) in enumerate(zip(result.boxes, masks)):
            mask = mask.cpu().numpy().astype(bool)
            if mask.any():
                keep.append(j)
                boxes.append(box.xyxy[0].tolist())
                objects.append(self.describe(self.model.names[int(box.cls)], float(box.conf), mask, depth))
        mark_cup_bases(objects, boxes)

        annotated = result[keep].plot() if keep else rgb.copy()
        for o in objects:                          # mark the point we report
            cv2.drawMarker(annotated, (int(o["x_px"]), int(o["y_px"])), (0, 0, 255),
                           cv2.MARKER_CROSS, 25, 3)
        return objects, annotated

    def describe(self, name, confidence, mask, depth):
        """Mask centre (u, v), median depth inside the mask -> camera and robot mm."""
        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())
        vals = depth[mask]
        vals = vals[vals > 0]                      # 0 = no depth there
        z = float(np.median(vals)) if vals.size else None

        x = y = None
        robot = (None, None, None)
        if z:                                      # pinhole model
            x = (u - self.K["cx"]) * z / self.K["fx"]
            y = (v - self.K["cy"]) * z / self.K["fy"]
            robot = self.calibration.to_robot(x, y, z)
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
        cups.append({"id": len(cups) + 1, "confidence": o["confidence"], "x": x, "y": y,
                     "z": o["robot_z_mm"], "status": "waiting" if reachable else "out of reach"})
    return cups


def print_objects(objects):
    for o in objects:
        log(f"   {o['class']:<11} {o['confidence']:.2f}")
        if not o["z_mm"]:
            log("      no depth")
            continue
        log(f"      YOLO (camera):     X={o['x_mm']:7.1f}  Y={o['y_mm']:7.1f}  Z={o['z_mm']:7.1f} mm")
        log(f"      calibrated (robot): X={o['robot_x_mm']:7.1f}  Y={o['robot_y_mm']:7.1f}  Z={o['robot_z_mm']:7.1f} mm")


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

    def _write_json(self, data, *names):
        for name in names:
            with open(self.folder / name, "w") as f:
                json.dump(data, f, indent=2)

    def _append_csv(self, objects, stamp):
        """One row per object (an old file with other columns is renamed and kept)."""
        path = self.folder / "detections.csv"
        if path.exists():
            with open(path) as f:
                if f.readline().strip() != ",".join(CSV_FIELDS):
                    path.rename(self.folder / f"detections_old_{stamp}.csv")
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
    """Capture, detect, save, then move the most certain reachable cup - once per Enter."""

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
        rgb, depth = self.gateway.frames()
        t = time.perf_counter()
        objects, annotated = self.detector.analyse(rgb, depth)
        ms = (time.perf_counter() - t) * 1000

        stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.captures:04d}"
        self.store.save_capture(stamp, rgb, depth, annotated, objects, self.detector.K)
        self.captures += 1
        log(f"[{stamp}] {len(objects)} objects, YOLO {ms:.0f} ms")
        print_objects(objects)

        cups = find_cups(objects)
        self.store.save_cups(cups, stamp)
        log(f"{len(cups)} cup(s) saved to cups.json")
        self.move_one(cups, stamp)

    def move_one(self, cups, stamp):
        """Hand over the first cup that is still "waiting" (the most certain reachable one)."""
        cup = next((c for c in cups if c["status"] == "waiting"), None)
        if cup is None:
            if cups:
                log("  no cup within reach (X_MIN..X_MAX, Y_MIN..Y_MAX)")
            return
        what = "left arm only" if self.left_only else "hand-over"
        log(f"  cup {cup['id']}: X={cup['x']} Y={cup['y']} Z={cup['z']} -> {what}")
        cup["status"] = "moving"
        self.store.save_cups(cups, stamp)
        placed = False
        try:
            with self.gateway.control():           # keep robot control for the whole sequence
                placed = self.handover.run(cup, self.left_only)
        finally:
            cup["status"] = "placed" if placed else "failed"
            self.store.save_cups(cups, stamp)
        log(f"  cup {cup['id']}: {cup['status']}")


def main():
    parser = argparse.ArgumentParser(description="Capture, detect cups and hand one over between the arms")
    parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
    parser.add_argument("--gateway", default="http://169.254.125.202:8000", help="Gateway address")
    parser.add_argument("--conf", type=float, default=0.5, help="YOLO confidence threshold")
    parser.add_argument("--left-only", action="store_true",
                        help="only the left arm: pick the cup, lift it and put it back (no hand-over)")
    args = parser.parse_args()

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
    Picker(gateway, Detector(model, calibration, K, args.conf),
           Handover(left, right, abort), store, args.left_only).run()


if __name__ == "__main__":
    main()
