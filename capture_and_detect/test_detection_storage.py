"""Exercise the image/depth/calibration/storage pipeline with synthetic YOLO output."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import capture_and_detect_custom as app
from camera_client import CameraError
from cup_inventory import SelectionError


class Result:
    def __init__(self, image, detections=None):
        self.image = image
        if detections is None:
            detections = [(0, (460, 50, 560, 150)), (1, (505, 95, 515, 105))]
        self.boxes = [SimpleNamespace(cls=kind, conf=.95, xyxy=np.array([box]))
                      for kind, box in detections]
        masks = []
        for _, (left, top, right, bottom) in detections:
            mask = np.zeros(image.shape[:2], dtype=bool)
            mask[top:bottom + 1, left:right + 1] = True
            masks.append(SimpleNamespace(cpu=lambda mask=mask: SimpleNamespace(numpy=lambda: mask)))
        self.masks = SimpleNamespace(data=masks)

    def __getitem__(self, indices):
        return self

    def plot(self):
        return self.image.copy()


class DetectionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.http = []
        for method in ("get", "post"):
            patcher = patch("requests." + method)
            mocked = patcher.start()
            self.http.append(mocked)
            self.addCleanup(mocked.assert_not_called)
            self.addCleanup(patcher.stop)
        for stream in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)
        self.out_dir = Path(self.temporary.name)
        self.calibration_path = self.out_dir / "calibration.json"
        self.calibration_path.write_text(json.dumps({
            "scale": 1, "R": np.eye(3).tolist(), "t": [0, 0, -750], "tag_offset": [-115, 0, 40]}))
        self.calibration = app.Calibration(self.calibration_path)
        self.K = {"fx": 800, "fy": 800, "cx": 0, "cy": 0, "width": 640, "height": 240}
        self.image = np.zeros((240, 640, 3), dtype=np.uint8)
        self.depth = np.full((240, 640), 800, dtype=np.uint16)
        self.model = SimpleNamespace(names={0: "CUP", 1: "CUP BASE", 2: "CUP HANDLE"},
                                     predict=Mock(return_value=[Result(self.image)]))
        self.detector = app.Detector(self.model, self.calibration, self.K, .5)
        self.frame = {"sequence": 1, "timestamp": "2000-01-01T00:00:00Z",
                      "observed_at": "2026-10-27T00:00:00Z",
                      "observation_started_monotonic": time.monotonic()}
        self.camera = SimpleNamespace(api="http://offline.invalid/api/v1",
                                      intrinsics=Mock(return_value=self.K.copy()),
                                      capture=Mock(return_value=(self.image, self.depth, self.frame)))
        self.store = app.CaptureStore(self.out_dir)
        self.service = app.CaptureService(self.camera, self.detector, self.store, "fixture.pt")

    def test_scan_persists_coordinates_numbered_images_and_verification_history(self):
        scan, started = self.service.capture("scan")
        verification, _ = self.service.capture("verification")
        self.assertEqual(started, self.frame["observation_started_monotonic"])
        cup = scan["cups"][0]
        self.assertEqual((cup["x"], cup["y"], cup["z"]), (510, 100, 50))
        self.assertEqual(cup["camera_mm"], {"x": 510, "y": 100, "z": 800})
        self.assertEqual(cup["hover_target"]["flange_mm"], {"x": 395, "y": 100, "z": 409})
        self.assertEqual(scan["tag_offset_mm"], [-115, 0, 40])
        self.assertTrue(cup["selectable"])
        self.assertEqual(json.loads((self.out_dir / "cups.json").read_text()), scan)
        record = json.loads((self.out_dir / "latest.json").read_text())
        self.assertEqual(record["capture"], scan["capture"])
        self.assertEqual([obj["detection_id"] for obj in record["objects"]], [0, 1])
        self.assertEqual(record["objects"][1]["bbox_xyxy"], [505, 95, 515, 105])
        self.assertEqual((cup["cup_detection_id"], cup["base_detection_id"]), (0, 1))
        self.assertEqual(cup["acceptance_reasons"], ["in_cup", "cup_height"])
        self.assertNotEqual(scan["capture"], verification["capture"])
        for observation in (scan, verification):
            self.assertEqual(json.loads((self.out_dir / f"cups_{observation['capture']}.json").read_text()), observation)
            self.assertTrue((self.out_dir / f"detections_{observation['capture']}.json").exists())
            self.assertTrue(all(Path(path).is_file() for path in observation["images"].values()))
        self.assertEqual(len((self.out_dir / "detections.csv").read_text().splitlines()), 5)
        self.assertEqual(cv2.imread(scan["images"]["depth"], cv2.IMREAD_UNCHANGED).dtype, np.dtype("uint16"))
        self.assertGreater(cv2.imread(scan["images"]["annotated"]).sum(), 0)
        self.assertNotIn("observation_started_monotonic", scan["frame"])

    def test_actual_mark_cup_bases_accepts_orphans_from_height_or_handle(self):
        cases = [([(1, (505, 95, 515, 105))], 800, "cup_height"),
                 ([(1, (505, 95, 515, 105)), (2, (515, 95, 525, 105))], 950, "near_handle")]
        for detections, depth_mm, reason in cases:
            with self.subTest(reason=reason):
                self.model.predict.return_value = [Result(self.image, detections)]
                self.camera.capture.return_value = (self.image, np.full_like(self.depth, depth_mm), self.frame)
                observation, _ = self.service.capture("scan")
                self.assertEqual(len(observation["cups"]), 1)
                cup = observation["cups"][0]
                self.assertIsNone(cup["cup_detection_id"])
                self.assertTrue(cup["selectable"])
                self.assertEqual(cup["acceptance_reasons"], [reason])
                record = json.loads((self.out_dir / "latest.json").read_text())
                base = next(obj for obj in record["objects"] if obj["class"] == "CUP BASE")
                self.assertIs(base[reason], True)
                self.assertIs(base["in_cup"], False)

    def assert_no_inventory(self):
        self.assertFalse((self.out_dir / "cups.json").exists())
        self.assertFalse((self.out_dir / "latest.json").exists())

    def test_changed_calibration_cannot_create_current_inventory(self):
        self.calibration_path.write_text("changed")
        with self.assertRaises(SelectionError):
            self.service.capture("scan")
        self.camera.capture.assert_not_called()
        self.model.predict.assert_not_called()
        self.assert_no_inventory()

    def test_changed_intrinsics_cannot_create_current_inventory(self):
        self.camera.intrinsics.return_value = dict(self.K, fx=900)
        with self.assertRaises(CameraError):
            self.service.capture("scan")
        self.camera.capture.assert_not_called()
        self.model.predict.assert_not_called()
        self.assert_no_inventory()

    def test_mismatched_rgb_or_depth_dimensions_cannot_create_current_inventory(self):
        for image, depth in ((self.image[:-1], self.depth), (self.image, self.depth[:-1])):
            with self.subTest(rgb=image.shape, depth=depth.shape):
                self.camera.capture.return_value = image, depth, self.frame
                with self.assertRaises(CameraError):
                    self.service.capture("scan")
                self.model.predict.assert_not_called()
                self.assert_no_inventory()

    def test_image_write_failure_cannot_publish_inventory(self):
        real_write = cv2.imwrite

        def write(path, array):
            return False if Path(path).name.startswith("annotated_") else real_write(path, array)

        with patch.object(cv2, "imwrite", side_effect=write):
            with self.assertRaises(OSError):
                self.service.capture("scan")
        self.assert_no_inventory()
        self.assertTrue(list(self.out_dir.glob("rgb_*.jpg")))
        self.assertEqual(list(self.out_dir.glob("cups_*.json")), [])

    def test_empty_detections_save_empty_inventory_without_any_robot_calls(self):
        self.model.predict.return_value = [Result(self.image, [])]
        scan, _ = self.service.capture("scan")
        self.assertEqual(scan["cups"], [])
        self.assertEqual(json.loads((self.out_dir / "cups.json").read_text())["cups"], [])
        self.assertEqual(json.loads((self.out_dir / "latest.json").read_text())["objects"], [])
        self.assertEqual(len((self.out_dir / "detections.csv").read_text().splitlines()), 1)

    def test_help_and_invalid_flags_do_not_load_model_token_or_gateway(self):
        cases = [(["--help"], 0), (["--unknown"], 2), (["--speed", "1501"], 2),
                 (["--conf", "0"], 2), (["--max-result-age", "nan"], 2)]
        for arguments, code in cases:
            with self.subTest(arguments=arguments):
                with patch.object(app, "load_model") as model, patch.object(app, "CameraClient") as camera, \
                        patch.object(app, "load_token") as token:
                    with self.assertRaises(SystemExit) as error:
                        app.main(arguments)
                    self.assertEqual(error.exception.code, code)
                    model.assert_not_called()
                    camera.assert_not_called()
                    token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
