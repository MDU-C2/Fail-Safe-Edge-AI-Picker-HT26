"""Offline checks of fixed-height picking and handover ordering/geometry."""

import copy
import unittest
from unittest.mock import patch

from cup_inventory import FLANGE_MAX, TRAVEL_Z, Z_MIN, SelectionError, hover_moves
from handover_plan import (
    CUP_Z_MIN, CUP_Z_MAX, DEFAULT_SPEED, HOME_SPEED, PICK_SPEED, PICK_Z,
    FLIP_ACCELERATION, FLIP_SPEED, PLACE_ABOVE,
    handover_jobs, motion_speeds, pick_position_problem,
)


class HandoverPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def plan(self, **kwargs):
        kwargs.setdefault("place", self.place)
        return handover_jobs(self.cup, **kwargs)

    def test_explicit_interlocks_require_holding_and_clearing_before_releasing(self):
        jobs = self.plan()
        names = [job["name"] for job in jobs]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[:9], ["right_close_travel", "right_home_start", "left_close_travel",
                                    "left_ready", "approach_cup", "left_open",
                                    "descend_to_cup", "grip_cup", "lift_cup"])
        by_name = {job["name"]: job for job in jobs}
        for first, second in (("right_home_start", "left_close_travel"),
                              ("lift_cup", "left_handover"), ("lift_cup", "right_via"),
                              ("approach_cup", "left_open"), ("left_open", "descend_to_cup"),
                              ("right_beside", "right_open"), ("right_open", "right_below"),
                              ("left_handover", "right_open"),
                              ("right_below", "right_take"), ("right_take", "right_grip"),
                              ("right_grip", "left_release"), ("left_release", "left_clear"),
                              ("left_clear", "right_lower"), ("right_lower", "right_turn_half"),
                              ("right_turn_half", "right_retreat"),
                              ("right_retreat", "left_close_return"), ("left_close_return", "left_home_lateral"),
                              ("left_home_lateral", "left_home"),
                              ("right_place", "right_release"),
                              ("right_release", "right_clear"), ("right_clear", "right_close_return"),
                              ("right_close_return", "right_home")):
            with self.subTest(first=first, second=second):
                self.assertIn(first, by_name[second]["after"])
        self.assertEqual(names[-1], "right_home")
        self.assertEqual(names[names.index("left_home") - 1], "left_home_lateral")
        self.assertEqual(len(names), 29)
        self.assertTrue(all(job["body"]["wait"] is True for job in jobs))
        for name, arm, action in (("left_open", "left", "open"), ("grip_cup", "left", "grip"),
                                  ("right_open", "right", "open"), ("right_grip", "right", "grip"),
                                  ("left_release", "left", "open"), ("right_release", "right", "open")):
            self.assertEqual(by_name[name]["endpoint"], "/robot/rws/gripper")
            self.assertEqual(by_name[name]["body"], {"arm": arm, "action": action, "wait": True})

    def test_original_tuned_flange_poses_and_orientations_are_preserved(self):
        bodies = {job["name"]: job["body"] for job in self.plan()}
        expected = {
            "left_ready": ("left", (400, 100, 450), (180, 0, 180)),
            "left_handover": ("left", (350, -100, 655), (180, 0, 180)),
            "left_clear": ("left", (350, -100, 690), (180, 0, 180)),
            "left_home_lateral": ("left", (350, 400, 690), (180, 0, 180)),
            "right_via": ("right", (300, -300, 250), (0, 0, 0)),
            "right_beside": ("right", (330, -235, 281), (0, 0, 0)),
            "right_below": ("right", (330, -115, 281), (0, 0, 0)),
            "right_take": ("right", (330, -115, 316), (0, 0, 0)),
            "right_lower": ("right", (330, -115, 281), (0, 0, 0)),
            "right_turn_half": ("right", (375, -157.5, 295.5), (90, 0, 0)),
            "right_retreat": ("right", (420, -200, 310), (180, 0, 0)),
            "right_place": ("right", (420, -200, 230), (180, 0, 0)),
            "right_clear": ("right", (420, -200, 310), (180, 0, 0)),
        }
        for name, (arm, xyz, rotation) in expected.items():
            with self.subTest(name=name):
                body = bodies[name]
                self.assertEqual(body["arm"], arm)
                self.assertEqual(tuple(body[a] for a in ("x", "y", "z")), xyz)
                self.assertEqual(tuple(body[a] for a in ("rx", "ry", "rz")), rotation)

    def test_calibration_xy_is_applied_once_only_to_cup_moves_and_z_is_fixed(self):
        original = copy.deepcopy(self.cup)
        baseline = {job["name"]: job for job in self.plan()}
        for z_offset in (-1000, 0, 40, 1000):
            with self.subTest(z_offset=z_offset):
                jobs = self.plan(tag_offset=(-115, 25, z_offset))
                for job in jobs:
                    name, body = job["name"], job["body"]
                    if name in ("approach_cup", "descend_to_cup", "lift_cup"):
                        self.assertEqual((body["x"], body["y"]), (385, 125))
                        self.assertEqual(body["z"], 229 if name == "descend_to_cup" else 334)
                        self.assertEqual(tuple(body[a] for a in ("rx", "ry", "rz")), (180, 0, 180))
                    else:
                        self.assertEqual(job, baseline[name])
        self.assertEqual(self.cup, original)

    def test_explicit_selected_speed_caps_home_and_turning(self):
        for speed in (1, 50, 120, 1500):
            for job in self.plan(speed=speed):
                with self.subTest(speed=speed, job=job["name"]):
                    self.assertIn(job["endpoint"], ("/robot/rws/move", "/robot/rws/home", "/robot/rws/gripper"))
                    self.assertEqual(set(job), {"name", "endpoint", "body", "after"})
                    body = job["body"]
                    if job["endpoint"].endswith(("/move", "/home")):
                        expected = ((min(speed, FLIP_SPEED), FLIP_ACCELERATION, 100)
                                    if job["name"] in ("right_turn_half", "right_retreat")
                                    else (min(speed, PICK_SPEED), 100, 100)
                                    if job["name"] in ("descend_to_cup", "lift_cup", "left_handover")
                                    else (min(speed, HOME_SPEED), 100, 100)
                                    if job["endpoint"].endswith("/home")
                                    else (speed, 100, 100))
                        self.assertEqual((body["speed"], body["acceleration"], body["ramp"]), expected)
                    if job["endpoint"].endswith("/home"):
                        self.assertEqual(set(body), {"arm", "wait", "speed", "acceleration", "ramp"})

    def test_default_and_explicit_home_speed_resolution(self):
        self.assertEqual(motion_speeds(), (250, 200))
        self.assertEqual(motion_speeds(400), (400, 200))
        self.assertEqual(motion_speeds(25), (25, 25))
        self.assertEqual(motion_speeds(1500), (1500, 200))
        self.assertEqual(motion_speeds(home_speed=75), (250, 75))
        self.assertEqual(motion_speeds(400, 200), (400, 200))
        default = self.plan()
        restored = self.plan(speed=250, home_speed=200)
        self.assertEqual(default, restored)
        for job in self.plan(speed=400, home_speed=75):
            if job["endpoint"].endswith("/home"):
                self.assertEqual(job["body"]["speed"], 75)

    def test_invalid_home_speed_never_returns_a_plan(self):
        for home_speed in (0, 200.1, float("nan"), float("inf"), True, "50", 10 ** 1000):
            with self.subTest(home_speed=home_speed), self.assertRaises(SelectionError):
                self.plan(home_speed=home_speed)
        with self.assertRaises(SelectionError):
            self.plan(speed=25, home_speed=200)

    def test_outside_existing_saved_or_corrected_workspace_is_blocked(self):
        for cup, offset in ((dict(self.cup, y=-627.1), (0, 0, 0)),
                            (dict(self.cup, y=708.2), (0, 0, 0)),
                            (dict(self.cup, x=349), (0, 0, 0)),
                            (dict(self.cup, x=801), (0, 0, 0)),
                            (self.cup, (-321, 0, 0)), (self.cup, (0, 401, 0))):
            with self.subTest(cup=cup, offset=offset), self.assertRaisesRegex(SelectionError, "workspace"):
                handover_jobs(cup, place=self.place, tag_offset=offset)

    def test_every_fixed_move_target_is_checked_before_any_plan_is_returned(self):
        for constant, value in (("HANDOVER_L", (400, -100, 701)),
                                ("LEFT_LIFT", 52), ("R_VIA", (179, -250, 200)),
                                ("PLACE_ABOVE", 471),
                                ("PICK_Z", -110)):
            with self.subTest(constant=constant), patch("handover_plan." + constant, value):
                with self.assertRaisesRegex(SelectionError, "workspace"):
                    self.plan()

    def test_ordinary_targets_require_minimum_flange_height(self):
        for constant in ("R_VIA", "LEFT_READY", "HANDOVER_L"):
            with self.subTest(constant=constant), patch("handover_plan." + constant, (400, 100, 198)):
                with self.assertRaisesRegex(SelectionError, "clearance of 199"):
                    self.plan()
        with patch("handover_plan.R_TAKE", (330, -115, 233)):
            with self.assertRaisesRegex(SelectionError, "right_beside.*clearance of 199"):
                self.plan()
        for offset in ((-20, -20, -20), (20, 20, 20)):
            for job in self.plan(handover_offset=offset):
                if job["endpoint"].endswith("/move"):
                    self.assertGreaterEqual(job["body"]["z"], Z_MIN)
                    self.assertLessEqual(job["body"]["z"], FLANGE_MAX[2])

    def test_pick_and_place_may_descend_below_clearance_but_not_gateway_floor(self):
        with patch("handover_plan.PICK_Z", -59):
            bodies = {job["name"]: job["body"] for job in self.plan(place=dict(self.place, z=119))}
        self.assertEqual(bodies["descend_to_cup"]["z"], 100)
        self.assertEqual(bodies["right_place"]["z"], 119)
        self.assertEqual(bodies["right_clear"]["z"], 199)
        with patch("handover_plan.PICK_Z", -110), self.assertRaisesRegex(SelectionError, "workspace"):
            self.plan()
        with self.assertRaisesRegex(SelectionError, "clearance"):
            self.plan(place=dict(self.place, z=118))
        self.plan(place=dict(self.place, z=620))
        with self.assertRaisesRegex(SelectionError, "workspace"):
            self.plan(place=dict(self.place, z=621))

    def test_fixed_height_pick_requires_table_height_without_changing_hover_rules(self):
        for height in (-55, -30.1, 150.1, 174.9):
            cup = dict(self.cup, z=height)
            with self.subTest(height=height):
                self.assertIsNotNone(pick_position_problem(cup))
                with self.assertRaisesRegex(SelectionError, "-30..150"):
                    handover_jobs(cup, place=self.place)
                self.assertEqual(len(hover_moves(cup)), 2)
        for height in (-30, -15, 0, 50, 150):
            self.assertIsNone(pick_position_problem(dict(self.cup, z=height)))
            self.assertEqual(len(handover_jobs(dict(self.cup, z=height), place=self.place)), 29)

    def test_ordinary_clearance_applies_except_to_explicit_pick_and_place(self):
        with patch("handover_plan.R_VIA", (300, -300, 198.9)):
            with self.assertRaisesRegex(SelectionError, "ordinary motion clearance"):
                self.plan()
        with patch("handover_plan.PICK_Z", 30):
            pick = next(job for job in self.plan() if job["name"] == "descend_to_cup")
            self.assertEqual(pick["body"]["z"], 189)
        jobs = self.plan(place=dict(self.place, z=119))
        by_name = {job["name"]: job["body"] for job in jobs}
        self.assertEqual(by_name["right_place"]["z"], 119)
        self.assertEqual(by_name["right_clear"]["z"], 199)
        with self.assertRaisesRegex(SelectionError, "ordinary motion clearance"):
            self.plan(place=dict(self.place, z=118.9))

    def test_invalid_inputs_or_blocked_selection_never_produce_a_plan(self):
        for axis in ("x", "y", "z"):
            for value in (None, float("nan"), float("inf"), True, "50"):
                with self.subTest(axis=axis, value=value), self.assertRaises(SelectionError):
                    handover_jobs(dict(self.cup, **{axis: value}), place=self.place)
        for offset in (None, (), (0, 0), (0, 0, float("nan")), (True, 0, 0)):
            with self.subTest(offset=offset), self.assertRaises(SelectionError):
                self.plan(tag_offset=offset)
        for speed in (0, 1501, float("nan"), float("inf"), True, "50", 10 ** 1000):
            with self.subTest(speed=speed), self.assertRaises(SelectionError):
                self.plan(speed=speed)
        for cup in (dict(self.cup, selectable=False), dict(self.cup, blocked_reason="ambiguous base")):
            with self.subTest(cup=cup), self.assertRaises(SelectionError):
                handover_jobs(cup, place=self.place)

    def test_place_is_required_and_controls_only_placing_targets(self):
        with self.assertRaises(TypeError):
            handover_jobs(self.cup)
        alternative = dict(self.place, id=10, x=480, y=-360)
        original = copy.deepcopy(alternative)
        jobs = {job["name"]: job for job in self.plan(place=alternative)}
        baseline = {job["name"]: job for job in self.plan()}
        expected = {"right_turn_half": (405, -237.5, 295.5),
                    "right_retreat": (480, -360, 310),
                    "right_place": (480, -360, 230),
                    "right_clear": (480, -360, 310)}
        for name, job in jobs.items():
            if name in expected:
                self.assertEqual(tuple(job["body"][axis] for axis in ("x", "y", "z")), expected[name])
            else:
                self.assertEqual(job, baseline[name])
        self.assertEqual(alternative, original)

    def test_invalid_place_never_returns_a_plan(self):
        for place in (None, (), {}, dict(self.place, x=float("nan")),
                      dict(self.place, y=501), dict(self.place, z=621)):
            with self.subTest(place=place), self.assertRaises(SelectionError):
                self.plan(place=place)

    def test_canonical_entry_uses_shared_motion_configuration(self):
        # The canonical entrypoint imports shared values; explicit pose and speed
        # checks above remain independent of that wiring.
        import capture_and_detect as entry
        self.assertEqual(entry.DEFAULT_SPEED, DEFAULT_SPEED)
        self.assertEqual(entry.HOME_SPEED, HOME_SPEED)
        self.assertEqual(entry.FLIP_SPEED, FLIP_SPEED)
        self.assertEqual((entry.CUP_Z_MIN, entry.CUP_Z_MAX), (CUP_Z_MIN, CUP_Z_MAX))
        self.assertTrue(callable(entry.main))


if __name__ == "__main__":
    unittest.main()
