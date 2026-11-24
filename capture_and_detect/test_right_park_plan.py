"""Optional right parking is planned offline; fixture poses are never defaults."""

import copy
import unittest

from cup_inventory import SelectionError
from handover_plan import handover_jobs, normalize_right_park_pose, right_via_pose
from robot_client import RobotClient


class RightParkPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "rz": 135, "selectable": True}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}
        # Arbitrary offline fixture, never persisted or dispatched to hardware.
        self.pose = {"x": 380, "y": -350, "z": 600, "rx": 170, "ry": -15, "rz": 25}

    def plan(self, **settings):
        return handover_jobs(self.cup, place=self.place, **settings)

    def test_none_preserves_existing_plans_and_optional_home_behavior(self):
        self.assertIsNone(normalize_right_park_pose())
        for home in (False, True):
            for camera_z in (False, True):
                with self.subTest(home=home, camera_z=camera_z):
                    settings = {"home_after_cup": home, "camera_z": camera_z}
                    default = self.plan(**settings)
                    self.assertEqual(self.plan(right_park_pose=None, **settings), default)
                    self.assertNotIn("right_park", [job["name"] for job in default])
                    self.assertEqual(default[-1]["name"], "right_home" if home else "right_clear")

    def test_parking_adds_only_one_joint_path_after_the_unchanged_clearance_lift(self):
        original = self.plan()
        jobs = self.plan(right_park_pose=self.pose)
        self.assertEqual(jobs[:-1], original)
        park = jobs[-1]
        self.assertEqual(park["name"], "right_park")
        self.assertEqual(park["endpoint"], RobotClient.PATH_ENDPOINT)
        self.assertEqual(park["after"], ["right_clear"])
        self.assertEqual(park["body"], {
            "arm": "right", "wait": True, "points": [dict(self.pose, linear=False, speed=250, zone=0)],
            "speed": 250, "zone": 0, "acceleration": 60, "ramp": 40,
        })
        self.assertEqual(RobotClient._validate_job(park), park)
        self.assertEqual(jobs[-2]["name"], "right_clear")
        self.assertTrue(jobs[-2]["body"]["points"][0]["linear"])
        self.assertEqual(jobs[-2]["body"]["points"][0]["z"], self.place["z"] + 80)
        right = [job for job in jobs if job["body"]["arm"] == "right"]
        self.assertEqual([job for job in right if "action" in job["body"]][-1]["body"]["action"], "open")
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))

    def test_pose_is_copied_and_never_receives_source_offsets_or_detected_yaw(self):
        original = copy.deepcopy(self.pose)
        normalized = normalize_right_park_pose(self.pose)
        self.assertEqual(normalized, original)
        self.assertIsNot(normalized, self.pose)
        normalized["x"] += 10
        self.assertEqual(self.pose, original)
        jobs = self.plan(right_park_pose=self.pose, tag_offset=(-115, 25, 40),
                         handover_offset=(10, 20, 30), camera_z=True)
        self.pose.update(x=450, rz=90)
        point = jobs[-1]["body"]["points"][0]
        self.assertEqual({axis: point[axis] for axis in original}, original)

    def test_right_via_preset_is_a_fresh_copy_of_the_existing_approach_endpoint(self):
        via = next(job for job in self.plan() if job["name"] == "right_via")
        expected = {"x": 300, "y": -300, "z": 250, "rx": 0, "ry": 0, "rz": 0}
        self.assertEqual({axis: via["body"]["points"][0][axis] for axis in expected}, expected)
        first, second = right_via_pose(), right_via_pose()
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertIsNot(first, second)
        first["x"], first["rx"] = 400, 180
        self.assertEqual(right_via_pose(), expected)

    def test_right_via_parking_reuses_exact_path_endpoint_without_skipping_initial_via(self):
        for settings in ({}, {"tag_offset": (-115, 25, 40), "handover_offset": (10, 20, 30),
                              "camera_z": True}):
            with self.subTest(settings=settings):
                original = self.plan(**settings)
                jobs = self.plan(right_park_pose=right_via_pose(), **settings)
                self.assertEqual(jobs[:-1], original)
                via = next(job for job in jobs if job["name"] == "right_via")
                park = jobs[-1]
                self.assertEqual(park["name"], "right_park")
                self.assertEqual(park["endpoint"], via["endpoint"])
                self.assertEqual(park["body"]["points"], via["body"]["points"])
                # Parking explicitly stops; the via path's default blend is
                # also overridden to fine by its single final endpoint.
                self.assertEqual(park["body"], dict(via["body"], zone=0))
                self.assertEqual(park["after"], ["right_clear"])
                self.assertEqual(sum(job["name"] == "right_via" for job in jobs), 1)
                self.assertEqual(RobotClient._validate_job(park), park)
                self.assertFalse(any(job["endpoint"] == RobotClient.HOME_ENDPOINT for job in jobs))

    def test_placement_speed_and_overall_cap_apply_to_empty_arm_parking(self):
        for overall, placement, expected in ((250, None, 250), (400, 140, 140),
                                              (25, 140, 25), (1500, 1500, 1500)):
            with self.subTest(overall=overall, placement=placement):
                profile = {"pick_lift": 100, "handover": None, "flip": 100, "placement": placement}
                jobs = self.plan(right_park_pose=self.pose, speed=overall, phase_speeds=profile)
                self.assertEqual(jobs[:-1], self.plan(speed=overall, phase_speeds=profile))
                body = jobs[-1]["body"]
                self.assertEqual(body["speed"], expected)
                self.assertEqual(body["points"][0]["speed"], expected)
                self.assertNotIn("max_acceleration", body)

    def test_explicit_parking_and_automatic_home_are_rejected_together(self):
        with self.assertRaisesRegex(SelectionError, "cannot be combined"):
            self.plan(right_park_pose=self.pose, home_after_cup=True)

    def test_invalid_pose_shapes_values_and_cartesian_limits_are_rejected(self):
        invalid = [{}, [], tuple(self.pose.values()), "pose", True,
                   {**self.pose, "extra": 0}, {key: value for key, value in self.pose.items() if key != "rz"}]
        for axis in self.pose:
            invalid += [dict(self.pose, **{axis: value}) for value in
                        (None, True, "100", float("nan"), float("inf"), -float("inf"), 10 ** 1000)]
        invalid += [dict(self.pose, **{axis: value}) for axis, value in
                    (("x", 179.9), ("x", 800.1), ("y", -400.1), ("y", 500.1),
                     ("z", 198.9), ("z", 700.1))]
        for pose in invalid:
            with self.subTest(pose=pose):
                with self.assertRaises(SelectionError):
                    normalize_right_park_pose(pose)
                with self.assertRaises(SelectionError):
                    self.plan(right_park_pose=pose)

    def test_existing_endpoint_boundaries_and_supplied_euler_angles_are_preserved(self):
        for xyz in ((180, -400, 199), (800, 500, 700)):
            pose = dict(zip(("x", "y", "z"), xyz), rx=-180, ry=180, rz=360)
            with self.subTest(pose=pose):
                self.assertEqual(normalize_right_park_pose(pose), pose)
                point = self.plan(right_park_pose=pose)[-1]["body"]["points"][0]
                self.assertEqual({axis: point[axis] for axis in pose}, pose)


if __name__ == "__main__":
    unittest.main()
