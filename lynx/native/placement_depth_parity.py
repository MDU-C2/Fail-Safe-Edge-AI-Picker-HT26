#!/usr/bin/env python3
"""Compare native depth ownership and uncertainty with existing offline tests.

Build placement_depth_parity.cpp with LYNX_PLACEMENT_DEPTH_PARITY_MAIN, linking
placement_depth.cpp, placement_common.cpp, and OpenCV core/imgproc. Then run:

    python lynx/native/placement_depth_parity.py --runner /path/to/executable

Every representable helper invocation in the existing tests is replayed. The
comparison includes every owner pixel, shared-owner pixel set, assignment field,
and uncertainty audit field. Every region case runs both immutable and consuming
ownership modes, and verifies the default input remains unchanged. Python-only invalid types and monkeypatched helper
implementations are reported as skipped; their original Python tests still run.
No camera, gateway, model, or robot connection is used.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "capture_and_detect"))

import placement_cup_uncertainty as uncertainty
import placement_depth_regions as regions
import test_placement_cup_uncertainty as uncertainty_tests
import test_placement_depth_regions as region_tests
import test_placement_depth_split as split_tests


def shared_json(shared):
    return [{"owners": sorted(owners), "pixels": np.column_stack(coordinates).tolist()}
            for owners, coordinates in shared]


def compare(expected, actual, path=""):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise AssertionError(f"{path}: different object fields")
        for key, value in expected.items():
            compare(value, actual[key], f"{path}/{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise AssertionError(f"{path}: different list lengths")
        for index, (first, second) in enumerate(zip(expected, actual)):
            compare(first, second, f"{path}/{index}")
    elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
        if not np.isclose(expected, actual, rtol=1e-12, atol=1e-12, equal_nan=True):
            raise AssertionError(f"{path}: {expected!r} != {actual!r}")
    elif expected != actual:
        raise AssertionError(f"{path}: {expected!r} != {actual!r}")


class Parity:
    def __init__(self, runner, directory):
        self.directory = Path(directory)
        self.process = subprocess.Popen([str(runner)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)
        self.counts = {"regions": 0, "regions_consumed": 0, "uncertainty": 0}
        self.skipped = []
        self.failures = []

    def wrap(self, function, mode):
        def wrapped(*args, **kwargs):
            expected = function(*args, **kwargs)
            data = dict(inspect.signature(function).bind(*args, **kwargs).arguments)
            test = next((item.function for item in inspect.stack()
                         if item.function.startswith("test_")), "unknown")
            try:
                depth = data.pop("depth")
                owners = data.pop("owners", None)
                if (not isinstance(depth, np.ndarray) or depth.ndim != 2
                        or not 0 < depth.size <= 8388608 or np.iscomplexobj(depth)):
                    raise ValueError("unsupported C++ matrix shape/type")
                if mode == "regions" and (not isinstance(owners, np.ndarray)
                        or owners.shape != depth.shape
                        or not np.issubdtype(owners.dtype, np.signedinteger)):
                    raise ValueError("unsupported C++ ownership matrix")
                for key, shape in (("transform", (3, 3)), ("translation", (3,))):
                    value = np.asarray(data[key], dtype=float)
                    if value.shape != shape or not np.isfinite(value).all():
                        raise ValueError("unsupported C++ geometry shape or JSON value")
                    data[key] = value.tolist()
                for key in ("cell_size", "table_z", "check_height", "radius_mm", "tail_radius_mm"):
                    if isinstance(data.get(key), bool):
                        raise ValueError("boolean is not a C++ double parameter")
                if (regions.MAX_REGION_PIXELS != 131072 or regions.MAX_FRAME_PIXELS != 8388608
                        or uncertainty.MAX_WORK_PIXELS != 33554432
                        or "unittest.mock" in type(regions.shared_boundaries).__module__):
                    raise ValueError("Python implementation is monkeypatched")
                json.dumps(data, allow_nan=False)
            except (ValueError, TypeError, KeyError) as error:
                self.skipped.append({"test": test, "reason": str(error)})
                return expected

            dtype = depth.dtype.name if depth.dtype.name in ("float32", "uint16") else "float64"
            data.update(mode=mode, height=depth.shape[0], width=depth.shape[1], depth_dtype=dtype,
                        depth_path=str(self.directory / "depth.bin"),
                        owners_path=str(self.directory / "owners.bin"),
                        output_path=str(self.directory / "output.bin"))
            depth.astype(dtype).tofile(data["depth_path"])
            if owners is not None:
                owners.astype(np.int32).tofile(data["owners_path"])
            for consume in ((False, True) if mode == "regions" else (False,)):
                data["consume_owners"] = consume
                self.process.stdin.write(json.dumps(data) + "\n")
                self.process.stdin.flush()
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("Native helper replay exited before returning a result")
                actual = json.loads(line)
                mode_label = "regions_consumed" if consume else mode
                self.counts[mode_label] += 1
                try:
                    if mode == "regions":
                        unchanged = actual.pop("input_unchanged")
                        aliases_input = actual.pop("aliases_input")
                        if not consume and not unchanged:
                            raise AssertionError("Default mode mutated the input ownership matrix")
                        compact = np.min(owners) >= -1 and np.max(owners) <= 64
                        if consume and compact and not aliases_input:
                            raise AssertionError("Compact ownership mode did not reuse the input buffer")
                        if consume and not compact and not unchanged:
                            raise AssertionError("Out-of-range ownership was narrowed or mutated")
                        native_owners = np.fromfile(data["output_path"], dtype=np.int32).reshape(depth.shape)
                        np.testing.assert_array_equal(expected[0], native_owners)
                        compare({"assignments": expected[1], "shared": shared_json(expected[2])}, actual)
                    else:
                        compare({"shared": shared_json(expected[0]), "evidence": expected[1]}, actual)
                except (AssertionError, TypeError, ValueError, KeyError) as error:
                    self.failures.append({"test": test, "mode": mode_label, "error": str(error)})
            return expected
        return wrapped

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lynx-depth-parity-") as directory:
        parity = Parity(args.runner.resolve(), directory)
        previous = (region_tests.assign_depth_regions, split_tests.assign_depth_regions,
                    uncertainty_tests.cup_uncertainty_regions)
        try:
            region_tests.assign_depth_regions = parity.wrap(regions.assign_depth_regions, "regions")
            split_tests.assign_depth_regions = parity.wrap(regions.assign_depth_regions, "regions")
            uncertainty_tests.cup_uncertainty_regions = parity.wrap(
                uncertainty.cup_uncertainty_regions, "uncertainty")
            loader = unittest.defaultTestLoader
            suite = unittest.TestSuite((loader.loadTestsFromModule(region_tests),
                loader.loadTestsFromModule(uncertainty_tests),
                loader.loadTestsFromTestCase(split_tests.DepthSplitIntegrationTests)))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            # Cover the compact representation's exact endpoints and fallback.
            # These ownership tags sit on real candidate pixels, so wrapping a
            # wider value would change observable conflicts or shared support.
            fixture = region_tests.PlacementDepthRegionTests()
            native_regions = parity.wrap(regions.assign_depth_regions, "regions")
            for sentinel in (-2147483648, -2, -1, 64, 65, 2147483647):
                fixture.setUp()
                mask, halo, objects = fixture.broad_cup()
                owners, _, assignments = fixture.assign(objects)
                ys, xs = np.nonzero(mask)
                owners[ys[0], xs[0]] = sentinel
                native_regions(fixture.depth, objects, fixture.locations, owners, assignments,
                    transform=fixture.vision.transform, translation=fixture.vision.translation,
                    intrinsics=fixture.K, cell_size=fixture.vision.cell_size,
                    table_z=fixture.vision.table_z)
        finally:
            (region_tests.assign_depth_regions, split_tests.assign_depth_regions,
             uncertainty_tests.cup_uncertainty_regions) = previous
            parity.close()
        print(json.dumps({"native_invocations": parity.counts, "skipped": parity.skipped,
                          "parity_failures": parity.failures}, indent=2))
        return int(bool(parity.failures) or not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
