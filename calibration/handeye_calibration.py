"""
Hand-eye calibration (eye-to-hand) for the YuMi with a ChArUco board held by the gripper.

The camera is fixed, the board is fixed to the gripper. The robot moves the board to POSES:
different places AND different tilts. At each pose the camera finds the board (24 corners)
and solvePnP gives the board's full pose (position and rotation) in camera coordinates. With
the robot's flange pose for each one, the solve finds two fixed unknowns:

    T_base_cam     where the camera is in robot coordinates  (what we want)
    T_flange_board where the board sits on the gripper        (comes for free)

from   T_base_flange[i] @ T_flange_board = T_base_cam @ T_cam_board[i]   for every pose i.

It uses the robot's flange (tool0), so errors in the gripper TCP (tGrip) don't matter here.
Depth is not used for the camera pose; instead the same pictures are used to fit a depth
correction  Z_true = depth_scale * depth + depth_offset, by comparing the stereo depth at
every board corner with the corner's true distance from the board pose.

Procedure (the same as test_handeye_poses.py, the dry run): HOME, then GRIP_POSE where the gripper
opens and asks once before gripping the board (Y = grip, n = stop), START_POSE, the poses, END_POSE,
and RELEASE_POSE where it asks before releasing. Those two questions come also with --auto. The arm
moves slowly (MOVE) and waits SETTLE seconds at every pose before taking pictures.

POSES give where the MIDDLE OF THE BOARD should be, in the work area, and the gripper orientation.
The flange target is worked out from BOARD_REACH (flange to the middle of the board, along the
fingers). All orientations are variations of (90, 0, 90): gripper horizontal, fingers forward, board
facing up; a few turn the gripper 90 degrees inward so the board turns in its own plane.

Either arm can hold the board (--arm left or right, default left). The right arm's poses are the
left arm's mirrored to the other side (y -> -y, turns mirrored too). Both arms give the camera in
the same base frame (wobj0). The camera mount checks were done for the left arm only.

Usage:
    py handeye_calibration.py --board        write charuco_board.png to print (300 dpi, 100 %)
    py handeye_calibration.py                left arm, poses step by step, then solve
    py handeye_calibration.py --arm right    right arm instead
    py handeye_calibration.py --auto         run all poses without stopping, then solve
    py handeye_calibration.py --fit handeye_runs/session_20261116_110000   solve a saved run
    py handeye_calibration.py --url http://169.254.219.127:8000/api/v1    another gateway address
"""
import argparse
import json
import os
import time
from datetime import datetime

import cv2
import numpy as np
import requests

import camera_calibration as cc
from camera_calibration import (acquire_control, capture, get_intrinsics, orient_zyx,
                                release_control, robot_job)

# Board: measure the printed squares with a ruler and put the real size here
SQUARES = (5, 7)                 # squares across, down
SQUARE_MM = 30.0                 # side of one chessboard square, mm
MARKER_MM = 22.0                 # side of one marker inside a white square, mm
DICTIONARY = cv2.aruco.DICT_5X5_100   # a different family from the AprilTag on the gripper
MIN_CORNERS = 10                 # fewer than this and the pose is skipped

SETTLE = 3.0                     # s to wait at every pose, so the arm and the board stop shaking
FRAMES = 4                       # frames per pose; the first is discarded

GATEWAY_URL = "http://169.254.219.127:8000/api/v1"   # robot and camera; overrides camera_calibration.py
MOVE = {"speed": 100, "acceleration": 50, "ramp": 50}     # TCP mm/s, % and %
GRIP_FORCE = 20                  # N, the SmartGripper's maximum

# Flange (tool0) poses, x y z mm, rx ry rz degrees (RAPID OrientZYX(rz, ry, rx)), wobj0, only for
# handing over the board and getting in and out, all in the normal orientation (90, 0, 90).
GRIP_POSE = (360, -180, 250, 90, 0, 90)
RELEASE_POSE = (360, 340, 250, 90, 0, 90)
START_POSE = (360, -180, 320, 90, 0, 90)
END_POSE = (360, 340, 320, 90, 0, 90)

BOARD_REACH = 280                # mm from the flange to the middle of the board, along the fingers

