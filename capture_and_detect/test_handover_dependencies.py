"""Offline dependency checks for overlapping arms and guarded handover phases."""

import unittest

from handover_plan import handover_jobs, left_only_jobs


class HandoverDependencyTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}
        self.jobs = handover_jobs(self.cup, place=self.place)
        self.by_name = {job["name"]: job for job in self.jobs}

    def ancestors(self, name, visiting=()):
        self.assertNotIn(name, visiting, "Dependency cycle")
        result = set()
        for parent in self.by_name[name]["after"]:
            self.assertIn(parent, self.by_name)
            result.add(parent)
            result.update(self.ancestors(parent, (*visiting, name)))
        return result

    def ready(self, completed):
        return {name for name, job in self.by_name.items()
                if name not in completed and set(job["after"]) <= completed}

    def test_every_job_has_valid_acyclic_dependencies_and_own_arm_predecessor(self):
        self.assertEqual(len(self.jobs), len(self.by_name))
        previous = {}
        for job in self.jobs:
            name, arm = job["name"], job["body"]["arm"]
            with self.subTest(name=name):
                self.assertEqual(set(job), {"name", "endpoint", "body", "after"})
                self.assertIsInstance(job["after"], list)
                self.assertEqual(len(job["after"]), len(set(job["after"])))
                self.assertNotIn("after", job["body"])
                self.ancestors(name)
                if arm in previous:
                    self.assertIn(previous[arm], job["after"])
                previous[arm] = name
        self.assertEqual(self.ready(set()), {"right_close_travel"})
        self.assertIn("right_home_start", self.ancestors("left_close_travel"))

    def test_after_lift_left_handover_overlaps_right_approach_but_open_waits_for_both(self):
        lifted = self.ancestors("lift_cup") | {"lift_cup"}
        self.assertEqual(self.ready(lifted), {"left_handover", "right_via"})
        right_waiting = lifted | {"right_via", "right_beside"}
        self.assertEqual(self.ready(right_waiting), {"left_handover"})
        self.assertEqual(self.ready(right_waiting | {"left_handover"}), {"right_open"})
        left_waiting = lifted | {"left_handover"}
        self.assertEqual(self.ready(left_waiting), {"right_via"})

    def test_handover_release_and_right_lowering_retain_cross_arm_barriers(self):
        self.assertIn("right_grip", self.by_name["left_release"]["after"])
        self.assertIn("left_clear", self.by_name["right_lower"]["after"])
        before_grip = self.ancestors("right_grip")
        self.assertNotIn("left_release", self.ready(before_grip))
        self.assertIn("left_release", self.ready(before_grip | {"right_grip"}))
        before_clear = self.ancestors("left_clear")
        self.assertNotIn("right_lower", self.ready(before_clear))
        self.assertIn("right_lower", self.ready(before_clear | {"left_clear"}))

    def test_after_right_retreat_left_return_and_right_place_are_independent(self):
        retreated = self.ancestors("right_retreat") | {"right_retreat"}
        self.assertEqual(self.ready(retreated), {"left_close_return", "right_place"})
        self.assertEqual(self.by_name["right_place"]["after"], ["right_retreat"])
        # Either arm may finish first; a shared lease still gates all dispatch.
        left_returned = retreated | {"left_close_return", "left_home_lateral", "left_home"}
        self.assertEqual(self.ready(left_returned), {"right_place"})
        right_returned = retreated | {"right_place", "right_release", "right_clear",
                                      "right_close_return", "right_home"}
        self.assertEqual(self.ready(right_returned), {"left_close_return"})
        self.assertNotIn("left_home", self.ancestors("right_home"))
        self.assertNotIn("right_home", self.ancestors("left_home"))

    def test_right_returns_after_placement_clearance_without_added_raise_or_retry(self):
        self.assertNotIn("right_home_raise", self.by_name)
        self.assertEqual(self.by_name["right_clear"]["after"], ["right_release"])
        self.assertEqual(self.by_name["right_close_return"]["after"], ["right_clear"])
        self.assertEqual(self.by_name["right_home"]["after"], ["right_close_return"])
        right_moves = [job for job in self.jobs
                       if job["body"]["arm"] == "right" and job["endpoint"].endswith("/move")]
        self.assertEqual(right_moves[-1]["name"], "right_clear")
        self.assertEqual(right_moves[-1]["body"]["z"], 310)

    def test_left_only_keeps_serial_schema_and_existing_raise_and_lateral_return(self):
        jobs = left_only_jobs(self.cup)
        self.assertTrue(all(set(job) == {"name", "endpoint", "body"} for job in jobs))
        self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
        self.assertEqual([job["name"] for job in jobs[-4:]],
                         ["left_close_return", "left_home_raise", "left_home_lateral", "left_home"])


if __name__ == "__main__":
    unittest.main()
