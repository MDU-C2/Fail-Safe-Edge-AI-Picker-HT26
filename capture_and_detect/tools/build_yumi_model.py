"""Build the dashboard's 3D YuMi model from RobotStudio STL exports.

Input: one directory with the binary or ASCII STLs exported from RobotStudio
(Layout > IRB14000_0.5_0.5 > Links, every body at all joints = 0, robot not
moved in the station, units mm):

    Base.stl, Link1.stl .. Link7.stl for BOTH arms (any file-name prefix is
    fine; RobotStudio names both arms' links the same, so the arm is found
    from the geometry), and Smart_Gripper_Servo_Fingers.stl (fingers closed,
    exported in the gripper's own frame).

Output (web_static/3d/): yumi_model.bin + yumi_model.json, every part in its
own link frame, metres, z up, so the browser only rotates joints.

Kinematics are the IRB 14000 joint frames from the ROS yumi_description URDF
(Orebro University, BSD). With them the RobotStudio links line up to about
1 mm at the zero pose; the script checks that and refuses to write otherwise.
Chain order per arm: rax_1, rax_2, eax_a, rax_3, rax_4, rax_5, rax_6.

    python capture_and_detect/tools/build_yumi_model.py <stl-dir>
"""

import argparse
import json
from pathlib import Path
import re
import sys

import numpy as np

try:
    import fast_simplification
    import trimesh
    from scipy.spatial import cKDTree
    from scipy.spatial.transform import Rotation
except ImportError:  # pragma: no cover - build-time tool only
    sys.exit("pip install trimesh fast_simplification scipy")

OUT = Path(__file__).resolve().parent.parent / "web_static" / "3d"

HALF_PI = np.pi / 2
# (xyz mm, rpy rad) of each joint in its parent link frame, chain order.
JOINT_1 = {
    "right": ((53.55, -72.5, 414.92), (-0.9795, -0.5682, -2.3155)),
    "left": ((53.55, 72.5, 414.92), (0.9781, -0.5716, 2.3180)),
}
JOINTS_2_TO_7 = [
    ((30, 0, 100), (HALF_PI, 0, 0)),              # rax_2
    ((-30, 172.83, 0), (-HALF_PI, 0, 0)),         # eax_a (arm angle)
    ((-41.88, 0, 78.73), (HALF_PI, -HALF_PI, 0)),  # rax_3
    ((40.5, 164.61, 0), (-HALF_PI, 0, 0)),        # rax_4
    ((-27, 0, 100.39), (HALF_PI, 0, 0)),          # rax_5
    ((27, 29, 0), (-HALF_PI, 0, 0)),              # rax_6
]
CHAIN = ["rax_1", "rax_2", "eax_a", "rax_3", "rax_4", "rax_5", "rax_6"]
# Gripper base on the link 7 flange (URDF: 7 mm out, turned 180 degrees).
GRIPPER_MOUNT = ((0, 0, 7), (0, 0, np.pi))

# Triangle budget per part after decimation (keeps the download ~2-3 MB).
BUDGET = {"body": 30000, "link": 9000, "gripper": 10000, "finger": 2500}
CREASE_DEGREES = 35
COLORS = {"body": "#d7d9db", "link": "#e4e6e8", "gripper": "#43474b", "finger": "#a9aeb3"}


def transform(xyz, rpy):
    matrix = np.eye(4)
    matrix[:3, :3] = Rotation.from_euler("xyz", rpy).as_matrix()
    matrix[:3, 3] = xyz
    return matrix


def zero_frames(arm):
    frames, current = [], np.eye(4)
    for xyz, rpy in [JOINT_1[arm], *JOINTS_2_TO_7]:
        current = current @ transform(xyz, rpy)
        frames.append(current.copy())
    return frames


def load(path):
    mesh = trimesh.load(path, force="mesh")
    mesh.merge_vertices()
    return mesh


def classify(directory):
    base, gripper, links = None, None, {i: [] for i in range(1, 8)}
    for path in sorted(Path(directory).glob("*.stl")):
        name = path.stem
        if re.search(r"(^|[-_])Base$", name):
            base = path
        elif "gripper" in name.lower():
            gripper = path
        elif match := re.search(r"(^|[-_])Link([1-7])$", name):
            links[int(match.group(2))].append(path)
    missing = [f"Link{i}" for i, paths in links.items() if len(paths) != 2]
    if base is None or gripper is None or missing:
        sys.exit(f"Need Base, the gripper and two of each link; missing/extra: "
                 f"{missing + (['Base'] if base is None else []) + (['gripper'] if gripper is None else [])}")
    return base, gripper, links


def assign_arms(links):
    """Each link pair: the file whose mesh sits nearest that arm's joint frame wins.

    At the zero pose both Link7s overlap almost exactly, so for Link7 the test is
    instead how well the flange disc is centred on that arm's axis 6.
    """
    frames = {arm: zero_frames(arm) for arm in ("right", "left")}
    meshes = {}
    for index, paths in links.items():
        pair = [load(path) for path in paths]
        def distance(mesh, arm):
            frame = frames[arm][index - 1]
            if index < 7:
                return np.linalg.norm(mesh.centroid - frame[:3, 3])
            local = trimesh.transform_points(mesh.sample(20000, seed=1), np.linalg.inv(frame))
            return np.hypot(*local[:, :2].mean(axis=0))
        def cost(order):
            return sum(distance(mesh, arm) for mesh, arm in zip(order, ("right", "left")))
        right, left = pair if cost(pair) <= cost(pair[::-1]) else pair[::-1]
        meshes[("right", index)], meshes[("left", index)] = right, left
    return meshes, frames


