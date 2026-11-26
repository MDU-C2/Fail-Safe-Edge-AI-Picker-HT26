"""
Take a picture from the gateway's camera every few seconds and save it.
Same camera and token as capture_and_detect.py, but no YOLO and no robot.

Set the token first (never in this file):
    PowerShell:  $env:GATEWAY_TOKEN = "<token>"
    Linux:       export GATEWAY_TOKEN="<token>"

Usage:
    python take_pictures.py
    python take_pictures.py --every 2 --depth
Ctrl+C to stop.
"""

import argparse
import os
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests

HERE = Path(__file__).resolve().parent

PICTURES = HERE / "captured_Images"     # a folder "images" next to this script (made if it is not there)


def frame_array(r, dtype, planes=1):
    """Raw frame bytes -> (planes, H, W) array, sized from the X-Frame-* headers."""
    w, h = int(r.headers["X-Frame-Width"]), int(r.headers["X-Frame-Height"])
    return np.frombuffer(r.content, dtype).reshape(planes, h, w)


def main():
    parser = argparse.ArgumentParser(description="Save a camera picture every few seconds")
    parser.add_argument("--gateway", default="http://192.168.125.20:8000", help="Gateway address")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between pictures")
    parser.add_argument("--output", default=str(PICTURES), help="folder for the pictures")
    parser.add_argument("--depth", action="store_true", help="also save the depth image (16-bit png, mm)")
    args = parser.parse_args()

    token = os.environ.get("GATEWAY_TOKEN")
    if not token:
        raise SystemExit('No gateway token. Set it first, e.g. in PowerShell:  $env:GATEWAY_TOKEN = "<token>"')
    api = args.gateway.rstrip("/") + "/api/v1"
    headers = {"Authorization": f"Bearer {token}"}
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)

    print(f"Saving to {folder}, one picture every {args.every:g} s. Ctrl+C to stop.")
    count = 0
    try:
        while True:
            try:
                r = requests.get(api + "/camera/rgb", headers=headers, timeout=15)
                r.raise_for_status()
                rgb = frame_array(r, np.uint8, 3).transpose(1, 2, 0).copy()   # planar BGR -> HxWx3
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                cv2.imwrite(str(folder / f"rgb_{stamp}.jpg"), rgb)
                if args.depth:
                    d = requests.get(api + "/camera/depth", headers=headers, timeout=15)
                    d.raise_for_status()
                    cv2.imwrite(str(folder / f"depth_{stamp}.png"), frame_array(d, np.uint16)[0])
                count += 1
                print(f"  {count}: rgb_{stamp}.jpg")
            except requests.RequestException as e:
                print(f"  no picture: {e}")          # gateway or camera not reachable: try again next time
            time.sleep(args.every)
    except KeyboardInterrupt:
        print(f"\nDone. {count} pictures in {folder}")


if __name__ == "__main__":
    main()