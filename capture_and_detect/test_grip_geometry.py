"""Offline regression tests for same-observation aperture and handle geometry."""

import copy
import json
import unittest

from cup_inventory import build_inventory, hover_moves


def base(identifier=1, *, x=500, y=100, u=100, **extra):
    return {"class": "CUP BASE", "detection_id": identifier, "confidence": 0.9,
            "robot_x_mm": x, "robot_y_mm": y, "robot_z_mm": 50,
            "x_mm": 10, "y_mm": 20, "z_mm": 800, "x_px": u, "y_px": 100,
            "bbox_xyxy": [u - 3, 97, u + 3, 103], "cup_height": True,
            "diameter_mm": 65.2, **extra}


def handle(*, x=540, y=100, **extra):
    return {"class": "CUP HANDLE", "detection_id": 99, "confidence": 0.9,
            "robot_x_mm": x, "robot_y_mm": y, "robot_z_mm": 40,
            "x_mm": 50, "y_mm": 20, "z_mm": 790, "x_px": 140, "y_px": 100,
            "bbox_xyxy": [135, 95, 145, 105], **extra}


def observe(objects, *, offset=(0, 0, 0)):
    return build_inventory(objects, capture_id="same-frame", frame={"sequence": 7},
                           calibration_id="cal", gateway="http://offline.invalid/api/v1",
                           images={}, model={}, tag_offset=offset)


