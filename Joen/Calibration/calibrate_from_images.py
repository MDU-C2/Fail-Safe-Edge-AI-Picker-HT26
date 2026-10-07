"""
Camera-to-robot calibration from saved images, depth method.

rgb/ and depth/ next to this script hold images named 1..N (.png or .jpg).
Image n was taken with the gripper at POSES[n-1]. Depth is 16-bit, millimetres.

Idea: the tag is one physical point seen in two coordinate systems. The robot knows where
it is (POSES, robot frame), the camera measures where it is (camera frame, full 3D from
pixel + depth). With enough pairs we find the rotation R and translation t that turn
camera coordinates into robot coordinates:

    p_robot = R @ p_cam + t        (mm)

The same pixel + depth -> 3D step is what the cup detection will use later, so any
systematic depth error is partly absorbed into R and t.
"""
import json
import os

import cv2
import numpy as np

# Robot side: where the gripper TCP was for image n (robot base frame, mm).
# The gripper orientation must be the same in every pose, so the tag sits at the same
# offset from the TCP each time (see TAG_OFFSET).
POSES = [
    (350, 60, 260), (350, 145, 300), (350, 230, 300),
    (375, 230, 300), (375, 145, 300), (375, 60, 260),
    (475, 60, 340), (400, 145, 260), (400, 230, 300),
    (425, 230, 260), (425, 145, 300), (480, 145, 340),
    (480, -145, 340), (425, -145, 300), (425, -230, 260),
    (400, -230, 300), (400, -145, 260), (475, -60, 340),
    (375, -60, 260), (375, -145, 300), (375, -230, 300),
    (350, -230, 300), (350, -145, 300), (350, -60, 260),
]
# Camera intrinsics (pinhole model), in pixels, valid for 1920x1080 images.
# fx, fy: focal length, how many pixels one unit of X/Z (or Y/Z) moves in the image.
# cx, cy: principal point, the pixel the camera looks straight at.
FX, FY, CX, CY = 1064.0, 1064.0, 960.0, 540.0

TAG_ID = 0

# The robot reports the TCP, the camera sees the tag. Their difference is constant
# (same orientation in every pose), so the fit hides it inside t. Measure it in the
# calibration pose, in robot coordinates, and it is added back at the end.
TAG_OFFSET = (0.0, 0.0, 0.0)                    # tag centre minus gripper TCP, mm

IGNORE_BOX = (1630, 805, 1770, 945)             # x0, y0, x1, y1: the tag lying on the table

HERE = os.path.dirname(os.path.abspath(__file__))

# AprilTag detector. Sub-pixel refinement places the corners more precisely than whole
# pixels; minDistanceToBorder = 0 keeps tags that touch the image edge.
params = cv2.aruco.DetectorParameters()
params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
params.minDistanceToBorder = 0
detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), params)


def load(folder, n, flags):
    for ext in (".png", ".jpg"):
        img = cv2.imread(os.path.join(HERE, folder, f"{n}{ext}"), flags)
        if img is not None:
            return img
    return None