# Calibration poses: MIDDLE OF THE BOARD x, y, z and the gripper orientation rx, ry, rz.
# rx 80-100 keeps the fingers at most 10 degrees down, low poses use rx 90; ry rolls and rz turns the
# gripper (+-20). The 90 degree turns point the fingers inward. The flange stays at x 190 or more,
# z 100 or more, and within about 150 mm of the camera mount (y 69) at x 340 or less; the order
# (one side, the middle, the other side) never sweeps across in front of the mount.
POSES_LEFT = [
    (660, -150, 160, 90, 20, 90),  (660, -150, 110, 90, 0, 110),  (660, -150, 240, 80, -15, 100),
    (560, -150, 240, 100, 15, 90), (560, -150, 110, 90, 15, 90),  (560, -150, 160, 90, -20, 110),
    (480, -150, 160, 80, 0, 100),  (480, -150, 240, 90, 0, 90),
    (340, 30, 160, 90, 0, 180),    (340, 30, 240, 90, 15, 170),       # turned 90 degrees inward (left)
    (480, 80, 240, 100, -15, 90),  (480, 80, 160, 90, 20, 90),    (480, 80, 110, 90, -15, 90),
    (540, 80, 160, 80, 15, 90),    (540, 80, 240, 90, 0, 90),     (600, 80, 240, 100, 0, 90),
    (600, 80, 160, 90, 20, 90),    (600, 80, 110, 90, 0, 90),
    (340, 110, 160, 90, 0, 0),     (340, 110, 240, 90, -15, 10),      # turned 90 degrees inward (right)
    (480, 330, 240, 90, 0, 70),    (480, 330, 160, 80, -15, 80),  (560, 330, 160, 100, 15, 90),
    (560, 330, 110, 90, 10, 70),   (560, 330, 240, 90, -20, 90),  (660, 330, 240, 90, 0, 70),
    (660, 330, 110, 90, -15, 90),  (660, 330, 160, 80, 0, 80),
]


def mirror(pose):
    """Left-arm pose -> the mirror image on the right arm's side: y -> -y, roll and turn mirrored."""
    x, y, z, rx, ry, rz = pose
    return (x, -y, z, rx, -ry, 180 - rz)


def flange_for_board(pose):
    """Board-middle pose -> flange pose: BOARD_REACH back along the fingers (the tool's z axis)."""
    x, y, z, rx, ry, rz = pose
    fx, fy, fz = np.array([x, y, z], float) - BOARD_REACH * orient_zyx(rx, ry, rz)[:, 2]
    return (round(float(fx), 1), round(float(fy), 1), round(float(fz), 1), rx, ry, rz)


def set_gateway(url):
    """Use this gateway address for both robot and camera (camera_calibration's functions)."""
    cc.ROBOT_URL = cc.CAMERA_URL = url.rstrip("/")
    print(f"Gateway: {cc.ROBOT_URL}")


ARM = "left"                     # set by --arm

HERE = os.path.dirname(os.path.abspath(__file__))

dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARY)
board = cv2.aruco.CharucoBoard(SQUARES, SQUARE_MM, MARKER_MM, dictionary)
detector = cv2.aruco.CharucoDetector(board)
BOARD_CENTRE = np.array([SQUARES[0] * SQUARE_MM / 2, SQUARES[1] * SQUARE_MM / 2, 0.0])


# Small helpers for 4x4 transforms

def make_T(R, t):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, np.ravel(t)
    return T


def inv(T):
    R, t = T[:3, :3], T[:3, 3]
    return make_T(R.T, -R.T @ t)


def nearest_rotation(M):
    U, _, Vt = np.linalg.svd(M)
    return U @ np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))]) @ Vt


def log_rotation(R):
    """Rotation matrix -> axis * angle (3-vector)."""
    v, _ = cv2.Rodrigues(R)
    return v.ravel()


# Board detection

def detect(rgb):
    """ChArUco corner ids and pixel positions, or None if too few corners."""
    corners, ids, _, _ = detector.detectBoard(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY))
    if ids is None or len(ids) < MIN_CORNERS:
        return None
    return {"ids": ids.ravel().tolist(), "corners": corners.reshape(-1, 2).tolist()}


