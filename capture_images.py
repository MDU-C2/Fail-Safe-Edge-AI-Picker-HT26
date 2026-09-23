"""
Capture RGB + aligned depth from the OAK-D Pro W PoE camera (DepthAI v3 API).

Saves, per capture:
    Data/Images/rgb/rgb_<stamp>.jpg        colour image
    Data/Images/depth/depth_<stamp>.png    16-bit depth, value = millimetres (0 = unknown)
    Data/Images/depth_vis/vis_<stamp>.jpg  colourised depth, for eyeballing only

Depth is aligned to the colour camera, so pixel (u, v) means the same thing
in both images: read depth[v, u] to get the Z of whatever is at rgb[v, u].

Usage:
    py capture_images.py --count 10
    py capture_images.py --interval 2 --warmup 4
"""

import argparse
import os
import time
from datetime import datetime

import cv2
import depthai as dai
import numpy as np

DEFAULT_OUTPUT = (
    r"C:\Users\rani-\Desktop\Robotics and Advanced Embedded Systems Project Course. "
    r"- DVA490, DVA474\Fail-Safe-Edge-AI-Picker-HT26\Data\Images"
)

parser = argparse.ArgumentParser()
parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Folder to save into")
parser.add_argument("--ip", default="169.254.1.223", help="Camera IP address")
parser.add_argument("--interval", type=float, default=5.0, help="Seconds between saves")
parser.add_argument("--count", type=int, default=0, help="Stop after N captures (0 = unlimited)")
parser.add_argument("--warmup", type=float, default=2.0, help="Seconds to discard while auto-exposure settles")
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
args = parser.parse_args()

rgb_dir = os.path.join(args.output, "rgb")
depth_dir = os.path.join(args.output, "depth")
vis_dir = os.path.join(args.output, "depth_vis")
for d in (rgb_dir, depth_dir, vis_dir):
    os.makedirs(d, exist_ok=True)

SIZE = (args.width, args.height)

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


print(f"Connecting to OAK-D at {args.ip}...", flush=True)
device = connect(args.ip)

with dai.Pipeline(device) as pipeline:
    color = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
    left = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_B)
    right = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_C)

    stereo = pipeline.create(dai.node.StereoDepth)
    stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.ROBOTICS)
    stereo.setLeftRightCheck(True)
    stereo.setSubpixel(True)
    # Warp depth into the colour camera's frame so the two images line up pixel for pixel.
    stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
    stereo.setOutputSize(*SIZE)

    left.requestOutput((640, 400)).link(stereo.left)
    right.requestOutput((640, 400)).link(stereo.right)

    rgb_queue = color.requestOutput(SIZE, dai.ImgFrame.Type.BGR888p).createOutputQueue()
    depth_queue = stereo.depth.createOutputQueue()

    pipeline.start()

    print(f"Warming up for {args.warmup}s...", flush=True)
    warmup_end = time.monotonic() + args.warmup
    while time.monotonic() < warmup_end:
        rgb_queue.get()
        depth_queue.get()

    print(f"Connected. Saving to: {args.output}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    saved = 0
    try:
        while pipeline.isRunning():
            if saved:
                print(f"Waiting {args.interval:g} seconds...", flush=True)
                # Keep draining so the next capture is a fresh frame, not a stale one.
                wait_end = time.monotonic() + args.interval
                while time.monotonic() < wait_end:
                    rgb_queue.get()
                    depth_queue.get()
                print("Next image", flush=True)

            print("Capturing...", flush=True)
            rgb = rgb_queue.get().getCvFrame()
            depth = depth_queue.get().getFrame()  # uint16, millimetres

            print("Saving...", flush=True)
            stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{saved:04d}"
            cv2.imwrite(os.path.join(rgb_dir, f"rgb_{stamp}.jpg"), rgb)
            cv2.imwrite(os.path.join(depth_dir, f"depth_{stamp}.png"), depth)

            valid = depth[depth > 0]
            if valid.size:
                norm = np.clip(depth.astype(np.float32) / valid.max(), 0, 1)
                vis = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
                vis[depth == 0] = 0
                cv2.imwrite(os.path.join(vis_dir, f"vis_{stamp}.jpg"), vis)

            centre_mm = int(depth[depth.shape[0] // 2, depth.shape[1] // 2])
            saved += 1
            print(f"Saved [{saved}] {stamp}  centre depth = {centre_mm} mm", flush=True)

            if args.count and saved >= args.count:
                break
    except KeyboardInterrupt:
        pass

    print(f"Done. {saved} captures saved.", flush=True)
    # DepthAI 3.10 segfaults on Windows while collecting a crash dump during
    # device close. Everything is already on disk, so skip that teardown.
    os._exit(0)
