"""Synthetic-only checks for the merged depth sampling and storage behavior."""
import csv
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from camera_client import CameraClient, CameraError, NoNewCameraFrame
from capture_and_detect import CaptureService, CaptureStore, Detector
from depth_average import average_depth, capture_fresh_samples, mask_depth


class DepthSamplingTests(unittest.TestCase):
    def pairs(self, sequences=(1, 2, 3)):
        return [(np.full((15, 15, 3), seq, np.uint8), np.full((15, 15), 800 + seq, np.uint16),
                 {"sequence": seq, "timestamp": str(seq), "observation_started_monotonic": float(seq)})
                for seq in sequences]

    def test_newest_raw_pair_is_retained_and_age_starts_at_oldest_sample(self):
        pairs = self.pairs()
        camera = SimpleNamespace(capture=Mock(side_effect=pairs))
        rgb, depth, frame, samples = capture_fresh_samples(camera, 3)
        self.assertIs(rgb, pairs[-1][0])
        self.assertIs(depth, pairs[-1][1])
        self.assertEqual(frame["sequence"], 3)
        self.assertEqual(frame["observation_started_monotonic"], 1.0)
        self.assertEqual(frame["depth_sampling"]["sequences"], [1, 2, 3])
        self.assertEqual(frame["depth_sampling"]["grid_evidence"], "latest_raw_aligned_frame")
        self.assertEqual(len(samples), 3)
        self.assertEqual(pairs[-1][2]["observation_started_monotonic"], 3.0)

    def test_first_sample_uses_latest_pair_then_waits_only_for_additional_samples(self):
        pairs = self.pairs()
        camera = SimpleNamespace(capture_latest=Mock(return_value=pairs[0]),
                                 capture=Mock(side_effect=pairs[1:]))
        _, _, frame, samples = capture_fresh_samples(camera, 3)
        camera.capture_latest.assert_called_once()
        self.assertEqual(camera.capture.call_count, 2)
        self.assertEqual(frame["depth_sampling"]["sequences"], [1, 2, 3])
        self.assertEqual(len(samples), 3)

    def test_repeated_sequence_uses_available_samples_and_restart_accepts_new_sequence(self):
        camera = SimpleNamespace(capture=Mock(side_effect=self.pairs((2, 2))))
        _, _, frame, samples = capture_fresh_samples(camera, 2)
        self.assertEqual(len(samples), 1)
        self.assertEqual(frame["sequence"], 2)
        camera.capture.side_effect = self.pairs((2, 1))
        self.assertEqual(len(capture_fresh_samples(camera, 2)[3]), 2)

    def test_only_no_new_frame_ends_burst_with_available_samples(self):
        camera = SimpleNamespace(capture=Mock(side_effect=[self.pairs()[0], NoNewCameraFrame("frozen")]))
        _, _, frame, samples = capture_fresh_samples(camera, 3)
        self.assertEqual(len(samples), 1)
        self.assertEqual(frame["depth_sampling"]["count"], 1)
        camera.capture.side_effect = [self.pairs()[0], CameraError("network disconnected")]
        with self.assertRaises(CameraError):
            capture_fresh_samples(camera, 3)
        camera.capture.side_effect = NoNewCameraFrame("no first observation")
        with self.assertRaises(NoNewCameraFrame):
            capture_fresh_samples(camera, 3)

    def test_dimension_change_and_frame_count_are_bounded(self):
        for count in (0, 10, True, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                capture_fresh_samples(None, count)
        pairs = self.pairs((1, 2))
        pairs[1] = (pairs[1][0], np.ones((14, 15), np.uint16), pairs[1][2])
        with self.assertRaises(CameraError):
            capture_fresh_samples(SimpleNamespace(capture=Mock(side_effect=pairs)), 2)

    def test_pixel_average_ignores_holes_without_warning(self):
        with np.errstate(all="raise"):
            result = average_depth([np.array([[0, 800]], np.uint16), np.array([[0, 810]], np.uint16)])
        np.testing.assert_array_equal(result, [[0, 805]])

    def test_oversized_frame_is_rejected_before_payload_download(self):
        response = SimpleNamespace(headers={"X-Frame-Sequence": "1", "X-Frame-Width": "4000",
            "X-Frame-Height": "4000", "X-Frame-Timestamp": "now", "X-Pixel-Format": "RAW16"})
        with self.assertRaises(CameraError):
            CameraClient._frame_headers(response, "RAW16")

    def test_mask_edge_does_not_dominate_depth(self):
        mask = np.ones((15, 15), bool)
        mask[:2] = mask[-2:] = False
        mask[:, :2] = mask[:, -2:] = False
        depth = np.full((15, 15), 1400, np.uint16)
        depth[6:9, 6:9] = 800
        self.assertEqual(mask_depth(depth, mask), 800)


class AveragedObjectTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.ones((15, 15), bool)
        self.depths = [np.full((15, 15), value, np.uint16) for value in (800, 804, 808)]
        self.detector = SimpleNamespace(K={"fx": 800, "fy": 800, "cx": 0, "cy": 0},
            calibration=SimpleNamespace(to_robot=lambda x, y, z: (x, y, z - 750)),
            _depth_samples=self.depths)

    def test_provider_compatible_v4_mean_and_per_frame_robot_z(self):
        result = Detector.describe(self.detector, "CUP BASE", .95, self.mask, self.depths[-1])
        self.assertEqual(result["z_mm"], 804)
        self.assertEqual(result["robot_z_mm"], 54)
        self.assertEqual(result["robot_z_frames_mm"], [50, 54, 58])
        self.assertNotIn("depth_quality", result)

    def test_missing_latest_depth_uses_available_positive_samples_as_in_v4(self):
        self.depths[-1][:] = 0
        result = Detector.describe(self.detector, "CUP BASE", .95, self.mask, self.depths[-1])
        self.assertEqual(result["z_mm"], 802)
        self.assertEqual(result["z_frames_mm"], [800, 804, None])

    def test_v4_mean_has_no_twenty_mm_spread_rejection(self):
        self.depths[-1][:] = 950
        result = Detector.describe(self.detector, "CUP BASE", .95, self.mask, self.depths[-1])
        self.assertEqual(result["z_mm"], 851.3)
        self.assertEqual(result["robot_z_mm"], 101.3)

    def test_observation_preserves_raw_grid_depth_and_cleans_sample_buffers(self):
        shape = (15, 15)
        raw = self.depths[-1]
        K = dict(self.detector.K, width=15, height=15)
        detector = SimpleNamespace(K=K, calibration=SimpleNamespace(ensure_unchanged=Mock()),
                                  analyse=Mock(return_value=([], np.zeros((*shape, 3), np.uint8))))
        camera = SimpleNamespace(intrinsics=lambda: K)
        service = CaptureService(camera, detector, None, "weights.pt", depth_frames=3)
        frame = {"observation_started_monotonic": 1.0}
        with patch("capture_and_detect.capture_fresh_samples", return_value=(np.zeros((*shape, 3), np.uint8), raw, frame, tuple(self.depths))):
            observation = service._observe()
        self.assertIs(observation[1], raw)
        self.assertEqual(detector._depth_samples, ())
        self.assertIs(detector.analyse.call_args.args[1], raw)


class CsvFallbackTests(unittest.TestCase):
    def test_locked_existing_csv_saves_capture_specific_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            original = store._write_rows
            calls = []
            def write(path, objects, stamp):
                calls.append(path.name)
                if path.name == "detections.csv":
                    raise PermissionError("locked")
                original(path, objects, stamp)
            with patch.object(store, "_write_rows", side_effect=write):
                store._append_csv([{"class": "CUP", "robot_z_frames_mm": [50, 51]}], "sample")
            self.assertEqual(calls, ["detections.csv", "detections_sample.csv"])
            with (Path(directory)/"detections_sample.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["robot_z_frames_mm"], "[50, 51]")

    def test_demo_can_preview_camera_height_mode_without_hardware(self):
        from web_app import demo_factory
        with tempfile.TemporaryDirectory() as directory, patch("requests.sessions.Session.request") as request:
            controller = SimpleNamespace(event=Mock(), publish_session=Mock())
            session, _ = demo_factory(Path(directory), camera_z=True)(controller)
            self.assertTrue(session.camera_z)
            self.assertTrue(session.robot.dry_run)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
