"""Offline checks of the smooth (path job) handover plans, which use the real-run poses."""

import unittest

from cup_inventory import GRIPPER_TCP_Z, LEFT_DOWN, LEFT_READY, X_MIN, SelectionError
from handover_plan import (
    CARRY_MAX_ACC, FLIP_ACCELERATION, FLIP_SPEED, LEFT_LIFT, OVERLAP_MIN_Y, PLACE_ABOVE, RIGHT_DOWN,
    RIGHT_SIDE, RIGHT_UP, RUN_HANDOVER_L, RUN_LEFT_CLEAR, RUN_LEFT_TOP, RUN_PICK_Z, RUN_PLACE,
    RUN_R_BELOW, RUN_R_BESIDE, RUN_R_TAKE, RUN_R_VIA, RUN_TRAVEL_Z, handover_batch, handover_paths,
)
from robot_client import RobotClient


def xyz(point):
    return tuple(point[a] for a in ("x", "y", "z"))


def rot(point):
    return tuple(point[a] for a in ("rx", "ry", "rz"))


def assert_order(test, names, *steps):
    positions = [names.index(step) for step in steps]
    test.assertEqual(positions, sorted(positions), steps)


def stage(jobs, group):
    return [j for j in jobs if j.get("group") == group]


def check_groups(test, jobs):
    """Each group is one contiguous block with work for both arms."""
    seen = []
    for job in jobs:
        RobotClient._validate_job(job)
        group = job.get("group")
        if group and (not seen or seen[-1] != group):
            test.assertNotIn(group, seen)
            seen.append(group)
        elif not group:
            seen.append(None)
    arms = {}
    for job in jobs:
        if "group" in job:
            arms.setdefault(job["group"], set()).add(job["body"]["arm"])
    test.assertTrue(all(value == {"left", "right"} for value in arms.values()), arms)


class RealRunPoseTests(unittest.TestCase):
    """The constants are the ones capture_and_detect.py ran on the real YuMi."""

    def test_heights_and_poses_match_the_real_run_program(self):
        self.assertEqual((RUN_PICK_Z, RUN_TRAVEL_Z), (70, 175))
        self.assertEqual(RUN_HANDOVER_L, (350, -100, 655))
        self.assertEqual(RUN_LEFT_CLEAR, (350, -100, RUN_LEFT_TOP))
        self.assertEqual(RUN_LEFT_TOP, 690)
        self.assertEqual(RUN_R_VIA, (300, -300, 250))
        self.assertEqual(RUN_R_TAKE, (330, -115, 316))
        self.assertEqual(RUN_R_BELOW, (330, -115, 281))
        self.assertEqual(RUN_R_BESIDE, (330, -235, 281))
        self.assertEqual(RUN_PLACE, (350, -200, 230))
        self.assertEqual((PLACE_ABOVE, FLIP_SPEED, FLIP_ACCELERATION), (80, 100, 30))


class HandoverPathTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}

    def plan(self, **kwargs):
        return handover_paths(self.cup, **kwargs)

    def by_name(self, **kwargs):
        return {job["name"]: job for job in self.plan(**kwargs)}

    def names(self, **kwargs):
        return [job["name"] for job in self.plan(**kwargs)]

    def test_handover_interlock_and_grippers(self):
        for more_cups in (False, True):
            with self.subTest(more_cups=more_cups):
                jobs = self.plan(more_cups=more_cups)
                names = [j["name"] for j in jobs]
                self.assertEqual(len(names), len(set(names)))
                # The right goes up to the rim only once the left holds still at the
                # handover, grips before the left lets go, and pulls the cup down
                # before the left moves away.
                assert_order(self, names, "left_ready", "left_open", "approach_cup", "grip_cup",
                             "lift_to_handover", "right_take", "right_grip", "left_release",
                             "right_lower", "right_retreat")
                assert_order(self, names, "left_release", "left_clear", "right_retreat")
                # The right travels closed, waits beside the handover and only opens
                # once the left holds still there.
                assert_order(self, names, "lift_to_handover", "right_open", "right_take")
                assert_order(self, names, "right_to_beside", "right_open")
                assert_order(self, names, "right_lower", "right_retreat", "right_place",
                             "right_release", "right_clear", "right_close", "right_home")
                for step in ("right_open", "right_take", "right_grip", "left_release", "right_retreat"):
                    self.assertNotIn("group", jobs[names.index(step)])
                check_groups(self, jobs)
        names = self.names()
        assert_order(self, names, "left_close", "left_home")

    def test_left_ready_is_its_own_stop_for_the_recheck(self):
        job = self.by_name()["left_ready"]
        self.assertEqual([xyz(p) for p in job["body"]["points"]], [LEFT_READY])
        self.assertNotIn("group", job)

    def test_the_recheck_happens_with_the_right_arm_still_at_home(self):
        jobs = self.plan()
        first_grouped = next(i for i, job in enumerate(jobs) if "group" in job)
        self.assertEqual(jobs[first_grouped]["name"], "approach_cup")
        before = [job["name"] for job in jobs[:first_grouped] if job["body"]["arm"] == "right"]
        self.assertEqual(before, ["right_home_start"])

    def test_pick_uses_the_real_run_heights(self):
        above, pick = self.by_name(speed=40)["approach_cup"]["body"]["points"]
        self.assertEqual(xyz(above), (500, 100, RUN_TRAVEL_Z + GRIPPER_TCP_Z))
        self.assertEqual(xyz(pick), (500, 100, RUN_PICK_Z + GRIPPER_TCP_Z))
        self.assertFalse(above["linear"])
        self.assertTrue(pick["linear"])
        self.assertEqual(pick["speed"], 40)
        for point in (above, pick):
            self.assertEqual(rot(point), LEFT_DOWN)

    def test_right_arm_waits_outside_the_cup_area_while_the_left_picks(self):
        pick = stage(self.plan(), "pick")
        self.assertEqual([j["name"] for j in pick], ["approach_cup", "grip_cup", "right_to_via"])
        self.assertEqual([xyz(p) for p in pick[-1]["body"]["points"]], [RUN_R_VIA])
        self.assertLess(RUN_R_VIA[0], X_MIN)

    def test_right_waits_beside_while_the_left_brings_the_cup_as_in_the_real_runs(self):
        lift = stage(self.plan(), "lift")
        self.assertEqual([(j["name"], j["body"]["arm"]) for j in lift],
                         [("lift_to_handover", "left"), ("right_to_beside", "right")])
        lift_cup, handover = lift[0]["body"]["points"]
        self.assertTrue(lift_cup["linear"])
        self.assertEqual(xyz(handover), RUN_HANDOVER_L)
        self.assertEqual(lift[0]["body"]["max_acceleration"], CARRY_MAX_ACC)
        self.assertEqual([xyz(p) for p in lift[1]["body"]["points"]], [RUN_R_BESIDE])
        # Beside, away from the left arm: 120 mm to the side of where it slides in.
        self.assertEqual(RUN_R_BELOW[1] - RUN_R_BESIDE[1], 120)

    def test_right_slides_in_and_goes_up_in_one_motion_then_straight_down_after(self):
        jobs = self.by_name(speed=40)
        below, take = jobs["right_take"]["body"]["points"]
        lower = jobs["right_lower"]["body"]["points"]
        self.assertEqual((xyz(below), xyz(take)), (RUN_R_BELOW, RUN_R_TAKE))
        # A small corner under the cup, so it cannot rise early into the rim.
        self.assertLessEqual(below["zone"], 5)
        self.assertEqual([xyz(p) for p in lower], [RUN_R_BELOW])
        for point in (below, take, *lower):
            self.assertTrue(point["linear"])
            self.assertEqual(point["speed"], 40)
            self.assertEqual(rot(point), RIGHT_UP)
        self.assertEqual(jobs["right_lower"]["body"]["max_acceleration"], CARRY_MAX_ACC)

    def test_cup_is_turned_over_in_two_slow_steps_as_in_the_real_runs(self):
        retreat = self.by_name()["right_retreat"]["body"]
        half, above = retreat["points"]
        place_above = (RUN_PLACE[0], RUN_PLACE[1], RUN_PLACE[2] + PLACE_ABOVE)
        self.assertEqual(xyz(above), place_above)
        self.assertEqual(xyz(half), tuple((a + b) / 2 for a, b in zip(RUN_R_BELOW, place_above)))
        self.assertEqual((rot(half), rot(above)), (RIGHT_SIDE, RIGHT_DOWN))
        self.assertEqual((retreat["speed"], retreat["acceleration"]), (FLIP_SPEED, FLIP_ACCELERATION))

    def test_after_the_release_both_arms_move_straight_apart_together(self):
        release = stage(self.plan(), "release")
        self.assertEqual([(j["name"], j["body"]["arm"]) for j in release],
                         [("right_lower", "right"), ("left_clear", "left")])
        self.assertEqual(xyz(release[1]["body"]["points"][0]), RUN_LEFT_CLEAR)
        self.assertTrue(all(j["body"]["points"][0]["linear"] for j in release))

    def test_left_waits_at_its_top_while_the_right_turns_the_cup(self):
        for more_cups in (False, True):
            with self.subTest(more_cups=more_cups):
                jobs = self.plan(more_cups=more_cups)
                names = [j["name"] for j in jobs]
                turn = names.index("right_retreat")
                # Nothing else runs during the turn, and the left has finished
                # going up before it starts.
                self.assertNotIn("group", jobs[turn])
                self.assertLess(names.index("left_clear"), turn)
                left_after = [j["name"] for j in jobs[turn:] if j["body"]["arm"] == "left"]
                self.assertEqual(left_after, [] if more_cups else ["left_close", "left_home"])

    def test_last_cup_left_homes_only_once_the_cup_is_turned_over(self):
        names = self.names()
        home = stage(self.plan(), "home")
        self.assertEqual([j["name"] for j in home],
                         ["left_close", "left_home", "right_place", "right_release", "right_clear",
                          "right_close"])
        assert_order(self, names, "right_retreat", "left_home")
        self.assertEqual(names[-1], "right_home")

    def test_left_arm_waits_at_its_top_when_more_cups_remain(self):
        more = self.plan(more_cups=True)
        names = [j["name"] for j in more]
        self.assertNotIn("left_home", names)
        left = [j for j in more if j["body"]["arm"] == "left"]
        self.assertEqual(left[-1]["name"], "left_clear")
        self.assertEqual(xyz(left[-1]["body"]["points"][-1]), RUN_LEFT_CLEAR)
        self.assertEqual(names[-1], "right_home")

    def test_place_spot_can_be_chosen_and_is_checked(self):
        spot = (430, -280, 230)
        jobs = self.by_name(place=spot)
        self.assertEqual(xyz(jobs["right_place"]["body"]["points"][0]), spot)
        self.assertEqual(xyz(jobs["right_clear"]["body"]["points"][0]), (430, -280, 310))
        for bad in ((430, -280), (430, -280, 1000), ("a", 0, 0)):
            with self.subTest(place=bad):
                with self.assertRaises(SelectionError):
                    self.plan(place=bad)

    def test_calibration_xy_offset_moves_only_the_cup_points(self):
        base = self.by_name()
        shifted = self.by_name(tag_offset=(-115, 25, 1000))
        for point in shifted["approach_cup"]["body"]["points"] + shifted["lift_to_handover"]["body"]["points"][:1]:
            self.assertEqual((point["x"], point["y"]), (385, 125))
        for name in ("left_ready", "right_to_via", "right_to_beside", "right_take", "right_lower",
                     "left_clear", "right_retreat", "right_place", "right_clear"):
            self.assertEqual(shifted[name], base[name])
        self.assertEqual(shifted["lift_to_handover"]["body"]["points"][1:],
                         base["lift_to_handover"]["body"]["points"][1:])

    def test_carrying_paths_limit_acceleration_and_empty_ones_do_not(self):
        jobs = self.by_name()
        for name in ("lift_to_handover", "right_lower", "right_retreat", "right_place"):
            self.assertEqual(jobs[name]["body"]["max_acceleration"], CARRY_MAX_ACC, name)
        for name in ("left_ready", "approach_cup", "right_to_via", "right_to_beside", "right_take",
                     "left_clear", "right_clear"):
            self.assertNotIn("max_acceleration", jobs[name]["body"], name)

    def test_every_motion_has_softer_acceleration(self):
        for job in self.plan():
            if job["endpoint"] != "/robot/rws/gripper":
                self.assertLess(job["body"]["acceleration"], 100)
                self.assertLess(job["body"]["ramp"], 100)

    def test_invalid_cup_or_speeds_are_refused(self):
        for kwargs in ({"speed": 0}, {"travel_speed": 2000}, {"carry_speed": float("nan")}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SelectionError):
                    self.plan(**kwargs)
        with self.assertRaises(SelectionError):
            handover_paths({**self.cup, "z": 500})
        with self.assertRaises(SelectionError):
            handover_paths({**self.cup, "selectable": False})


