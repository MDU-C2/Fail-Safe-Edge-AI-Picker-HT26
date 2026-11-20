"""Offline regressions for repeated smooth transfers without automatic homing."""

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
        """Compare common jobs after removing optional homes and idle parking."""
        expected = []
        for original in jobs:
            name = original["name"]
            if "home" in name or name.endswith("_close_return") or name == "left_park":
                continue
            job = copy.deepcopy(original)
            if "after" in job:
                job["after"] = [parent for parent in job["after"]
                                if "home" not in parent and not parent.endswith("_close_return")]
                if name == "right_via" and "right_home_start" in original["after"]:
                    # Its same-arm predecessor becomes the startup open when
                    # the optional initial right-home job is removed.
                    job["after"].insert(0, "right_open_start")
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

    def test_default_plan_disables_homes_for_both_modes(self):
        self.assertEqual(handover_jobs(self.cup, place=self.place),
                         handover_jobs(self.cup, place=self.place, home_after_cup=False))
        self.assertEqual(left_only_jobs(self.cup),
                         left_only_jobs(self.cup, home_after_cup=False))

    def test_each_consecutive_cup_has_full_smooth_choreography_without_home_actions(self):
        # Consecutive cups change position/yaw/height/settings; the planner must
        # still route through LEFT_READY and synchronize receiving each time.
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
                self.assertEqual(self.without_home(jobs), self.without_home(baseline))
                self.check_dependency_closure(jobs)
                names = {job["name"] for job in jobs}
                self.assertTrue({"left_park", "approach_cup", "grip_cup", "right_via",
                                 "left_handover", "right_beside", "right_take",
                                 "left_release", "right_place"} <= names)
                self.assertFalse(any("home" in name for name in names))
                self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))

    def test_normal_cycle_starts_open_and_finishes_both_open_at_clearance(self):
        jobs = handover_jobs(self.cup, place=self.place)
        by_name = {job["name"]: job for job in jobs}
        self.assertEqual(by_name["left_open"]["after"], [])
        self.assertEqual(by_name["right_open_start"]["after"], [])
        self.assertEqual(by_name["left_open"]["body"],
                         {"arm": "left", "wait": True, "action": "move", "position": 12.5})
        self.assertEqual(by_name["right_open_start"]["body"]["action"], "open")
        self.assertEqual([job["name"] for job in jobs if job["body"].get("action") == "grip"],
                         ["grip_cup", "right_grip"])
        left = [job for job in jobs if job["body"]["arm"] == "left"]
        right = [job for job in jobs if job["body"]["arm"] == "right"]
        self.assertEqual(left[-1]["name"], "left_park")
        self.assertEqual(right[-1]["name"], "right_clear")
        for lane, xyz, action in ((left, (350, 400, 690), "open"),
                                  (right, (420, -200, 310), "open")):
            last_path = [job for job in lane if job["endpoint"].endswith("/path")][-1]
            point = last_path["body"]["points"][-1]
            self.assertEqual(tuple(point[axis] for axis in "xyz"), xyz)
            self.assertEqual([job for job in lane if job["endpoint"].endswith("/gripper")][-1]
                             ["body"]["action"], action)

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
                self.assertEqual(jobs[-1]["body"]["z"], 409)

    def test_non_boolean_policy_fails_before_returning_a_plan(self):
        for value in (None, 0, 1, "false", [], {}):
            for planner, args in ((handover_jobs, {"place": self.place}), (left_only_jobs, {})):
                with self.subTest(value=value, planner=planner.__name__):
                    with self.assertRaisesRegex(SelectionError, "home_after_cup must be a boolean"):
                        planner(self.cup, home_after_cup=value, **args)


if __name__ == "__main__":
    unittest.main()
