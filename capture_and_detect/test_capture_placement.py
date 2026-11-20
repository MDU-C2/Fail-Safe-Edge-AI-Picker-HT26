"""Capture/placement integration uses synthetic frames and temporary grids only."""

from contextlib import redirect_stdout
import copy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import numpy as np

from camera_client import CameraError, NoNewCameraFrame
from detection_masks import encode_mask
from picker_workflow import CaptureService
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
                                      placing_grid=self.grid, scan_observations=3)
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
        self.camera.intrinsics.assert_not_called()
        self.assertEqual(self.detector.analyse.call_count, 3)
        self.calibration.ensure_unchanged.assert_not_called()
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
        self.calibration.ensure_unchanged.assert_not_called()
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

    def test_scan_uses_loaded_intrinsics_without_repeated_configuration_checks(self):
        self.camera.intrinsics.side_effect = CameraError("intrinsics would have changed")
        result, _ = self.service.capture("scan")
        self.camera.intrinsics.assert_not_called()
        self.assertEqual(result["placement_observation"]["frame_count"], 3)
        self.store.save_capture.assert_called_once()

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

    def test_scan_uses_loaded_calibration_without_per_capture_file_identity_checks(self):
        self.calibration.ensure_unchanged.side_effect = SelectionError("Calibration changed")
        self.service.capture("scan")
        self.calibration.ensure_unchanged.assert_not_called()
        self.store.save_capture.assert_called_once()

    def test_default_scan_is_one_observation_and_preserves_placement_evidence(self):
        self.service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                      placing_grid=self.grid)
        result, _ = self.service.capture("scan")
        self.assertEqual(self.camera.capture.call_count, 1)
        self.assertEqual(self.detector.analyse.call_count, 1)
        self.assertEqual(result["placement_observation"]["frame_count"], 1)
        self.assertEqual(result["placement_observation"]["frame_sequences"], [10])
        self.assertEqual([slot["state"] for slot in result["placement_observation"]["slots"]], ["clear"] * 3)

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
                                      placing_grid=self.grid, scan_observations=3)
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

    def test_between_cups_uses_one_fresh_depth_burst_without_grid_or_reference_access(self):
        self.service.capture("scan")
        self.camera.capture.reset_mock(side_effect=True)
        self.camera.capture.side_effect = [self.frame(sequence) for sequence in (20, 21, 22)]
        self.camera.capture_latest = Mock(side_effect=AssertionError("Cached image reused"))
        self.detector.analyse.reset_mock()
        self.store.save_capture.reset_mock()
        self.service.depth_frames = 3
        with patch.object(self.grid, "snapshot", side_effect=AssertionError("Grid read")), \
                patch.object(self.service, "_read_table_reference", side_effect=AssertionError("Reference read")), \
                patch.object(self.service, "_check_table_reference", side_effect=AssertionError("Reference validation")), \
                patch.object(self.service.placement_vision, "assess", side_effect=AssertionError("Grid assessment")), \
                patch.object(self.service.placement_vision, "draw", side_effect=AssertionError("Grid drawing")):
            result, _ = self.service.capture("between_cups")
        self.assertEqual(self.camera.capture.call_count, 3)
        self.camera.capture_latest.assert_not_called()
        self.detector.analyse.assert_called_once()
        self.assertEqual(result["frame"]["depth_sampling"]["sequences"], [20, 21, 22])
        self.assertNotIn("placement_observation", result)
        self.store.save_capture.assert_called_once()
        self.assertEqual(self.store.save_capture.call_args.args[-1], "between_cups")

    def test_between_cups_excludes_placed_cups_using_original_scan_geometry_only(self):
        self.service.capture("scan")
        geometry = copy.deepcopy(self.service._source_grid_geometry)
        self.assertTrue(all(set(place) == {"id", "x", "y"} for place in geometry["locations"]))
        objects = []
        for u, v, x, y in ((320, 240, 515, 0), (460, 100, 650, 100)):
            for kind in ("CUP", "CUP BASE"):
                obj = detection(kind, u, x=x)
                obj.update(robot_y_mm=y, y_px=v)
                obj["bbox_xyxy"][1::2] = [v - 20, v + 20]
                objects.append(obj)
        self.detector.analyse.side_effect = lambda rgb, depth: (copy.deepcopy(objects), rgb.copy())
        self.camera.capture.side_effect = [self.frame(20)]
        # Neither current occupancy/geometry nor the reference is consulted.
        self.grid.path.write_text("invalid grid after original scan")
        self.service.table_reference_path.write_text("invalid reference after original scan")
        self.service.placement_vision = None
        result, _ = self.service.capture("between_cups")
        placed, source = result["cups"]
        self.assertFalse(placed["selectable"])
        self.assertEqual(placed["placement_location_ids"], [1])
        self.assertTrue(source["selectable"])
        self.assertNotIn("placement_observation", result)
        self.assertEqual(self.service._source_grid_geometry, geometry)

    def test_between_cups_without_full_scan_fails_before_camera_or_grid_access(self):
        with patch.object(self.grid, "snapshot", side_effect=AssertionError("Grid read")), \
                patch.object(self.service, "_check_table_reference", side_effect=AssertionError("Reference validation")):
            with self.assertRaisesRegex(SelectionError, "full scan"):
                self.service.capture("between_cups")
        self.camera.capture.assert_not_called()
        self.detector.analyse.assert_not_called()
        self.store.save_capture.assert_not_called()

    def test_between_cups_without_new_source_frame_does_not_infer_or_save(self):
        self.service.capture("scan")
        self.camera.capture.side_effect = NoNewCameraFrame("No new source frame")
        self.camera.capture_latest = Mock(return_value=self.frame(12))
        self.detector.analyse.reset_mock()
        self.store.save_capture.reset_mock()
        with self.assertRaisesRegex(NoNewCameraFrame, "No new source frame"):
            self.service.capture("between_cups")
        self.camera.capture_latest.assert_not_called()
        self.detector.analyse.assert_not_called()
        self.store.save_capture.assert_not_called()


    def test_between_cups_grid_forces_fresh_bursts_and_keeps_complete_scan_evidence(self):
        for observation_count in (1, 3):
            with self.subTest(observations=observation_count):
                service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                         placing_grid=self.grid, depth_frames=3,
                                         scan_observations=observation_count)
                frames = [self.frame(sequence) for sequence in range(20, 20 + observation_count * 3)]
                self.camera.capture.reset_mock(side_effect=True)
                self.camera.capture.side_effect = frames
                self.camera.capture_latest = Mock(side_effect=AssertionError("Cached pre-motion image reused"))
                self.detector.analyse.reset_mock()
                self.store.save_capture.reset_mock()
                before, snapshot = self.grid.path.read_bytes(), self.grid.snapshot()
                with patch.object(service.placement_vision, "assess", wraps=service.placement_vision.assess) as assess:
                    result, _ = service.capture("between_cups_grid")
                self.assertEqual(self.camera.capture.call_count, observation_count * 3)
                self.camera.capture_latest.assert_not_called()
                self.assertTrue(all(call.kwargs.get("timeout_s", 0) > 0 for call in self.camera.capture.call_args_list))
                self.assertEqual(self.detector.analyse.call_count, observation_count)
                self.assertEqual(assess.call_count, observation_count)
                for index, call in enumerate(assess.call_args_list):
                    self.assertIs(call.args[0], frames[3 * index + 2][1], "Grid must use latest raw aligned depth")
                observation = result["placement_observation"]
                self.assertEqual(observation["frame_count"], observation_count)
                self.assertEqual(observation["frame_sequences"], list(range(22, 20 + observation_count * 3, 3)))
                self.assertEqual(observation["grid_snapshot"], snapshot)
                self.assertEqual([slot["state"] for slot in observation["slots"]], ["clear"] * 3)
                self.assertFalse(observation["applied"])
                self.assertEqual(result["frame"]["sequence"], frames[-1][2]["sequence"])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.store.save_capture.assert_called_once()
                self.assertEqual(self.store.save_capture.call_args.args[-1], "between_cups_grid")

    def test_between_cups_grid_observes_removed_and_added_cups_and_excludes_placements_from_sources(self):
        service = CaptureService(self.camera, self.detector, self.store, "offline.pt", placing_grid=self.grid)
        objects = []
        rgb, depth, frame = self.frame(20)
        # P1 is occupied in bookkeeping but empty in this photo. A new cup is
        # centered on P2, while the second detection remains in the source area.
        for u, v, x, y in ((368, 240, 595, 0), (460, 100, 650, 100)):
            for kind in ("CUP", "CUP BASE"):
                obj = detection(kind, u, x=x)
                obj.update(robot_y_mm=y, y_px=v)
                obj["bbox_xyxy"][1::2] = [v - 20, v + 20]
                x0, y0, x1, y1 = obj["bbox_xyxy"]
                mask = np.zeros(depth.shape, dtype=bool)
                mask[y0:y1, x0:x1] = True
                obj["segmentation"] = encode_mask(mask)
                if y == 0:
                    depth[mask] = 900  # A measured cup surface 100 mm above the table.
                objects.append(obj)
        self.detector.analyse.side_effect = lambda rgb, depth: (copy.deepcopy(objects), rgb.copy())
        self.camera.capture.side_effect = [(rgb, depth, frame)]
        self.camera.capture_latest = Mock(side_effect=AssertionError("Cached image reused"))
        before = self.grid.path.read_bytes()
        result, _ = service.capture("between_cups_grid")
        observation = result["placement_observation"]
        self.assertEqual([slot["state"] for slot in observation["slots"]], ["clear", "occupied", "clear"])
        self.assertTrue(observation["grid_snapshot"]["locations"][0]["occupied"])
        self.assertFalse(observation["grid_snapshot"]["locations"][1]["occupied"])
        self.assertEqual(observation["slots"][0]["evidence"], [])
        self.assertEqual(observation["slots"][1]["cup_count"], 1)
        self.assertIn("cup", observation["slots"][1]["evidence"])
        self.assertEqual(observation["cup_summary"]["location_ids"], [2])
        placed, source = result["cups"]
        self.assertEqual(placed["placement_location_ids"], [2])
        self.assertFalse(placed["selectable"])
        self.assertTrue(source["selectable"])
        self.assertNotIn("placement_location_ids", source)
        self.assertEqual([cup["x"] for cup in result["cups"] if not cup.get("placement_location_ids")], [650])
        # Applying occupancy is the session's responsibility after completion.
        self.assertEqual(self.grid.path.read_bytes(), before)
        self.assertFalse(observation["applied"])
        self.assertEqual(result["source_presence"], {"cup_detections": 1})
        self.camera.capture_latest.assert_not_called()

    def test_between_cups_grid_without_new_frame_does_not_fall_back_or_save(self):
        self.camera.capture.side_effect = NoNewCameraFrame("No post-motion frame")
        self.camera.capture_latest = Mock(return_value=self.frame(12))
        before = self.grid.path.read_bytes()
        with self.assertRaisesRegex(NoNewCameraFrame, "No post-motion frame"):
            self.service.capture("between_cups_grid")
        self.camera.capture_latest.assert_not_called()
        self.detector.analyse.assert_not_called()
        self.store.save_capture.assert_not_called()
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_between_cups_grid_keeps_invalid_depth_unknown_instead_of_clearing_occupancy(self):
        service = CaptureService(self.camera, self.detector, self.store, "offline.pt", placing_grid=self.grid)
        rgb, depth, frame = self.frame(20)
        self.camera.capture.side_effect = [(rgb, np.zeros_like(depth), frame)]
        before = self.grid.path.read_bytes()
        result, _ = service.capture("between_cups_grid")
        self.assertEqual([slot["state"] for slot in result["placement_observation"]["slots"]], ["unknown"] * 3)
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_between_cups_grid_rejects_changed_table_reference_before_capture(self):
        self.service.table_reference_path.write_text("changed after startup")
        with self.assertRaisesRegex(SelectionError, "reference changed"):
            self.service.capture("between_cups_grid")
        self.camera.capture.assert_not_called()
        self.detector.analyse.assert_not_called()
        self.store.save_capture.assert_not_called()

    def test_source_presence_counts_visible_cup_without_a_pickable_base(self):
        for purpose in ("scan", "between_cups_grid", "between_cups"):
            with self.subTest(purpose=purpose):
                service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                         placing_grid=self.grid)
                if purpose == "between_cups":
                    self.camera.capture.side_effect = [self.frame(19)]
                    self.detector.analyse.side_effect = lambda rgb, depth: ([], rgb.copy())
                    service.capture("scan")
                objects = [detection("CUP", 100, x=650)]
                original = copy.deepcopy(objects)
                self.detector.analyse.side_effect = lambda rgb, depth: (objects, rgb.copy())
                self.camera.capture.side_effect = [self.frame(20)]
                result, _ = service.capture(purpose)
                self.assertEqual(result["cups"], [])
                self.assertEqual(result["source_presence"], {"cup_detections": 1})
                self.assertEqual(objects, original)
                self.assertEqual(self.store.save_capture.call_args.args[0]["source_presence"],
                                 {"cup_detections": 1})

    def test_source_presence_counts_unlocated_cups_even_over_the_grid_image(self):
        for missing in ("robot_x_mm", "robot_y_mm", "robot_z_mm"):
            with self.subTest(missing=missing):
                service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                         placing_grid=self.grid)
                cup = detection("CUP", 320, x=515)
                cup.update(robot_y_mm=0, y_px=240, bbox_xyxy=[305, 220, 335, 260])
                cup[missing] = None
                self.detector.analyse.side_effect = lambda rgb, depth: ([cup], rgb.copy())
                self.camera.capture.side_effect = [self.frame(20)]
                result, _ = service.capture("between_cups_grid")
                self.assertEqual(result["cups"], [])
                self.assertEqual(result["source_presence"], {"cup_detections": 1})

    def test_source_presence_excludes_grid_cups_by_existing_xy_offsets_without_a_base(self):
        self.calibration.offset = (-115, 17, 40)
        for purpose in ("scan", "between_cups_grid", "between_cups"):
            with self.subTest(purpose=purpose):
                service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                         placing_grid=self.grid)
                if purpose == "between_cups":
                    self.camera.capture.side_effect = [self.frame(19)]
                    self.detector.analyse.side_effect = lambda rgb, depth: ([], rgb.copy())
                    service.capture("scan")
                cup = detection("CUP", 320, x=515)
                cup.update(robot_y_mm=-17, y_px=240, bbox_xyxy=[305, 220, 335, 260])
                self.detector.analyse.side_effect = lambda rgb, depth: ([cup], rgb.copy())
                self.camera.capture.side_effect = [self.frame(20)]
                result, _ = service.capture(purpose)
                self.assertEqual(result["cups"], [])
                self.assertEqual(result["source_presence"], {"cup_detections": 0})

    def test_source_presence_reuses_latest_mask_assignment_when_centroid_depth_is_unknown(self):
        service = CaptureService(self.camera, self.detector, self.store, "offline.pt",
                                 placing_grid=self.grid, scan_observations=3)
        grid_cup = detection("CUP", 368, x=None, z=None)
        grid_cup.update(robot_y_mm=None, y_px=240, bbox_xyxy=[353, 220, 383, 260])
        mask = np.zeros((480, 640), dtype=bool)
        mask[220:260, 353:383] = True
        grid_cup["segmentation"] = encode_mask(mask)
        source = detection("CUP", 100, x=650)
        # Object indices differ between frames: only the latest assignments
        # may exclude the latest CUPs, not an index union from earlier frames.
        groups = [[grid_cup, source], [grid_cup, source], [source, grid_cup]]
        self.detector.analyse.side_effect = [(copy.deepcopy(group), self.frame(20)[0]) for group in groups]
        frames = []
        for sequence in (20, 21, 22):
            rgb, depth, frame = self.frame(sequence)
            depth[mask] = 900
            frames.append((rgb, depth, frame))
        self.camera.capture.side_effect = frames
        result, _ = service.capture("between_cups_grid")
        self.assertEqual(result["cups"], [])
        self.assertEqual(result["placement_observation"]["slots"][1]["cup_count"], 1)
        self.assertEqual(result["placement_observation"]["slots"][1]["assigned_cups"][0]["object_indices"], [1])
        self.assertEqual(result["source_presence"], {"cup_detections": 1})

    def test_source_presence_is_zero_for_empty_or_parts_only_views(self):
        service = CaptureService(self.camera, self.detector, self.store, "offline.pt", placing_grid=self.grid)
        for objects in ([], [detection("CUP BASE", 100)], [detection("CUP HANDLE", 100)]):
            with self.subTest(objects=objects):
                self.detector.analyse.side_effect = lambda rgb, depth: (objects, rgb.copy())
                self.camera.capture.side_effect = [self.frame(20)]
                result, _ = service.capture("between_cups_grid")
                self.assertEqual(result["source_presence"], {"cup_detections": 0})


if __name__ == "__main__":
    unittest.main()