class GripGeometryTests(unittest.TestCase):
    def test_aperture_uses_base_diameter_and_retains_serializable_fields(self):
        whole = {**base(0, diameter_mm=150), "class": "CUP", "bbox_xyxy": [80, 80, 120, 120]}
        cup = observe([whole, base()])["cups"][0]
        self.assertEqual(cup["diameter"], 65.2)
        self.assertEqual(cup["aperture"], 135.2)
        self.assertEqual((cup["rz"], cup["has_handle"], cup["grip_geometry_error"]), (180, False, None))
        json.dumps(cup, allow_nan=False)

    def test_invalid_or_missing_diameter_preserves_full_open_fallback(self):
        for diameter in (None, 0, -1, True, "65", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(diameter=str(diameter)[:20]):
                cup = observe([base(diameter_mm=diameter)])["cups"][0]
                self.assertIsNone(cup["diameter"])
                self.assertIsNone(cup["aperture"])
                self.assertTrue(cup["selectable"])
                json.dumps(cup, allow_nan=False)

    def test_whole_cup_without_a_base_cannot_supply_aperture(self):
        cup = observe([{**base(), "class": "CUP"}])["cups"][0]
        self.assertIsNone(cup["diameter"])
        self.assertIsNone(cup["aperture"])
        self.assertFalse(cup["selectable"])

    def test_yaw_is_perpendicular_and_uses_symmetric_jaw_orientation(self):
        for dx, dy, expected in ((40, 0, 90), (0, 40, 180), (-40, 0, 90),
                                 (0, -40, 180), (40, 40, 135), (40, -40, 225)):
            with self.subTest(dx=dx, dy=dy):
                cup = observe([base(), handle(x=500 + dx, y=100 + dy)])["cups"][0]
                self.assertEqual(cup["rz"], expected)
                self.assertTrue(cup["has_handle"])
                self.assertIsNone(cup["grip_geometry_error"])

    def test_nearest_cup_owns_handle_and_limit_is_inclusive(self):
        cups = observe([base(), base(2, x=650, u=200), handle(x=590)])["cups"]
        self.assertFalse(cups[0]["has_handle"])
        self.assertTrue(cups[1]["has_handle"])
        for distance, expected in ((100, True), (100.1, False)):
            with self.subTest(distance=distance):
                cup = observe([base(), handle(x=500 + distance)])["cups"][0]
                self.assertEqual(cup["has_handle"], expected)
                self.assertIsNone(cup["grip_geometry_error"])

    def test_nearest_handle_is_selected_without_sharing_it_with_another_cup(self):
        cups = observe([base(), base(2, x=650, u=200), handle(x=520),
                        handle(x=500, y=140)])["cups"]
        self.assertEqual(cups[0]["rz"], 90)
        self.assertTrue(cups[0]["has_handle"])
        self.assertFalse(cups[1]["has_handle"])

    def test_equal_distance_cup_ownership_is_explicitly_ambiguous(self):
        cups = observe([base(), base(2, x=600, u=200), handle(x=550)])["cups"]
        for cup in cups:
            self.assertIn("multiple cup bases", cup["grip_geometry_error"])
            self.assertFalse(cup["has_handle"])
            self.assertEqual(cup["rz"], 180)
            self.assertTrue(cup["selectable"])
            self.assertEqual(len(hover_moves(cup)), 2)

    def test_equal_distance_distinct_handles_are_ambiguous(self):
        cup = observe([base(), handle(x=520), handle(x=500, y=120)])["cups"][0]
        self.assertIn("ambiguous grip direction", cup["grip_geometry_error"])
        self.assertFalse(cup["has_handle"])

    def test_repeated_identical_handle_detection_retains_one_direction(self):
        cup = observe([base(), handle(), handle(detection_id=100)])["cups"][0]
        self.assertTrue(cup["has_handle"])
        self.assertEqual(cup["rz"], 90)
        self.assertIsNone(cup["grip_geometry_error"])

    def test_zero_length_handle_direction_is_an_error_but_hover_remains_valid(self):
        cup = observe([base(), handle(x=500)])["cups"][0]
        self.assertIn("undefined", cup["grip_geometry_error"])
        self.assertFalse(cup["has_handle"])
        self.assertTrue(cup["selectable"])
        self.assertEqual(len(hover_moves(cup)), 2)

    def test_invalid_handle_coordinates_or_depth_cannot_supply_yaw(self):
        for key in ("robot_x_mm", "robot_y_mm", "robot_z_mm", "z_mm"):
            for value in (None, True, "40", float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    cup = observe([base(), handle(**{key: value})])["cups"][0]
                    self.assertFalse(cup["has_handle"])
                    self.assertEqual(cup["rz"], 180)
                    self.assertIsNone(cup["grip_geometry_error"])
        for changes in ({"z_mm": 0}, {"z_mm": -1}, {"depth_quality": {"valid": False}}):
            cup = observe([base(), handle(**changes)])["cups"][0]
            self.assertFalse(cup["has_handle"])

    def test_unreliable_base_cannot_supply_handle_geometry(self):
        cup = observe([base(depth_quality={"valid": False, "reason": "unreliable"}), handle()])["cups"][0]
        self.assertFalse(cup["selectable"])
        self.assertFalse(cup["has_handle"])
        self.assertEqual(cup["rz"], 180)

    def test_blocked_neighbour_keeps_its_handle_from_a_farther_selectable_cup(self):
        cups = observe([base(x=750), base(2, x=801, u=200), handle(x=805)])["cups"]
        self.assertTrue(cups[0]["selectable"])
        self.assertFalse(cups[1]["selectable"])
        self.assertFalse(any(cup["has_handle"] for cup in cups))

    def test_geometry_is_recomputed_per_observation_without_mutating_detections(self):
        objects = [base(rz=55, aperture=999, has_handle=True), handle(x=540, y=140)]
        unchanged = copy.deepcopy(objects)
        first = observe(objects)["cups"][0]
        second = observe([objects[0]])["cups"][0]
        translated = observe(objects, offset=(-115, 25, 40))["cups"][0]
        self.assertEqual(objects, unchanged)
        self.assertEqual((first["rz"], translated["rz"]), (135, 135))
        self.assertEqual(second["rz"], 180)
        self.assertFalse(second["has_handle"])
        self.assertEqual(second["aperture"], 135.2)


if __name__ == "__main__":
    unittest.main()
