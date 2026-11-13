"""Grid-owned cell geometry is checked with synthetic frames and temporary files."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from capture_and_detect import CaptureService
from cup_inventory import SelectionError
from placing_grid import PlacingGrid
from placement_vision import PlacementVision
from test_cup_selection import detection


class GridCellSizeTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "isolated_grid.json"
        self.document = {"description": "offline fixture", "locations": [
            {"id": 2, "x": 400, "y": 0, "z": 230, "occupied": False}]}
        self.write()
        self.grid = PlacingGrid(self.path)

    def write(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def service(self, **options):
        intrinsics = {"width": 640, "height": 480, "fx": 600., "fy": 600.,
                      "cx": 320., "cy": 240.}
        calibration = SimpleNamespace(
            scale=1., R=np.diag([1., -1., -1.]), t=np.array([515., 0., 960.]),
            offset=(-115., 0., 40.), digest="offline", ensure_unchanged=Mock())
        self.sequence = 0

        def capture():
            self.sequence += 1
            return (np.zeros((480, 640, 3), np.uint8), np.full((480, 640), 1000, np.uint16),
                    {"sequence": self.sequence, "observation_started_monotonic": 100.})

        camera = SimpleNamespace(api="http://offline.invalid/api/v1",
                                 intrinsics=lambda: copy.deepcopy(intrinsics), capture=capture)
        detector = SimpleNamespace(calibration=calibration, K=intrinsics, conf=.5,
                                   analyse=lambda rgb, depth: ([], rgb.copy()))
        store = SimpleNamespace(folder=self.root, save_capture=Mock())
        return CaptureService(camera, detector, store, "offline.pt", placing_grid=self.grid, **options)

    def assert_floor_width(self, result, size):
        self.assertEqual(result["placement_observation"]["cell_size_mm"], size)
        polygon = np.array(result["placement_observation"]["slots"][0]["polygon_px"])
        self.assertAlmostEqual(float(np.ptp(polygon[:, 0])), size * .6, places=2)

    def test_metadata_is_optional_and_valid_sizes_round_trip_without_mutation(self):
        self.assertNotIn("cell_size_mm", self.grid.snapshot())
        for size in (20, 90, 90.5, 200):
            with self.subTest(size=size):
                self.document["cell_size_mm"] = size
                self.write()
                before = self.path.read_bytes()
                snapshot = self.grid.snapshot()
                self.assertEqual(snapshot["cell_size_mm"], size)
                snapshot["cell_size_mm"] = 40
                self.assertEqual(self.grid.snapshot()["cell_size_mm"], size)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_optional_size_cannot_be_read_as_a_valid_grid(self):
        for size in (None, True, False, "90", [], {}, 0, -90, 19.99, 200.01,
                     float("nan"), float("inf"), float("-inf")):
            with self.subTest(size=size):
                self.document["cell_size_mm"] = size
                self.write()
                before = self.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.grid.snapshot()
                self.assertEqual(self.path.read_bytes(), before)

    def test_grid90_overrides_cli100_for_all_scan_frames_and_overlay(self):
        self.document["cell_size_mm"] = 90
        self.write()
        before = self.path.read_bytes()
        service = self.service(grid_cell_size=100)
        sizes = []
        assess = PlacementVision.assess

        def observe(vision, *args, **kwargs):
            sizes.append(vision.cell_size)
            return assess(vision, *args, **kwargs)

        with patch.object(PlacementVision, "assess", observe):
            result, _ = service.capture("scan")
        self.assertEqual(sizes, [90, 90, 90])
        self.assertEqual(result["placement_observation"]["grid_snapshot"]["cell_size_mm"], 90)
        self.assert_floor_width(result, 90)
        self.assertEqual(self.path.read_bytes(), before)

    def test_exclusion_uses_grid90_instead_of_cli100(self):
        self.document["cell_size_mm"] = 90
        self.write()
        service = self.service(grid_cell_size=100)
        # Physical X447 lies outside the 90 mm cell's edge445, but inside
        # the old 100 mm cell's edge450. Both body/base describe the same cup.
        objects = []
        for kind in ("CUP", "CUP BASE"):
            obj = detection(kind, 348, x=562)
            obj.update(robot_y_mm=0, y_px=240)
            obj["bbox_xyxy"][1::2] = [220, 260]
            objects.append(obj)
        service.detector.analyse = lambda rgb, depth: (copy.deepcopy(objects), rgb.copy())
        result, _ = service.capture("verification")
        cup = result["cups"][0]
        self.assertTrue(cup["selectable"], cup.get("blocked_reason"))
        self.assertNotIn("placement_location_ids", cup)
        self.assert_floor_width(result, 90)

    def test_each_capture_reads_fresh_metadata_and_removal_restores_cli_fallback(self):
        self.document["cell_size_mm"] = 90
        self.write()
        service = self.service(grid_cell_size=100)
        for size in (90, 70, None):
            with self.subTest(size=size):
                if size is None:
                    self.document.pop("cell_size_mm")
                else:
                    self.document["cell_size_mm"] = size
                self.write()
                result, _ = service.capture("scan")
                self.assert_floor_width(result, 100 if size is None else size)
                snapshot = result["placement_observation"]["grid_snapshot"]
                self.assertEqual(snapshot, self.grid.snapshot())

    def test_legacy_grid_uses_default80_or_explicit_cli_size(self):
        for options, expected in (({}, 80), ({"grid_cell_size": 110}, 110)):
            with self.subTest(options=options):
                service = self.service(**options)
                result, _ = service.capture("verification")
                self.assert_floor_width(result, expected)

    def test_size_change_invalidates_old_observation_and_reservation_preview(self):
        self.document["cell_size_mm"] = 100
        self.write()
        snapshot, preview = self.grid.snapshot(), self.grid.preview(1)
        self.document["cell_size_mm"] = 90
        self.write()
        before = self.path.read_bytes()
        self.assertNotEqual(self.grid.snapshot()["fingerprint"], snapshot["fingerprint"])
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.reconcile_observation(snapshot, {2: "clear"})
        with self.assertRaisesRegex(SelectionError, "changed"):
            self.grid.reserve(preview)
        self.assertEqual(self.path.read_bytes(), before)

    def test_forged_snapshot_size_is_rejected_despite_matching_fingerprint(self):
        for current, claimed in ((90, 100), (90, None), (None, 90)):
            with self.subTest(current=current, claimed=claimed):
                if current is None:
                    self.document.pop("cell_size_mm", None)
                else:
                    self.document["cell_size_mm"] = current
                self.write()
                snapshot = self.grid.snapshot()
                if claimed is None:
                    snapshot.pop("cell_size_mm", None)
                else:
                    snapshot["cell_size_mm"] = claimed
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SelectionError, "cell size"):
                    self.grid.reconcile_observation(snapshot, {2: "clear"})
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
