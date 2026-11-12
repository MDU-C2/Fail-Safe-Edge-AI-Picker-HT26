"""Capture/placement integration uses synthetic frames and temporary grids only."""

from contextlib import redirect_stdout
import copy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import weakref

import numpy as np

from camera_client import CameraError
from capture_and_detect import CaptureService
from cup_inventory import SelectionError
from test_cup_selection import detection
from test_support_grid import temporary_grid


class CapturePlacementTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.K = {"width": 640, "height": 480, "fx": 600.0, "fy": 600.0,
                  "cx": 320.0, "cy": 240.0}
        self.calibration = SimpleNamespace(
            scale=1.0, R=np.diag([1.0, -1.0, -1.0]), t=np.array([515.0, 0.0, 960.0]),
            offset=(-115.0, 0.0, 40.0), digest="offline-calibration", ensure_unchanged=Mock())
        self.grid = temporary_grid(self, locations=[
            {"id": 1, "x": 400, "y": 0, "z": 230, "occupied": True},
            {"id": 2, "x": 480, "y": 0, "z": 230, "occupied": False},
            {"id": 3, "x": 560, "y": 0, "z": 230, "occupied": False}])
        self.camera = SimpleNamespace(api="http://offline.invalid/api/v1",
                                      intrinsics=Mock(return_value=copy.deepcopy(self.K)), capture=Mock())
        self.detector = SimpleNamespace(calibration=self.calibration, K=self.K, conf=0.5,
                                        analyse=Mock(side_effect=lambda rgb, depth: ([], rgb.copy())))
        self.store = SimpleNamespace(folder=self.grid.path.parent, save_capture=Mock())
        self.service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                      placing_grid=self.grid)
        self.frames = [self.frame(sequence) for sequence in (10, 11, 12)]
        self.camera.capture.side_effect = self.frames

    @staticmethod
    def frame(sequence):
        return (np.full((480, 640, 3), sequence, dtype=np.uint8),
                np.full((480, 640), 1000, dtype=np.uint16),
                {"sequence": sequence, "timestamp": "2026-10-27T12:00:00Z",
                 "observed_at": "2026-10-27T12:00:00Z",
                 "received_monotonic": 100.0 + sequence,
                 "observation_started_monotonic": 99.0 + sequence})

    def test_scan_uses_three_frames_draws_observation_and_never_changes_grid(self):
        before = self.grid.path.read_bytes()
        snapshot = self.grid.snapshot()
        result, started = self.service.capture("scan")
        self.assertEqual(self.camera.capture.call_count, 3)
        self.assertEqual(self.camera.intrinsics.call_count, 3)
        self.assertEqual(self.detector.analyse.call_count, 3)
        self.assertEqual(self.calibration.ensure_unchanged.call_count, 4)
        observation = result["placement_observation"]
        self.assertEqual(observation["frame_count"], 3)
        self.assertEqual(observation["frame_sequences"], [10, 11, 12])
        self.assertEqual(observation["grid_snapshot"], snapshot)
        self.assertEqual([slot["state"] for slot in observation["slots"]], ["clear"] * 3)
        self.assertFalse(observation["applied"])
        self.assertEqual(observation["released_ids"], [])
        self.assertEqual(result["frame"]["sequence"], 12)
        self.assertEqual(started, self.frames[-1][2]["observation_started_monotonic"])
        self.store.save_capture.assert_called_once()
        saved, rgb, depth, annotated, objects, K, purpose = self.store.save_capture.call_args.args
        self.assertIs(saved, result)
        self.assertIs(rgb, self.frames[-1][0])
        self.assertIs(depth, self.frames[-1][1])
        self.assertFalse(np.array_equal(rgb, annotated))
        self.assertTrue(np.any(annotated[:, :, 1] > annotated[:, :, 0]))
        self.assertEqual(objects, [])
        self.assertEqual(K, self.K)
        self.assertEqual(purpose, "scan")
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_verification_uses_one_frame_without_reconciliation_snapshot(self):
        before = self.grid.path.read_bytes()
        result, _ = self.service.capture("verification")
        self.camera.capture.assert_called_once()
        self.detector.analyse.assert_called_once()
        self.assertEqual(self.calibration.ensure_unchanged.call_count, 2)
        observation = result["placement_observation"]
        self.assertEqual(observation["frame_count"], 1)
        self.assertNotIn("grid_snapshot", observation)
        self.assertFalse(observation["applied"])
        self.assertEqual(observation["released_ids"], [])
        self.assertIn("unchanged", observation["message"])
        self.store.save_capture.assert_called_once()
        self.assertEqual(self.store.save_capture.call_args.args[-1], "verification")
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_scan_releases_previous_images_before_requesting_the_next_frame(self):
        # Plain callables deliberately avoid Mock's argument history, which
        # would itself retain the arrays whose production lifetime we check.
        references = []
        captures = []

        def capture():
            self.assertTrue(all(reference() is None for reference in references),
                            "Previous observation images remain alive during the next capture")
            sequence = 10 + len(captures)
            captures.append(sequence)
            rgb, depth, frame = self.frame(sequence)
            references.extend((weakref.ref(rgb), weakref.ref(depth)))
            return rgb, depth, frame

        def analyse(rgb, depth):
            annotated = rgb.copy()
            references.append(weakref.ref(annotated))
            return [], annotated

        self.camera.capture = capture
        self.detector.analyse = analyse
        result, _ = self.service.capture("scan")
        self.assertEqual(captures, [10, 11, 12])
        self.assertEqual(result["frame"]["sequence"], 12)
        self.store.save_capture.assert_called_once()
        self.assertTrue(all(reference() is None for reference in references[:-3]))
        self.assertTrue(all(reference() is not None for reference in references[-3:]))

    def test_repeated_sequence_rejects_scan_before_any_capture_save(self):
        before = self.grid.path.read_bytes()
        self.camera.capture.side_effect = [self.frame(sequence) for sequence in (10, 10, 11)]
        with self.assertRaisesRegex(SelectionError, "advancing camera frames"):
            self.service.capture("scan")
        self.assertEqual(self.camera.capture.call_count, 3)
        self.store.save_capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_intrinsics_change_mid_scan_prevents_saving_partial_evidence(self):
        before = self.grid.path.read_bytes()
        self.camera.intrinsics.side_effect = [self.K, {**self.K, "fx": 601.0}]
        with self.assertRaisesRegex(CameraError, "intrinsics changed"):
            self.service.capture("scan")
        self.camera.capture.assert_called_once()
        self.detector.analyse.assert_called_once()
        self.store.save_capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_misaligned_depth_mid_scan_prevents_saving_partial_evidence(self):
        before = self.grid.path.read_bytes()
        rgb, _, frame = self.frames[1]
        self.camera.capture.side_effect = [self.frames[0], (rgb, np.zeros((240, 320), np.uint16), frame)]
        with self.assertRaisesRegex(CameraError, "Depth dimensions"):
            self.service.capture("scan")
        self.assertEqual(self.camera.capture.call_count, 2)
        self.detector.analyse.assert_called_once()
        self.store.save_capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_calibration_change_after_last_frame_prevents_save(self):
        before = self.grid.path.read_bytes()
        self.calibration.ensure_unchanged.side_effect = [None, None, None, SelectionError("Calibration changed")]
        with self.assertRaisesRegex(SelectionError, "Calibration changed"):
            self.service.capture("scan")
        self.assertEqual(self.camera.capture.call_count, 3)
        self.store.save_capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_object_seen_only_in_earlier_frame_still_blocks_final_clear_image(self):
        calls = [0]

        def analyse(rgb, depth):
            calls[0] += 1
            objects = ([{"class": "CUP", "bbox_xyxy": [310, 230, 330, 250]}]
                       if calls[0] == 1 else [])
            return objects, rgb.copy()

        self.detector.analyse.side_effect = analyse
        before = self.grid.path.read_bytes()
        result, _ = self.service.capture("scan")
        self.assertEqual(result["cups"], [])
        self.assertEqual(result["placement_observation"]["slots"][0]["state"], "occupied")
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.store.save_capture.assert_called_once()

    def test_already_placed_cup_is_excluded_using_xy_offsets_while_source_remains_selectable(self):
        self.calibration.offset = (-115.0, 17.0, 40.0)
        self.calibration.t = np.array([515.0, -17.0, 960.0])
        self.service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                      placing_grid=self.grid)
        objects = []
        # Saved coordinates include the calibration tag displacement. After
        # applying both XY offsets, the first cup is in P1 at (400, 0); the
        # second remains outside the placement row at physical (535, 100).
        for u, v, x, y in ((320, 240, 515, -17), (460, 100, 650, 83)):
            for kind in ("CUP", "CUP BASE"):
                obj = detection(kind, u, x=x)
                obj.update(robot_y_mm=y, y_px=v)
                obj["bbox_xyxy"][1::2] = [v - 20, v + 20]
                objects.append(obj)
        self.detector.analyse.side_effect = lambda rgb, depth: (copy.deepcopy(objects), rgb.copy())
        before = self.grid.path.read_bytes()
        result, _ = self.service.capture("scan")
        placed, source = result["cups"]
        self.assertFalse(placed["selectable"])
        self.assertEqual(placed["status"], "blocked")
        self.assertEqual(placed["placement_location_ids"], [1])
        self.assertIn("already in the placement area", placed["blocked_reason"])
        self.assertTrue(source["selectable"])
        self.assertEqual(source["status"], "available")
        self.assertNotIn("placement_location_ids", source)
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertEqual(self.store.save_capture.call_args.args[0]["cups"], result["cups"])


if __name__ == "__main__":
    unittest.main()
