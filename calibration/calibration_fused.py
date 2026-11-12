"""
Experimental: compare three ways to measure the tag's 3D position, on a saved session.

  depth   stereo depth, median over the tag (what camera_calibration.py uses)
  pixel   distance from the tag's size in the image (solvePnP on the 4 corners, TAG_SIZE)
  fused   tag centre pixel, with Z = average of the depth Z and the pixel Z

The tag corners are found again in the saved colour images (rgb/<n>.jpg), so any session
from camera_calibration.py works. No robot, no camera. Each method gets its own fit, its
own unseen error, and its own calibration_<method>.json in the session folder.
calibration.json is not touched.

Usage:
    py calibration_fused.py calibration_runs/session_20261109_114929
    py calibration_fused.py calibration_runs/session_20261109_114929 --exclude 4 13
"""
import argparse
import json
import os

import cv2
import numpy as np

from camera_calibration import TAG_OFFSET, find_tag, fit, to_camera

TAG_SIZE = 28.5                  # mm, side of the black square of the printed tag

s = TAG_SIZE / 2
TAG_CORNERS = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float64)


def pixel_position(corners, k):
    """Tag centre in camera coordinates (mm) from the corners alone, using the tag's known size."""
    K = np.array([[k["fx"], 0, k["cx"]], [0, k["fy"], k["cy"]], [0, 0, 1]])
    dist = np.array(k["dist"]) if k["dist"] else None
    ok, _, tvec = cv2.solvePnP(TAG_CORNERS, corners.astype(np.float64), K, dist,
                               flags=cv2.SOLVEPNP_IPPE_SQUARE)
    return tvec.ravel() if ok else None


def unseen_errors(cam, rob):
    out = []
    for i in range(len(cam)):
        keep = np.arange(len(cam)) != i
        si, Ri, ti = fit(cam[keep], rob[keep])
        out.append(si * Ri @ cam[i] + ti - rob[i])
    return np.array(out)


def main():
    parser = argparse.ArgumentParser(description="Compare depth, pixel and fused tag positions")
    parser.add_argument("session", help="session folder from camera_calibration.py")
    parser.add_argument("--exclude", nargs="*", type=int, default=[], help="image numbers to leave out")
    args = parser.parse_args()

    with open(os.path.join(args.session, "session.json")) as f:
        session = json.load(f)
    k = session["intrinsics"]

    rows = []
    for smp in session["samples"]:
        if not smp["tag"] or smp["image"] in args.exclude:
            continue
        rgb = cv2.imread(os.path.join(args.session, "rgb", f"{smp['image']}.jpg"))
        corners = find_tag(rgb) if rgb is not None else None
        if corners is None:
            print(f"  image {smp['image']}: tag not found in the saved picture, left out")
            continue
        p_pix = pixel_position(corners, k)
        if p_pix is None:
            print(f"  image {smp['image']}: solvePnP failed, left out")
            continue
        u, v, z_depth = smp["tag"]["u"], smp["tag"]["v"], smp["tag"]["z"]
        rows.append({"image": smp["image"], "pose": smp["pose"],
                     "depth": to_camera(u, v, z_depth, k),
                     "pixel": p_pix,
                     "fused": to_camera(u, v, (z_depth + p_pix[2]) / 2, k)})

    if len(rows) < 4:
        raise SystemExit(f"Only {len(rows)} usable poses, need at least 4")

    rob = np.array([r["pose"] for r in rows], float)
    print(f"\n{'image':>5}  {'pose':>18}  {'Z depth':>8}  {'Z pixel':>8}  {'diff':>6}")
    for r in rows:
        zd, zp = r["depth"][2], r["pixel"][2]
        print(f"{r['image']:5d}  {str(tuple(r['pose'])):>18}  {zd:6.0f} mm  {zp:6.0f} mm  {zd - zp:+5.0f}")

    print(f"\n{'method':>6}  {'unseen':>8}  {'max':>8}  {'x':>6}  {'y':>6}  {'z':>6}  {'scale':>6}")
    for method in ("depth", "pixel", "fused"):
        cam = np.array([r[method] for r in rows])
        scale, R, t = fit(cam, rob)
        unseen_xyz = unseen_errors(cam, rob)
        unseen = np.linalg.norm(unseen_xyz, axis=1)
        ax = np.abs(unseen_xyz).mean(axis=0)
        print(f"{method:>6}  {unseen.mean():5.1f} mm  {unseen.max():5.1f} mm  "
              f"{ax[0]:6.1f}  {ax[1]:6.1f}  {ax[2]:6.1f}  {scale:6.3f}")
        result = {"method": method, "scale": float(scale), "R": R.tolist(), "t": t.tolist(),
                  "intrinsics": k, "tag_offset": list(TAG_OFFSET), "tag_size_mm": TAG_SIZE,
                  "unseen_error_mm": float(unseen.mean()), "unseen_error_xyz_mm": ax.tolist(),
                  "images_used": [r["image"] for r in rows],
                  "session": os.path.basename(os.path.normpath(args.session))}
        with open(os.path.join(args.session, f"calibration_{method}.json"), "w") as f:
            json.dump(result, f, indent=2)

    print(f"\n{len(rows)} poses. Saved calibration_depth/pixel/fused.json in the session folder.")


if __name__ == "__main__":
    main()
