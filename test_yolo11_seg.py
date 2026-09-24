"""
Run pretrained YOLO11-seg on the captured RGB images.

Saves to Data/Images/seg_results/:
    seg_<name>.jpg   image with masks and labels drawn
    detections.csv   one row per detected object

If a matching depth PNG exists, the CSV also gets the median depth (mm)
inside each object's mask, ignoring holes (depth == 0).

Usage:
    py test_yolo11_seg.py
    py test_yolo11_seg.py --conf 0.4 --classes cup
"""

import argparse
import csv
import os
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

IMAGES = Path(__file__).parent / "Data" / "Images"

parser = argparse.ArgumentParser()
parser.add_argument("--input", default=str(IMAGES / "rgb"))
parser.add_argument("--output", default=str(IMAGES / "seg_results"))
parser.add_argument("--depth", default=str(IMAGES / "depth"))
parser.add_argument("--model", default="yolo11n-seg.pt")
parser.add_argument("--conf", type=float, default=0.25)
parser.add_argument("--classes", nargs="*", help="Only keep these class names, e.g. cup")
args = parser.parse_args()

out_dir = Path(args.output)
out_dir.mkdir(parents=True, exist_ok=True)

model = YOLO(args.model)
images = sorted(p for p in Path(args.input).iterdir() if p.suffix.lower() in {".jpg", ".png"})
print(f"{len(images)} images, model {args.model}")

rows = []
for i, img_path in enumerate(images, 1):
    result = model.predict(str(img_path), conf=args.conf, retina_masks=True, verbose=False)[0]

    depth_path = Path(args.depth) / img_path.name.replace("rgb_", "depth_").replace(".jpg", ".png")
    depth = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED) if depth_path.exists() else None

    keep = []
    for j, box in enumerate(result.boxes):
        name = model.names[int(box.cls)]
        if args.classes and name not in args.classes:
            continue
        keep.append(j)

        mask = result.masks.data[j].cpu().numpy().astype(bool) if result.masks is not None else None
        cx = cy = area = depth_mm = ""
        if mask is not None and mask.any():
            ys, xs = np.nonzero(mask)
            cx, cy, area = int(xs.mean()), int(ys.mean()), int(mask.sum())
            if depth is not None and depth.shape == mask.shape:
                vals = depth[mask]
                vals = vals[vals > 0]
                depth_mm = int(np.median(vals)) if vals.size else ""

        rows.append({
            "image": img_path.name,
            "class": name,
            "confidence": round(float(box.conf), 3),
            "mask_cx": cx,
            "mask_cy": cy,
            "mask_area_px": area,
            "median_depth_mm": depth_mm,
        })

    annotated = result[keep].plot() if keep else result.orig_img
    cv2.imwrite(str(out_dir / f"seg_{img_path.name}"), annotated)
    print(f"[{i}/{len(images)}] {img_path.name}: {len(keep)} objects")

csv_path = out_dir / "detections.csv"
with open(csv_path, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["image", "class", "confidence", "mask_cx", "mask_cy", "mask_area_px", "median_depth_mm"])
    writer.writeheader()
    writer.writerows(rows)

print(f"\nDone. {len(rows)} detections -> {csv_path}")
