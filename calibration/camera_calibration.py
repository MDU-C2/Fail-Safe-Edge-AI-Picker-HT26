"""
Full camera-to-robot calibration for the YuMi left arm, through the gateway API.

What it does, in one run:
  1. Reads the camera intrinsics (and lens distortion, if the gateway gives it).
  2. Takes control, moves the arm HOME, then to each pose in POSES with the gripper in
     CALIB_ROT. Step by step (Enter before every move) unless --auto.
  3. At each pose: waits for the arm to settle, grabs several colour + depth frames, finds
     the AprilTag on the gripper and reads its depth. The first frame is thrown away, so a
     stale depth frame from before the move is never used.
  4. Saves every image and the pose it belongs to in calibration_runs/session_<time>/,
     so images and poses can never get out of step.
  5. Fits  p_robot = scale * R @ p_cam + t  and prints the error per pose, both on the
     fit and on poses left out of it ("unseen"), then saves calibration.json.

The robot is moved through the gateway's RWS API (POST /robot/rws/home and
/robot/rws/move, see gateway/scripts/client_template.py), so the RAPID program in
rapid/ must be running (python rapid/rapidctl.py status). POSES are GRIPPER TCP
positions (tGrip, 159 mm past the flange) and are saved and fitted as such; the arm
modules move tool0 (the flange), so flange_target() moves each one back along the
tool axis for CALIB_ROT before it is sent. A pose the arm cannot reach is skipped.

Refit a saved session without the robot (e.g. to leave out bad poses):
    py camera_calibration.py --fit calibration_runs/session_20261012_140000 --exclude 10 15

Usage:
    py camera_calibration.py            step by step
    py camera_calibration.py --auto     all poses without stopping
"""
import argparse
import json
import os
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import requests

CAMERA_URL = "http://169.254.125.202:8000/api/v1"   # camera gateway: rgb, depth, intrinsics
ROBOT_URL = "http://169.254.125.202:8000/api/v1"        # robot API: control, RWS jobs (or --robot-url)
HEADERS = {"Authorization": "Bearer BAD_TOKEN_OPERATOR", "X-Dev-Client": "operator"}
ROBOT_TIMEOUT = 90               # s, longer than the gateway's own 60 s job timeout
ARM = "left"
GRIPPER_TCP_Z = 159              # tGrip in rapid/LeftArmInstructions.mod: TCP 159 mm past the flange

# Gripper orientation during calibration: horizontal, flat side with the tag up.
CALIB_ROT = (90, 0, 90)

# Gripper TCP positions, mm, wobj0, table = z 0. The first pose stays high, because the
# move from HOME turns the gripper into CALIB_ROT.
POSES = [
    (600, 410, 140), (600, 250, 90), (600, 90, 170), (600, -70, 70), (600, -230, 110),
    (520, -230, 140), (520, -70, 90), (520, 90, 110), (520, 250, 70), (520, 410, 90),
    (440, 410, 90), (440, 250, 170), (440, 90, 70), (440, -70, 110), (440, -230, 140),
    (380, -150, 170), (350, -70, 110), (350, 90, 90), (380, 250, 90), (350, 410, 140),
]

TAG_ID = 0
TAG_OFFSET = (-115, 0.0, 40.0)     # tag centre minus gripper TCP, mm, robot frame, in CALIB_ROT
SETTLE = 1.0                     # s to wait after the robot reports DONE
FRAMES = 11                       # frames per pose; the first is discarded, the rest are combined

HERE = os.path.dirname(os.path.abspath(__file__))

params = cv2.aruco.DetectorParameters()
params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
params.minDistanceToBorder = 0
detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), params)


# Gateway

def get_intrinsics():
    """fx, fy, cx, cy, width, height and distortion coefficients (empty if not given)."""
    r = requests.get(f"{CAMERA_URL}/camera/intrinsics", headers=HEADERS, timeout=10)
    r.raise_for_status()
    k = r.json()
    dist = next((k[key] for key in ("distortion", "dist_coeffs", "distortion_coefficients", "D")
                 if key in k), [])
    return {"fx": k["fx"], "fy": k["fy"], "cx": k["cx"], "cy": k["cy"],
            "width": k["width"], "height": k["height"], "dist": list(dist)}


