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
    python capture_and_detect/tools/build_yumi_model.py <stl-dir> --left-finger DownFingerV5.STL

--left-finger: the custom finger on the LEFT gripper (one finger, mm, any CAD
frame; the other finger is the same part turned 180 degrees about the gripper
axis). It is bolted exactly where the standard finger goes, by matching its
screw holes (4 x r 1.2 mm on a 12 x 4 mm rectangle, 2 x r 1.0 mm 18 mm apart).
The holes fit two ways round; the finger is designed to cross over the centre
like the standard fingers, with its jaw offset back onto the gripper's centre
line, so the way round that centres the jaw is used. Each jaw moves out 1 mm per
gripper position. DownFingerV5: jaws 72 mm across the outside at position 0,
122 mm at 25 (measured on the robot: 75 and 125, handover_plan.LEFT_GAP_CLOSED).
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


def screw_holes(mesh):
    """Mounting-screw holes: (axis index, 2-D centre, radius, depth range) of small
    cylindrical bores (r 0.8-1.5 mm) in any axis direction."""
    holes = []
    for axis in range(3):
        normal_along = np.abs(mesh.face_normals[:, axis]) < 0.05
        if not normal_along.any():
            continue
        walls = mesh.submesh([np.where(normal_along)[0]], append=True)
        other = [i for i in range(3) if i != axis]
        for wall in walls.split(only_watertight=False):
            points = wall.vertices
            centre = points[:, other].mean(axis=0)
            radius = np.linalg.norm(points[:, other] - centre, axis=1)
            if len(wall.faces) >= 8 and 0.8 <= radius.mean() <= 1.5 and radius.std() < 0.3:
                holes.append((axis, centre, float(radius.mean()),
                              (float(points[:, axis].min()), float(points[:, axis].max()))))
    return holes


def finger_mount_pattern(holes):
    """The 6-hole finger pattern: (axis, pattern centre, face coordinate, face side)."""
    by_axis = {}
    for hole in holes:
        by_axis.setdefault(hole[0], []).append(hole)
    for axis, group in by_axis.items():
        if len(group) >= 6:
            centres = np.array([hole[1] for hole in group])
            low = min(hole[3][0] for hole in group)
            high = max(hole[3][1] for hole in group)
            return axis, centres.mean(axis=0), low, high
    sys.exit("Could not find the 6 screw holes of the finger mounting pattern.")


def mount_left_finger(finger, standard_finger):
    """Bolt the custom finger where standard_finger (screws on the +x carriage) goes.

    Returns the finger in gripper coordinates and its jaw's outside width across
    both fingers at position 0 (for the build report)."""
    axis, centre, low, _ = finger_mount_pattern(screw_holes(finger))
    g_axis, g_centre, g_face, _ = finger_mount_pattern(screw_holes(standard_finger))
    if g_axis != 2:
        sys.exit("The standard finger's screw holes are not along the gripper axis.")
    other = [i for i in range(3) if i != axis]
    options = []
    for flip in (1.0, -1.0):
        # finger axis -> gripper +z (away from the gripper); the two in-plane
        # axes -> gripper x and y, both ways round.
        rotation = np.zeros((3, 3))
        rotation[2, axis] = 1.0
        rotation[0, other[0]] = flip
        rotation[1, other[1]] = 1.0
        if np.linalg.det(rotation) < 0:   # keep it a rotation, never a mirror
            rotation[1] *= -1
        source = np.zeros(3)
        source[axis], source[other] = low, centre
        matrix = np.eye(4)
        matrix[:3, :3] = rotation
        matrix[:3, 3] = np.array([g_centre[0], g_centre[1], g_face]) - rotation @ source
        mounted = finger.copy()
        mounted.apply_transform(matrix)
        jaw = mounted.vertices[mounted.vertices[:, 2] > mounted.bounds[1][2] - 30]
        off_centre = abs(jaw[:, 1].min() + jaw[:, 1].max()) / 2
        options.append((off_centre, mounted, 2 * np.abs(jaw[:, 0]).max()))
    off_centre, mounted, width = min(options, key=lambda option: option[0])
    if off_centre > 5:
        sys.exit(f"No way round puts the custom finger's jaw on the gripper centre line ({off_centre:.1f} mm off).")
    return mounted, width


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stl_dir")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--left-finger", type=Path, help="custom finger STL for the left gripper (mm)")
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
    identity = [float(v) for v in np.eye(4).T.ravel()]
    turned = [round(float(v), 9) for v in trimesh.transformations.rotation_matrix(np.pi, [0, 0, 1]).T.ravel()]
    # Standard servo fingers: closed (tips touching) at the exported pose; each
    # tip moves away from the axis along gripper x, 1 mm per position.
    standard = {"fingers": [{"part": "finger_tip_neg", "matrix": identity, "axis": [-1, 0, 0]},
                            {"part": "finger_tip_pos", "matrix": identity, "axis": [1, 0, 0]}],
                # Cup centre along the gripper axis when held (right arm takes the
                # cup at the handover: left flange 655, right flange 316 mm).
                "cup_grip_centre_m": 0.1575}
    grippers = {"left": standard, "right": standard}
    left_note = ""
    if args.left_finger:
        # The standard finger whose screws are on the +x carriage.
        carriage = tip_neg if finger_mount_pattern(screw_holes(tip_neg))[1][0] > 0 else tip_pos
        custom, width = mount_left_finger(load(args.left_finger), carriage)
        parts["left_finger"] = ("finger", custom)
        grippers["left"] = {
            # Same carriages and crossing layout as the standard fingers: the
            # finger on the +x carriage has its jaw on the -x side and moves -x
            # when the gripper opens.
            "fingers": [{"part": "left_finger", "matrix": identity, "axis": [-1, 0, 0]},
                        {"part": "left_finger", "matrix": turned, "axis": [-1, 0, 0]}],
            # Pick: flange 229 mm over the table, mug 95 mm tall upside-down.
            "cup_grip_centre_m": 0.1815,
            "source": args.left_finger.name,
        }
        left_note = f"; left finger {args.left_finger.name}: jaws {width:.0f} mm across the outside at position 0"

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
        # Per arm: gripper body, fingers (fixed matrix in the gripper frame, then
        # moved along "axis" by 1 mm per gripper position) and where a held cup sits.
        "grippers": {arm: {"body": "gripper", **spec} for arm, spec in grippers.items()},
        "parts": manifest_parts,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "yumi_model.bin").write_bytes(bytes(blob))
    (args.out / "yumi_model.json").write_text(json.dumps(manifest, indent=1) + "\n")
    triangles = sum(p["index_count"] // 3 for p in manifest_parts.values())
    print(f"Wrote {len(manifest_parts)} parts, {triangles} triangles, {len(blob) / 1e6:.1f} MB; "
          f"worst joint-to-mesh distance {worst:.1f} mm{left_note}")


if __name__ == "__main__":
    main()
