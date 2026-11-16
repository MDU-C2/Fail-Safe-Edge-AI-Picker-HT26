"""Offline regression checks for the default vision_detection4 choreography."""

import copy
import unittest

from cup_inventory import SelectionError
from handover_plan import handover_jobs, left_only_jobs


class NoHomePlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "rz": 150,
                    "aperture": 100, "selectable": True}
        self.place = {"id": 2, "x": 420, "y": -200, "z": 230, "occupied": False}

    def without_home(self, jobs):
        """Independent expected edit: remove only startup/home return actions."""
        expected = []
        for original in jobs:
            name = original["name"]
            if "home" in name or name.endswith("_close_return"):
                continue
            job = copy.deepcopy(original)
            if name == "left_close_travel" and "after" in job:
                job["after"] = []
            elif name == "right_beside":
                job["after"] = ["right_close_travel", "lift_cup"]
            expected.append(job)
        return expected

    def check_dependency_closure(self, jobs):
        completed, previous = set(), {}
        for job in jobs:
            name, arm = job["name"], job["body"]["arm"]
            self.assertNotIn(name, completed)
            self.assertTrue(set(job["after"]) <= completed,
                            f"Missing, forward, or cyclic dependencies for {name}")
            if arm in previous:
                self.assertIn(previous[arm], job["after"])
            completed.add(name)
            previous[arm] = name

    def test_default_plan_ends_at_clearance_for_both_modes(self):
        self.assertEqual(handover_jobs(self.cup, place=self.place),
                         handover_jobs(self.cup, place=self.place, home_after_cup=False))
        self.assertEqual(left_only_jobs(self.cup),
                         left_only_jobs(self.cup, home_after_cup=False))

    def test_complete_plan_removes_only_home_actions_for_consecutive_cups(self):
        # Each cup gets a complete new plan, including LEFT_READY and the right
        # lift barrier; the previous cup's clear pose does not skip a pick step.
        cases = ((self.cup, self.place, {}),
                 (dict(self.cup, x=460, y=210, z=65, rz=240, aperture=None),
                  dict(self.place, id=3, x=450, y=-260),
                  {"camera_z": True, "tag_offset": (-20, 10, 100),
                   "speed": 180, "handover_offset": (5, -5, 10),
                   "phase_speeds": {"pick_lift": 80, "handover": 130,
                                    "flip": 60, "placement": 110}}))
        for cup, place, settings in cases:
            with self.subTest(cup=cup):
                baseline = handover_jobs(cup, place=place, home_after_cup=True, **settings)
                jobs = handover_jobs(cup, place=place, **settings)
                self.assertEqual(jobs, self.without_home(baseline))
                self.check_dependency_closure(jobs)
                self.assertEqual(len(jobs), 21)
                self.assertEqual([job["name"] for job in jobs[:3]],
                                 ["right_close_travel", "left_close_travel", "left_ready"])
                self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))

    def test_release_clearance_and_right_approach_barriers_remain(self):
        jobs = handover_jobs(self.cup, place=self.place)
        by_name = {job["name"]: job for job in jobs}
        self.assertEqual(by_name["left_close_travel"]["after"], [])
        self.assertEqual(by_name["right_close_travel"]["after"], [])
        self.assertEqual(by_name["right_beside"]["after"], ["right_close_travel", "lift_cup"])
        for earlier, later in (("left_handover", "right_open"),
                               ("right_grip", "left_release"),
                               ("left_clear", "right_lower"),
                               ("right_retreat", "right_place"),
                               ("right_place", "right_release"),
                               ("right_release", "right_clear")):
            self.assertIn(earlier, by_name[later]["after"])
        for arm in ("left", "right"):
            arm_jobs = [job for job in jobs if job["body"]["arm"] == arm]
            self.assertEqual(arm_jobs[-1]["name"], arm + "_clear")
            grip = [job for job in arm_jobs if job["endpoint"].endswith("/gripper")][-1]
            self.assertEqual(grip["body"]["action"], "open")

    def test_left_only_replaces_then_clears_without_closing_or_homing(self):
        for cup in (self.cup, dict(self.cup, x=470, y=220, rz=240, aperture=None)):
            with self.subTest(cup=cup):
                jobs = left_only_jobs(cup)
                self.assertEqual(jobs, self.without_home(left_only_jobs(cup, home_after_cup=True)))
                self.assertEqual(len(jobs), 10)
                self.assertTrue(all(set(job) == {"name", "endpoint", "body"} for job in jobs))
                self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
                self.assertEqual([job["name"] for job in jobs[-3:]],
                                 ["replace_cup", "left_release", "left_clear"])
                self.assertEqual(jobs[-2]["body"]["action"], "open")

    def test_non_boolean_policy_fails_before_returning_a_plan(self):
        for value in (None, 0, 1, "false", [], {}):
            for planner, args in ((handover_jobs, {"place": self.place}), (left_only_jobs, {})):
                with self.subTest(value=value, planner=planner.__name__):
                    with self.assertRaisesRegex(SelectionError, "home_after_cup must be a boolean"):
                        planner(self.cup, home_after_cup=value, **args)


if __name__ == "__main__":
    unittest.main()