class HandoverBatchTests(unittest.TestCase):
    def cup(self, y=100, x=500):
        return {"x": x, "y": y, "z": 50, "selectable": True, "blocked_reason": None}

    def test_one_cup_is_the_single_cup_plan_with_numbered_steps(self):
        batch = handover_batch([self.cup()])
        single = handover_paths(self.cup())
        self.assertEqual([j["name"] for j in batch], [j["name"] + "#1" for j in single])
        for b, s in zip(batch, single):
            self.assertEqual(b["body"], s["body"])

    def test_left_picks_the_next_cup_while_the_right_places_and_waits_beside(self):
        jobs = handover_batch([self.cup(), self.cup(y=200)])
        together = stage(jobs, "next#2")
        right = [j["name"] for j in together if j["body"]["arm"] == "right"]
        left = [j["name"] for j in together if j["body"]["arm"] == "left"]
        # Not home in between: from the place spot straight to beside the handover.
        self.assertEqual(right, ["right_place#1", "right_release#1", "right_clear#1", "right_close#1",
                                 "right_to_beside#2"])
        self.assertEqual(left, ["left_ready#2", "left_open#2", "approach_cup#2", "grip_cup#2"])
        names = [j["name"] for j in jobs]
        self.assertNotIn("right_home#1", names)
        beside = next(j for j in together if j["name"] == "right_to_beside#2")
        self.assertEqual([xyz(p) for p in beside["body"]["points"]], [RUN_R_BESIDE])
        # Only once cup 1 is turned over, with the left waiting at its top.
        assert_order(self, names, "left_release#1", "left_clear#1", "right_retreat#1", "left_ready#2")
        ready = next(j for j in together if j["name"] == "left_ready#2")
        self.assertEqual([xyz(p) for p in ready["body"]["points"]], [LEFT_READY])
        # The right is already waiting, so the left lifts on its own.
        lift = jobs[names.index("lift_to_handover#2")]
        self.assertNotIn("group", lift)
        assert_order(self, names, "lift_to_handover#2", "right_open#2", "right_take#2")

    def test_each_cup_goes_to_its_own_place_spot(self):
        spots = [(350, -200, 230), (430, -200, 230)]
        jobs = {j["name"]: j for j in handover_batch([self.cup(), self.cup(y=200)], places=spots)}
        for n, spot in enumerate(spots, 1):
            self.assertEqual(xyz(jobs[f"right_place#{n}"]["body"]["points"][0]), spot)
        with self.assertRaises(SelectionError):
            handover_batch([self.cup(), self.cup()], places=spots[:1])

    def test_cup_near_the_place_spot_waits_for_the_right_arm(self):
        near = self.cup(y=OVERLAP_MIN_Y - 1)
        jobs = handover_batch([self.cup(), near])
        left = [j["name"] for j in stage(jobs, "next#2") if j["body"]["arm"] == "left"]
        self.assertEqual(left, ["left_ready#2", "left_open#2"])
        names = [j["name"] for j in jobs]
        # Near the place spots: the right goes home first, it does not wait beside.
        assert_order(self, names, "right_home#1", "approach_cup#2")
        self.assertNotIn("right_to_beside#2", [j["name"] for j in stage(jobs, "next#2")])
        self.assertEqual([j["name"] for j in stage(jobs, "pick#2")],
                         ["approach_cup#2", "grip_cup#2", "right_to_via#2"])

    def test_each_cup_keeps_the_handover_interlocks_and_only_the_last_homes_the_left(self):
        jobs = handover_batch([self.cup(), self.cup(y=200), self.cup(y=-150)])
        names = [j["name"] for j in jobs]
        self.assertEqual(len(names), len(set(names)))
        for n in (1, 2, 3):
            assert_order(self, names, *(f"{step}#{n}" for step in (
                "approach_cup", "grip_cup", "lift_to_handover", "right_open", "right_take", "right_grip",
                "left_release", "right_lower", "right_retreat", "right_place", "right_release")))
            for step in ("right_open", "right_take", "right_grip", "left_release", "right_retreat"):
                self.assertNotIn("group", jobs[names.index(f"{step}#{n}")])
            if n > 1:
                assert_order(self, names, f"right_retreat#{n - 1}", f"left_ready#{n}", f"right_take#{n}")
        self.assertEqual([n for n in names if n.startswith("left_home")], ["left_home#3"])
        # Cup 2 is on the far side (right waits beside), cup 3 near the place spots
        # (right homes first), and after the last cup the right homes.
        self.assertEqual([n for n in names if n.startswith("right_home") and n != "right_home_start#1"],
                         ["right_home#2", "right_home#3"])
        self.assertEqual(names[-1], "right_home#3")
        check_groups(self, jobs)

    def test_bad_input_is_refused(self):
        with self.assertRaises(SelectionError):
            handover_batch([])
        with self.assertRaises(SelectionError):
            handover_batch([self.cup(), {**self.cup(), "selectable": False}])


if __name__ == "__main__":
    unittest.main()
