"""Frozen empty-table depth reference for placement floor agreement only.

The calibrated robot table is still the motion coordinate reference. This plane
models the depth camera's independently observed empty-table height, including
its spatial bias; it does not replace robot calibration or prove that an object
is absent. Callers retain positive-height, detection, occlusion and missing-depth
guards. No fitting is performed while loading or evaluating a saved reference.
"""

import copy
import json
import math
import re

import cv2
import numpy as np

from cup_inventory import SelectionError, finite_number


SCHEMA_VERSION = 1
MAX_FRAME_PIXELS = 8_388_608
MAX_FIT_PIXELS = 262144
MAX_LOCATIONS = 64
MIN_BASELINE_COVERAGE = 0.995
MAX_REFERENCE_OFFSET_MM = 30.0
MAX_SLOPE_DEGREES = 5.0
MAX_FIT_RMS_MM = 5.0
MAX_CELL_RESIDUAL_P99_MM = 15.0
MIN_FIT_BINS = 100
XY_BIN_MM = 2.0
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _fail(message):
    raise SelectionError("Table reference: " + message)


def _source(value):
    if not isinstance(value, dict):
        _fail("source provenance must be an object.")
    capture, digest, sequences = (value.get(key) for key in
                                  ("capture", "depth_sha256", "frame_sequences"))
    if (not isinstance(capture, str) or not capture.strip() or len(capture) > 256
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not isinstance(sequences, list) or len(sequences) != 3
            or any(type(item) is not int or item < 0 for item in sequences)
            or any(a >= b for a, b in zip(sequences, sequences[1:]))):
        _fail("source requires a capture, depth SHA256 and three advancing frame sequences.")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        _fail("source provenance must contain finite JSON values.")
    if len(encoded) > 16384:
        _fail("source provenance is too large.")
    return json.loads(encoded)


def _identity(calibration, intrinsics, locations, cell_size, table_z):
    if (not finite_number(cell_size) or not 20 <= cell_size <= 200
            or not finite_number(table_z)
            or not isinstance(locations, (list, tuple))
            or not 1 <= len(locations) <= MAX_LOCATIONS
            or not isinstance(intrinsics, dict)):
        _fail("invalid grid geometry or intrinsics.")
    digest = getattr(calibration, "digest", None)
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        _fail("calibration must have a SHA256 identity.")
    scale = getattr(calibration, "scale", None)
    if not finite_number(scale) or scale <= 0:
        _fail("invalid calibration scale.")
    try:
        rotation = np.asarray(calibration.R, dtype=float)
        translation = np.asarray(calibration.t, dtype=float)
        offset = np.asarray(calibration.offset, dtype=float)
    except (AttributeError, TypeError, ValueError):
        _fail("invalid calibration transform.")
    if (rotation.shape != (3, 3) or translation.shape != (3,) or offset.shape != (3,)
            or not np.isfinite(rotation).all() or not np.isfinite(translation).all()
            or not np.isfinite(offset).all()):
        _fail("invalid calibration transform.")
    with np.errstate(over="ignore", invalid="ignore"):
        transform = float(scale) * rotation
        origin = translation + offset
        determinant = np.linalg.det(transform)
    if (not np.isfinite(transform).all() or not np.isfinite(origin).all()
            or not math.isfinite(float(determinant)) or abs(determinant) < 1e-9):
        _fail("invalid calibration transform.")
    camera = {}
    for key in ("width", "height"):
        item = intrinsics.get(key)
        if type(item) is not int or item <= 0:
            _fail("invalid camera dimensions.")
        camera[key] = item
    if camera["width"] * camera["height"] > MAX_FRAME_PIXELS:
        _fail("camera frame exceeds the reference resource limit.")
    for key in ("fx", "fy", "cx", "cy"):
        item = intrinsics.get(key)
        if not finite_number(item) or (key in ("fx", "fy") and item <= 0):
            _fail("invalid camera intrinsics.")
        camera[key] = float(item)
    places, seen = [], set()
    for location in locations:
        if not isinstance(location, dict):
            _fail("grid locations must be objects.")
        identifier = location.get("id")
        if type(identifier) is not int or identifier <= 0 or identifier in seen:
            _fail("grid location IDs must be unique positive integers.")
        if not all(finite_number(location.get(axis)) for axis in ("x", "y")):
            _fail("grid coordinates must be finite.")
        seen.add(identifier)
        place = {"id": identifier, "x": float(location["x"]), "y": float(location["y"])}
        if "z" in location:
            if not finite_number(location["z"]):
                _fail("grid coordinates must be finite.")
            place["z"] = float(location["z"])
        places.append(place)
    return {"calibration_sha256": digest.lower(), "scale": float(scale),
            "rotation": rotation.tolist(), "translation": translation.tolist(),
            "tag_offset_mm": offset.tolist(), "intrinsics": camera,
            "locations": places, "cell_size_mm": float(cell_size), "table_z_mm": float(table_z)}


