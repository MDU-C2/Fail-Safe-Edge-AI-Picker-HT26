"""Offline Python/native placement differential checks; never accesses a gateway.

Build placement_replay.cpp with LYNX_PLACEMENT_REPLAY_MAIN, then supply that
executable with --runner. Synthetic cases reuse the project's existing tests;
--archive additionally evaluates saved detections with an explicitly supplied
calibration and frozen reference. Capture and placement state are read only.
"""
import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "capture_and_detect"))
import cv2
import numpy as np
from picker_workflow import Calibration
from detection_masks import encode_mask
import placement_vision
from placement_vision import PlacementVision


def differences(expected, actual, path=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            return [path + ": keys " + str(set(expected) ^ set(actual))]
        return sum((differences(value, actual[key], path + "/" + str(key))
                    for key, value in expected.items()), [])
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [path + ": array length"]
        return sum((differences(a, b, path + "/" + str(i))
                    for i, (a, b) in enumerate(zip(expected, actual))), [])
    if isinstance(expected, (float, int)) and not isinstance(expected, bool) and isinstance(actual, (float, int)):
        if abs(expected - actual) <= 1e-6:
            return []
    if expected == actual and type(expected) is type(actual):
        return []
    return [f"{path}: {expected!r} != {actual!r}"]


class Replay:
    def __init__(self, runner, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="lynx-placement-parity-")
        self.depth_path = Path(self.temporary.name) / "depth.f64"
        self.process = subprocess.Popen([str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.records = []
        self.runner_sha256 = hashlib.sha256(Path(runner).read_bytes()).hexdigest()
        self.sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in Path(__file__).parent.glob("placement*.*")
                        if p.suffix in (".hpp", ".cpp") and "parity" not in p.name and "replay" not in p.name}

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=10)
        self.process.stdout.close()
        self.process.stderr.close()
        self.temporary.cleanup()

    def assess(self, vision, depth, objects, locations, expected, label, *, expected_error=None):
        dtype = str(depth.dtype) if depth.dtype in (np.uint16, np.float32, np.float64) else "float64"
        np.asarray(depth, dtype=np.dtype(dtype).newbyteorder("<")).tofile(self.depth_path)
        cal = vision.calibration
        config = {"calibration": {"scale": cal.scale, "R": np.asarray(cal.R).tolist(),
                                  "t": np.asarray(cal.t).tolist(), "tag_offset": list(cal.offset)},
                  "intrinsics": vision.K, "grid_snapshot": {"locations": locations},
                  "cell_size_mm": vision.cell_size, "table_z_mm": vision.table_z,
                  "table_reference": vision.table_reference}
        request = {"width": depth.shape[1], "height": depth.shape[0],
                   "depth_path": str(self.depth_path), "depth_dtype": dtype,
                   "objects": list(objects), "config": config}
        started = time.monotonic()
        self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise RuntimeError("Native replay exited: " + self.process.stderr.read()[-2000:])
        result = json.loads(response)
        elapsed = time.monotonic() - started
        actual = result.get("slots")
        if expected_error is not None:
            diff = [] if expected_error in result.get("error", "") else ["Expected rejection: " + expected_error]
        else:
            diff = differences(expected, actual) if actual is not None else [result.get("error", "Missing slots")]
        self.records.append({"case": label, "passed": not diff, "native_seconds": elapsed,
                             "native_peak_rss_mib": result.get("peak_rss_mib"),
                             "differences": diff, "states": {str(s["id"]): s["state"] for s in actual or []}})
        if diff:
            folder = self.output / ("failure-" + str(len(self.records)))
            folder.mkdir(exist_ok=True)
            frozen = folder / "depth.f64"
            frozen.write_bytes(self.depth_path.read_bytes())
            request["depth_path"] = str(frozen)
            (folder / "request.json").write_text(json.dumps(request, indent=2, allow_nan=False))
            (folder / "expected.json").write_text(json.dumps(expected, indent=2, allow_nan=False))
            (folder / "actual.json").write_text(json.dumps(result, indent=2, allow_nan=False))
            raise AssertionError("Native placement differs: " + "; ".join(diff[:12]))
        return actual


def synthetic(replay):
    modules = ["test_placement_vision", "test_placement_cups", "test_placement_depth_regions",
               "test_placement_cup_uncertainty_integration", "test_placement_depth_group",
               "test_placement_depth_shared", "test_placement_depth_split", "test_placement_table_reference"]
    originals = {}
    for name in ("placement_vision", "placement_cups", "placement_depth_regions", "placement_depth_group",
                 "placement_cup_uncertainty", "placement_depth_shared", "placement_depth_split"):
        module = importlib.import_module(name)
        originals[module] = {key: value for key, value in vars(module).items()
                             if key != "PlacementVision" and (isinstance(value, types.FunctionType)
                                 or (key.isupper() and isinstance(value, (int, float))))}
    original_assess = PlacementVision.assess
    def compare(vision, depth, objects, locations):
        expected = original_assess(vision, depth, objects, locations)
        # Tests that replace a Python policy deliberately are not references for
        # the production policy; their original Python assertions still run.
        altered = any(getattr(module, key) is not value
                      for module, values in originals.items() for key, value in values.items())
        if not altered:
            replay.assess(vision, depth, objects, locations, expected,
                          "synthetic-" + str(len(replay.records) + 1))
        return expected
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in modules)
    with patch.object(PlacementVision, "assess", compare):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    calibration = types.SimpleNamespace(scale=1., R=np.diag([1., -1., -1.]),
                                        t=np.array([0., 0., 600.]), offset=(0., 0., 0.))
    K = {"width": 640, "height": 480, "fx": 600., "fy": 600., "cx": 320., "cy": 240.}
    depth = np.full((480, 640), 600, np.uint16)
    locations = [{"id": 1, "x": 0., "y": 0., "z": 230.}]
    for table_z in (-2.675, -1.005, 2.675, 1.005):
        vision = PlacementVision(calibration, K, table_z=table_z)
        replay.assess(vision, depth, [], locations, original_assess(vision, depth, [], locations),
                      "scalar-decimal-rounding-" + str(table_z))
    vision = PlacementVision(calibration, K)
    oversized = {"class": "CUP", "confidence": .9, "bbox_xyxy": [0, 0, 640, 480],
                 "segmentation": encode_mask(np.ones(depth.shape, bool))}
    replay.assess(vision, depth, [oversized], locations, None, "oversized-mask-fails-closed",
                  expected_error="resource limit")
    return result.wasSuccessful()


