"""Offline tests of snapshot grouping and the calibrated two-step RWS hover plan."""

import copy
import json
import unittest

from cup_inventory import (
    GRIPPER_TCP_Z, LEFT_READY, TRAVEL_Z, SelectionError, build_inventory,
    hover_moves, match_observation, position_problem, selected_cup,
)


def detection(kind, u, detection_id, *, x=500, y=100, z=50, **flags):
    radius = 15 if kind == "CUP" else 3
    return {"class": kind, "detection_id": detection_id, "confidence": 0.9,
            "x_px": u, "y_px": 100, "bbox_xyxy": [u - radius, 80, u + radius, 120],
            "robot_x_mm": x, "robot_y_mm": y, "robot_z_mm": z,
            "x_mm": 10, "y_mm": 20, "z_mm": 800 if z is not None else None, **flags}


def snapshot(objects=None, offset=(0, 0, 0)):
    if objects is None:
        objects = [detection("CUP", 100, 0), detection("CUP BASE", 100, 1, in_cup=True)]
    return build_inventory(objects, capture_id="scan", frame={"sequence": 1},
                           calibration_id="calibration", gateway="http://offline.invalid/api/v1",
                           images={}, model={}, tag_offset=offset)


class CupInventoryTests(unittest.TestCase):
    def test_current_calibration_produces_only_ready_then_corrected_hover(self):
        result = snapshot(offset=(-115, 0, 40))
        cup = selected_cup(result, "scan", 1)
        moves = hover_moves(cup, tag_offset=(-115, 0, 40))
        self.assertEqual(len(moves), 2)
        self.assertEqual((moves[0]["x"], moves[0]["y"], moves[0]["z"]), LEFT_READY)
        self.assertEqual((moves[1]["x"], moves[1]["y"], moves[1]["z"]), (385, 100, 409))
        for move in moves:
            self.assertEqual(set(move), {"x", "y", "z", "rx", "ry", "rz", "arm", "speed",
                                         "acceleration", "ramp", "wait"})
            self.assertEqual((move["rx"], move["ry"], move["rz"]), (180, 0, 180))
            self.assertEqual((move["arm"], move["speed"], move["acceleration"], move["ramp"], move["wait"]),
                             ("left", 50, 100, 100, True))
        self.assertEqual((cup["x"], cup["y"], cup["z"]), (500, 100, 50))

    def test_inventory_explains_original_coordinates_offset_and_motion(self):
        result = snapshot(offset=(-115, 0, 40))
        cup = result["cups"][0]
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["tag_offset_mm"], [-115, 0, 40])
        self.assertEqual(cup["camera_mm"], {"x": 10, "y": 20, "z": 800})
        self.assertEqual(cup["robot_mm"], {"x": 500, "y": 100, "z": 50})
        self.assertEqual(cup["hover_target"]["tcp_mm"], {"x": 385, "y": 100, "z": TRAVEL_Z})
        self.assertEqual(cup["hover_target"]["flange_mm"], {"x": 385, "y": 100, "z": TRAVEL_Z + GRIPPER_TCP_Z})
        self.assertEqual(cup["hover_target"]["tag_offset_applied_axes"], ["x", "y"])
        self.assertFalse(result["hover_motion"]["tag_offset_z_applied"])
        self.assertEqual(result["hover_motion"]["endpoint"], "/robot/rws/move")
        json.dumps(result, allow_nan=False)

    def test_xy_offset_applied_once_and_z_offset_deliberately_ignored(self):
        cup = snapshot()["cups"][0]
        for oz in (-1000, 0, 40, 1000):
            with self.subTest(oz=oz):
                moves = hover_moves(cup, tag_offset=(-115, 25, oz), speed=100)
                self.assertEqual((moves[1]["x"], moves[1]["y"], moves[1]["z"]), (385, 125, 409))
                self.assertEqual(moves[0]["z"], 450)
                self.assertEqual(moves[1]["speed"], 100)

    def test_corrected_flange_bounds_block_the_entire_plan(self):
        cases = [(350, 100, (-171, 0, 0)), (800, 100, (1, 0, 0)),
                 (500, -230, (0, -171, 0)), (500, 420, (0, 81, 0))]
        for x, y, offset in cases:
            with self.subTest(x=x, y=y, offset=offset):
                cup = {"x": x, "y": y, "z": 50}
                with self.assertRaisesRegex(SelectionError, "corrected flange"):
                    hover_moves(cup, tag_offset=offset)
                result = snapshot([detection("CUP BASE", 100, 0, x=x, y=y, cup_height=True)], offset)
                self.assertFalse(result["cups"][0]["selectable"])
                self.assertIsNone(result["cups"][0]["hover_target"])
                with self.assertRaises(SelectionError):
                    selected_cup(result, "scan", 1)

    def test_gateway_boundary_is_inclusive_but_original_scene_bounds_still_apply(self):
        for x, y, offset in [(350, -230, (-170, -170, 0)), (800, 420, (0, 80, 0))]:
            with self.subTest(x=x, y=y):
                self.assertEqual(len(hover_moves({"x": x, "y": y, "z": 50}, tag_offset=offset)), 2)
        for x, y in [(349, 100), (801, 100), (500, -231), (500, 421)]:
            with self.subTest(x=x, y=y):
                with self.assertRaisesRegex(SelectionError, "configured XY"):
                    hover_moves({"x": x, "y": y, "z": 50})

    def test_base_at_hover_height_or_above_is_rejected_but_negative_z_is_allowed(self):
        for z in (250, 251, 1000):
            with self.subTest(z=z):
                with self.assertRaisesRegex(SelectionError, "at or above"):
                    hover_moves({"x": 500, "y": 100, "z": z})
        for z in (-2.8, 0, 249.9):
            self.assertEqual(hover_moves({"x": 500, "y": 100, "z": z})[1]["z"], 409)

    def test_nonfinite_or_invalid_coordinates_offsets_and_speed_never_produce_plan(self):
        cup = {"x": 500, "y": 100, "z": 50}
        for axis in ("x", "y", "z"):
            for value in (None, float("nan"), float("inf"), True, "500"):
                with self.subTest(axis=axis, value=value):
                    with self.assertRaises(SelectionError):
                        hover_moves(dict(cup, **{axis: value}))
        for offset in (None, (), (1, 2), (1, 2, 3, 4), (True, 0, 0), (0, 0, float("nan")),
                       (float("inf"), 0, 0), (0, "0", 0)):
            with self.subTest(offset=offset):
                with self.assertRaises(SelectionError):
                    hover_moves(cup, tag_offset=offset)
                with self.assertRaises(SelectionError):
                    snapshot(offset=offset)
        for speed in (0, 1501, float("nan"), float("inf"), True, "50"):
            with self.subTest(speed=speed):
                with self.assertRaises(SelectionError):
                    hover_moves(cup, speed=speed)
        for speed in (1, 1500):
            self.assertEqual(hover_moves(cup, speed=speed)[0]["speed"], speed)

    def test_orphan_base_accepts_each_literal_support_flag(self):
        for flag in ("in_cup", "near_handle", "cup_height"):
            with self.subTest(flag=flag):
                result = snapshot([detection("CUP BASE", 100, 0, **{flag: True})])
                self.assertTrue(result["cups"][0]["selectable"])
                self.assertEqual(result["cups"][0]["acceptance_reasons"], [flag])
        result = snapshot([detection("CUP BASE", 100, 0, near_handle=True, cup_height=True)])
        self.assertEqual(result["cups"][0]["acceptance_reasons"], ["near_handle", "cup_height"])

    def test_truthy_legacy_or_nonboolean_flags_do_not_authorize_orphan_base(self):
        for flags in ({"is_cup": True}, {"in_cup": 1}, {"near_handle": "true"},
                      {"cup_height": [True]}, {"in_cup": False}, {}):
            with self.subTest(flags=flags):
                cup = snapshot([detection("CUP BASE", 100, 0, **flags)])["cups"][0]
                self.assertFalse(cup["selectable"])
                self.assertEqual(cup["acceptance_reasons"], [])

    def test_left_to_right_ids_include_blocked_cups_and_supported_orphans(self):
        objects = [detection("CUP BASE", 300, 0, cup_height=True),
                   detection("CUP", 200, 1), detection("CUP BASE", 100, 2, near_handle=True)]
        cups = snapshot(objects)["cups"]
        self.assertEqual([(c["id"], c["pixel"]["u"]) for c in cups], [(1, 100), (2, 200), (3, 300)])
        self.assertEqual([c["selectable"] for c in cups], [True, False, True])
        self.assertIn("no CUP BASE", cups[1]["blocked_reason"])

    def test_contained_base_is_not_also_saved_as_an_orphan(self):
        result = snapshot([detection("CUP", 100, 0),
                           detection("CUP BASE", 100, 1, near_handle=True, cup_height=True)])
        self.assertEqual(len(result["cups"]), 1)
        cup = result["cups"][0]
        self.assertEqual(cup["base_detection_id"], 1)
        self.assertEqual(cup["acceptance_reasons"], ["in_cup", "near_handle", "cup_height"])

    def test_multiple_bases_overlap_and_duplicate_bases_remain_blocked(self):
        first = detection("CUP BASE", 100, 1, cup_height=True)
        cases = [
            ([detection("CUP", 100, 0), first, detection("CUP BASE", 102, 2)], "multiple"),
            ([detection("CUP", 95, 0), detection("CUP", 105, 2), first], "overlapping"),
            ([first, dict(first, detection_id=2)], "duplicate"),
            ([first, detection("CUP BASE", 200, 1, cup_height=True)], "duplicate"),
            ([first, first], "duplicate"),
        ]
        for objects, reason in cases:
            with self.subTest(reason=reason):
                cups = snapshot(objects)["cups"]
                self.assertTrue(all(not cup["selectable"] for cup in cups))
                self.assertTrue(all(reason in cup["blocked_reason"] for cup in cups))

    def test_invalid_depth_stays_visible_and_serializable_but_blocked(self):
        for z in (None, float("nan"), float("inf")):
            with self.subTest(z=z):
                result = snapshot([detection("CUP BASE", 100, 0, z=z, near_handle=True)])
                self.assertEqual(len(result["cups"]), 1)
                cup = result["cups"][0]
                self.assertIsNone(cup["z"])
                self.assertIsNone(cup["robot_mm"]["z"])
                self.assertFalse(cup["selectable"])
                self.assertIn("missing or invalid", cup["blocked_reason"])
                json.dumps(result, allow_nan=False)

    def test_fresh_match_checks_corrected_bounds_again(self):
        original = snapshot()["cups"][0]
        fresh = copy.deepcopy(original)
        self.assertIs(match_observation(original, [fresh], 20, 40, tag_offset=(-115, 0, 40)), fresh)
        with self.assertRaisesRegex(SelectionError, "corrected flange"):
            match_observation(original, [fresh], 20, 40, tag_offset=(-400, 0, 40))
        self.assertIsNotNone(position_problem(original, (0, 0, float("nan"))))


if __name__ == "__main__":
    unittest.main()