def _corners(identity):
    half = identity["cell_size_mm"] / 2
    return np.array([(location["x"] + dx, location["y"] + dy)
                     for location in identity["locations"]
                     for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half))])


def _check_plane(coefficients, identity):
    if (not isinstance(coefficients, (list, tuple)) or len(coefficients) != 3
            or not all(finite_number(value) for value in coefficients)):
        _fail("plane coefficients must be three finite numbers.")
    coefficients = np.asarray(coefficients, dtype=float)
    slope = math.degrees(math.atan(float(np.hypot(*coefficients[:2]))))
    if slope > MAX_SLOPE_DEGREES:
        _fail("fitted table slope exceeds the allowed bound.")
    predicted = _corners(identity) @ coefficients[:2] + coefficients[2]
    if (not np.isfinite(predicted).all()
            or np.any(np.abs(predicted - identity["table_z_mm"]) > MAX_REFERENCE_OFFSET_MM)):
        _fail("fitted table differs too far from the nominal table plane.")
    return coefficients


class TableReference:
    """Validated immutable-context plane; expected heights are physical robot Z."""

    def __init__(self, value):
        self._value = copy.deepcopy(value)
        self._coefficients = np.asarray(value["plane_z_mm"], dtype=float)
        self._table_z = value["identity"]["table_z_mm"]

    @classmethod
    def from_dict(cls, value, *, calibration, intrinsics, locations, cell_size, table_z=0.0):
        identity = _identity(calibration, intrinsics, locations, cell_size, table_z)
        if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
                or type(value.get("schema_version")) is not int
                or value.get("kind") != "placement_table_reference" or value.get("units") != "mm"):
            _fail("unsupported artifact format.")
        if value.get("identity") != identity:
            _fail("calibration, camera or grid geometry changed; rebuild the reference from an empty table.")
        _source(value.get("source"))
        _check_plane(value.get("plane_z_mm"), identity)
        fit = value.get("fit")
        if (not isinstance(fit, dict)
                or any(type(fit.get(key)) is not int for key in
                       ("sample_count", "bin_count", "inlier_bin_count"))
                or not MIN_FIT_BINS <= fit["inlier_bin_count"] <= fit["bin_count"] <= fit["sample_count"] <= MAX_FIT_PIXELS
                or fit["inlier_bin_count"] < 0.8 * fit["bin_count"]
                or not finite_number(fit.get("rms_mm")) or not 0 <= fit["rms_mm"] <= MAX_FIT_RMS_MM
                or not finite_number(fit.get("max_cell_residual_p99_mm"))
                or not 0 <= fit["max_cell_residual_p99_mm"] <= MAX_CELL_RESIDUAL_P99_MM):
            _fail("invalid or insufficient reference fit quality.")
        return cls(value)

    def expected_height(self, x, y):
        """Evaluate the frozen plane; cap extrapolation to the original Z bound.

        Classification may sample slightly beyond a projected cell because its
        measured depth is biased. Clamping prevents distant erroneous samples
        from extrapolating an unbounded reference and hiding a deep depression.
        Nonfinite coordinates remain unknown through a NaN result.
        """
        x, y = np.broadcast_arrays(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
        with np.errstate(over="ignore", invalid="ignore"):
            height = self._coefficients[0] * x + self._coefficients[1] * y + self._coefficients[2]
        height = np.clip(height, self._table_z - MAX_REFERENCE_OFFSET_MM,
                         self._table_z + MAX_REFERENCE_OFFSET_MM)
        return np.where(np.isfinite(x) & np.isfinite(y), height, np.nan)

    @property
    def metadata(self):
        return {"source_capture": self._value["source"]["capture"],
                "source_depth_sha256": self._value["source"]["depth_sha256"],
                "plane_z_mm": self._coefficients.tolist(),
                "fit_rms_mm": self._value["fit"]["rms_mm"]}


def fit_reference(depth, locations, *, calibration, intrinsics, cell_size,
                  table_z=0.0, source, baseline_slots):
    """Fit a frozen JSON artifact from an independently cleared empty scan.

    The caller supplies the final depth frame of the accepted three-frame scan,
    its provenance and combined slots. All cells must be clear, cup-free and
    fully visible; there is no exclusion of cups, invalid pixels or holes to
    manufacture an empty reference. Fitting is balanced by 2 mm XY bins and
    robust median/MAD residual rejection, with independently bounded cell error.
    This function neither reads files nor acquires camera/robot data.
    """
    identity = _identity(calibration, intrinsics, locations, cell_size, table_z)
    source = _source(source)
    camera = identity["intrinsics"]
    if (not isinstance(depth, np.ndarray) or depth.shape != (camera["height"], camera["width"])
            or not np.issubdtype(depth.dtype, np.number) or np.iscomplexobj(depth)):
        _fail("baseline depth does not match camera dimensions.")
    if (not isinstance(baseline_slots, (list, tuple)) or len(baseline_slots) != len(locations)
            or any(not isinstance(slot, dict) for slot in baseline_slots)
            or [slot.get("id") for slot in baseline_slots] != [place["id"] for place in locations]):
        _fail("baseline slots do not match the grid.")
    for slot in baseline_slots:
        if (slot.get("state") != "clear" or type(slot.get("cup_count")) is not int
                or slot["cup_count"] != 0 or slot.get("assigned_cups") != []
                or slot.get("evidence") != []
                or slot.get("table_reference_applied", False) is not False
                or any(slot.get(key) != 0 for key in ("cup_excluded_pixels", "cup_shared_excluded_pixels"))
                or any(not finite_number(slot.get(key)) or slot[key] != 1.0
                       for key in ("visible_fraction", "floor_visible_fraction"))
                or any(not finite_number(slot.get(key)) or not MIN_BASELINE_COVERAGE <= slot[key] <= 1
                       for key in ("valid_fraction", "table_fraction"))):
            _fail("every baseline cell must be independently clear, cup-free and fully visible.")
    transform = identity["scale"] * np.asarray(identity["rotation"])
    translation = np.asarray(identity["translation"]) + np.asarray(identity["tag_offset_mm"])
    corner_xy = _corners(identity).reshape(-1, 4, 2)
    samples, cell_samples, total_pixels = [], [], 0
    for xy in corner_xy:
        physical = np.column_stack((xy, np.full(4, table_z)))
        camera_corners = np.linalg.solve(transform, (physical - translation).T).T
        if not np.isfinite(camera_corners).all() or np.any(camera_corners[:, 2] <= 0):
            _fail("baseline cell lies behind the camera.")
        with np.errstate(over="ignore", invalid="ignore"):
            polygon = camera_corners[:, :2] / camera_corners[:, 2, None]
            polygon = polygon * [camera["fx"], camera["fy"]] + [camera["cx"], camera["cy"]]
        if (not np.isfinite(polygon).all() or np.any(polygon < 1)
                or np.any(polygon[:, 0] >= camera["width"] - 1)
                or np.any(polygon[:, 1] >= camera["height"] - 1)):
            _fail("baseline cell is not completely in view.")
        x0, y0 = np.floor(polygon.min(axis=0)).astype(int)
        x1, y1 = np.ceil(polygon.max(axis=0)).astype(int) + 1
        if (x1 - x0) * (y1 - y0) + total_pixels > MAX_FIT_PIXELS:
            _fail("baseline floor crops exceed the reference resource limit.")
        floor = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.fillConvexPoly(floor, np.rint(polygon - [x0, y0]).astype(np.int32), 1)
        yy, xx = np.nonzero(floor)
        total_pixels += int(len(xx))
        optical_z = depth[y0 + yy, x0 + xx].astype(float)
        valid = np.isfinite(optical_z) & (optical_z > 0)
        if len(xx) < 100 or valid.mean() < MIN_BASELINE_COVERAGE:
            _fail("baseline cell has insufficient valid depth.")
        camera_points = np.column_stack(((xx + x0 - camera["cx"]) / camera["fx"] * optical_z,
                                         (yy + y0 - camera["cy"]) / camera["fy"] * optical_z,
                                         optical_z))
        with np.errstate(over="ignore", invalid="ignore"):
            points = camera_points @ transform.T + translation
        valid &= np.isfinite(points).all(axis=1)
        table = valid & (np.abs(points[:, 2] - table_z) <= MAX_REFERENCE_OFFSET_MM)
        if table.mean() < MIN_BASELINE_COVERAGE:
            _fail("baseline depth does not independently match the nominal table.")
        # An eight-pixel hole/feature may be safety-relevant even with 99.5%
        # overall coverage. Match the pre-existing floor evidence criterion.
        rejected = np.zeros_like(floor)
        rejected[yy[~table], xx[~table]] = 1
        count, _, stats, _ = cv2.connectedComponentsWithStats(rejected, connectivity=8)
        if count > 1 and stats[1:, cv2.CC_STAT_AREA].max() >= 8:
            _fail("baseline contains a contiguous missing-depth or off-table feature.")
        sample = points[table]
        samples.append(sample)
        cell_samples.append(sample)
    samples = np.concatenate(samples)
    bins = np.floor(samples[:, :2] / XY_BIN_MM).astype(np.int64)
    _, inverse = np.unique(bins, axis=0, return_inverse=True)
    counts = np.bincount(inverse)
    balanced = np.column_stack([np.bincount(inverse, weights=samples[:, axis]) / counts
                                for axis in range(3)])
    if len(balanced) < MIN_FIT_BINS or np.any(np.ptp(balanced[:, :2], axis=0) < 20):
        _fail("baseline has insufficient spatial support for a plane.")
    center = np.median(balanced[:, :2], axis=0)
    design = np.column_stack((balanced[:, :2] - center, np.ones(len(balanced))))
    keep = np.ones(len(balanced), dtype=bool)
    for _ in range(8):
        fitted, _, rank, _ = np.linalg.lstsq(design[keep], balanced[keep, 2], rcond=None)
        if rank != 3:
            _fail("baseline plane fit is degenerate.")
        residual = balanced[:, 2] - design @ fitted
        median = float(np.median(residual[keep]))
        mad = float(np.median(np.abs(residual[keep] - median)))
        updated = np.abs(residual - median) <= max(1.0, 3 * 1.4826 * mad)
        if updated.sum() < MIN_FIT_BINS or updated.mean() < 0.8:
            _fail("too little consistent empty-table evidence for a reference.")
        if np.array_equal(updated, keep):
            break
        keep = updated
    fitted, _, _, _ = np.linalg.lstsq(design[keep], balanced[keep, 2], rcond=None)
    coefficients = [float(fitted[0]), float(fitted[1]), float(fitted[2] - center @ fitted[:2])]
    _check_plane(coefficients, identity)
    rms = float(np.sqrt(np.mean((balanced[keep, 2] - design[keep] @ fitted) ** 2)))
    max_cell_p99 = max(float(np.percentile(np.abs(sample[:, 2] -
                       (sample[:, :2] @ fitted[:2] + coefficients[2])), 99)) for sample in cell_samples)
    artifact = {"schema_version": SCHEMA_VERSION, "kind": "placement_table_reference", "units": "mm",
                "identity": identity, "source": source, "plane_z_mm": coefficients,
                "fit": {"sample_count": int(len(samples)), "bin_count": int(len(balanced)),
                        "inlier_bin_count": int(keep.sum()), "rms_mm": rms,
                        "max_cell_residual_p99_mm": max_cell_p99}}
    TableReference.from_dict(artifact, calibration=calibration, intrinsics=intrinsics,
                             locations=locations, cell_size=cell_size, table_z=table_z)
    return artifact
