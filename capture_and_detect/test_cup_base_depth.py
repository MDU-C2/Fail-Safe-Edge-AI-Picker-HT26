"""Offline V4 mask-depth behavior, including sparse and incomplete samples."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from picker_workflow import Detector
from cup_inventory import build_inventory


class CupBaseDepthTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.zeros((31, 31), dtype=bool)
        self.mask[5:26, 5:26] = True
        self.depth = np.full(self.mask.shape, 800, dtype=np.uint16)
        self.calibration = SimpleNamespace(to_robot=Mock(return_value=(500, 100, 100)))
        self.detector = Detector(SimpleNamespace(names={0: "CUP BASE"}), self.calibration,
                                 {"cx": 15, "cy": 15, "fx": 100, "fy": 100}, 0.5)

    def describe(self, name="CUP BASE", mask=None, depth=None):
        return self.detector.describe(name, .9, self.mask if mask is None else mask,
                                      self.depth if depth is None else depth)

    def test_uniform_depth_preserves_coordinates_without_extra_core_gate(self):
        obj = self.describe()
        self.assertEqual((obj["x_px"], obj["y_px"], obj["z_mm"]), (15, 15, 800))
        self.assertEqual((obj["robot_x_mm"], obj["robot_y_mm"], obj["robot_z_mm"]), (500, 100, 100))
        self.assertNotIn("depth_quality", obj)
        self.calibration.to_robot.assert_called_once_with(0, 0, 800)
        json.dumps(obj, allow_nan=False)

    def test_mask_edge_is_eroded_before_the_positive_depth_median(self):
        self.depth[10:21, 10:21] = 700
        self.assertEqual(float(np.median(self.depth[self.mask])), 800)
        self.assertEqual(self.describe()["z_mm"], 700)

    def test_no_depth_in_eroded_mask_has_no_robot_coordinates(self):
        self.depth[10:21, 10:21] = 0
        obj = self.describe()
        for key in ("x_mm", "y_mm", "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm"):
            self.assertIsNone(obj[key])
        self.calibration.to_robot.assert_not_called()

    def test_central_hole_does_not_reject_other_available_mask_depth(self):
        self.depth[14:17, 14:17] = 0
        self.assertEqual(self.describe()["z_mm"], 800)

    def test_sparse_positive_depth_is_accepted_without_coverage_threshold(self):
        self.depth[:] = 0
        self.depth[15, 15] = 800
        self.assertEqual(self.describe()["z_mm"], 800)

    def test_small_thin_and_disconnected_masks_use_v4_erosion_fallback(self):
        for shape in ((1, 1), (2, 2), (3, 3), (4, 4), (1, 25), (25, 2)):
            with self.subTest(shape=shape):
                self.assertEqual(self.describe(mask=np.ones(shape, bool),
                                               depth=np.full(shape, 700, np.uint16))["z_mm"], 700)
        lobes = np.zeros_like(self.mask)
        lobes[8:23, 2:12] = lobes[8:23, 19:29] = True
        self.assertEqual(self.describe(mask=lobes)["z_mm"], 800)

    def test_invalid_numeric_depth_is_never_serialized_as_a_coordinate(self):
        depth = self.depth.astype(float)
        depth[15, 15], depth[10, 10], depth[11, 11] = np.nan, np.inf, -10
        obj = self.describe(depth=depth)
        self.assertEqual(obj["z_mm"], 800)
        json.dumps(obj, allow_nan=False)

    def test_every_class_uses_the_same_v4_mask_depth(self):
        self.depth[14:17, 14:17] = 0
        for name in ("CUP", "CUP BASE", "CUP HANDLE"):
            with self.subTest(name=name):
                obj = self.describe(name)
                self.assertEqual(obj["z_mm"], 800)
                self.assertNotIn("depth_quality", obj)

    def test_missing_coordinates_remain_visible_but_cannot_be_selected(self):
        self.depth[10:21, 10:21] = 0
        base = dict(self.describe(), detection_id=1, bbox_xyxy=[5, 5, 25, 25])
        whole = {"class": "CUP", "detection_id": 0, "bbox_xyxy": [0, 0, 30, 30]}
        inventory = build_inventory([whole, base], capture_id="depth-test", frame={},
                                    calibration_id="calibration", gateway="offline", images={}, model={})
        self.assertEqual(len(inventory["cups"]), 1)
        self.assertFalse(inventory["cups"][0]["selectable"])
        self.assertIn("missing or invalid", inventory["cups"][0]["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