def archive(replay, directory, calibration_path, reference_path, pattern):
    calibration = Calibration(calibration_path)
    reference = json.loads(reference_path.read_text())
    locations = [dict(location, occupied=False, reserved=False) for location in reference["identity"]["locations"]]
    successful = True
    for path in sorted(directory.glob("detections_" + pattern + ".json")):
        detection = json.loads(path.read_text())
        if detection.get("calibration_sha256") != calibration.digest:
            continue
        capture = path.name.removeprefix("detections_").removesuffix(".json")
        depth_path = directory / ("depth_" + capture + ".png")
        if not depth_path.exists():
            continue
        depth = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED)
        vision = PlacementVision(calibration, detection["intrinsics"],
                                 cell_size=reference["identity"]["cell_size_mm"], table_reference=reference)
        expected = vision.assess(depth, detection["objects"], locations)
        try:
            replay.assess(vision, depth, detection["objects"], locations, expected, capture)
        except AssertionError:
            successful = False
        if len(replay.records) % 10 == 0:
            print(json.dumps({"completed": len(replay.records), "latest": capture,
                              "failures": sum(not row["passed"] for row in replay.records)}), flush=True)
    return successful


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--pattern", default="*")
    args = parser.parse_args()
    if not args.synthetic and not args.archive:
        parser.error("Select --synthetic and/or --archive")
    if args.archive and (not args.calibration or not args.reference):
        parser.error("Archive replay requires explicit --calibration and --reference")
    replay = Replay(args.runner, args.output)
    ok = False
    try:
        ok = synthetic(replay) if args.synthetic else True
        if args.archive:
            ok = archive(replay, args.archive, args.calibration, args.reference, args.pattern) and ok
    finally:
        replay.close()
        records = replay.records
        report = {"passed": bool(ok and records and all(record["passed"] for record in records)), "assessments": len(records),
                  "median_native_seconds": statistics.median(row["native_seconds"] for row in records) if records else None,
                  "native_peak_rss_mib": max((row.get("native_peak_rss_mib") or 0) for row in records) if records else None,
                  "runner_sha256": replay.runner_sha256, "sources_at_start": replay.sources,
                  "records": records}
        (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({key: report[key] for key in ("passed", "assessments", "median_native_seconds")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
