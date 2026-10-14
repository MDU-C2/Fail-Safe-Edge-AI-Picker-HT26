"""
Get a photo from the camera gateway, run the trained YOLO11-seg model on it,
and save where every object is - in pixels, camera mm and robot mm.

Pipeline (repeats every time you press Enter):
    1. get RGB + depth (aligned to RGB) from the Robot & Camera Gateway over HTTP
    2. YOLO11-seg -> one mask per CUP BASE and CUP HANDLE (other classes are ignored)
    3. per object: mask centre (u, v) + median depth inside the mask
       -> X, Y, Z in mm using the colour camera intrinsics (camera frame)
    4. calibrate(x, y, z) -> robot X, Y, Z in mm, using ../calibration.json
    5. save to Data/Detections/:
           rgb_<stamp>.jpg          photo
           depth_<stamp>.png        16-bit depth, mm
           annotated_<stamp>.jpg    photo with masks drawn
           detections_<stamp>.json  all objects of this capture
           latest.json              same as above, always the newest capture
           detections.csv           one row per object, all captures

x_mm, y_mm, z_mm are from the camera; robot_x_mm, robot_y_mm, robot_z_mm are
the same point after calibrate() - the ones the robot uses.

The model is picked by the device the script runs on; the weights must sit in
the same folder as this file:
    Westermo Lynx  -> best_ncnn_model/   (set PICKER_DEVICE=lynx in the Lynx container)
    Raspberry Pi   -> best.pt            (detected automatically)
    anything else  -> best.pt            (e.g. the development laptop)

The gateway runs in strict mode, so a read-only access token is needed.
Set it once in the terminal (never put it in this file):
    PowerShell:  $env:GATEWAY_TOKEN = "<token>"
    Linux:       export GATEWAY_TOKEN="<token>"

Usage:
    py capture_and_detect.py
    py capture_and_detect.py --gateway http://169.254.125.202:8000 --conf 0.6
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
KEEP_CLASSES = ["CUP BASE", "CUP HANDLE"]

parser = argparse.ArgumentParser()
parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
parser.add_argument("--gateway", default="http://192.168.125.202:8000", help="Camera gateway address")
parser.add_argument("--conf", type=float, default=0.5)
args = parser.parse_args()

out_dir = Path(args.output)
out_dir.mkdir(parents=True, exist_ok=True)

API = args.gateway.rstrip("/") + "/api/v1"
TOKEN = os.environ.get("GATEWAY_TOKEN")
if not TOKEN:
    raise SystemExit('No gateway token. Set it first, e.g. in PowerShell:  $env:GATEWAY_TOKEN = "<token>"')
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px"]

with open(HERE.parent / "calibration.json") as f:
    CAL = json.load(f)


def calibrate(x, y, z):
    """Camera x, y, z (mm) -> robot x, y, z (mm):  robot = scale * R @ camera + t + tag_offset

    tag_offset = tag centre minus gripper TCP (robot axes, mm). The calibration maps the
    tag onto the TCP, so the offset is added back to land on the true position.
    """
    robot = (CAL["scale"] * np.array(CAL["R"]) @ np.array([x, y, z])
             + np.array(CAL["t"]))
    return robot[0], robot[1], robot[2]


SEND_SCRIPT = HERE.parent / "Joen" / "Calibration" / "SendCalibrationPoses.py"


def send_to_robot(x, y, z):
    """Move the arm to x, y, z (robot mm) with Joen's SendCalibrationPoses.py."""
    # z = z                     # real cup base height - use again once picking is ready
    z = 120                     # for now: stay 150 mm above the table so the gripper doesn't hit the cup
    print(f"Moving the arm to X={x} Y={y} Z={z}", flush=True)
    subprocess.run([sys.executable, str(SEND_SCRIPT), "--x", str(x), "--y", str(y), "--z", str(z),
                    "--url", API, "--token", TOKEN])