def capture():
    """One colour frame (BGR) and its aligned depth (uint16, mm)."""
    rgb_r = requests.get(f"{CAMERA_URL}/camera/rgb", headers=HEADERS, timeout=10)
    depth_r = requests.get(f"{CAMERA_URL}/camera/depth", headers=HEADERS, timeout=10)
    rgb_r.raise_for_status()
    depth_r.raise_for_status()
    w, h = int(rgb_r.headers["X-Frame-Width"]), int(rgb_r.headers["X-Frame-Height"])
    dw, dh = int(depth_r.headers["X-Frame-Width"]), int(depth_r.headers["X-Frame-Height"])
    rgb = np.frombuffer(rgb_r.content, np.uint8).reshape(3, h, w).transpose(1, 2, 0).copy()
    depth = np.frombuffer(depth_r.content, np.uint16).reshape(dh, dw).copy()
    return rgb, depth


def acquire_control():
    """Take control and keep it. The lease lasts 5 s, so a background thread renews it every 2 s."""
    r = requests.post(f"{ROBOT_URL}/control/acquire", headers=HEADERS, timeout=10)
    r.raise_for_status()
    stop = threading.Event()

    def renew():
        while not stop.wait(2):
            try:
                requests.post(f"{ROBOT_URL}/control/renew", headers=HEADERS, timeout=5).raise_for_status()
            except requests.RequestException as e:
                print(f"  lease renew failed: {e}")

    threading.Thread(target=renew, daemon=True).start()
    return stop


def release_control(lease):
    lease.set()
    try:
        requests.post(f"{ROBOT_URL}/control/release", headers=HEADERS, timeout=5)
    except requests.RequestException:
        pass                                   # the lease runs out by itself after 5 s


def robot_job(path, body):
    """POST one job to /robot/rws/<path> and wait for the arm. True if it reports DONE."""
    try:
        r = requests.post(f"{ROBOT_URL}/robot/rws/{path}", headers=HEADERS,
                          json={"arm": ARM, **body}, timeout=ROBOT_TIMEOUT)
    except requests.RequestException as e:
        print(f"  {path} {body}  ->  {e}")
        return False
    # 200: {"accepted", "message"}; 409 busy / no lease / RAPID stopped, 503 unreachable, 504 timeout
    reply = r.json()["message"] if r.ok else f"HTTP {r.status_code}: {r.text}"
    print(f"  {path} {body}  ->  {reply}")
    return reply == "DONE"


def orient_zyx(rx, ry, rz):
    """Rotation matrix of RAPID's OrientZYX(rz, ry, rx), degrees: about z, then y, then x."""
    a, b, c = np.radians([rz, ry, rx])
    Rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    Ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    Rx = np.array([[1, 0, 0], [0, np.cos(c), -np.sin(c)], [0, np.sin(c), np.cos(c)]])
    return Rz @ Ry @ Rx


def flange_target(tcp, rot):
    """tool0 (flange) position that puts the gripper TCP at tcp with orientation rot:
    GRIPPER_TCP_Z back along the tool's z axis. In CALIB_ROT that axis is +x."""
    return np.array(tcp, float) - GRIPPER_TCP_Z * orient_zyx(*rot)[:, 2]


def home():
    return robot_job("home", {})


def rotate_in_place(rot):
    """Turn the hand to rot = (rx, ry, rz) degrees with the flange (tool0) where it is now."""
    r = requests.get(f"{ROBOT_URL}/robot/arms/{ARM}/pose", headers=HEADERS,
                     params={"tool": "tool0", "wobj": "wobj0"}, timeout=10)
    if not r.ok:
        print(f"  could not read the arm's pose: HTTP {r.status_code}: {r.text}")
        return False
    pose = r.json()
    rx, ry, rz = rot
    return robot_job("move", {"x": pose["x"], "y": pose["y"], "z": pose["z"], "rx": rx, "ry": ry, "rz": rz})


def goto(tcp, rot):
    """Gripper TCP to tcp (mm, wobj0) with orientation rot = (rx, ry, rz) degrees."""
    x, y, z = flange_target(tcp, rot).round(1)
    rx, ry, rz = rot
    return robot_job("move", {"x": float(x), "y": float(y), "z": float(z), "rx": rx, "ry": ry, "rz": rz})


