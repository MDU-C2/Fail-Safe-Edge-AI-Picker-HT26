"""Table reference identity and quality contract, without image dependencies."""
import json
import math
import re
from cup_inventory import SelectionError, finite_number
from calibration_depth import depth_parameters

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
        rotation = [[float(v) for v in row] for row in calibration.R]
        translation = [float(v) for v in calibration.t]
        offset = [float(v) for v in calibration.offset]
    except (AttributeError, TypeError, ValueError, OverflowError):
        _fail("invalid calibration transform.")
    if (len(rotation) != 3 or any(len(row) != 3 for row in rotation)
            or len(translation) != 3 or len(offset) != 3
            or not all(math.isfinite(v) for row in rotation for v in row)
            or not all(math.isfinite(v) for v in translation + offset)):
        _fail("invalid calibration transform.")
    transform = [[float(scale) * v for v in row] for row in rotation]
    a, b, c = transform
    determinant = (a[0] * (b[1]*c[2] - b[2]*c[1])
                   - a[1] * (b[0]*c[2] - b[2]*c[0])
                   + a[2] * (b[0]*c[1] - b[1]*c[0]))
    if (not all(math.isfinite(v) for row in transform for v in row)
            or not all(math.isfinite(v+o) for v, o in zip(translation, offset))
            or not math.isfinite(determinant) or abs(determinant) < 1e-9):
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
    identity = {"calibration_sha256": digest.lower(), "scale": float(scale),
            "rotation": rotation, "translation": translation,
            "tag_offset_mm": offset, "intrinsics": camera,
            "locations": places, "cell_size_mm": float(cell_size), "table_z_mm": float(table_z)}
    try:
        depth_scale, depth_offset = depth_parameters(calibration)
    except ValueError as error:
        _fail(str(error))
    # Keep legacy reference identities unchanged for the identity mapping.
    # Active correction must be recorded separately from the calibration hash:
    # a reference made by software that ignored it must never validate here.
    if (depth_scale, depth_offset) != (1.0, 0.0):
        identity.update(depth_scale=depth_scale, depth_offset=depth_offset)
    return identity


def _check_plane(coefficients, identity):
    if (not isinstance(coefficients, (list, tuple)) or len(coefficients) != 3
            or not all(finite_number(value) for value in coefficients)):
        _fail("plane coefficients must be three finite numbers.")
    slope = math.degrees(math.atan(math.hypot(*coefficients[:2])))
    if slope > MAX_SLOPE_DEGREES:
        _fail("fitted table slope exceeds the allowed bound.")
    half = identity["cell_size_mm"] / 2
    for place in identity["locations"]:
        for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half)):
            predicted = ((place["x"] + dx) * coefficients[0]
                         + (place["y"] + dy) * coefficients[1] + coefficients[2])
            if not math.isfinite(predicted) or abs(predicted - identity["table_z_mm"]) > MAX_REFERENCE_OFFSET_MM:
                _fail("fitted table lies outside the allowed offset from the calibrated table.")


def _identity_mismatch(saved, current):
    if not isinstance(saved, dict):
        _fail("identity is missing or is not an object; rebuild the reference from an empty table.")
    changed = [key for key in current if key in saved and saved[key] != current[key]]
    missing = [key for key in current if key not in saved]
    unexpected = [str(key) for key in saved if key not in current]
    if (changed == ["calibration_sha256"] and not missing and not unexpected
            and isinstance(saved["calibration_sha256"], str)
            and _SHA256.fullmatch(saved["calibration_sha256"])):
        _fail("only calibration_sha256 differs; calibration geometry, camera and grid still match. "
              "The calibration file contents may have changed, including LF/CRLF line endings. "
              "Run `python repair_reference_line_endings.py` to check for a line-ending-only repair. "
              "Otherwise rebuild the reference from an empty table; this reference remains blocked.")
    details = []
    for label, fields in (("changed", changed), ("missing", missing), ("unexpected", unexpected)):
        if fields:
            details.append(label + ": " + ", ".join(fields))
    _fail("identity mismatch (" + "; ".join(details)
          + "); rebuild the reference from an empty table.")


def validate_reference(value, *, calibration, intrinsics, locations, cell_size, table_z=0.0):
    identity = _identity(calibration, intrinsics, locations, cell_size, table_z)
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or type(value.get("schema_version")) is not int
            or value.get("kind") != "placement_table_reference" or value.get("units") != "mm"):
        _fail("unsupported artifact format.")
    if value.get("identity") != identity:
        _identity_mismatch(value.get("identity"), identity)
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
    return identity