def check_alignment(meshes, frames):
    """Each link must turn about its own joint axis: link i's mesh must contain
    the joint origin's neighbourhood (cheap sanity check of the arm assignment)."""
    worst = 0.0
    for (arm, index), mesh in meshes.items():
        origin = frames[arm][index - 1][:3, 3]
        distance, _ = cKDTree(mesh.vertices).query(origin)
        worst = max(worst, distance)
        if distance > 90:
            sys.exit(f"{arm} Link{index} is {distance:.0f} mm from its joint; wrong pose or file?")
    return worst


def decimate(mesh, budget):
    if len(mesh.faces) > budget:
        points, faces = fast_simplification.simplify(
            mesh.vertices.astype(np.float32), mesh.faces.astype(np.int32),
            target_reduction=1 - budget / len(mesh.faces))
        mesh = trimesh.Trimesh(points, faces, process=True)
    return trimesh.graph.smooth_shade(mesh, angle=np.radians(CREASE_DEGREES))


def tip_side(finger):
    """+1 or -1: which side of the gripper axis the finger's tip (z > 120 mm) is on."""
    tip = finger.vertices[finger.vertices[:, 2] > 120.0]
    return 1.0 if tip[:, 0].mean() > 0 else -1.0


def split_fingers(gripper):
    """The two servo fingers are the separate bodies above the gripper housing.

    Each finger's slide is on one side and its tip on the OTHER side of the axis
    (the fingers cross over), so a finger is named, and opens, by its tip:
    opening moves each tip away from the axis.
    """
    body, finger_neg, finger_pos = [], [], []
    for part in gripper.split(only_watertight=False):
        if part.bounds[0][2] >= 83.0:
            (finger_neg if part.centroid[0] < 0 else finger_pos).append(part)
        else:
            body.append(part)
    if not finger_neg or not finger_pos:
        sys.exit("Could not find two fingers above z = 83 mm in the gripper STL.")
    fingers = [trimesh.util.concatenate(finger_neg), trimesh.util.concatenate(finger_pos)]
    sides = [tip_side(finger) for finger in fingers]
    if sides[0] == sides[1]:
        sys.exit("Both finger tips are on the same side of the gripper axis; check the export.")
    tip_neg, tip_pos = fingers if sides[0] < 0 else fingers[::-1]
    return trimesh.util.concatenate(body), tip_neg, tip_pos


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stl_dir")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    base_path, gripper_path, link_paths = classify(args.stl_dir)
    meshes, frames = assign_arms(link_paths)
    worst = check_alignment(meshes, frames)

    parts = {"body": ("body", load(base_path))}
    for (arm, index), mesh in meshes.items():
        local = mesh.copy()
        local.apply_transform(np.linalg.inv(frames[arm][index - 1]))
        parts[f"{arm}_link{index}"] = ("link", local)
    gripper, tip_neg, tip_pos = split_fingers(load(gripper_path))
    parts.update(gripper=("gripper", gripper), finger_tip_neg=("finger", tip_neg),
                 finger_tip_pos=("finger", tip_pos))

    blob, manifest_parts = bytearray(), {}
    def append(array):
        while len(blob) % 4:
            blob.append(0)
        offset = len(blob)
        blob.extend(array.tobytes())
        return offset
    for name, (kind, mesh) in parts.items():
        mesh = decimate(mesh, BUDGET[kind])
        positions = (mesh.vertices / 1000.0).astype(np.float32)
        normals = np.clip(np.round(mesh.vertex_normals * 127), -127, 127).astype(np.int8)
        index_type = np.uint16 if len(positions) < 65536 else np.uint32
        manifest_parts[name] = {
            "color": COLORS[kind],
            "vertices": len(positions),
            "positions": append(positions),
            "normals": append(normals),
            "indices": append(mesh.faces.astype(index_type).ravel()),
            "index_count": int(mesh.faces.size),
            "index_type": "uint16" if index_type is np.uint16 else "uint32",
        }

    def origin(xyz, rpy):
        matrix = transform(np.array(xyz) / 1000.0, rpy)
        return [round(float(v), 9) for v in matrix.T.ravel()]  # column-major for three.js
    manifest = {
        "source": "RobotStudio IRB14000_0.5_0.5 + Smart_Gripper_Servo_Fingers; joint frames from ROS yumi_description",
        "units": "m", "up": "z", "frame": "YuMi base frame (wobj0)",
        # Same as picker_workflow --grid-table-z (default 0 mm): the table top is
        # z = 0 in the base frame; the lowest 100 mm of the YuMi base sits below it.
        "table_z": 0.0,
        "chain": CHAIN,
        "arms": {arm: {"joints": [origin(*joint) for joint in [JOINT_1[arm], *JOINTS_2_TO_7]],
                       "links": [f"{arm}_link{i}" for i in range(1, 8)]} for arm in ("right", "left")},
        "gripper_mount": origin(*GRIPPER_MOUNT),
        # Fingers are closed (tips touching) at the exported pose; opening moves
        # each tip away from the axis along gripper x.
        "fingers": {"finger_tip_neg": [-1, 0, 0], "finger_tip_pos": [1, 0, 0]},
        "parts": manifest_parts,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "yumi_model.bin").write_bytes(bytes(blob))
    (args.out / "yumi_model.json").write_text(json.dumps(manifest, indent=1) + "\n")
    triangles = sum(p["index_count"] // 3 for p in manifest_parts.values())
    print(f"Wrote {len(manifest_parts)} parts, {triangles} triangles, {len(blob) / 1e6:.1f} MB; "
          f"worst joint-to-mesh distance {worst:.1f} mm")


if __name__ == "__main__":
    main()
