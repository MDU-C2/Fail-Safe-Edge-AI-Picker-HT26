"""Offline differential fixtures for the native object/depth conversion.

Run with the development Python environment, not the production image::

    python lynx/native/object_depth_parity.py --harness /path/object-depth-harness
    python lynx/native/object_depth_parity.py --reference-only

Add --malformed to check rejection of invalid geometry, masks and depth files.

Harness argv: CONFIG_JSON DETECTIONS_JSON MASKS_BIN DEPTH_RAW [DEPTH_RAW ...].
Configuration contains calibration {scale,R,t,tag_offset}, intrinsics and names.
Detections/masks use the native worker's existing JSON/cropped uint8-mask format.
Depths are matching uint16 little-endian images in chronological order. One depth
omits temporal metadata; two or more record all available per-frame mask medians. stdout must be an object-array JSON.

The authoritative reference reconstructs full-frame masks and calls the current
Detector.describe and mark_cup_bases. Erosion must distinguish an actual image
border from the edge of an object ROI. These fixtures check that contract; they
do not establish whether an earlier production path violated it.

Optional --replay-config/--replay-response/--replay-depth adds one cached LNXR
response and saved aligned depth (.npy or raw uint16le); it never runs a model,
connects to a camera, sends robot commands or reads deployment credentials.
Temporary fixtures are deleted unless --output-dir supplies a new directory.
"""

import argparse
import copy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import cv2
import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "capture_and_detect"))
from picker_workflow import Calibration, Detector, KEEP_CLASSES, mark_cup_bases


NAMES = ["CUP", "CUP BASE", "CUP HANDLE", "TRAY"]
MAX_PIXELS = 1920 * 1080


@dataclass
class Fixture:
    name: str
    config: dict
    detections: dict
    masks: bytes
    depths: tuple
    checks: tuple = ()


def rectangle(shape, x0, y0, x1, y1):
    result = np.zeros(shape, dtype=bool)
    result[y0:y1, x0:x1] = True
    return result


def configuration(shape):
    height, width = shape
    return {"calibration": {"scale": 1.0, "R": [[1, 0, 0], [0, 1, 0], [0, 0, -1]],
                            "t": [500, 100, 850], "tag_offset": [-122, 25, 40]},
            "intrinsics": {"fx": 100, "fy": 110, "cx": (width - 1) / 2,
                           "cy": (height - 1) / 2, "width": width, "height": height},
            "names": list(NAMES)}


def fixture(name, entries, depths, *, config=None, checks=()):
    """entries are (class name, full-frame mask, optional raw box, score)."""
    shape = depths[0].shape
    height, width = shape
    config = copy.deepcopy(config or configuration(shape))
    detections, payload = [], bytearray()
    for label, mask, box, score in entries:
        if mask.shape != shape or mask.dtype != np.bool_:
            raise ValueError("Fixture masks must match the full depth image")
        ys, xs = np.nonzero(mask)
        if box is None:
            box = ([max(0, int(xs.min()) - 2), max(0, int(ys.min()) - 2),
                    min(width, int(xs.max()) + 3), min(height, int(ys.max()) + 3)]
                   if xs.size else [0, 0, 1, 1])
        x0, y0, x1, y1 = [math.ceil(value) for value in box]
        crop = mask[y0:y1, x0:x1]
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height) or crop.sum() != mask.sum():
            raise ValueError("Fixture box must enclose every mask pixel")
        raw = crop.astype(np.uint8).tobytes()
        detections.append({"class_id": config["names"].index(label), "score": score,
                           "box_xyxy": list(box), "mask_bbox": [x0, y0, x1 - x0, y1 - y0],
                           "mask_offset": len(payload), "mask_length": len(raw)})
        payload.extend(raw)
    return Fixture(name, config, {"width": width, "height": height,
                                 "detections": detections, "inference_ms": 0.0},
                   bytes(payload), tuple(np.array(depth, dtype="<u2", copy=True) for depth in depths),
                   tuple(checks))