# Measurement

def find_tag(rgb):
    """Tag corners in pixels, or None unless exactly one tag with TAG_ID is visible."""
    corners, ids, _ = detector.detectMarkers(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY))
    if ids is None or list(ids.ravel()).count(TAG_ID) != 1:
        return None
    return corners[list(ids.ravel()).index(TAG_ID)][0]


def tag_depth(corners, depth):
    """Median depth (mm) over the tag area, or None if the depth has a hole there."""
    mask = np.zeros(depth.shape, np.uint8)
    cv2.fillConvexPoly(mask, corners.astype(np.int32), 1)
    d = depth[(mask > 0) & (depth > 0)]
    return float(np.median(d)) if d.size else None


def to_camera(u, v, z, k):
    """Pixel (u, v) at depth z -> camera coordinates (mm).

    Pinhole model, solved backwards: X = (u - cx) * Z / fx, Y = (v - cy) * Z / fy.
    If the gateway gives lens distortion, the pixel is undistorted first.
    """
    if k["dist"]:
        K = np.array([[k["fx"], 0, k["cx"]], [0, k["fy"], k["cy"]], [0, 0, 1]])
        x, y = cv2.undistortPoints(np.array([[[u, v]]], np.float64), K, np.array(k["dist"])).ravel()
    else:
        x, y = (u - k["cx"]) / k["fx"], (v - k["cy"]) / k["fy"]
    return np.array([x * z, y * z, z])


def measure(folder, n):
    """Grab FRAMES frames, drop the first, combine the rest. Saves the last pair as image n."""
    rgb = depth = None
    found = []
    for i in range(FRAMES):
        rgb, depth = capture()
        if i == 0:
            continue                       # may still be from before the move
        c = find_tag(rgb)
        z = tag_depth(c, depth) if c is not None else None
        if z is not None:
            found.append((c.mean(axis=0), z))
        time.sleep(0.2)
    cv2.imwrite(os.path.join(folder, "rgb", f"{n}.jpg"), rgb)
    cv2.imwrite(os.path.join(folder, "depth", f"{n}.png"), depth)
    if not found:
        return None
    uv = np.median([f[0] for f in found], axis=0)
    return {"u": float(uv[0]), "v": float(uv[1]), "z": float(np.median([f[1] for f in found])),
            "frames": len(found)}


# Fit

def fit(P, Q):
    """Best scale, R, t with Q = scale * R @ P + t (Umeyama).

    Centre both point clouds, find the rotation that aligns them with an SVD, the scale
    that matches their spread, and the translation that lines up their centres. The scale
    absorbs depth that reads a few percent short or long.
    """
    pc, qc = P.mean(axis=0), Q.mean(axis=0)
    U, S, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    s = np.trace(np.diag(S) @ D) / ((P - pc) ** 2).sum()
    return s, R, qc - s * R @ pc


