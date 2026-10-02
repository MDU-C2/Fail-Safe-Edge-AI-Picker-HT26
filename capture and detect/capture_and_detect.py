"""
Take a photo with the OAK-D Pro W PoE, run the trained YOLO11-seg model on it,
and save where every object is - in pixels AND in 3D camera coordinates (mm).

Pipeline (repeats every time you press Enter):
    1. capture RGB + depth aligned to RGB   (same camera setup as capture_images.py)
    2. YOLO11-seg -> one mask per CUP BASE and CUP HANDLE (other classes are ignored)
    3. per object: mask centre (u, v) + median depth inside the mask
       -> X, Y, Z in mm using the colour camera intrinsics
    4. save to Data/Detections/:
           rgb_<stamp>.jpg          photo
           depth_<stamp>.png        16-bit depth, mm
           annotated_<stamp>.jpg    photo with masks drawn
           detections_<stamp>.json  all objects of this capture
           latest.json              same as above, always the newest capture
           detections.csv           one row per object, all captures

Coordinates are in the CAMERA frame (X right, Y down, Z forward, mm).
They still need a camera->robot calibration before YuMi can use them.

The model is picked by the device the script runs on; both weights must sit in
the same folder as this file:
    Westermo Lynx  -> best_ncnn_model/   (set PICKER_DEVICE=lynx in the Lynx container)
    Raspberry Pi   -> best.pt            (detected automatically)
    anything else  -> best.pt            (e.g. the development laptop)

Usage:
    py capture_and_detect.py
    py capture_and_detect.py --conf 0.6
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import cv2
import depthai as dai
import numpy as np
from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
KEEP_CLASSES = ["CUP BASE", "CUP HANDLE"]

parser = argparse.ArgumentParser()
parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
parser.add_argument("--ip", default="169.254.1.223", help="Camera IP address")
parser.add_argument("--conf", type=float, default=0.5)
parser.add_argument("--warmup", type=float, default=2.0, help="Seconds to discard while auto-exposure settles")
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
args = parser.parse_args()

out_dir = Path(args.output)
out_dir.mkdir(parents=True, exist_ok=True)
SIZE = (args.width, args.height)

CSV_FIELDS = ["capture", "class", "confidence", "u_px", "v_px", "mask_area_px",
              "depth_mm", "x_mm", "y_mm", "z_mm"]


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


def connect(ip, timeout=60):
    """PoE cameras reboot after a pipeline closes and are unreachable for ~30s."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            return dai.Device(dai.DeviceInfo(ip))
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            print(f"  camera not ready (attempt {attempt}), retrying...", flush=True)
            time.sleep(3)


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

        obj = {
            "class": name,
            "confidence": round(float(box.conf), 3),
            "u_px": round(u, 1),
            "v_px": round(v, 1),
            "mask_area_px": int(mask.sum()),
            "depth_mm": round(z, 1) if z else None,
            "x_mm": round((u - cx0) * z / fx, 1) if z else None,
            "y_mm": round((v - cy0) * z / fy, 1) if z else None,
            "z_mm": round(z, 1) if z else None,
        }
        objects.append(obj)

    annotated = result[keep].plot() if keep else rgb.copy()
    for o in objects:                              # mark the point we report
        cv2.drawMarker(annotated, (int(o["u_px"]), int(o["v_px"])), (0, 0, 255),
                       cv2.MARKER_CROSS, 25, 3)
    return objects, annotated


device_name = detect_device()
weights = model_path(device_name)
print(f"Device: {device_name} -> loading {weights.name} ...", flush=True)
model = YOLO(str(weights), task="segment")
keep_ids = [i for i, n in model.names.items() if n in KEEP_CLASSES]
print("Detecting:", [model.names[i] for i in keep_ids], flush=True)

print(f"Connecting to OAK-D at {args.ip}...", flush=True)
device = connect(args.ip)

# Colour camera intrinsics at the output resolution (depth is aligned to this camera)
K = np.array(device.readCalibration().getCameraIntrinsics(dai.CameraBoardSocket.CAM_A, *SIZE))
fx, fy, cx0, cy0 = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
print(f"Intrinsics: fx={fx:.1f} fy={fy:.1f} cx={cx0:.1f} cy={cy0:.1f}", flush=True)

with dai.Pipeline(device) as pipeline:
    color = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
    left = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_B)
    right = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_C)

    stereo = pipeline.create(dai.node.StereoDepth)
    stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.ROBOTICS)
    stereo.setLeftRightCheck(True)
    stereo.setSubpixel(True)
    stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
    stereo.setOutputSize(*SIZE)

    left.requestOutput((640, 400)).link(stereo.left)
    right.requestOutput((640, 400)).link(stereo.right)

    rgb_queue = color.requestOutput(SIZE, dai.ImgFrame.Type.BGR888p).createOutputQueue(maxSize=1, blocking=False)
    depth_queue = stereo.depth.createOutputQueue(maxSize=1, blocking=False)

    pipeline.start()

    print(f"Warming up for {args.warmup}s...", flush=True)
    warmup_end = time.monotonic() + args.warmup
    while time.monotonic() < warmup_end:
        rgb_queue.get()
        depth_queue.get()

    print(f"Ready. Saving to: {out_dir}", flush=True)
    captures = 0
    try:
        while pipeline.isRunning():
            cmd = input("\nPress Enter to capture + detect (q + Enter to quit): ").strip().lower()
            if cmd == "q":
                break

            # queues keep only the newest frame, so this is a fresh photo
            rgb = rgb_queue.get().getCvFrame()
            depth = depth_queue.get().getFrame()   # uint16, mm

            t = time.perf_counter()
            objects, annotated = analyse(rgb, depth, fx, fy, cx0, cy0)
            ms = (time.perf_counter() - t) * 1000

            stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{captures:04d}"
            cv2.imwrite(str(out_dir / f"rgb_{stamp}.jpg"), rgb)
            cv2.imwrite(str(out_dir / f"depth_{stamp}.png"), depth)
            cv2.imwrite(str(out_dir / f"annotated_{stamp}.jpg"), annotated)

            record = {"capture": stamp, "frame": "camera", "units": "mm",
                      "intrinsics": {"fx": fx, "fy": fy, "cx": cx0, "cy": cy0},
                      "objects": objects}
            for name in (f"detections_{stamp}.json", "latest.json"):
                with open(out_dir / name, "w") as f:
                    json.dump(record, f, indent=2)

            csv_path = out_dir / "detections.csv"
            new_file = not csv_path.exists()
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerows({"capture": stamp, **o} for o in objects)

            captures += 1
            print(f"[{stamp}] {len(objects)} objects, YOLO {ms:.0f} ms", flush=True)
            for o in objects:
                xyz = (f"X={o['x_mm']:7.1f}  Y={o['y_mm']:7.1f}  Z={o['z_mm']:7.1f} mm"
                       if o["z_mm"] else "no depth")
                print(f"   {o['class']:<11} {o['confidence']:.2f}  {xyz}", flush=True)
    except (KeyboardInterrupt, EOFError):
        pass

    print(f"Done. {captures} captures saved.", flush=True)
    # DepthAI 3.10 segfaults on Windows while collecting a crash dump during
    # device close. Everything is already on disk, so skip that teardown.
    os._exit(0)