def synthetic_fixtures():
    shape = (40, 48)
    base = rectangle(shape, 10, 8, 31, 29)
    depth = np.full(shape, 800, dtype=np.uint16)

    def single(name, mask=base, image=depth, *, label="CUP BASE", box=None, score=.9, checks=()):
        return fixture(name, [(label, mask, box, score)], [image], checks=checks)

    yield single("base_good", checks=((0, "z_mm", 800.0),))
    rim = depth.copy()
    rim[13:24, 15:26] = 700
    yield single("base_core_ignores_background_rim", image=rim, checks=((0, "z_mm", 700.0),))
    hole = depth.copy()
    hole[17:20, 19:22] = 0
    yield single("base_centroid_depth_hole", image=hole,
                 checks=((0, "z_mm", 800.0),))
    one_hole = depth.copy()
    one_hole[18, 20] = 0
    yield single("base_one_center_hole", image=one_hole,
                 checks=((0, "z_mm", 800.0),))
    for count in (5, 6):
        image = depth.copy()
        image[17:20, 19:22] = 0
        for index in range(count):
            image[17 + index // 3, 19 + index % 3] = 800
        yield single(f"base_center_{count}_of_nine", image=image,
                     checks=((0, "z_mm", 800.0),))
    sparse = depth.copy()
    sparse[13:24, 15:26] = 0
    sparse[17:20, 19:22] = 800
    yield single("base_sparse_core_good_center", image=sparse,
                 checks=((0, "z_mm", 800.0),))
    for name, mask in (("base_tiny", rectangle(shape, 10, 10, 12, 12)),
                       ("base_thin", rectangle(shape, 10, 10, 35, 11))):
        yield single(name, mask=mask, checks=((0, "z_mm", 800.0),))
    border = rectangle(shape, 0, 0, 5, 5)
    yield single("base_at_image_border", mask=border,
                 checks=((0, "z_mm", 800.0),))
    lobes = rectangle(shape, 3, 10, 13, 25) | rectangle(shape, 25, 10, 35, 25)
    yield single("base_disconnected_centroid_gap", mask=lobes,
                 checks=((0, "z_mm", 800.0),))
    ring = base.copy()
    ring[16:21, 18:23] = False
    yield single("base_mask_centroid_hole", mask=ring,
                 checks=((0, "z_mm", 800.0),))
    even_mask = rectangle(shape, 10, 10, 18, 18)
    even = depth.copy()
    even[:, 14:] = 801
    yield single("base_even_core_median", mask=even_mask, image=even,
                 checks=((0, "z_mm", 800.5),))
    eroded = np.full(shape, 1400, dtype=np.uint16)
    eroded[13:24, 15:26] = 700
    yield single("cup_erosion_uses_full_frame_zeros", image=eroded, label="CUP",
                 checks=((0, "z_mm", 700.0),))
    for name, mask in (("cup_erosion_at_image_edge", rectangle(shape, 0, 0, 15, 15)),
                       ("cup_erosion_full_image", np.ones(shape, dtype=bool))):
        image = np.full(shape, 1200, dtype=np.uint16)
        image[:10, :10] = 600
        yield single(name, mask=mask, image=image, label="CUP")
    handle = rectangle(shape, 10, 10, 12, 12)
    even = depth.copy()
    even[10:12, 11] = 802
    yield single("handle_thin_erosion_fallback_even_median", mask=handle, image=even,
                 label="CUP HANDLE", checks=((0, "z_mm", 801.0),))
    yield single("cup_no_valid_eroded_depth", image=np.zeros(shape, np.uint16), label="CUP",
                 checks=((0, "z_mm", None),))
    jagged = rectangle(shape, 11, 9, 16, 14)
    jagged[10:13, 12:15] = False
    jagged[9, 11] = False
    yield single("packed_mask_holes_and_padding_bits", mask=jagged, label="CUP HANDLE")
    rounding = configuration(shape)
    rounding["calibration"].update(scale=1.025, R=[[0, -1, 0], [1, 0, 0], [0, 0, -1]],
                                    t=[2.675, -1.15, 850.05])
    yield fixture("decimal_rounding_and_rotated_calibration",
                  [("CUP BASE", rectangle(shape, 12, 9, 20, 17), [1.15, 8.25, 20.65, 17.95], .5565)],
                  [depth], config=rounding,
                  checks=((0, "bbox_xyxy", [1.1, 8.2, 20.6, 17.9]),))
    # Raw xmax 14.95 excludes rounded centroid 15.0; rounding the box first would include it.
    cup = rectangle(shape, 3, 10, 10, 20)
    associated_base = rectangle(shape, 13, 13, 18, 18)
    high = np.full(shape, 600, dtype=np.uint16)
    yield fixture("association_uses_raw_boxes", [
        ("CUP", cup, [2.0, 8.0, 14.95, 22.0], .9),
        ("CUP BASE", associated_base, [11, 11, 20, 20], .9)], [high],
        checks=((1, "in_cup", False), (1, "cup_height", False), (1, "is_cup", False)))
    yield fixture("association_in_cup_and_filtered_detection_ids", [
        ("TRAY", np.ones(shape, bool), None, .95),
        ("CUP", np.zeros(shape, bool), None, .9),
        ("CUP", rectangle(shape, 5, 5, 36, 34), None, .9),
        ("CUP BASE", base, None, .9)], [high],
        checks=((0, "detection_id", 2), (1, "detection_id", 3), (1, "in_cup", True),
                (1, "cup_height", False), (1, "is_cup", True)))
    yield fixture("association_near_handle", [
        ("CUP BASE", rectangle(shape, 10, 10, 18, 18), [10, 10, 18, 18], .9),
        ("CUP HANDLE", rectangle(shape, 21, 12, 24, 15), [21, 12, 24, 15], .8)], [high],
        checks=((0, "near_handle", True), (0, "in_cup", False), (0, "cup_height", False)))
    yield fixture("association_cup_height_only", [("CUP BASE", base, None, .9)], [depth],
                  checks=((0, "in_cup", False), (0, "near_handle", False), (0, "cup_height", True)))
    for name, values, expected in (("temporal_span_twenty", (800, 810, 820), 810.0),
                                   ("temporal_span_twenty_one", (800, 810, 821), 810.3),
                                   ("temporal_all_invalid", (0, 0, 0), None),
                                   ("temporal_fractional_mean", (800, 800, 801), 800.3),
                                   ("temporal_available_samples", (800, 0, 820), 810.0)):
        samples = [np.full(shape, value, np.uint16) for value in values]
        yield fixture(name, [("CUP BASE", base, None, .9)], samples,
                      checks=((0, "z_mm", expected),))
    yield fixture("temporal_latest_center_hole", [("CUP BASE", base, None, .9)],
                  [depth, depth, hole], checks=((0, "z_mm", 800.0),))
    yield fixture("temporal_old_center_hole", [("CUP BASE", base, None, .9)],
                  [hole, depth, depth], checks=((0, "z_mm", 800.0),))
    yield fixture("temporal_nonbase_stable", [("CUP", base, None, .9)],
                  [depth, depth + np.uint16(10), depth + np.uint16(20)], checks=((0, "z_mm", 810.0),))
    yield fixture("temporal_nonbase_invalid_sample", [("CUP HANDLE", handle, None, .9)],
                  [depth, np.zeros(shape, np.uint16), depth], checks=((0, "z_mm", 800.0),))
    # 512*512 is exactly the retained raw-crop budget, independent of nonzero area.
    boundary_shape = (512, 512)
    boundary_depth = np.full(boundary_shape, 800, np.uint16)
    boundary_mask = np.ones(boundary_shape, bool)
    for label in KEEP_CLASSES:
        yield fixture("resource_crop_exact_limit_" + label.lower().replace(" ", "_"),
                      [(label, boundary_mask, None, .9)], [boundary_depth],
                      checks=((0, "mask_area_px", 262144), (0, "z_mm", 800.0)))
    # Oversized TRAY is ignored; a retained base after it must still be described.
    tray_shape = (481, 545)  # 262145 = per-crop limit + 1.
    tray_depth = np.full(tray_shape, 800, np.uint16)
    yield fixture("resource_oversized_tray_skipped", [
        ("TRAY", np.ones(tray_shape, bool), None, .9),
        ("CUP BASE", rectangle(tray_shape, 10, 10, 15, 15), None, .9)], [tray_depth],
        checks=((0, "detection_id", 1), (0, "z_mm", 800.0)))
    # Sparse masks keep the aggregate boundary test cheap while retaining large raw ROIs.
    sparse_boundary = rectangle(boundary_shape, 250, 250, 255, 255)
    yield fixture("resource_aggregate_exact_limit",
                  [(KEEP_CLASSES[index % 3], sparse_boundary, [0, 0, 512, 512], .9)
                   for index in range(32)], [boundary_depth],
                  checks=((31, "detection_id", 31),))


def reference_objects(case, calibration_path):
    """Use production Python routines, preserving full-frame geometry and order."""
    calibration_path.write_text(json.dumps(case.config["calibration"], allow_nan=False))
    detector = SimpleNamespace(calibration=Calibration(calibration_path), K=case.config["intrinsics"],
                               _depth_samples=case.depths if len(case.depths) > 1 else ())
    shape = (case.detections["height"], case.detections["width"])
    objects, boxes = [], []
    for index, item in enumerate(case.detections["detections"]):
        name = case.config["names"][item["class_id"]]
        if name not in KEEP_CLASSES:
            continue
        x, y, width, height = item["mask_bbox"]
        crop = np.frombuffer(case.masks, dtype=np.uint8, count=item["mask_length"],
                             offset=item["mask_offset"]).reshape(height, width)
        mask = np.zeros(shape, dtype=bool)
        mask[y:y + height, x:x + width] = crop != 0
        if not mask.any():
            continue
        obj = Detector.describe(detector, name, item["score"], mask, case.depths[-1])
        obj["detection_id"] = index
        obj["bbox_xyxy"] = [round(float(value), 1) for value in item["box_xyxy"]]
        objects.append(obj)
        boxes.append(item["box_xyxy"])
    mark_cup_bases(objects, boxes)
    for index, key, expected in case.checks:
        value = objects[index]
        for part in key.split("."):
            value = value[part]
        if value != expected:
            raise AssertionError(f"Fixture {case.name} misses intended branch: {key}={value!r}, expected {expected!r}")
    if len(case.depths) == 1 and any("z_frames_mm" in obj for obj in objects):
        raise AssertionError("Single-depth reference unexpectedly enabled temporal sampling")
    json.dumps(objects, allow_nan=False)
    return objects


def differences(expected, actual, path="$", *, limit=20):
    result = []

    def visit(want, got, location):
        if len(result) >= limit:
            return
        if isinstance(want, dict) and isinstance(got, dict):
            for key in sorted(want.keys() - got.keys()):
                result.append(f"{location}.{key}: missing")
            for key in sorted(got.keys() - want.keys()):
                result.append(f"{location}.{key}: unexpected")
            for key in want.keys() & got.keys():
                visit(want[key], got[key], f"{location}.{key}")
        elif isinstance(want, list) and isinstance(got, list):
            if len(want) != len(got):
                result.append(f"{location}: length {len(got)}, expected {len(want)}")
            for index, (left, right) in enumerate(zip(want, got)):
                visit(left, right, f"{location}[{index}]")
        elif type(want) in (int, float) and type(got) in (int, float):
            if not math.isfinite(got) or not math.isclose(want, got, rel_tol=0, abs_tol=1e-9):
                result.append(f"{location}: {got!r}, expected {want!r}")
        elif type(want) is not type(got) or want != got:
            result.append(f"{location}: {got!r}, expected {want!r}")

    visit(expected, actual, path)
    return result[:limit]


def write_fixture(case, directory):
    directory.mkdir()
    if not 1 <= len(case.depths) <= 9:
        raise ValueError("Fixtures require 1..9 aligned depths")
    height, width = case.detections["height"], case.detections["width"]
    if not 0 < width <= 1920 or not 0 < height <= 1080 or width * height > MAX_PIXELS:
        raise ValueError("Fixture image exceeds the native frame contract")
    config = directory / "config.json"
    detections = directory / "detections.json"
    masks = directory / "masks.bin"
    config.write_text(json.dumps(case.config, allow_nan=False))
    detections.write_text(json.dumps(case.detections, allow_nan=False))
    masks.write_bytes(case.masks)
    paths = []
    for index, depth in enumerate(case.depths):
        if depth.shape != (height, width) or depth.dtype != np.dtype("<u2"):
            raise ValueError("Fixture depth must match image shape and uint16le")
        path = directory / f"depth_{index:02d}.raw"
        path.write_bytes(depth.tobytes())
        paths.append(path)
    expected = reference_objects(case, directory / "reference_calibration.json")
    (directory / "expected.json").write_text(json.dumps(expected, indent=2, allow_nan=False))
    return [config, detections, masks, *paths], expected


def cached_fixture(config_path, response_path, depth_paths):
    config = json.loads(config_path.read_text())
    if response_path.stat().st_size > 65 * 1024 * 1024:
        raise ValueError("Cached response exceeds the bounded single-response contract")
    data, offset, frames = response_path.read_bytes(), 0, []
    while offset < len(data):
        if len(data) - offset < 20:
            raise ValueError("Truncated cached native header")
        magic, request_id, status, json_size, mask_size = struct.unpack_from("<4sIIII", data, offset)
        offset += 20
        if magic != b"LNXR" or status or not 0 < json_size <= 65536 or mask_size > 64 * 1024 * 1024:
            raise ValueError("Invalid cached native response")
        if offset + json_size + mask_size > len(data):
            raise ValueError("Truncated cached native payload")
        document = json.loads(data[offset:offset + json_size])
        offset += json_size
        masks = data[offset:offset + mask_size]
        offset += mask_size
        if request_id:
            frames.append((document, masks))
        elif document.get("ready") is not True or masks:
            raise ValueError("Invalid cached readiness response")
    if len(frames) != 1:
        raise ValueError("Replay needs exactly one inference response, optionally preceded by readiness")
    document, masks = frames[0]
    shape = document["height"], document["width"]
    if not 0 < shape[0] <= 1080 or not 0 < shape[1] <= 1920:
        raise ValueError("Invalid cached frame dimensions")
    depths = []
    for path in depth_paths:
        depth = (np.load(path, allow_pickle=False) if path.suffix == ".npy"
                 else np.fromfile(path, dtype="<u2").reshape(shape))
        if depth.dtype.kind != "u" or depth.dtype.itemsize != 2 or depth.shape != shape:
            raise ValueError("Saved depths must be matching uint16 images")
        depths.append(np.asarray(depth, dtype="<u2"))
    return Fixture("cached_native_response", config, document, masks, tuple(depths))


def run_cases(cases, directory, harness, timeout):
    results = []
    for case in cases:
        case_dir = directory / case.name
        try:
            paths, expected = write_fixture(case, case_dir)
            if harness is None:
                result = {"case": case.name, "status": "reference_only", "objects": len(expected)}
            else:
                run = subprocess.run([str(harness), *map(str, paths)], capture_output=True,
                                     timeout=timeout, check=False)
                (case_dir / "native.stdout").write_bytes(run.stdout)
                (case_dir / "native.stderr").write_bytes(run.stderr)
                if run.returncode:
                    raise RuntimeError(f"Harness exit {run.returncode}: {run.stderr.decode(errors='replace')[-2000:]}")
                actual = json.loads(run.stdout)
                diff = differences(expected, actual)
                result = {"case": case.name, "status": "failed" if diff else "passed",
                          "objects": len(expected), "differences": diff}
        except (AssertionError, ValueError, TypeError, KeyError, IndexError, OSError, RuntimeError,
                subprocess.TimeoutExpired) as error:
            result = {"case": case.name, "status": "failed", "error": str(error)}
        results.append(result)
        print(json.dumps(result, allow_nan=False), flush=True)
    summary = {"cases": len(results), "passed": sum(r["status"] == "passed" for r in results),
               "reference_only": sum(r["status"] == "reference_only" for r in results),
               "failed": sum(r["status"] == "failed" for r in results),
               "reference": "current full-frame Detector.describe + mark_cup_bases",
               "temporal_contract": "one depth omits temporal metadata; multiple depths include all, newest last",
               "results": results}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}), flush=True)
    return bool(summary["failed"])


