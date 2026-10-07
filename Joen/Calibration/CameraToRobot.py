"""
Convert camera coordinates to robot coordinates using calibration.json.

Type X Y Z in mm (camera frame, Z = distance straight ahead), or q to quit.
"""
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "calibration.json")) as f:
    calib = json.load(f)
R, t = np.array(calib["R"]), np.array(calib["t"])

while True:
    text = input("camera X Y Z > ").strip()
    if text == "q":
        break
    try:
        p_cam = np.array([float(v) for v in text.split()])
    except ValueError:
        print("Give three numbers")
        continue
    if p_cam.size != 3:
        print("Give three numbers")
        continue
    x, y, z = R @ p_cam + t
    print(f"robot  x {x:7.1f}  y {y:7.1f}  z {z:7.1f} mm")