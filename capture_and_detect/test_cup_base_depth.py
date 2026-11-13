"""Offline checks: a detected base must not inherit background through a depth hole."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from capture_and_detect import Detector
from cup_base_depth import cup_base_depth
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

    def test_uniform_depth_preserves_coordinates_and_records_quality(self):
        obj = self.describe()
        self.assertEqual((obj["x_px"], obj["y_px"], obj["z_mm"]), (15, 15, 800))
        self.assertEqual((obj["robot_x_mm"], obj["robot_y_mm"], obj["robot_z_mm"]), (500, 100, 100))
        self.assertTrue(obj["depth_quality"]["valid"])
        self.assertEqual(obj["depth_quality"]["core_valid_fraction"], 1)
        self.calibration.to_robot.assert_called_once_with(0, 0, 800)
        json.dumps(obj, allow_nan=False)

    def test_valid_background_rim_cannot_supply_missing_core_depth(self):
        self.depth[10:21, 10:21] = 0
        obj = self.describe()
        self.assertFalse(obj["depth_quality"]["valid"])
        self.assertIn("centre", obj["depth_quality"]["reason"])
        self.assertGreater(obj["depth_quality"]["valid_mask_pixels"], 0)
        self.assertEqual(obj["depth_quality"]["valid_core_pixels"], 0)
        for axis in ("x_mm", "y_mm", "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm"):
            self.assertIsNone(obj[axis])
        self.calibration.to_robot.assert_not_called()

    def test_accepted_base_uses_core_median_not_majority_background_edges(self):
        self.depth[10:21, 10:21] = 700
        self.assertEqual(float(np.median(self.depth[self.mask])), 800)
        self.assertEqual(self.describe()["z_mm"], 700)

    def test_one_bad_centre_pixel_is_tolerated_with_good_neighbourhood(self):
        self.depth[15, 15] = 0
        obj = self.describe()
        self.assertTrue(obj["depth_quality"]["valid"])
        self.assertEqual(obj["depth_quality"]["valid_center_pixels"], 8)
        self.assertEqual(obj["z_mm"], 800)

    def test_central_hole_is_not_avoided_by_a_valid_outer_core(self):
        self.depth[14:17, 14:17] = 0
        obj = self.describe()
        self.assertGreater(obj["depth_quality"]["core_valid_fraction"], .6)
        self.assertFalse(obj["depth_quality"]["valid"])
        self.assertIsNone(obj["z_mm"])

    def test_valid_centre_alone_does_not_make_sparse_core_reliable(self):
        self.depth[10:21, 10:21] = 0
        self.depth[14:17, 14:17] = 800
        obj = self.describe()
        self.assertEqual(obj["depth_quality"]["valid_center_pixels"], 9)
        self.assertIn("across its interior", obj["depth_quality"]["reason"])
        self.assertIsNone(obj["z_mm"])

    def test_invalid_float_depth_never_enters_the_core_median(self):
        depth = self.depth.astype(float)
        depth[15, 15], depth[10, 10], depth[11, 11] = np.nan, np.inf, -10
        obj = self.describe(depth=depth)
        self.assertTrue(obj["depth_quality"]["valid"])
        self.assertEqual(obj["z_mm"], 800)
        json.dumps(obj, allow_nan=False)

    def test_disconnected_lobes_cannot_replace_missing_mask_centre(self):
        mask = np.zeros_like(self.mask)
        mask[8:23, 2:12] = mask[8:23, 19:29] = True
        obj = self.describe(mask=mask)
        self.assertIn("centre", obj["depth_quality"]["reason"])
        self.assertIsNone(obj["z_mm"])
        self.calibration.to_robot.assert_not_called()

    def test_tiny_and_thin_masks_fail_closed(self):
        for shape in ((1, 1), (2, 2), (3, 3), (4, 4), (1, 25), (25, 2)):
            with self.subTest(shape=shape):
                z, quality = cup_base_depth(np.ones(shape, dtype=bool), np.full(shape, 700))
                self.assertIsNone(z)
                self.assertFalse(quality["valid"])
                self.assertTrue(quality["reason"])

    def test_small_mask_with_nine_pixel_core_is_supported(self):
        z, quality = cup_base_depth(np.ones((5, 5), dtype=bool), np.full((5, 5), 700))
        self.assertEqual(z, 700)
        self.assertEqual(quality["core_pixels"], 9)
        self.assertTrue(quality["valid"])

    def test_empty_or_misaligned_input_cannot_create_depth(self):
        z, quality = cup_base_depth(np.zeros((5, 5), dtype=bool), np.ones((5, 5)))
        self.assertIsNone(z)
        self.assertFalse(quality["valid"])
        with self.assertRaises(ValueError):
            cup_base_depth(self.mask, self.depth[:-1])

    def test_other_classes_use_upstream_eroded_mask_depth(self):
        self.depth[10:21, 10:21] = 0
        for name in ("CUP", "CUP HANDLE"):
            with self.subTest(name=name):
                obj = self.describe(name)
                self.assertIsNone(obj["z_mm"])
                self.assertNotIn("depth_quality", obj)

    def test_bad_base_stays_visible_but_cannot_be_selected(self):
        self.depth[10:21, 10:21] = 0
        base = dict(self.describe(), detection_id=1, bbox_xyxy=[5, 5, 25, 25])
        cup = {"class": "CUP", "detection_id": 0, "bbox_xyxy": [0, 0, 30, 30]}
        inventory = build_inventory([cup, base], capture_id="depth-test", frame={},
                                    calibration_id="calibration", gateway="offline", images={}, model={})
        self.assertEqual(len(inventory["cups"]), 1)
        observed = inventory["cups"][0]
        self.assertFalse(observed["selectable"])
        self.assertEqual(observed["blocked_reason"], "CUP BASE has insufficient valid depth at its centre.")
        self.assertEqual(observed["depth_quality"], base["depth_quality"])
        self.assertEqual(observed["pixel"], {"u": 15, "v": 15})


if __name__ == "__main__":
    unittest.main()
