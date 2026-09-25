"""
Capture RGB + aligned depth from the camera through the gateway API.

Saves, per capture:
    Data/Images/rgb/rgb_<stamp>.jpg        colour image
    Data/Images/depth/depth_<stamp>.png    16-bit depth, value = millimetres (0 = unknown)
    Data/Images/depth_vis/vis_<stamp>.jpg  colourised depth, for eyeballing only

Depth is aligned to the colour camera, so pixel (u, v) means the same thing
in both images: read depth[v, u] to get the Z of whatever is at rgb[v, u].

Usage:
    py capture_images.py --count 10
    py capture_images.py --interval 2
"""

import argparse
import os
import time
from datetime import datetime

import requests
import cv2
import numpy as np

url = "http://127.0.0.1:8000/api/v1"
headers = {"X-Dev-Client": "operator"}


parser = argparse.ArgumentParser()
parser.add_argument("--output", default="out", help="Folder to save into")
parser.add_argument("--interval", type=float, default=10.0, help="Seconds between saves")
parser.add_argument("--count", type=int, default=0, help="Stop after N captures (0 = unlimited)")
args = parser.parse_args()


rgb_dir = os.path.join(args.output, "rgb")
depth_dir = os.path.join(args.output, "depth")
vis_dir = os.path.join(args.output, "depth_vis")

for d in (rgb_dir, depth_dir, vis_dir):
    os.makedirs(d, exist_ok=True)


saved = 0

try:
    while True:
        if saved:
            print(f"Waiting {args.interval:g} seconds...", flush=True)
            # The gateway keeps the latest frame, so there are no queues to drain.
            time.sleep(args.interval)
            print("Next image", flush=True)

        print("Capturing...", flush=True)

        rgb_response = requests.get(f"{url}/camera/rgb", headers=headers)
        depth_response = requests.get(f"{url}/camera/depth", headers=headers)
        rgb_response.raise_for_status()
        depth_response.raise_for_status()

        rgb_width = int(rgb_response.headers["X-Frame-Width"])
        rgb_height = int(rgb_response.headers["X-Frame-Height"])
        depth_width = int(depth_response.headers["X-Frame-Width"])
        depth_height = int(depth_response.headers["X-Frame-Height"])

        # BGR888p from the API -> the BGR array cv2.imwrite expects.
        rgb = np.frombuffer(rgb_response.content, np.uint8).reshape(
            3, rgb_height, rgb_width
        ).transpose(1, 2, 0)

        depth = np.frombuffer(depth_response.content, np.uint16).reshape(
            depth_height, depth_width
        )  # uint16, millimetres

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

        print(
            f"Saved [{saved}] {stamp}  centre depth = {centre_mm} mm",
            flush=True,
        )

        if args.count and saved >= args.count:
            break

except KeyboardInterrupt:
    pass


print(f"Done. {saved} captures saved.", flush=True)