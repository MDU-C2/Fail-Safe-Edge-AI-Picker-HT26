"""Lynx's legacy strict setting follows the shared saved-scan workflow."""

import unittest
from unittest.mock import Mock

import lynx  # Make the sibling picking modules available.
from cup_commands import CupSession
from cup_inventory import SelectionError
from test_cup_selection import TRANSFER_JOB_COUNT
import test_multiframe_verification as multi_fixtures
import test_placement_verification as fixtures


class SingleVerificationTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp
    observed = staticmethod(fixtures.PlacementVerificationTests.observed)
    recheck_observed = classmethod(fixtures.PlacementVerificationTests.recheck_observed.__func__)
    frames = multi_fixtures.MultiFrameVerificationTests.frames
    execute = staticmethod(fixtures.PlacementVerificationTests.execute)

    def session(self, *, scan=None, max_age=60.0):
        return multi_fixtures.MultiFrameVerificationTests.session(
            self, scan=scan, verification_frames=1, strict=True, max_age=max_age)

    def test_constructor_rejects_malformed_or_incompatible_legacy_policy(self):
        legacy = CupSession(Mock(), Mock(), self.robot, "cal-1")
        self.assertEqual(legacy.max_result_age, 15.0)
        self.assertEqual(legacy.verification_frames, 1)
        self.assertFalse(legacy.strict_single_verification)
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CupSession(Mock(), Mock(), self.robot, "cal-1", strict_single_verification=value)
        with self.assertRaises(ValueError):
            CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=3,
                       strict_single_verification=True)

    def test_strict_legacy_setting_performs_no_second_capture_or_age_check(self):
        session = self.session(max_age=0.1)
        session.clock = lambda: 1000000.0
        self.capture.side_effect = AssertionError("No verification capture")
        jobs = self.execute(session)
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.capture.assert_called_once_with("scan")
        self.assertEqual(session.inventory["cups"][0]["last_verification"]["source"], "saved_scan")
        self.assertFalse(session.motion_uncertain)

    def test_saved_nonclear_destination_remains_a_block_with_no_retry(self):
        for state in ("table_depth", "invalid_depth", "occluded", "raised_depth", "cup", "detected_object"):
            with self.subTest(state=state):
                fixtures.PlacementVerificationTests.setUp(self)
                session = self.session()
                session.inventory["placement_observation"]["slots"][0] = self.frames([state])[0]["placement_observation"]["slots"][0]
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError):
                    self.execute(session)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.capture.assert_called_once_with("scan")

    def test_elapsed_time_during_persistence_or_before_grip_does_not_abort_transfer(self):
        session = self.session()
        now = [100.0]
        session.clock = lambda: now[0]
        self.save.side_effect = lambda _: now.__setitem__(0, now[0] + 1000)
        self.robot.after_move = lambda _: now.__setitem__(0, now[0] + 1000)
        self.execute(session)
        self.assertEqual(self.robot.jobs[-1]["name"], "right_close")
        self.assertFalse(any("home" in job["name"] for job in self.robot.jobs))
        self.capture.assert_called_once_with("scan")
        self.assertFalse(session.motion_uncertain)

    def test_stop_after_descent_still_blocks_grip_and_requires_recovery(self):
        session = self.session()
        self.robot.after_move = lambda _: session.latch_stop() if self.robot.jobs[-1]["name"] == "approach_cup" else None
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            self.execute(session)
        self.assertEqual(self.robot.jobs[-1]["name"], "approach_cup")
        self.assertNotIn("grip_cup", [job["name"] for job in self.robot.jobs])
        self.assertTrue(session.motion_uncertain)
        self.assertTrue(self.recovery.required())
        self.capture.assert_called_once_with("scan")

    def test_second_single_pick_uses_the_same_snapshot(self):
        from test_cup_selection import detection, inventory
        scan = self.recheck_observed(sequence=10)
        scan["cups"] = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                         detection("CUP", 250, x=560), detection("CUP BASE", 250, x=560)])["cups"]
        session = self.session(scan=scan)
        self.execute(session)
        session.clock = lambda: 1000000.0
        session.execute_selection(action="goto", capture_id=session.inventory["capture"], cup_id=2)
        self.assertEqual(len(self.robot.jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertTrue(all(cup["status"] == "placed" for cup in session.inventory["cups"]))
        self.capture.assert_called_once_with("scan")
        self.assertFalse(session.motion_uncertain)


if __name__ == "__main__":
    unittest.main()
