"""Snapshot batches retain clearance dependencies without automatic home."""

import copy
import threading
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from test_cup_selection import RobotDouble, detection, inventory, TRANSFER_JOB_COUNT
import test_placement_verification as fixtures


class NoHomeSessionTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp

    def make_session(self, *, failed_clearance=False, verification_frames=3, left_only=False):
        scan = fixtures.PlacementVerificationTests.recheck_observed(sequence=10)
        scan["cups"] = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                         detection("CUP", 250, x=560), detection("CUP BASE", 250, x=560)])["cups"]
        observation = scan["placement_observation"]
        observation["grid_snapshot"] = self.grid.snapshot()
        observation["slots"] = [dict(copy.deepcopy(observation["slots"][0]), id=place["id"])
                                for place in self.grid.snapshot()["locations"]]
        observation.update(frame_count=1, frame_sequences=[10], released_ids=[])
        clearance = "left_clear" if left_only else "right_clear"
        self.robot = RobotDouble(error=RuntimeError("clearance failed") if failed_clearance else None,
                                 error_at=clearance)
        self.capture = Mock(side_effect=[(scan, 99.0)])
        session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: 100.0,
                             placing_grid=self.grid, recovery=self.recovery,
                             verification_frames=verification_frames, left_only=left_only)
        session.scan()
        self.capture.side_effect = AssertionError("Execution must use the saved scan")
        return session

    def execute(self, session):
        return session.execute_batch(capture_id=session.inventory["capture"])

    def test_two_cups_use_one_snapshot_and_clear_before_next_pick_without_home(self):
        session = self.make_session()
        boundaries = []
        def check_start(_):
            if len(self.robot.jobs) == TRANSFER_JOB_COUNT + 1:
                first = session.inventory["cups"][0]["execution"]
                boundaries.append((first["state"], first["last_completed_step"]))
        self.robot.after_move = check_start
        jobs = self.execute(session)
        self.assertEqual(len(jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual(jobs, self.robot.jobs)
        self.assertEqual(boundaries, [("completed", "right_close")])
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))
        self.assertEqual([job["name"] for job in jobs[TRANSFER_JOB_COUNT:TRANSFER_JOB_COUNT + 3]],
                         ["right_close_travel", "left_close_travel", "left_ready"])
        for offset in (0, TRANSFER_JOB_COUNT):
            planned = {job["name"]: job for job in jobs[offset:offset + TRANSFER_JOB_COUNT]}
            for before, after in (("grip_cup", "right_beside"), ("left_handover", "right_open"),
                                  ("right_grip", "left_release"), ("left_release", "right_lower"),
                                  ("left_clear", "right_retreat")):
                self.assertIn(before, planned[after]["after"])
        for cup in session.inventory["cups"]:
            self.assertFalse(cup["execution"]["home_after_cup"])
            self.assertEqual(cup["execution"]["last_completed_step"], "right_close")
            self.assertEqual(cup["last_verification"]["capture"], "capture-10")
            self.assertEqual(cup["last_verification"]["source"], "saved_scan")
        self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
        self.assertEqual(session.batch_progress["state"], "completed")
        self.assertFalse(session.motion_uncertain)
        self.assertFalse(self.recovery.required())
        self.assertTrue(session.scan_usable)
        for slot in self.grid.snapshot()["locations"][:2]:
            self.assertTrue(slot["occupied"])
            self.assertFalse(slot.get("reserved", False))
        self.capture.assert_called_once_with("scan")
        self.assertFalse(any(thread.name == "cup-camera-prefetch" for thread in threading.enumerate()))

    def test_failed_clearance_never_starts_the_next_cup(self):
        for left_only in (False, True):
            with self.subTest(left_only=left_only):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.make_session(failed_clearance=True, left_only=left_only)
                with self.assertRaisesRegex(RuntimeError, "clearance failed"):
                    self.execute(session)
                self.assertEqual(len(self.robot.jobs), 10 if left_only else TRANSFER_JOB_COUNT - 1)
                self.assertEqual(sum(job["name"] == "grip_cup" for job in self.robot.jobs), 1)
                self.assertEqual(session.batch_progress["completed_cups"], [])
                self.assertTrue(session.motion_uncertain)
                self.assertTrue(self.recovery.required())
                self.assertFalse(session.scan_usable)
                execution = session.inventory["cups"][0]["execution"]
                clearance = "left_clear" if left_only else "right_clear"
                self.assertEqual(execution["state"], "interrupted")
                self.assertNotIn(clearance, [job["name"] for job in execution["completed_steps"]])
                self.assertIn(clearance, execution["pending_jobs"])
                if not left_only:
                    self.assertTrue(all(slot.get("reserved") for slot in self.grid.snapshot()["locations"][:2]))
                self.capture.assert_called_once_with("scan")

    def test_elapsed_time_after_clearance_does_not_expire_the_next_cup(self):
        for frames in (1, 3):
            with self.subTest(legacy_frames=frames):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.make_session(verification_frames=frames)
                def age_after_clearance(_):
                    if self.robot.jobs[-1]["name"] == "right_clear":
                        session.clock = lambda: 1000000.0
                self.robot.after_move = age_after_clearance
                self.assertEqual(len(self.execute(session)), 2 * TRANSFER_JOB_COUNT)
                self.assertEqual(session.batch_progress["completed_cups"], [1, 2])
                self.capture.assert_called_once_with("scan")

    def test_left_only_batch_clears_each_replaced_cup_and_never_moves_right_arm(self):
        session = self.make_session(left_only=True)
        jobs = self.execute(session)
        self.assertEqual(len(jobs), 20)
        self.assertTrue(all(job["body"]["arm"] == "left" for job in jobs))
        self.assertFalse(any(job["endpoint"].endswith("/home") for job in jobs))
        self.assertEqual(jobs[9]["name"], "left_clear")
        self.assertEqual(jobs[-1]["name"], "left_clear")
        self.assertEqual(jobs[8]["body"]["action"], "open")
        self.assertEqual(jobs[-2]["body"]["action"], "open")
        self.assertEqual([cup["status"] for cup in session.inventory["cups"]], ["replaced", "replaced"])
        self.assertFalse(self.recovery.required())
        self.assertTrue(all(not slot["occupied"] for slot in self.grid.snapshot()["locations"]))
        self.capture.assert_called_once_with("scan")

    def test_home_policy_requires_boolean_and_is_read_only(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "home_after_cup must be a boolean"):
                CupSession(Mock(), Mock(), self.robot, "cal-1", home_after_cup=value)
        for value in (False, True):
            session = CupSession(Mock(), Mock(), self.robot, "cal-1", home_after_cup=value)
            self.assertIs(session.home_after_cup, value)
            with self.assertRaises(AttributeError):
                session.home_after_cup = not value
        self.assertFalse(CupSession(Mock(), Mock(), self.robot, "cal-1").home_after_cup)


if __name__ == "__main__":
    unittest.main()
