"""Offline batch checks for the terminal's branch-4 clearance-only workflow."""

import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from test_cup_selection import RobotDouble, detection, inventory
import test_placement_verification as fixtures


class NoHomeSessionTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp

    def make_session(self, *, failed_clearance=False, occluded=False, stale_offset=None,
                     verification_frames=3, left_only=False):
        cups = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                  detection("CUP", 250, x=560),
                                  detection("CUP BASE", 250, x=560)])["cups"]
        sequence = [9]
        now = [100.0]
        coordinator = threading.get_ident()
        observations = []
        frames_per_cup = verification_frames if not left_only else 1
        first_job_count = 10 if left_only else 23
        self.robot = RobotDouble(error=RuntimeError("right clearance failed") if failed_clearance else None,
                                 error_at="right_clear")
        session = None

        def capture(kind):
            self.assertEqual(threading.get_ident(), coordinator,
                             "No-home mode must not start a return-phase capture worker")
            sequence[0] += 1
            seq = sequence[0]
            started = 99.0 if kind == "scan" else now[0]
            frame = fixtures.PlacementVerificationTests.recheck_observed(sequence=seq)
            frame["cups"] = copy.deepcopy(cups)
            observation = frame["placement_observation"]
            prototype = observation["slots"][0]
            observation["slots"] = [dict(copy.deepcopy(prototype), id=place["id"])
                                    for place in self.grid.snapshot()["locations"]]
            if kind == "scan":
                observation.update(grid_snapshot=self.grid.snapshot(), frame_count=3,
                                   frame_sequences=[8, 9, 10], released_ids=[])
            next_cup = kind != "scan" and seq >= 11 + frames_per_cup
            if next_cup:
                first_execution = session.inventory["cups"][0]["execution"]
                self.assertEqual(first_execution["state"], "completed")
                self.assertEqual(first_execution["last_completed_step"],
                                 "left_clear" if left_only else "right_clear")
                self.assertEqual(len(self.robot.jobs), first_job_count)
                next_offset = seq - (11 + frames_per_cup)
                if stale_offset == next_offset:
                    started = 109.0
                if occluded:
                    observation["slots"][1].update(
                        state="unknown", reason_code="occluded", visible_fraction=0.2,
                        floor_visible_fraction=0.3, reason="An arm still obscures the destination.")
            observations.append({"sequence": seq, "kind": kind, "started": started,
                                 "after_first_clearance": next_cup})
            return frame, started

        def after_move(_):
            if len(self.robot.jobs) == first_job_count:
                self.assertEqual(self.robot.jobs[-1]["name"],
                                 "left_clear" if left_only else "right_clear")
                now[0] = 110.0

        self.robot.after_move = after_move
        self.capture = Mock(side_effect=capture)
        session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: now[0],
                             placing_grid=self.grid, recovery=self.recovery,
                             verification_frames=verification_frames, home_after_cup=False,
                             left_only=left_only)
        session.scan()
        return session, observations

    def execute(self, session):
        with patch("cup_commands._VerificationPrefetch", side_effect=AssertionError(
                "No-home mode must not create a return-phase prefetch")) as prefetch:
            result = session.execute_batch(capture_id=session.inventory["capture"])
            prefetch.assert_not_called()
        return result

    def assert_no_second_pick(self, session):
        self.assertEqual(len(self.robot.jobs), 23)
        self.assertEqual(sum(job["name"] == "grip_cup" for job in self.robot.jobs), 1)
        self.assertEqual(session.batch_progress["state"], "interrupted")
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        slots = self.grid.snapshot()["locations"]
        self.assertTrue(slots[1]["reserved"])
        self.assertFalse(session.scan_usable)

    def test_two_cups_verify_after_completed_clearance_without_any_home_or_prefetch(self):
        session, observations = self.make_session()
        jobs = self.execute(session)
        self.assertEqual(len(jobs), 46)
        self.assertEqual(jobs, self.robot.jobs)
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))
        self.assertEqual([job["name"] for job in jobs[23:26]],
                         ["right_close_travel", "left_close_travel", "left_ready"])
        self.assertEqual([(row["sequence"], row["started"], row["after_first_clearance"])
                          for row in observations],
                         [(10, 99.0, False), (11, 100.0, False), (12, 100.0, False),
                          (13, 100.0, False), (14, 110.0, True), (15, 110.0, True), (16, 110.0, True)])
        for cup, completion_time in zip(session.inventory["cups"], (None, 110.0)):
            execution = cup["execution"]
            self.assertFalse(execution["home_after_cup"])
            self.assertEqual(execution["last_completed_step"], "right_clear")
            self.assertEqual(execution["state"], "completed")
            audit = cup["last_verification"]
            self.assertEqual(audit["multi_frame"], {
                "required_clear": 3, "clear_count": 3,
                "max_post_clearance_observations": 6, "post_clearance_observations": 3})
            self.assertEqual(audit["prefetch"]["clearance_completed_at"], completion_time)
            self.assertEqual(audit["prefetch"]["capture_count"], 0)
            self.assertEqual(audit["prefetch"]["max_observations"], 0)
            self.assertTrue(all(not row["prefetched"] for row in audit["placement_checks"]))
            self.assertNotIn("post_home", json.dumps(audit))
            self.assertNotIn("home_completed_at", json.dumps(audit))
        self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
        self.assertEqual(session.batch_progress["state"], "completed")
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(self.recovery.required())
        for slot in self.grid.snapshot()["locations"][:2]:
            self.assertTrue(slot["occupied"])
            self.assertFalse(slot.get("reserved", False))
        self.assertFalse(any(thread.name == "cup-camera-prefetch" for thread in threading.enumerate()))

    def test_failed_clearance_never_verifies_or_starts_next_cup(self):
        session, observations = self.make_session(failed_clearance=True)
        with self.assertRaisesRegex(RuntimeError, "right clearance failed"):
            self.execute(session)
        self.assert_no_second_pick(session)
        self.assertEqual(self.capture.call_count, 4)
        self.assertTrue(all(not row["after_first_clearance"] for row in observations))
        self.assertTrue(self.grid.snapshot()["locations"][0]["reserved"])
        self.assertEqual(session.batch_progress["completed_cups"], [])
        execution = session.inventory["cups"][0]["execution"]
        self.assertEqual(execution["state"], "interrupted")
        self.assertNotIn("right_clear", [job["name"] for job in execution["completed_steps"]])
        self.assertIn("right_clear", execution["pending_jobs"])

    def test_arm_occlusion_still_blocks_next_cup_after_six_observations(self):
        session, observations = self.make_session(occluded=True)
        with self.assertRaisesRegex(SelectionError, "six post-clearance observations.*arm still obscures"):
            self.execute(session)
        self.assert_no_second_pick(session)
        self.assertEqual(self.capture.call_count, 10)
        self.assertEqual(sum(row["after_first_clearance"] for row in observations), 6)
        audit = session.inventory["cups"][1]["last_verification"]
        self.assertEqual(audit["multi_frame"]["clear_count"], 0)
        self.assertEqual(audit["state"], "failed")
        self.assertEqual(session.batch_progress["completed_cups"], [1])
        first = self.grid.snapshot()["locations"][0]
        self.assertTrue(first["occupied"])
        self.assertFalse(first.get("reserved", False))

    def test_any_observation_predating_previous_clearance_is_rejected(self):
        for stale_offset in range(3):
            with self.subTest(stale_offset=stale_offset):
                fixtures.PlacementVerificationTests.setUp(self)
                session, _ = self.make_session(stale_offset=stale_offset)
                with self.assertRaisesRegex(SelectionError, "after the previous cup's clearance moves have completed"):
                    self.execute(session)
                self.assert_no_second_pick(session)
                self.assertEqual(self.capture.call_count, 5 + stale_offset)
                self.assertEqual(session.inventory["cups"][1]["last_verification"]["state"], "failed")

    def test_single_frame_policy_also_requires_observation_after_previous_clearance(self):
        session, _ = self.make_session(stale_offset=0, verification_frames=1)
        with self.assertRaisesRegex(SelectionError, "after the previous cup's clearance moves have completed"):
            self.execute(session)
        self.assert_no_second_pick(session)
        self.assertEqual(self.capture.call_count, 3)

    def test_left_only_batch_clears_each_replaced_cup_and_never_moves_right_arm(self):
        session, observations = self.make_session(left_only=True)
        jobs = self.execute(session)
        self.assertEqual(len(jobs), 20)
        self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))
        self.assertEqual(jobs[9]["name"], "left_clear")
        self.assertEqual(jobs[-1]["name"], "left_clear")
        self.assertEqual(jobs[8]["body"]["action"], "open")
        self.assertEqual(jobs[-2]["body"]["action"], "open")
        self.assertEqual([row["started"] for row in observations], [99.0, 100.0, 110.0])
        self.assertEqual([cup["status"] for cup in session.inventory["cups"]], ["replaced", "replaced"])
        self.assertFalse(self.recovery.required())
        self.assertTrue(all(not slot["occupied"] for slot in self.grid.snapshot()["locations"]))

    def test_home_policy_requires_boolean_and_is_read_only(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "home_after_cup must be a boolean"):
                CupSession(Mock(), Mock(), self.robot, "cal-1", home_after_cup=value)
        for value in (False, True):
            session = CupSession(Mock(), Mock(), self.robot, "cal-1", home_after_cup=value)
            self.assertIs(session.home_after_cup, value)
            with self.assertRaises(AttributeError):
                session.home_after_cup = not value
            self.assertIs(session.home_after_cup, value)
        self.assertTrue(CupSession(Mock(), Mock(), self.robot, "cal-1").home_after_cup)


if __name__ == "__main__":
    unittest.main()