def run_rejections(directory, harness, timeout):
    """Invalid fixtures must exit normally with failure and no object output."""
    def edit_json(index, update):
        def mutate(paths):
            value = json.loads(paths[index].read_text())
            update(value)
            # NaN is intentional in one malformed-JSON fixture.
            paths[index].write_text(json.dumps(value, allow_nan=True))
        return mutate

    def edit_bytes(index, update):
        return lambda paths: paths[index].write_bytes(update(paths[index].read_bytes()))

    def repeated_offsets(paths):
        doc = json.loads(paths[1].read_text())
        doc["detections"].append(copy.deepcopy(doc["detections"][0]))
        paths[1].write_text(json.dumps(doc))
        paths[2].write_bytes(paths[2].read_bytes() * 2)

    def excessive_count(paths):
        doc = json.loads(paths[1].read_text())
        item, raw = doc["detections"][0], paths[2].read_bytes()
        doc["detections"] = [dict(item, mask_offset=index * len(raw)) for index in range(65)]
        paths[1].write_text(json.dumps(doc))
        paths[2].write_bytes(raw * 65)

    def crop_budget(shape, labels):
        def mutate(paths):
            height, width = shape
            # Only 25 foreground pixels: reject based on raw crop area, not tight bounds.
            mask = rectangle(shape, 10, 10, 15, 15).astype(np.uint8).tobytes()
            items = [{"class_id": NAMES.index(label), "score": .9,
                      "box_xyxy": [0, 0, width, height], "mask_bbox": [0, 0, width, height],
                      "mask_offset": index * len(mask), "mask_length": len(mask)}
                     for index, label in enumerate(labels)]
            paths[0].write_text(json.dumps(configuration(shape)))
            paths[1].write_text(json.dumps({"width": width, "height": height,
                                           "detections": items, "inference_ms": 0.0}))
            paths[2].write_bytes(mask * len(labels))
            paths[3].write_bytes(np.full(shape, 800, dtype="<u2").tobytes())
        return mutate

    cases = [
        ("zero_focal", edit_json(0, lambda value: value["intrinsics"].update(fx=0))),
        ("negative_focal", edit_json(0, lambda value: value["intrinsics"].update(fy=-1))),
        ("boolean_focal", edit_json(0, lambda value: value["intrinsics"].update(fx=True))),
        ("nan_focal_json", edit_json(0, lambda value: value["intrinsics"].update(fx=float("nan")))),
        ("intrinsics_shape_mismatch", edit_json(0, lambda value: value["intrinsics"].update(width=47))),
        ("zero_calibration_scale", edit_json(0, lambda value: value["calibration"].update(scale=0))),
        ("bad_rotation_shape", edit_json(0, lambda value: value["calibration"].update(R=[[1, 0], [0, 1]]))),
        ("bad_tag_offset", edit_json(0, lambda value: value["calibration"].update(tag_offset=[0, 0]))),
        ("duplicate_class_names", edit_json(0, lambda value: value.update(names=["CUP", "CUP BASE", "CUP BASE"]))),
        ("nonbinary_mask", edit_bytes(2, lambda raw: bytes([2]) + raw[1:])),
        ("truncated_mask", edit_bytes(2, lambda raw: raw[:-1])),
        ("unclaimed_mask_byte", edit_bytes(2, lambda raw: raw + b"\x00")),
        ("repeated_mask_offset", repeated_offsets),
        ("wrong_mask_length", edit_json(1, lambda value: value["detections"][0].update(mask_length=1))),
        ("truncated_depth", edit_bytes(3, lambda raw: raw[:-2])),
        ("oversized_depth", edit_bytes(3, lambda raw: raw + b"\x00\x00")),
        ("no_depth", lambda paths: paths.__delitem__(slice(3, None))),
        ("ten_depths", lambda paths: paths.extend([paths[-1]] * 9)),
        ("sixty_five_detections", excessive_count),
        ("oversized_width", edit_json(1, lambda value: value.update(width=1921))),
        ("noninteger_width", edit_json(1, lambda value: value.update(width=48.0))),
        ("negative_class_id", edit_json(1, lambda value: value["detections"][0].update(class_id=-1))),
        ("score_above_one", edit_json(1, lambda value: value["detections"][0].update(score=1.1))),
        ("roi_exceeds_frame", edit_json(1, lambda value: value["detections"][0].update(mask_bbox=[0, 0, 49, 40]))),
    ]
    cases.extend(("crop_limit_plus_one_" + label.lower().replace(" ", "_"),
                  crop_budget((481, 545), [label])) for label in KEEP_CLASSES)
    cases.append(("aggregate_retained_crop_limit",
                  crop_budget((512, 512), [KEEP_CLASSES[index % 3] for index in range(33)])))
    baseline = next(synthetic_fixtures())
    results = []
    for name, mutate in cases:
        case_dir = directory / ("malformed_" + name)
        try:
            paths, _ = write_fixture(baseline, case_dir)
            mutate(paths)
            run = subprocess.run([str(harness), *map(str, paths)], capture_output=True,
                                 timeout=timeout, check=False)
            (case_dir / "native.stdout").write_bytes(run.stdout)
            (case_dir / "native.stderr").write_bytes(run.stderr)
            rejected = run.returncode > 0 and not run.stdout.strip()
            result = {"case": name, "status": "passed" if rejected else "failed",
                      "returncode": run.returncode,
                      "error": run.stderr.decode(errors="replace")[-1000:].strip()}
        except (ValueError, TypeError, KeyError, IndexError, OSError, RuntimeError,
                subprocess.TimeoutExpired) as error:
            result = {"case": name, "status": "failed", "error": str(error)}
        results.append(result)
        print(json.dumps({"malformed": True, **result}), flush=True)
    summary = {"malformed_cases": len(results), "passed": sum(r["status"] == "passed" for r in results),
               "failed": sum(r["status"] == "failed" for r in results), "results": results}
    (directory / "malformed_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}), flush=True)
    return bool(summary["failed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--harness", type=Path)
    mode.add_argument("--reference-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="Keep fixtures/results in a new directory")
    parser.add_argument("--case", action="append", default=[], help="Run only the named synthetic case; repeatable")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--malformed", action="store_true", help="Also require invalid inputs to fail without output")
    parser.add_argument("--replay-config", type=Path)
    parser.add_argument("--replay-response", type=Path)
    parser.add_argument("--replay-depth", type=Path, nargs="+")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("Use a timeout in (0,120] seconds")
    if args.malformed and args.reference_only:
        parser.error("Malformed rejection checks require --harness")
    if any((args.replay_config, args.replay_response, args.replay_depth)) and not all(
            (args.replay_config, args.replay_response, args.replay_depth)):
        parser.error("Replay needs --replay-config, --replay-response and --replay-depth together")
    cv2.setNumThreads(1)
    cases = list(synthetic_fixtures())
    if args.case:
        unknown = set(args.case) - {case.name for case in cases}
        if unknown:
            parser.error("Unknown fixture: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case.name in args.case]
    if args.replay_config:
        cases.append(cached_fixture(args.replay_config, args.replay_response, args.replay_depth))
    harness = args.harness.resolve() if args.harness else None
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        failed = run_cases(cases, args.output_dir.resolve(), harness, args.timeout)
        if args.malformed:
            failed = run_rejections(args.output_dir.resolve(), harness, args.timeout) or failed
    else:
        with tempfile.TemporaryDirectory(prefix="lynx-object-depth-") as temporary:
            failed = run_cases(cases, Path(temporary), harness, args.timeout)
            if args.malformed:
                failed = run_rejections(Path(temporary), harness, args.timeout) or failed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