def solve(folder, exclude=()):
    with open(os.path.join(folder, "session.json")) as f:
        session = json.load(f)
    k = session["intrinsics"]
    samples = [s for s in session["samples"] if s["tag"] and s["image"] not in exclude]
    if len(samples) < 4:
        raise SystemExit(f"Only {len(samples)} usable poses, need at least 4")

    cam = np.array([to_camera(s["tag"]["u"], s["tag"]["v"], s["tag"]["z"], k) for s in samples])
    rob = np.array([s["pose"] for s in samples], float)
    scale, R, t = fit(cam, rob)
    err = np.linalg.norm(scale * cam @ R.T + t - rob, axis=1)
    unseen_xyz = []
    for i in range(len(samples)):
        keep = np.arange(len(samples)) != i
        si, Ri, ti = fit(cam[keep], rob[keep])
        unseen_xyz.append(si * Ri @ cam[i] + ti - rob[i])
    unseen_xyz = np.array(unseen_xyz)          # predicted minus real, mm, robot frame
    unseen = np.linalg.norm(unseen_xyz, axis=1)

    print(f"\n{'image':>5}  {'pose':>18}  {'fit':>8}  {'unseen':>8}  {'dx':>6}  {'dy':>6}  {'dz':>6}  {'depth':>7}")
    for s, e, u, d, p in zip(samples, err, unseen, unseen_xyz, cam):
        flag = "   <- check" if u > 2 * np.median(unseen) else ""
        print(f"{s['image']:5d}  {str(tuple(s['pose'])):>18}  {e:5.1f} mm  {u:5.1f} mm  "
              f"{d[0]:6.1f}  {d[1]:6.1f}  {d[2]:6.1f}  {p[2]:4.0f} mm{flag}")
    print(f"\n{len(samples)} poses, fit {err.mean():.1f} mm (max {err.max():.1f}), "
          f"unseen {unseen.mean():.1f} mm (max {unseen.max():.1f}), scale {scale:.3f}")
    print("Unseen per axis, mean |error| (mean signed):  " + "   ".join(
        f"{a} {np.abs(unseen_xyz[:, j]).mean():.1f} ({unseen_xyz[:, j].mean():+.1f}) mm"
        for j, a in enumerate("xyz")))
    if exclude:
        print(f"Excluded images: {sorted(exclude)}")

    result = {"scale": float(scale), "R": R.tolist(), "t": (t).tolist(),
              "intrinsics": k, "tag_offset": list(TAG_OFFSET),
              "fit_error_mm": float(err.mean()), "unseen_error_mm": float(unseen.mean()),
              "unseen_error_xyz_mm": np.abs(unseen_xyz).mean(axis=0).tolist(),
              "images_used": [s["image"] for s in samples], "session": os.path.basename(folder)}
    for path in (os.path.join(folder, "calibration.json"), os.path.join(HERE, "calibration.json")):
        with open(path, "w") as f:
            json.dump(result, f, indent=2)
    print(f"Saved calibration.json (in the session folder and next to this script)")
    if TAG_OFFSET == (0.0, 0.0, 0.0):
        print("TAG_OFFSET is (0, 0, 0): positions are off by the tag-to-TCP distance until it is measured.")


# Robot run

def run(auto):
    k = get_intrinsics()
    print(f"Intrinsics: fx {k['fx']:.1f}  fy {k['fy']:.1f}  cx {k['cx']:.1f}  cy {k['cy']:.1f}  "
          f"{k['width']}x{k['height']}  distortion {'yes' if k['dist'] else 'no'}")

    folder = os.path.join(HERE, "calibration_runs", datetime.now().strftime("session_%Y%m%d_%H%M%S"))
    os.makedirs(os.path.join(folder, "rgb"))
    os.makedirs(os.path.join(folder, "depth"))
    session = {"intrinsics": k, "calib_rot": CALIB_ROT, "samples": []}

    def ask(what):
        if auto:
            return True
        return input(f"Enter = {what}, q = stop: ").strip().lower() != "q"

    lease = acquire_control()
    if not ask("go HOME") or not home():
        release_control(lease)
        raise SystemExit("Stopped before the first pose")

    for n, pose in enumerate(POSES, 1):
        if not ask(f"pose {n}/{len(POSES)} {pose}"):
            break
        if not goto(pose, CALIB_ROT):
            print(f"  pose {n}: robot did not reach it, skipped")
            continue
        time.sleep(SETTLE)
        tag = measure(folder, n)
        print(f"  pose {n}: " + (f"tag at ({tag['u']:.0f}, {tag['v']:.0f}) px, depth {tag['z']:.0f} mm, "
                                 f"{tag['frames']}/{FRAMES - 1} frames" if tag else "tag or depth NOT found"))
        session["samples"].append({"image": n, "pose": list(pose), "tag": tag})
        with open(os.path.join(folder, "session.json"), "w") as f:
            json.dump(session, f, indent=2)

    release_control(lease)
    print(f"\nImages and poses saved in {folder}")
    solve(folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Camera-to-robot calibration")
    parser.add_argument("--auto", action="store_true", help="run all poses without waiting for Enter")
    parser.add_argument("--fit", metavar="SESSION_FOLDER", help="refit a saved session, no robot")
    parser.add_argument("--exclude", nargs="*", type=int, default=[], help="image numbers to leave out")
    parser.add_argument("--robot-url", help="robot API address, e.g. http://192.168.0.5:8000/api/v1")
    args = parser.parse_args()
    if args.robot_url:
        ROBOT_URL = args.robot_url.rstrip("/")

    if args.fit:
        solve(args.fit, set(args.exclude))
    else:
        run(args.auto)