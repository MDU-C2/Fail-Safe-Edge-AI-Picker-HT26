"""Offline checks of fixed-height picking and handover ordering/geometry."""

import copy
import unittest
from unittest.mock import patch

from cup_inventory import SelectionError
from handover_plan import (
    CUP_Z_MIN, CUP_Z_MAX, DEFAULT_SPEED, HOME_SPEED, FLIP_SPEED,
    handover_jobs, motion_speeds, pick_position_problem,
)

class HandoverPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def plan(self, **kwargs):
        kwargs.setdefault("place", self.place)
        return handover_jobs(self.cup, **kwargs)

    def test_grip_and_release_boundaries_preserve_cross_arm_interlocks(self):
        jobs = self.plan()
        by_name = {job["name"]: job for job in jobs}
        self.assertEqual(len(jobs), len(by_name))
        for first, second in (("right_open_start", "right_via"),
                              ("left_open", "approach_cup"),
                              ("approach_cup", "grip_cup"),
                              ("grip_cup", "left_handover"), ("right_via", "left_handover"),
                              ("grip_cup", "right_beside"), ("right_via", "right_beside"),
                              ("left_handover", "right_open"), ("right_beside", "right_open"),
                              ("right_open", "right_take"), ("right_take", "right_grip"),
                              ("right_grip", "left_release"),
                              ("left_release", "left_clear"), ("left_release", "right_lower"),
                              ("left_clear", "left_park"),
                              ("right_retreat", "left_park"),
                              ("left_clear", "right_retreat"), ("right_lower", "right_retreat"),
                              ("right_retreat", "right_place"), ("right_place", "right_release"),
                              ("right_release", "right_clear")):
            with self.subTest(first=first, second=second):
                self.assertIn(first, by_name[second]["after"])
        self.assertTrue(all(job["body"]["wait"] is True for job in jobs))
        for name, arm, action in (("right_open_start", "right", "open"),
                                  ("left_open", "left", "open"), ("grip_cup", "left", "grip"),
                                  ("right_open", "right", "open"), ("right_grip", "right", "grip"),
                                  ("left_release", "left", "open"), ("right_release", "right", "open")):
            self.assertEqual(by_name[name]["endpoint"], "/robot/rws/gripper")
            self.assertEqual(by_name[name]["body"], {"arm": arm, "action": action, "wait": True})
        self.assertEqual([job["name"] for job in jobs if job["body"].get("action") == "grip"],
                         ["grip_cup", "right_grip"])
        self.assertEqual(jobs[-1]["name"], "right_clear")

    def test_smooth_branch_geometry_and_local_linear_segments(self):
        # Literal flange targets from the upstream blended approach/parking
        # plan, rather than imported constants: this catches drift in tuning.
        expected = {
            "approach_cup": [(400, 100, 450, 180, 0, 180, False, 10),
                             (500, 100, 334, 180, 0, 180, False, 10),
                             (500, 100, 229, 180, 0, 180, True, 0)],
            "left_handover": [(500, 100, 334, 180, 0, 180, True, 10),
                              (350, -100, 655, 180, 0, 180, False, 0)],
            "left_clear": [(350, -100, 690, 180, 0, 180, True, 0)],
            "left_park": [(350, 400, 690, 180, 0, 180, False, 0)],
            "right_via": [(300, -300, 250, 0, 0, 0, False, 0)],
            "right_beside": [(330, -235, 281, 0, 0, 0, False, 0)],
            "right_take": [(330, -115, 281, 0, 0, 0, True, 3),
                           (330, -115, 316, 0, 0, 0, True, 0)],
            "right_lower": [(330, -115, 281, 0, 0, 0, True, 0)],
            "right_retreat": [(375, -157.5, 295.5, 90, 0, 0, False, 10),
                              (420, -200, 310, 180, 0, 0, False, 0)],
            "right_place": [(420, -200, 230, 180, 0, 0, True, 0)],
            "right_clear": [(420, -200, 310, 180, 0, 0, True, 0)],
        }
        paths = {job["name"]: job for job in self.plan() if job["endpoint"].endswith("/path")}
        self.assertEqual(set(paths), set(expected))
        for name, points in expected.items():
            with self.subTest(name=name):
                body = paths[name]["body"]
                self.assertEqual(body["arm"], "left" if name.startswith("left") or name == "approach_cup" else "right")
                self.assertEqual([tuple(point[key] for key in ("x", "y", "z", "rx", "ry", "rz", "linear", "zone"))
                                  for point in body["points"]], points)
        self.assertFalse(any(job["endpoint"].endswith("/move") for job in self.plan()))

    def test_calibration_xy_is_applied_once_only_to_cup_points_and_z_is_fixed(self):
        original = copy.deepcopy(self.cup)
        baseline = {job["name"]: job for job in self.plan()}
        for z_offset in (-1000, 0, 40, 1000):
            with self.subTest(z_offset=z_offset):
                jobs = self.plan(tag_offset=(-115, 25, z_offset))
                for job in jobs:
                    name = job["name"]
                    initial = baseline[name]
                    changed = copy.deepcopy(job)
                    if name in ("approach_cup", "left_handover"):
                        # The first approach point is the fixed ready pose.
                        indices = (1, 2) if name == "approach_cup" else (0,)
                        for index in indices:
                            point = changed["body"]["points"][index]
                            self.assertEqual((point["x"], point["y"]), (385, 125))
                            self.assertEqual(point["z"], 229 if name == "approach_cup" and index == 2 else 334)
                            point["x"], point["y"] = 500, 100
                    self.assertEqual(changed, initial)
        self.assertEqual(self.cup, original)

    def test_ui_yaw_aperture_and_camera_height_only_adjust_selected_cup(self):
        cup = dict(self.cup, rz=145, aperture=100, z=75)
        jobs = {job["name"]: job for job in handover_jobs(cup, place=self.place, camera_z=True)}
        approach = jobs["approach_cup"]["body"]["points"]
        self.assertEqual([p["rz"] for p in approach], [180, 145, 145])
        self.assertEqual([p["z"] for p in approach], [450, 334, 254])
        lift = jobs["left_handover"]["body"]["points"]
        self.assertEqual([p["rz"] for p in lift], [145, 180])
        self.assertEqual(jobs["left_open"]["body"],
                         {"arm": "left", "action": "move", "position": 12.5, "wait": True})

    def test_loaded_paths_use_smooth_acceleration_and_turnover_reduces_it_further(self):
        carrying = {"left_handover", "right_lower", "right_retreat", "right_place"}
        for job in self.plan():
            if not job["endpoint"].endswith("/path"):
                continue
            name, body = job["name"], job["body"]
            with self.subTest(name=name):
                self.assertEqual(body["acceleration"], 30 if name == "right_retreat" else 60)
                self.assertEqual(body["ramp"], 40)
                if name in carrying:
                    self.assertEqual(body["max_acceleration"], 1.5)
                else:
                    self.assertNotIn("max_acceleration", body)
                self.assertEqual(body["points"][-1]["zone"], 0)

    def test_explicit_selected_speed_caps_every_motion_and_waypoint(self):
        for speed in (1, 50, 120, 1500):
            for job in self.plan(speed=speed, home_after_cup=True):
                with self.subTest(speed=speed, job=job["name"]):
                    self.assertIn(job["endpoint"], ("/robot/rws/move", "/robot/rws/path",
                                                    "/robot/rws/home", "/robot/rws/gripper"))
                    self.assertEqual(set(job), {"name", "endpoint", "body", "after"})
                    body = job["body"]
                    if "speed" in body:
                        self.assertLessEqual(body["speed"], speed)
                    for point in body.get("points", []):
                        self.assertLessEqual(point["speed"], speed)
                    if job["endpoint"].endswith("/home"):
                        self.assertLessEqual(body["speed"], HOME_SPEED)

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
        for job in self.plan(speed=400, home_speed=75, home_after_cup=True):
            if job["endpoint"].endswith("/home"):
                self.assertEqual(job["body"]["speed"], 75)

    def test_invalid_home_speed_never_returns_a_plan(self):
        for home_speed in (0, 200.1, float("nan"), float("inf"), True, "50", 10 ** 1000):
            with self.subTest(home_speed=home_speed), self.assertRaises(SelectionError):
                self.plan(home_speed=home_speed)
        with self.assertRaises(SelectionError):
            self.plan(speed=25, home_speed=200)

    def test_outside_v4_source_xy_workspace_is_blocked(self):
        for cup, offset in ((dict(self.cup, y=-627.1), (0, 0, 0)),
                            (dict(self.cup, y=708.2), (0, 0, 0)),
                            (dict(self.cup, x=349), (0, 0, 0)),
                            (dict(self.cup, x=801), (0, 0, 0))):
            with self.subTest(cup=cup, offset=offset), self.assertRaisesRegex(SelectionError, "workspace"):
                handover_jobs(cup, place=self.place, tag_offset=offset)

    def test_source_xy_is_preserved_without_extra_height_or_grip_gates(self):
        for height in (-55, -30.1, 150.1, 300):
            cup = dict(self.cup, z=height, grip_geometry_error="old ambiguity", rz=45, aperture=0,
                       selectable=False, blocked_reason="old verification result")
            self.assertIsNone(pick_position_problem(cup))
            jobs = handover_jobs(cup, place=self.place)
            descent = next(job["body"]["points"][-1] for job in jobs if job["name"] == "approach_cup")
            self.assertEqual((descent["z"], descent["rz"]), (229, 45))
        for x, y in ((349, 100), (801, 100), (500, -231), (500, 421)):
            with self.assertRaises(SelectionError):
                handover_jobs(dict(self.cup, x=x, y=y), place=self.place)

    def test_plan_does_not_apply_extra_workspace_or_blend_envelope_filters(self):
        with patch("handover_plan.R_VIA", (179, -300, 205)):
            body = next(job["body"] for job in self.plan(tag_offset=(400, 0, 0))
                        if job["name"] == "right_via")
        self.assertEqual((body["points"][0]["x"], body["points"][0]["z"]), (179, 205))
        self.assertEqual(body["points"][0]["zone"], 0)
        # V4 checks a move's height when that move is dispatched.
        with patch("handover_plan.R_VIA", (300, -300, 701)):
            self.assertEqual(next(job["body"]["points"][0]["z"] for job in self.plan()
                                  if job["name"] == "right_via"), 701)

    def test_invalid_numeric_inputs_never_produce_a_plan(self):
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

    def test_place_is_required_and_controls_only_placing_targets(self):
        with self.assertRaises(TypeError):
            handover_jobs(self.cup)
        alternative = dict(self.place, id=10, x=480, y=-360)
        original = copy.deepcopy(alternative)
        jobs = {job["name"]: job for job in self.plan(place=alternative)}
        baseline = {job["name"]: job for job in self.plan()}
        expected = {"right_retreat": (480, -360, 310),
                    "right_place": (480, -360, 230),
                    "right_clear": (480, -360, 310)}
        for name, job in jobs.items():
            if name in expected:
                points = job["body"].get("points", [job["body"]])
                self.assertEqual(tuple(points[-1][axis] for axis in ("x", "y", "z")), expected[name])
                if name == "right_retreat":
                    self.assertEqual(tuple(points[0][axis] for axis in "xyz"), (405, -237.5, 295.5))
            else:
                self.assertEqual(job, baseline[name])
        self.assertEqual(alternative, original)

    def test_only_transit_points_blend_and_legacy_alignment_does_not_change_paths(self):
        jobs = self.plan()
        for job in jobs:
            if not job["endpoint"].endswith("/path"):
                continue
            points = job["body"]["points"]
            self.assertEqual(points[-1]["zone"], 0, job["name"])
            self.assertTrue(all(point["zone"] > 0 for point in points[:-1]), job["name"])
        self.assertEqual(jobs, self.plan(handover_offset=(2, 0, 0)))

    def test_invalid_place_never_returns_a_plan(self):
        for place in (None, (), {}, dict(self.place, x=float("nan")),
                      dict(self.place, z=float("inf"))):
            with self.subTest(place=place), self.assertRaises(SelectionError):
                self.plan(place=place)

    def test_canonical_entry_uses_shared_motion_configuration(self):
        # The canonical entrypoint imports shared values; explicit pose and speed
        # checks above remain independent of that wiring.
        import picker_workflow as entry
        self.assertEqual(entry.DEFAULT_SPEED, DEFAULT_SPEED)
        self.assertEqual(entry.HOME_SPEED, HOME_SPEED)
        self.assertEqual(entry.FLIP_SPEED, FLIP_SPEED)
        self.assertEqual((entry.CUP_Z_MIN, entry.CUP_Z_MAX), (CUP_Z_MIN, CUP_Z_MAX))
        self.assertTrue(callable(entry.main))

if __name__ == "__main__":
    unittest.main()