def find_tag(rgb):
    """Tag corners in pixels, or None if not exactly one tag with TAG_ID.

    The table tag is painted white first. If two tags with the same ID are visible
    we cannot tell which one is on the gripper, so the image is rejected.
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = IGNORE_BOX
    gray[y0:y1, x0:x1] = 255
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None or list(ids.ravel()).count(TAG_ID) != 1:
        return None
    return corners[list(ids.ravel()).index(TAG_ID)][0]


def to_camera(c, depth):
    """Tag centre in camera coordinates (mm), or None if there is no depth on the tag.

    Pinhole model: a camera point (X, Y, Z) lands on pixel u = fx * X / Z + cx,
    v = fy * Y / Z + cy. A pixel alone only gives a direction; the stereo depth gives Z.
    The median over all pixels inside the tag ignores holes (0) and stray values.
    With Z known the model is solved backwards at the tag's centre pixel (u, v):
        X = (u - cx) * Z / fx,   Y = (v - cy) * Z / fy
    """
    mask = np.zeros(depth.shape, np.uint8)
    cv2.fillConvexPoly(mask, c.astype(np.int32), 1)
    d = depth[(mask > 0) & (depth > 0)]
    if d.size == 0:
        return None
    z = float(np.median(d))
    u, v = c.mean(axis=0)
    return np.array([(u - CX) * z / FX, (v - CY) * z / FY, z])


def fit(P, Q):
    """Best R, t with Q = R @ P + t in the least-squares sense (Kabsch algorithm).

    1. Move both point clouds to their centroids. That removes t, leaving only rotation.
    2. H = sum of (p - p_mean)(q - q_mean)^T describes how the two clouds line up.
    3. SVD of H gives the rotation that best aligns them: R = V U^T.
    4. D flips the sign of one axis if the result is a mirror image (det = -1), which a
       real rotation cannot be.
    5. With R known, t is what moves the rotated camera centroid onto the robot centroid.
    """
    pc, qc = P.mean(axis=0), Q.mean(axis=0)
    U, _, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    return R, qc - R @ pc


# Measure every pose: find the tag, read its depth, convert to camera coordinates.
cam, rob, used = [], [], []
for n, pose in enumerate(POSES, 1):
    rgb = load("rgb", n, cv2.IMREAD_COLOR)
    depth = load("depth", n, cv2.IMREAD_UNCHANGED)
    c = find_tag(rgb) if rgb is not None else None
    p = to_camera(c, depth) if c is not None and depth is not None else None
    if p is None:
        print(f"pose {n:2d}: skipped (missing image, no tag or no depth)")
        continue
    cam.append(p)
    rob.append(pose)
    used.append(n)

# Fit R and t. They have 6 unknowns (3 rotation, 3 translation) and every pose gives
# 3 equations, so 3 poses is the bare minimum; more poses average out noise.
cam, rob = np.array(cam), np.array(rob, float)
R, t = fit(cam, rob)

# Error per pose: distance between where the calibration puts the tag and where the robot
# was. Errors that are equal in every pose (like TAG_OFFSET) end up in t and never show here.
err = np.linalg.norm(cam @ R.T + t - rob, axis=1)

# Error on new points: refit without one pose, predict that pose, repeat for every pose.
# A pose the fit has not seen shows the error to expect for a real cup inside the same area.
loo = []
for k in range(len(used)):
    keep = np.arange(len(used)) != k
    Rk, tk = fit(cam[keep], rob[keep])
    loo.append(np.linalg.norm(Rk @ cam[k] + tk - rob[k]))
loo = np.array(loo)

print(f"\n{'pose':>4}  {'fit':>8}  {'unseen':>8}  {'depth':>8}")
for n, e, l, p in zip(used, err, loo, cam):
    print(f"{n:4d}  {e:5.1f} mm  {l:5.1f} mm  {p[2]:5.0f} mm")
print(f"\n{len(used)} poses, fit error mean {err.mean():.1f} mm (max {err.max():.1f}), "
      f"unseen mean {loo.mean():.1f} mm (max {loo.max():.1f})")
print(f"Calibrated depth range: {cam[:, 2].min():.0f}-{cam[:, 2].max():.0f} mm from the camera")

# The fit maps the tag onto the TCP. Adding the offset makes it map the tag onto the
# tag's true position, so any point the camera sees lands where it really is.
with open(os.path.join(HERE, "calibration.json"), "w") as f:
    json.dump({"R": R.tolist(), "t": (t + np.array(TAG_OFFSET)).tolist(),
               "fit_error_mm": float(err.mean()), "unseen_error_mm": float(loo.mean()),
               "depth_range_mm": [float(cam[:, 2].min()), float(cam[:, 2].max())],
               "poses_used": used}, f, indent=2)
print("Saved calibration.json")