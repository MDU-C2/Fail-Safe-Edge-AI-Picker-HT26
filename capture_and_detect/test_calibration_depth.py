"""Hand-eye depth correction must agree across picking, grids and references."""

import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from calibration_depth import corrected_depth, corrected_depth_image, depth_parameters
from cup_inventory import SelectionError
from picker_workflow import Calibration, Detector
from placement_vision import PlacementVision
from placement_table_reference import TableReference, fit_reference
import depth_average


class CalibrationDepthTests(unittest.TestCase):
    def setUp(self):
        self.K = {"width": 320, "height": 240, "fx": 200., "fy": 200., "cx": 160., "cy": 120.}
        self.raw_calibration = {
            "scale": 1., "R": np.diag([1., -1., -1.]).tolist(), "t": [300., 0., 1000.],
            "tag_offset": [0., 0., 0.], "depth_scale": .5, "depth_offset": 100.}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "calibration.json"
        self.path.write_text(json.dumps(self.raw_calibration))
        self.calibration = Calibration(self.path)
        self.location = {"id": 1, "x": 350., "y": 50., "z": 230.}
        self.raw_depth = np.full((240, 320), 1800, dtype=np.uint16)
        self.vision = PlacementVision(self.calibration, self.K, cell_size=80.)

    def reference(self, depth=None):
        raw = self.raw_depth if depth is None else depth
        slots = self.vision.assess(raw, [], [self.location])
        return fit_reference(raw, [self.location], calibration=self.calibration,
            intrinsics=self.K, cell_size=80., baseline_slots=slots,
            source={"capture": "verified_empty", "depth_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
                    "frame_sequences": [1, 2, 3]})

    def load_reference(self, reference, calibration=None):
        return TableReference.from_dict(reference,
            calibration=self.calibration if calibration is None else calibration,
            intrinsics=self.K, locations=[self.location], cell_size=80.)

    def test_affine_optical_depth_is_applied_before_xyz_and_diameter(self):
        detector = Detector(SimpleNamespace(names={0: "CUP"}), self.calibration, self.K, .5)
        mask = np.zeros(self.raw_depth.shape, dtype=bool)
        mask[100:121, 170:191] = True  # u=180,v=110: a ray away from the optical axis.
        raw = np.full_like(self.raw_depth, 1600)  # corrected optical Z=900mm.
        result = detector.describe("CUP", .9, mask, raw)
        self.assertEqual([result[key] for key in ("x_mm", "y_mm", "z_mm")], [90., -45., 900.])
        self.assertEqual([result[key] for key in ("robot_x_mm", "robot_y_mm", "robot_z_mm")], [390., 45., 100.])
        self.assertEqual(result["diameter_mm"], round(2*np.sqrt(441/np.pi)*900/200, 1))
        np.testing.assert_array_equal(raw, np.full_like(raw, 1600))
        self.assertEqual(self.calibration.offset, (0., 0., 0.))

    def test_each_frame_corrected_once_and_invalid_frames_do_not_gain_offset(self):
        detector = Detector(SimpleNamespace(names={0: "CUP"}), self.calibration, self.K, .5)
        mask = np.zeros(self.raw_depth.shape, dtype=bool)
        mask[100:121, 170:191] = True
        detector._depth_samples = tuple(np.full_like(self.raw_depth, value) for value in (1600, 1640, 0))
        result = detector.describe("CUP", .9, mask, detector._depth_samples[-1])
        self.assertEqual(result["z_frames_mm"], [900., 920., None])
        self.assertEqual(result["robot_z_frames_mm"], [100., 80., None])
        self.assertEqual(result["z_mm"], 910.)
        self.assertEqual(result["robot_z_mm"], 90.)
        self.assertEqual(result["x_mm"], 91.)

    def test_grid_and_reference_share_corrected_geometry_without_mutating_raw_image(self):
        original = self.raw_depth.copy()
        slots = self.vision.assess(self.raw_depth, [], [self.location])
        self.assertEqual(slots[0]["state"], "clear")
        reference = self.reference()
        np.testing.assert_allclose(reference["plane_z_mm"], [0., 0., 0.], atol=1e-8)
        self.assertEqual(reference["identity"]["depth_scale"], .5)
        self.assertEqual(reference["identity"]["depth_offset"], 100.)
        self.load_reference(reference)
        np.testing.assert_array_equal(self.raw_depth, original)
        # A real 100mm raised patch must still block; correction cannot merely
        # make an empty calibration fixture pass while removing object height.
        raw = self.raw_depth.copy()
        raw[108:113, 168:173] = 1600
        self.assertEqual(self.vision.assess(raw, [], [self.location])[0]["state"], "occupied")

    def test_missing_depth_remains_unknown_and_cannot_train_a_reference(self):
        raw = self.raw_depth.copy()
        raw[108:113, 168:173] = 0
        self.assertEqual(self.vision.assess(raw, [], [self.location])[0]["state"], "unknown")
        with self.assertRaises(SelectionError):
            self.reference(raw)

    def test_reference_rejects_changed_or_ignored_mapping_even_with_same_digest(self):
        reference = self.reference()
        for scale, offset in ((.6, 100.), (.5, 101.), (1., 0.)):
            with self.subTest(scale=scale, offset=offset):
                calibration = copy.copy(self.calibration)
                calibration.depth_scale, calibration.depth_offset = scale, offset
                with self.assertRaisesRegex(SelectionError, "depth_scale|depth_offset"):
                    self.load_reference(reference, calibration)
        missing = copy.deepcopy(reference)
        missing["identity"].pop("depth_scale")
        missing["identity"].pop("depth_offset")
        with self.assertRaisesRegex(SelectionError, "depth_scale|depth_offset"):
            self.load_reference(missing)

    def test_legacy_calibration_and_reference_do_not_gain_identity_fields(self):
        calibration = copy.copy(self.calibration)
        del calibration.depth_scale
        del calibration.depth_offset
        depth = np.full_like(self.raw_depth, 1000)
        vision = PlacementVision(calibration, self.K, cell_size=80.)
        reference = fit_reference(depth, [self.location], calibration=calibration,
            intrinsics=self.K, cell_size=80., baseline_slots=vision.assess(depth, [], [self.location]),
            source={"capture": "legacy_empty", "depth_sha256": "b"*64, "frame_sequences": [1, 2, 3]})
        self.assertNotIn("depth_scale", reference["identity"])
        self.assertNotIn("depth_offset", reference["identity"])
        self.load_reference(reference, calibration)
        calibration.depth_scale, calibration.depth_offset = 1., 0.
        self.load_reference(reference, calibration)

    def test_invalid_raw_and_corrected_depth_do_not_become_measurements(self):
        for value in (None, 0., -1., float("nan"), float("inf")):
            self.assertIsNone(corrected_depth(value, .5, 100.))
        self.assertIsNone(corrected_depth(100., 1., -200.))
        self.assertIsNone(corrected_depth(1e308, 2., 0.))
        values = np.array([0., -1., np.nan, np.inf, 100., 500.])
        np.testing.assert_array_equal(corrected_depth_image(values, {"depth_scale": .5, "depth_offset": 100.}),
                                      [0., 0., 0., 0., 150., 350.])
        np.testing.assert_array_equal(corrected_depth_image(values, {"depth_scale": 1., "depth_offset": -200.}),
                                      [0., 0., 0., 0., 0., 300.])
        self.assertTrue(np.isnan(values[2]))

    def test_invalid_parameters_fail_at_load_and_reference_validation(self):
        for key, value in (("depth_scale", 0), ("depth_scale", -1), ("depth_scale", True),
                           ("depth_scale", "1"), ("depth_scale", float("inf")),
                           ("depth_offset", False), ("depth_offset", float("nan")), ("depth_offset", None)):
            with self.subTest(key=key, value=value):
                raw = dict(self.raw_calibration, **{key: value})
                self.path.write_text(json.dumps(raw))
                with self.assertRaises(ValueError):
                    Calibration(self.path)
                with self.assertRaises(ValueError):
                    depth_parameters(raw)

    def test_legacy_terminal_variants_use_the_same_optical_calibration(self):
        # Load the real geometry classes without importing YOLO, launching a
        # terminal, or opening a gateway connection.
        for name in ("capture_and_detect.py", "capture_and_detect_que.py", "capture_and_detect1.py"):
            with self.subTest(workflow=name):
                path = Path(__file__).with_name(name)
                nodes = [node for node in ast.parse(path.read_text()).body
                         if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in {"Calibration", "Detector", "mm"}
                         or isinstance(node, ast.ImportFrom) and node.module == "calibration_depth"]
                namespace = {"np": np, "json": json, "depth_average": depth_average,
                             "KEEP_CLASSES": ["CUP"], "log": lambda *args: None}
                exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
                calibration = namespace["Calibration"](self.path)
                detector = namespace["Detector"](SimpleNamespace(names={0: "CUP"}), calibration, self.K, .5)
                mask = np.zeros(self.raw_depth.shape, dtype=bool)
                mask[100:121, 170:191] = True
                frames = tuple(np.full_like(self.raw_depth, value) for value in (1600, 1640, 0))
                single = detector.describe("CUP", .9, mask, frames[0])
                self.assertEqual((single["x_mm"], single["y_mm"], single["z_mm"]), (90., -45., 900.))
                self.assertEqual((single["robot_x_mm"], single["robot_y_mm"], single["robot_z_mm"]), (390., 45., 100.))
                multiple = detector.describe("CUP", .9, mask, frames[-1], frames)
                self.assertEqual(multiple["z_frames_mm"], [900., 920., None])
                self.assertEqual(multiple["robot_z_frames_mm"], [100., 80., None])
                self.assertEqual(multiple["z_mm"], 910.)
                missing = detector.describe("CUP", .9, mask, frames[-1])
                self.assertIsNone(missing["z_mm"])
                self.assertIsNone(missing["robot_z_mm"])


if __name__ == "__main__":
    unittest.main()
