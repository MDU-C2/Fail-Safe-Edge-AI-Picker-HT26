"""Offline graph checks for smooth handover concurrency and release barriers."""

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
        self.assertEqual(self.ready(set()), {"right_open_start", "left_open_start"})
        for name in self.ready(set()):
            self.assertEqual(self.by_name[name]["body"]["action"], "open")

    def test_right_prepares_while_left_picks_and_both_wait_before_entering_handover(self):
        prepared = self.ancestors("approach_cup") | self.ancestors("right_via")
        self.assertEqual(self.ready(prepared), {"approach_cup", "right_via"})
        # No arm may start the handover until the cup is gripped AND the right
        # arm has reached its separate via position, whichever finishes first.
        left_gripped = prepared | {"approach_cup", "grip_cup"}
        self.assertEqual(self.ready(left_gripped), {"right_via"})
        right_arrived = prepared | {"right_via"}
        self.assertEqual(self.ready(right_arrived), {"approach_cup"})
        together = left_gripped | {"right_via"}
        self.assertEqual(self.ready(together), {"left_handover", "right_beside"})
        for finished, waiting in (("left_handover", "right_beside"),
                                  ("right_beside", "left_handover")):
            self.assertEqual(self.ready(together | {finished}), {waiting})
        self.assertEqual(self.ready(together | {"left_handover", "right_beside"}), {"right_open"})

    def test_left_release_requires_right_grip_then_arms_separate_together(self):
        before_grip = self.ancestors("right_grip")
        self.assertNotIn("left_release", self.ready(before_grip))
        self.assertEqual(self.ready(before_grip | {"right_grip"}), {"left_release"})
        released = before_grip | {"right_grip", "left_release"}
        self.assertEqual(self.ready(released), {"left_clear", "right_lower"})
        # Turning must wait for BOTH separation movements to report DONE; an
        # early completion by either arm must never release the turn barrier.
        for finished, waiting in (("left_clear", "right_lower"), ("right_lower", "left_clear")):
            self.assertEqual(self.ready(released | {finished}), {waiting})
        self.assertEqual(self.ready(released | {"left_clear", "right_lower"}), {"right_retreat"})

    def test_left_holds_at_clearance_until_right_has_finished_turning(self):
        left = [job for job in self.jobs if job["body"]["arm"] == "left"]
        self.assertEqual(left[-1]["name"], "left_clear")
        retreated = self.ancestors("right_retreat") | {"right_retreat"}
        self.assertEqual(self.ready(retreated), {"right_place"})
        self.assertIn("right_place", self.ancestors("right_release"))
        self.assertIn("right_release", self.ancestors("right_clear"))
        self.assertEqual(self.by_name["right_clear"]["after"], ["right_release"])
        self.assertEqual(self.ready(set(self.by_name)), set())
        self.assertEqual(self.jobs[-1]["name"], "right_clear")
        self.assertNotIn("right_close", self.by_name)

    def test_optional_homes_start_and_finish_with_confirmed_clearances(self):
        self.jobs = handover_jobs(self.cup, place=self.place, home_after_cup=True)
        self.by_name = {job["name"]: job for job in self.jobs}
        self.assertEqual(self.ready(set()), {"right_open_start", "left_open_start"})
        self.assertEqual(self.by_name["right_home_start"]["after"], ["right_open_start"])
        self.assertEqual(self.ready({"right_open_start", "left_open_start"}), {"right_home_start"})
        self.assertIn("right_home_start", self.ancestors("left_ready"))
        self.assertIn("left_open_start", self.by_name["left_ready"]["after"])
        retreated = self.ancestors("right_retreat") | {"right_retreat"}
        self.assertEqual(self.ready(retreated), {"left_close_return", "right_place"})
        self.assertEqual(self.by_name["right_home"]["after"], ["right_clear"])
        self.assertNotIn("right_home_raise", self.by_name)
        self.assertNotIn("left_home", self.ancestors("right_home"))
        self.assertNotIn("right_home", self.ancestors("left_home"))
        right_clear = self.by_name["right_clear"]["body"]["points"][-1]
        self.assertEqual(right_clear["z"], 310)

    def test_left_only_keeps_serial_schema_and_existing_raise_and_lateral_return(self):
        jobs = left_only_jobs(self.cup, home_after_cup=True)
        self.assertTrue(all(set(job) == {"name", "endpoint", "body"} for job in jobs))
        self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
        self.assertEqual([job["name"] for job in jobs[-4:]],
                         ["left_close_return", "left_home_raise", "left_home_lateral", "left_home"])


if __name__ == "__main__":
    unittest.main()