def detect_device():
    """Which hardware are we on? The Lynx container sets PICKER_DEVICE=lynx."""
    if os.environ.get("PICKER_DEVICE", "").lower() == "lynx":
        return "lynx"
    # device-tree is hidden inside Docker, /proc/cpuinfo is not
    for path in ("/proc/device-tree/model", "/proc/cpuinfo"):
        try:
            with open(path) as f:
                if "raspberry pi" in f.read().lower():
                    return "raspberry_pi"
        except OSError:
            pass
    return "laptop"


def model_path(device_name):
    """NCNN on the Lynx, PyTorch weights everywhere else - always next to this file."""
    path = HERE / ("best_ncnn_model" if device_name == "lynx" else "best.pt")
    if not path.exists():
        raise SystemExit(f"Model not found: {path}\n"
                         f"Put best.pt and best_ncnn_model/ in the same folder as capture_and_detect.py.")
    return path


def gateway_get(path):
    """GET from the gateway; stop with a clear message on auth/connection errors."""
    try:
        r = requests.get(API + path, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        raise SystemExit(f"Cannot reach the gateway at {API}: {e}")
    if r.status_code in (401, 403):
        raise SystemExit(f"Gateway refused the token ({r.status_code}): {r.text}")
    r.raise_for_status()
    return r


def get_frames(tries=5):
    """Latest RGB (BGR, HxWx3) + depth (uint16 mm, HxW) from the same camera frame."""
    for _ in range(tries):
        r_rgb = gateway_get("/camera/rgb")
        r_depth = gateway_get("/camera/depth")
        if r_rgb.headers["X-Frame-Sequence"] != r_depth.headers["X-Frame-Sequence"]:
            continue                               # a new frame arrived in between - try again
        w, h = int(r_rgb.headers["X-Frame-Width"]), int(r_rgb.headers["X-Frame-Height"])
        # BGR888p = planar: all blue, then all green, then all red
        rgb = np.frombuffer(r_rgb.content, np.uint8).reshape(3, h, w).transpose(1, 2, 0).copy()
        dw, dh = int(r_depth.headers["X-Frame-Width"]), int(r_depth.headers["X-Frame-Height"])
        depth = np.frombuffer(r_depth.content, np.uint16).reshape(dh, dw)
        return rgb, depth
    raise RuntimeError("Could not get a matching RGB + depth pair from the gateway")


def analyse(rgb, depth, fx, fy, cx0, cy0):
    """Run YOLO on one frame; return (list of objects, annotated image)."""
    result = model.predict(rgb, conf=args.conf, classes=keep_ids, retina_masks=True, verbose=False)[0]

    objects, keep = [], []
    for j, box in enumerate(result.boxes):
        name = model.names[int(box.cls)]
        if result.masks is None:
            continue
        mask = result.masks.data[j].cpu().numpy().astype(bool)
        if not mask.any():
            continue
        keep.append(j)

        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())

        vals = depth[mask]
        vals = vals[vals > 0]                      # 0 = no depth there
        z = float(np.median(vals)) if vals.size else None

        x = (u - cx0) * z / fx if z else None
        y = (v - cy0) * z / fy if z else None
        rx, ry, rz = calibrate(x, y, z) if z else (None, None, None)

        obj = {
            "class": name,
            "confidence": round(float(box.conf), 3),
            "x_mm": round(x, 1) if z else None,
            "y_mm": round(y, 1) if z else None,
            "x_px": round(u, 1),
            "y_px": round(v, 1),
            "z_mm": round(z, 1) if z else None,   # distance from the camera
            "robot_x_mm": round(float(rx), 1) if z else None,
            "robot_y_mm": round(float(ry), 1) if z else None,
            "robot_z_mm": round(float(rz), 1) if z else None,
            "mask_area_px": int(mask.sum()),
        }
        objects.append(obj)

    annotated = result[keep].plot() if keep else rgb.copy()
    for o in objects:                              # mark the point we report
        cv2.drawMarker(annotated, (int(o["x_px"]), int(o["y_px"])), (0, 0, 255),
                       cv2.MARKER_CROSS, 25, 3)
    return objects, annotated