def board_pose(det, k):
    """T_cam_board from the detected corners (solvePnP), and the mean reprojection error in px."""
    K = np.array([[k["fx"], 0, k["cx"]], [0, k["fy"], k["cy"]], [0, 0, 1]])
    dist = np.array(k["dist"]) if k["dist"] else np.zeros(5)
    obj = board.getChessboardCorners()[det["ids"]].astype(np.float64)
    img = np.array(det["corners"], np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return None, None
    proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
    reproj = float(np.linalg.norm(proj.reshape(-1, 2) - img, axis=1).mean())
    return make_T(cv2.Rodrigues(rvec)[0], tvec), reproj


def flange_T(pose):
    x, y, z, rx, ry, rz = pose
    return make_T(orient_zyx(rx, ry, rz), [x, y, z])


# Solve:  A[i] @ Z = Y @ B[i]   with A = T_base_flange, B = T_cam_board, Y = T_base_cam, Z = T_flange_board

def solve_handeye(A, B, iterations=50):
    # 1. Park & Martin on relative motions: (A_j A_i^-1) Y = Y (B_j B_i^-1)
    M = np.zeros((3, 3))
    for i in range(len(A)):
        for j in range(i + 1, len(A)):
            Ra = (A[j] @ inv(A[i]))[:3, :3]
            Rb = (B[j] @ inv(B[i]))[:3, :3]
            M += np.outer(log_rotation(Ra), log_rotation(Rb))
    R_Y = nearest_rotation(M)
    R_Z = nearest_rotation(sum(a[:3, :3].T @ R_Y @ b[:3, :3] for a, b in zip(A, B)))

    # 2. Refine both rotations together: R_A R_Z = R_Y R_B for every pose
    for _ in range(iterations):
        R_Y = nearest_rotation(sum(a[:3, :3] @ R_Z @ b[:3, :3].T for a, b in zip(A, B)))
        R_Z = nearest_rotation(sum(a[:3, :3].T @ R_Y @ b[:3, :3] for a, b in zip(A, B)))

    # 3. Both translations at once, least squares: R_A t_Z - t_Y = R_Y t_B - t_A
    rows, rhs = [], []
    for a, b in zip(A, B):
        rows.append(np.hstack([a[:3, :3], -np.eye(3)]))
        rhs.append(R_Y @ b[:3, 3] - a[:3, 3])
    t = np.linalg.lstsq(np.vstack(rows), np.hstack(rhs), rcond=None)[0]
    return make_T(R_Y, t[3:]), make_T(R_Z, t[:3])


def centre_errors(A, B, Y, Z):
    """Board centre via the robot (A Z) minus via the camera (Y B), mm, per pose."""
    c = np.append(BOARD_CENTRE, 1)
    return np.array([(a @ Z @ c - Y @ b @ c)[:3] for a, b in zip(A, B)])


def depth_correction(samples, folder, k):
    """Fit Z_true = scale * depth + offset over all board corners of all poses."""
    pairs = []
    for s in samples:
        depth = cv2.imread(os.path.join(folder, "depth", f"{s['image']}.png"), cv2.IMREAD_UNCHANGED)
        if depth is None:
            continue
        sx, sy = depth.shape[1] / k["width"], depth.shape[0] / k["height"]
        obj = board.getChessboardCorners()[s["board"]["ids"]]
        cam = (s["T_cam_board"][:3, :3] @ obj.T).T + s["T_cam_board"][:3, 3]
        for (u, v), z_true in zip(s["board"]["corners"], cam[:, 2]):
            x, y = int(round(u * sx)), int(round(v * sy))
            win = depth[max(y - 2, 0):y + 3, max(x - 2, 0):x + 3]
            win = win[win > 0]
            if win.size:
                pairs.append((float(np.median(win)), z_true))
    if len(pairs) < 10:
        return None
    d, z = np.array(pairs).T
    scale, offset = np.linalg.lstsq(np.c_[d, np.ones_like(d)], z, rcond=None)[0]
    rms = float(np.sqrt(np.mean((scale * d + offset - z) ** 2)))
    raw = float(np.sqrt(np.mean((d - z) ** 2)))
    return {"depth_scale": float(scale), "depth_offset": float(offset),
            "rms_before_mm": raw, "rms_after_mm": rms, "points": len(pairs)}


def solve(folder):
    with open(os.path.join(folder, "session.json")) as f:
        session = json.load(f)
    k = session["intrinsics"]

    samples = []
    for s in session["samples"]:
        if not s["board"]:
            continue
        T, reproj = board_pose(s["board"], k)
        if T is None:
            continue
        samples.append({**s, "T_cam_board": T, "reproj": reproj})
    if len(samples) < 6:
        raise SystemExit(f"Only {len(samples)} usable poses, need at least 6")

    A = [flange_T(s["flange"]) for s in samples]
    B = [s["T_cam_board"] for s in samples]
    Y, Z = solve_handeye(A, B)
    fit_err = np.linalg.norm(centre_errors(A, B, Y, Z), axis=1)

    unseen_xyz = []
    for i in range(len(samples)):
        keep = [j for j in range(len(samples)) if j != i]
        Yi, Zi = solve_handeye([A[j] for j in keep], [B[j] for j in keep])
        unseen_xyz.append(centre_errors([A[i]], [B[i]], Yi, Zi)[0])
    unseen_xyz = np.array(unseen_xyz)
    unseen = np.linalg.norm(unseen_xyz, axis=1)

    print(f"\n{'image':>5}  {'flange pose':>32}  {'corners':>7}  {'reproj':>7}  {'fit':>8}  {'unseen':>8}"
          f"  {'dx':>6}  {'dy':>6}  {'dz':>6}")
    for s, e, u, d in zip(samples, fit_err, unseen, unseen_xyz):
        print(f"{s['image']:5d}  {str(tuple(s['flange'])):>32}  {len(s['board']['ids']):7d}  "
              f"{s['reproj']:5.2f}px  {e:5.1f} mm  {u:5.1f} mm  {d[0]:6.1f}  {d[1]:6.1f}  {d[2]:6.1f}")
    print(f"\n{len(samples)} poses, fit {fit_err.mean():.1f} mm (max {fit_err.max():.1f}), "
          f"unseen {unseen.mean():.1f} mm (max {unseen.max():.1f})")
    print("Unseen per axis, mean |error|:  " + "   ".join(
        f"{a} {np.abs(unseen_xyz[:, j]).mean():.1f} mm" for j, a in enumerate("xyz")))
    print(f"Camera in robot coordinates: x {Y[0, 3]:.0f}  y {Y[1, 3]:.0f}  z {Y[2, 3]:.0f} mm")

    dc = depth_correction(samples, folder, k)
    if dc:
        print(f"Depth correction from {dc['points']} corners: Z = {dc['depth_scale']:.4f} * depth "
              f"{dc['depth_offset']:+.1f} mm   (RMS {dc['rms_before_mm']:.1f} -> {dc['rms_after_mm']:.1f} mm)")
    else:
        print("Depth correction: not enough depth readings at the board corners")

    result = {"method": "handeye_charuco", "arm": session.get("arm", "left"), "R": Y[:3, :3].tolist(), "t": Y[:3, 3].tolist(),
              "intrinsics": k, "T_flange_board": Z.tolist(),
              "depth_scale": dc["depth_scale"] if dc else 1.0,
              "depth_offset": dc["depth_offset"] if dc else 0.0,
              "fit_error_mm": float(fit_err.mean()), "unseen_error_mm": float(unseen.mean()),
              "unseen_error_xyz_mm": np.abs(unseen_xyz).mean(axis=0).tolist(),
              "images_used": [s["image"] for s in samples], "session": os.path.basename(os.path.normpath(folder)),
              "board": {"squares": list(SQUARES), "square_mm": SQUARE_MM, "marker_mm": MARKER_MM}}
    for path in (os.path.join(folder, "calibration_handeye.json"), os.path.join(HERE, "calibration_handeye.json")):
        with open(path, "w") as f:
            json.dump(result, f, indent=2)
    print("Saved calibration_handeye.json (in the session folder and next to this script)")


# Robot run

def poses_for_arm():
    """START, POSES and END for the arm in ARM."""
    if ARM == "left":
        return START_POSE, POSES_LEFT, END_POSE
    return mirror(START_POSE), [mirror(p) for p in POSES_LEFT], mirror(END_POSE)


def move_flange(pose):
    x, y, z, rx, ry, rz = pose
    return robot_job("move", {"arm": ARM, **MOVE, "x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz})


def home_arm():
    return robot_job("home", {"arm": ARM, **MOVE})


def side(pose):
    """A left-arm pose as it is used for ARM (mirrored for the right arm)."""
    return pose if ARM == "left" else mirror(pose)


def gripper(action):
    """open or grip, through POST /robot/rws/gripper. True if the gripper reports DONE."""
    body = {"arm": ARM, "action": action}
    if action == "grip":
        body["force"] = GRIP_FORCE
    try:
        r = requests.post(f"{cc.ROBOT_URL}/robot/rws/gripper", headers=cc.HEADERS, json=body,
                          timeout=cc.ROBOT_TIMEOUT)
    except requests.RequestException as e:
        print(f"  gripper {action}  ->  {e}")
        return False
    reply = r.json()["message"] if r.ok else f"HTTP {r.status_code}: {r.text}"
    print(f"  gripper {action}  ->  {reply}")
    return reply == "DONE"


def confirm(question, default=True):
    """Always asks, also with --auto. Enter gives the default."""
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def take_board():
    """Go to GRIP_POSE, open the gripper, and grip the board after one question. False to stop."""
    pose = side(GRIP_POSE)
    print(f"Going to the grip pose {pose}")
    if not move_flange(pose) or not gripper("open"):
        return False
    return confirm("Place the board between the fingers. Grip it?") and gripper("grip")


def give_back_board():
    """Go to RELEASE_POSE and open the gripper, after confirming that someone holds the board."""
    pose = side(RELEASE_POSE)
    print(f"Going to the release pose {pose}")
    move_flange(pose)
    if confirm("Hold the board. Release it now?"):
        gripper("open")
    else:
        print("  Board kept in the gripper.")


def measure(folder, n):
    """Grab FRAMES frames, drop the first, keep the one with the most board corners."""
    best = None
    for i in range(FRAMES):
        rgb, depth = capture()
        if i == 0:
            continue                       # may still be from before the move
        det = detect(rgb)
        if det and (best is None or len(det["ids"]) > len(best[0]["ids"])):
            best = (det, rgb, depth)
        time.sleep(0.2)
    if best is None:
        cv2.imwrite(os.path.join(folder, "rgb", f"{n}.jpg"), rgb)
        return None
    det, rgb, depth = best
    cv2.imwrite(os.path.join(folder, "rgb", f"{n}.png"), rgb)
    cv2.imwrite(os.path.join(folder, "depth", f"{n}.png"), depth)
    return det


def run(auto):
    k = get_intrinsics()
    start, poses, end = poses_for_arm()
    folder = os.path.join(HERE, "handeye_runs", datetime.now().strftime(f"session_%Y%m%d_%H%M%S_{ARM}"))
    os.makedirs(os.path.join(folder, "rgb"))
    os.makedirs(os.path.join(folder, "depth"))
    session = {"intrinsics": k, "arm": ARM, "samples": []}
    print(f"Arm: {ARM}")

    def ask(what):
        return auto or input(f"Enter = {what}, q = stop: ").strip().lower() != "q"

    lease = acquire_control()
    if not ask(f"go HOME ({ARM} arm)") or not home_arm() or not take_board() \
            or not ask(f"start pose {start}") or not move_flange(start):
        release_control(lease)
        raise SystemExit("Stopped before the first pose")

    for n, pose in enumerate(poses, 1):
        if not ask(f"pose {n}/{len(poses)}, board at {pose[:3]}, rot {pose[3:]}"):
            break
        flange = flange_for_board(pose)
        if not move_flange(flange):
            print(f"  pose {n}: robot did not reach it, skipped")
            continue
        time.sleep(SETTLE)
        det = measure(folder, n)
        print(f"  pose {n}: " + (f"{len(det['ids'])} board corners" if det else "board NOT found"))
        session["samples"].append({"image": n, "flange": list(flange), "board_target": list(pose),
                                   "board": det})
        with open(os.path.join(folder, "session.json"), "w") as f:
            json.dump(session, f, indent=2)

    print(f"Going to the end pose {end}")
    move_flange(end)
    give_back_board()
    release_control(lease)
    print(f"\nImages and poses saved in {folder}")
    solve(folder)


def write_board():
    """Board image at 300 dpi: print at 100 % (no 'fit to page') and measure SQUARE_MM afterwards."""
    px_per_mm = 300 / 25.4
    margin = round(10 * px_per_mm)
    size = (round(SQUARES[0] * SQUARE_MM * px_per_mm) + 2 * margin,
            round(SQUARES[1] * SQUARE_MM * px_per_mm) + 2 * margin)
    img = board.generateImage(size, marginSize=margin)
    path = os.path.join(HERE, "charuco_board.png")
    cv2.imwrite(path, img)
    print(f"Wrote {path}: {SQUARES[0]} x {SQUARES[1]} squares of {SQUARE_MM} mm, 300 dpi")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hand-eye calibration with a ChArUco board")
    parser.add_argument("--auto", action="store_true", help="run all poses without waiting for Enter")
    parser.add_argument("--fit", metavar="SESSION_FOLDER", help="solve a saved run, no robot")
    parser.add_argument("--board", action="store_true", help="write the board image to print")
    parser.add_argument("--arm", choices=("left", "right"), default="left", help="arm that holds the board")
    parser.add_argument("--url", default=GATEWAY_URL, help="gateway address")
    args = parser.parse_args()
    ARM = args.arm
    set_gateway(args.url)

    if args.board:
        write_board()
    elif args.fit:
        solve(args.fit)
    else:
        run(args.auto)