device_name = detect_device()
weights = model_path(device_name)
print(f"Device: {device_name} -> loading {weights.name} ...", flush=True)
model = YOLO(str(weights), task="segment")
keep_ids = [i for i, n in model.names.items() if n in KEEP_CLASSES]
print("Detecting:", [model.names[i] for i in keep_ids], flush=True)

print(f"Gateway: {API}", flush=True)
status = gateway_get("/camera/status").json()
if not status.get("connected"):
    raise SystemExit(f"The gateway is running but its camera is not connected: {status}")

# Colour camera intrinsics (depth is aligned to this camera)
K = gateway_get("/camera/intrinsics").json()
fx, fy, cx0, cy0 = K["fx"], K["fy"], K["cx"], K["cy"]
print(f"Intrinsics: fx={fx:.1f} fy={fy:.1f} cx={cx0:.1f} cy={cy0:.1f}", flush=True)

print(f"Ready. Saving to: {out_dir}", flush=True)
captures = 0
try:
    while True:
        cmd = input("\nPress Enter to capture + detect (q + Enter to quit): ").strip().lower()
        if cmd == "q":
            break

        rgb, depth = get_frames()

        t = time.perf_counter()
        objects, annotated = analyse(rgb, depth, fx, fy, cx0, cy0)
        ms = (time.perf_counter() - t) * 1000

        stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{captures:04d}"
        cv2.imwrite(str(out_dir / f"rgb_{stamp}.jpg"), rgb)
        cv2.imwrite(str(out_dir / f"depth_{stamp}.png"), depth)
        cv2.imwrite(str(out_dir / f"annotated_{stamp}.jpg"), annotated)

        record = {"capture": stamp, "units": "mm",
                  "intrinsics": {"fx": fx, "fy": fy, "cx": cx0, "cy": cy0},
                  "objects": objects}
        for name in (f"detections_{stamp}.json", "latest.json"):
            with open(out_dir / name, "w") as f:
                json.dump(record, f, indent=2)

        csv_path = out_dir / "detections.csv"
        if csv_path.exists():                  # old file with other columns -> keep it aside
            with open(csv_path) as f:
                if f.readline().strip() != ",".join(CSV_FIELDS):
                    csv_path.rename(out_dir / f"detections_old_{stamp}.csv")
        new_file = not csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows({"capture": stamp, **o} for o in objects)

        captures += 1
        print(f"[{stamp}] {len(objects)} objects, YOLO {ms:.0f} ms", flush=True)
        for o in objects:
            print(f"   {o['class']:<11} {o['confidence']:.2f}", flush=True)
            if not o["z_mm"]:
                print("      no depth", flush=True)
                continue
            print(f"      YOLO (camera):     X={o['x_mm']:7.1f}  Y={o['y_mm']:7.1f}  Z={o['z_mm']:7.1f} mm", flush=True)
            print(f"      calibrated (robot): X={o['robot_x_mm']:7.1f}  Y={o['robot_y_mm']:7.1f}  Z={o['robot_z_mm']:7.1f} mm", flush=True)

        # Send the best CUP BASE (robot coordinates) to the arm
        bases = [o for o in objects if o["class"] == "CUP BASE" and o["robot_x_mm"] is not None]
        if bases:
            best = max(bases, key=lambda o: o["confidence"])
            send_to_robot(best["robot_x_mm"], best["robot_y_mm"], best["robot_z_mm"])
        else:
            print("No CUP BASE with depth - nothing sent to the robot.", flush=True)
except (KeyboardInterrupt, EOFError):
    pass

print(f"Done. {captures} captures saved.", flush=True)
